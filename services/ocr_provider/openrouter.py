"""Vision OCR adapter — Gemini-via-OpenRouter, OpenAI-compatible REST via httpx.

Selected when OCR_PROVIDER=openrouter (default stays local). Mirrors the
auth/retry/fallback/error-taxonomy of OpenRouterLlmProvider: on transport/HTTP
failure it RAISES a ProviderError subclass (tagged with .code). The one
exception is a 200 response whose body cannot be parsed as receipt JSON — there
we return an all-0.0-confidence OcrResult so the confidence router (run_ocr_core)
gracefully gap-fills every field rather than failing the whole receipt.

OpenRouter is OpenAI-compatible, so we POST a multimodal message to
/chat/completions with httpx (an existing dependency) rather than a vendor SDK.
Reads the OCR_* settings, falling back to the generic LLM_* settings where an
OCR-specific value is absent (FR-A6 swappability): credentials come from
OCR_API_KEY or LLM_API_KEY, the endpoint from OCR_BASE_URL or LLM_BASE_URL, and
the fallback model from LLM_MODEL_FALLBACK.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from config.settings import get_settings
from services.exceptions import (
    ProviderConfigurationError,
    ProviderError,
    ProviderUnavailableError,
)
from services.ocr_provider.interface import RECEIPT_FIELD_KEYS, OcrProvider, OcrResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "google/gemini-2.5-flash"
# Placeholder value shipped in .env.example — treat as "no key configured".
_PLACEHOLDER_KEY = "your-ocr-api-key-here"
# Statuses worth one retry on the same model (rate/transient/server-side).
_TRANSIENT_STATUS = frozenset({402, 408, 409, 425, 429, 500, 502, 503, 504})
# Hard ceiling on output tokens regardless of the (narrative-tuned) LLM_MAX_TOKENS.
_MAX_OUTPUT_TOKENS = 1024
# Confidence assigned to a field the model returned without a usable score.
_DEFAULT_CONFIDENCE = 0.75
# Lodging-only extras surfaced for downstream per-night reconciliation.
_EXTRA_FIELD_KEYS = ("nights", "per_night_rate")
# C7-18: monetary extras the model reports alongside the total so the pipeline
# can cross-check that the "amount" is the grand total and not a subtotal.
_EXTRA_MONEY_KEYS = ("subtotal", "tax")
# C7-18: boolean signal — False when the TOTAL line is present but unreadable.
TOTAL_LEGIBLE_KEY = "total_legible"
# C7-18: boolean signal — True when the printed tax is already included in the
# total (VAT-inclusive receipts, where total == subtotal is legitimate).
TAX_INCLUDED_KEY = "tax_included"

# The expense_type vocabulary the prompt allows. Every value MUST map to an
# ExpenseCategory via categorize.category_for_expense_type (C7-21 regression).
OCR_EXPENSE_TYPES = ("airfare", "hotel", "meals", "transportation", "other")

# MIME sniffing by magic bytes — keep the data URL honest for the vision model.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_GIF_MAGIC = (b"GIF87a", b"GIF89a")
# Raw PDF bytes are never forwarded: since phase 12 a PDF receipt is rasterised
# to a PNG at upload (receipt/pdf_raster.py) and only the PNG is stored, so PDF
# bytes here mean something bypassed that path — refused (defence in depth).
# Readers find the header within the first 1 KiB.
_PDF_HEADER = b"%PDF-"
_PDF_HEADER_WINDOW = 1024

_SYSTEM_PROMPT = (
    "You are a precise receipt OCR engine. Reply ONLY with minified JSON — no "
    "prose, no markdown fences."
)
_USER_INSTRUCTION = (
    "Extract receipt fields from the attached image. Reply ONLY with a single "
    "minified JSON object. For each field return an object "
    '{"value": <value>, "confidence": <0..1>}. Required fields: '
    '"vendor" (merchant name); '
    '"date" (ISO-8601 YYYY-MM-DD); '
    '"amount" (the grand TOTAL actually charged, i.e. the line labelled TOTAL, '
    "AMOUNT DUE, BALANCE or similar, as a number with no currency symbol or "
    "thousands separators). NEVER substitute the subtotal, a pre-tax amount, a "
    "tip line or any other partial sum for the total, and never compute a total "
    "you cannot read. If the total line is missing, blurred, cut off or otherwise "
    "unreadable, return null value and 0 confidence for amount; "
    '"currency" (ISO-4217 code, e.g. USD); '
    '"location" (city and region/country); '
    '"expense_type" (one of: ' + ", ".join(OCR_EXPENSE_TYPES) + "). "
    "Also return, to improve total/lodging reading: "
    '"subtotal" (the subtotal / pre-tax amount as a number, if printed), '
    '"tax" (tax amount as a number), '
    '"tax_included" (true only if the printed tax is already included in the '
    "total, e.g. 'incl. VAT'; false if the tax is added on top of the subtotal), "
    '"total_legible" (true only if the grand total line is clearly readable; '
    "false if it is present but blurred/unreadable or absent), "
    '"nights" (number of lodging nights, for hotels), and '
    '"per_night_rate" (nightly room rate as a number). '
    "Confidence must reflect how clearly the value is printed: use a value "
    "below 0.5 for anything partially legible or inferred. "
    "Use null for value and 0 for confidence when a field is not present."
)


class OpenRouterVisionOcrProvider(OcrProvider):
    """Gemini-vision OCR provider. OpenAI-compatible /chat/completions over httpx."""

    # ---- config helpers -----------------------------------------------------

    @staticmethod
    def _api_key(settings: Any) -> str:
        # Prefer the OCR-specific key; fall back to the shared LLM key. Treat the
        # shipped placeholder as empty so a half-configured .env fails loudly.
        key = str(settings.OCR_API_KEY or "").strip()
        if key == _PLACEHOLDER_KEY:
            key = ""
        if not key:
            key = str(settings.LLM_API_KEY or "").strip()
        if not key:
            raise ProviderConfigurationError(
                "OCR_API_KEY (or LLM_API_KEY) is required when OCR_PROVIDER=openrouter"
            )
        return key

    @staticmethod
    def _base_url(settings: Any) -> str:
        url = str(settings.OCR_BASE_URL or settings.LLM_BASE_URL or _DEFAULT_BASE_URL)
        return url.rstrip("/")

    @staticmethod
    def _model(settings: Any) -> str:
        return str(getattr(settings, "OCR_MODEL", "") or _DEFAULT_MODEL)

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Atlas",  # optional OpenRouter app attribution
        }

    @staticmethod
    def _httpx() -> Any:
        try:
            import httpx  # noqa: PLC0415 — lazy import keeps import-time hermetic
        except ImportError as exc:  # pragma: no cover - httpx is a declared dependency
            raise ProviderUnavailableError("httpx not installed; run: pip install httpx") from exc
        return httpx

    # ---- request building ---------------------------------------------------

    @staticmethod
    def _sniff_mime(image: bytes) -> str:
        if image.startswith(_PNG_MAGIC):
            return "image/png"
        if image.startswith(_JPEG_MAGIC):
            return "image/jpeg"
        if image.startswith(b"RIFF") and image[8:12] == b"WEBP":
            return "image/webp"
        if image.startswith(_GIF_MAGIC):
            return "image/gif"
        # Default to PNG; the model tolerates a generic image MIME for raster bytes.
        return "image/png"

    def _data_url(self, image: bytes) -> str:
        mime = self._sniff_mime(image)
        encoded = base64.b64encode(image).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _build_payload(self, *, settings: Any, model: str, data_url: str) -> dict[str, Any]:
        max_tokens = min(int(settings.LLM_MAX_TOKENS), _MAX_OUTPUT_TOKENS)
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _USER_INSTRUCTION},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "reasoning": {"enabled": False},
            "response_format": {"type": "json_object"},
        }

    # ---- transport (raises ProviderError on failure) ------------------------

    @staticmethod
    def _is_transient(exc: ProviderError) -> bool:
        code = exc.code
        if code in {"PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE"}:
            return True
        if not code.startswith("PROVIDER_HTTP_"):
            return False
        return int(code.rsplit("_", 1)[-1]) in _TRANSIENT_STATUS

    def _post_once(
        self,
        httpx: Any,
        *,
        base_url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """One POST. Raises ProviderError (tagged with .code) on any failure."""
        try:
            resp = httpx.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"OpenRouter OCR request timed out after {timeout}s", code="PROVIDER_TIMEOUT"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError(
                f"OpenRouter OCR unreachable: {type(exc).__name__}"
            ) from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenRouter OCR error: HTTP {resp.status_code}",
                code=f"PROVIDER_HTTP_{resp.status_code}",
            )

        # A 200 with a non-JSON body (HTML error page from a proxy, empty body)
        # must surface as a ProviderError so the pipeline marks the receipt
        # ocr_status=failed instead of the upload 500-ing.
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(
                "OpenRouter OCR returned a non-JSON body", code="PROVIDER_BAD_RESPONSE"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(
                "OpenRouter OCR returned a non-object JSON body", code="PROVIDER_BAD_RESPONSE"
            )
        choices = data.get("choices") or []
        if choices and choices[0].get("finish_reason") == "content_filter":
            raise ProviderError(
                "OpenRouter OCR blocked output (content_filter)", code="PROVIDER_CONTENT_FILTER"
            )
        return data

    def _request_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Primary model (initial + ONE retry on transient), then ONE fallback attempt."""
        settings = get_settings()
        api_key = self._api_key(settings)
        base_url = self._base_url(settings)
        headers = self._headers(api_key)
        timeout = float(settings.OCR_TIMEOUT_SECONDS)
        httpx = self._httpx()

        primary = str(payload["model"])
        fallback = str(settings.LLM_MODEL_FALLBACK or "")
        last_exc: ProviderError | None = None

        for attempt in range(2):  # initial + one retry
            try:
                return self._post_once(
                    httpx, base_url=base_url, headers=headers, payload=payload, timeout=timeout
                )
            except ProviderError as exc:
                last_exc = exc
                logger.warning(
                    "OpenRouter OCR '%s' failed (%s) try %d/2", primary, exc.code, attempt + 1
                )
                if not self._is_transient(exc):
                    break  # model/auth error — retrying the same model won't help

        if fallback and fallback != primary:
            logger.warning("OpenRouter OCR falling back to '%s'", fallback)
            try:
                return self._post_once(
                    httpx,
                    base_url=base_url,
                    headers=headers,
                    payload={**payload, "model": fallback},
                    timeout=timeout,
                )
            except ProviderError as exc:
                last_exc = exc

        if last_exc is None:  # pragma: no cover - a failure path always sets last_exc
            last_exc = ProviderError("OpenRouter OCR request failed")
        raise last_exc

    # ---- response parsing ---------------------------------------------------

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Drop ```json … ``` markdown fences a model may wrap JSON in."""
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        # Remove the opening fence line (``` or ```json) and the trailing fence.
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
        return stripped.strip()

    @staticmethod
    def _coerce_amount(value: Any) -> float | None:
        """Coerce an amount-like value to float, stripping $ and thousands commas."""
        if value is None:
            return None
        if isinstance(value, bool):  # guard: bool is an int subclass
            return None
        if isinstance(value, int | float):
            return float(value)
        text = str(value).strip().replace("$", "").replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _clamp(conf: Any) -> float:
        try:
            value = float(conf)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, value))

    def _empty_result(self) -> OcrResult:
        """All-0.0-confidence result so the router gap-fills every field (graceful)."""
        return OcrResult(
            fields={key: None for key in RECEIPT_FIELD_KEYS},
            field_confidence={key: 0.0 for key in RECEIPT_FIELD_KEYS},
        )

    def _parse_field(self, key: str, raw: Any) -> tuple[Any, float]:
        """Normalize one model-returned field into (value, confidence)."""
        if isinstance(raw, dict):
            value = raw.get("value")
            conf_raw = raw.get("confidence")
        else:
            # Model returned a bare scalar instead of {value, confidence}.
            value = raw
            conf_raw = None

        if key == "amount":
            value = self._coerce_amount(value)

        if value is None:
            return None, 0.0

        if conf_raw is None:
            confidence = _DEFAULT_CONFIDENCE
        else:
            confidence = self._clamp(conf_raw)
        return value, confidence

    def _build_result(self, parsed: dict[str, Any]) -> OcrResult:
        fields: dict[str, str | float | None] = {}
        confidence: dict[str, float] = {}

        for key in RECEIPT_FIELD_KEYS:
            value, conf = self._parse_field(key, parsed.get(key))
            fields[key] = value
            confidence[key] = conf

        # Lodging extras: surfaced as EXTRA keys for downstream per-night
        # reconciliation. The pipeline currently persists only the 6 routed keys.
        for extra in _EXTRA_FIELD_KEYS:
            if extra in parsed:
                value, _conf = self._parse_field("per_night_rate", parsed.get(extra))
                if value is not None:
                    fields[extra] = value

        # C7-18: subtotal/tax + the total-legibility signal feed the pipeline's
        # deterministic total/subtotal cross-check (receipt_pipeline.core).
        for extra in _EXTRA_MONEY_KEYS:
            if extra in parsed:
                money, _conf = self._parse_field("amount", parsed.get(extra))
                if money is not None:
                    fields[extra] = money
        for flag_key in (TOTAL_LEGIBLE_KEY, TAX_INCLUDED_KEY):
            flag = self._parse_bool(parsed.get(flag_key))
            if flag is not None:
                fields[flag_key] = flag

        return OcrResult(fields=fields, field_confidence=confidence)

    @staticmethod
    def _parse_bool(raw: Any) -> bool | None:
        """Read a boolean the model returned bare or as {"value": <bool>}."""
        if isinstance(raw, dict):
            raw = raw.get("value")
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
            return raw.strip().lower() == "true"
        return None

    def _parse_response(self, data: dict[str, Any]) -> OcrResult:
        choices = data.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        text = message.get("content") or ""
        if not isinstance(text, str) or not text.strip():
            logger.warning("OpenRouter OCR returned an empty body; gap-filling all fields.")
            return self._empty_result()

        try:
            parsed = json.loads(self._strip_fences(text))
        except (json.JSONDecodeError, ValueError):
            logger.warning("OpenRouter OCR body was not valid JSON; gap-filling all fields.")
            return self._empty_result()

        if not isinstance(parsed, dict):
            logger.warning("OpenRouter OCR body was not a JSON object; gap-filling all fields.")
            return self._empty_result()

        return self._build_result(parsed)

    # ---- interface ----------------------------------------------------------

    def extract(self, image: bytes | str) -> OcrResult:
        if isinstance(image, str):
            # Production resolves object-store keys to bytes before calling us.
            raise ProviderError(
                "OpenRouterVisionOcrProvider.extract requires raw image bytes, not a key",
                code="PROVIDER_INVALID_INPUT",
            )

        settings = get_settings()
        # Validate credentials up front so a missing key fails fast at call time.
        self._api_key(settings)

        if _PDF_HEADER in image[: _PDF_HEADER_WINDOW + len(_PDF_HEADER)]:
            raise ProviderError(
                "Raw PDF bytes are never sent to OCR; send a PNG, JPEG or WebP image",
                code="PROVIDER_UNSUPPORTED_MEDIA",
            )

        max_bytes = int(settings.OCR_MAX_FILE_SIZE_MB) * 1024 * 1024
        if len(image) > max_bytes:
            raise ProviderError(
                f"Receipt image exceeds OCR_MAX_FILE_SIZE_MB ({settings.OCR_MAX_FILE_SIZE_MB} MB)",
                code="PROVIDER_PAYLOAD_TOO_LARGE",
            )

        payload = self._build_payload(
            settings=settings,
            model=self._model(settings),
            data_url=self._data_url(image),
        )
        data = self._request_json(payload)
        return self._parse_response(data)
