"""Smoke tests for services/ocr_provider."""

import json

import pytest


def test_factory_returns_local_by_default() -> None:
    from services.ocr_provider import LocalOcrProvider, get_ocr_provider

    p = get_ocr_provider()
    assert isinstance(p, LocalOcrProvider)


def test_local_airfare_extraction() -> None:
    """LocalOcrProvider returns correct fields for the P3-T5 airfare seed key."""
    from services.ocr_provider import RECEIPT_FIELD_KEYS, get_ocr_provider

    p = get_ocr_provider()
    r = p.extract("receipts/seed/employee-airfare-001.png")
    assert r.fields["expense_type"] == "airfare"
    assert all(k in r.fields for k in RECEIPT_FIELD_KEYS)
    assert all(k in r.field_confidence for k in RECEIPT_FIELD_KEYS)
    assert all(0.0 <= v <= 1.0 for v in r.field_confidence.values())


def test_local_hotel_extraction() -> None:
    from services.ocr_provider import get_ocr_provider

    p = get_ocr_provider()
    r = p.extract("receipts/seed/employee-hotel-001.png")
    assert r.fields["expense_type"] == "hotel"


def test_local_meal_extraction() -> None:
    from services.ocr_provider import get_ocr_provider

    p = get_ocr_provider()
    r = p.extract("receipts/seed/employee-meal-001.png")
    assert r.fields["expense_type"] == "meals"


def test_anthropic_ocr_provider_fails_fast_at_settings_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Product gap 7: OCR_PROVIDER=anthropic was offered but always raised "not yet
    implemented" at upload time.  The dead option is gone; a stale configuration
    must fail at settings validation, naming the variable and the valid options."""
    from pydantic import ValidationError

    from config.settings.base import Settings

    monkeypatch.setenv("OCR_PROVIDER", "anthropic")
    with pytest.raises(ValidationError) as ei:
        Settings()
    message = str(ei.value)
    assert "OCR_PROVIDER" in message
    assert "anthropic" in message
    assert "no longer supported" in message
    assert "local" in message and "openrouter" in message


@pytest.mark.parametrize("mode", ["local", "openrouter", "LOCAL"])
def test_supported_ocr_providers_still_validate(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    from config.settings.base import Settings

    monkeypatch.setenv("OCR_PROVIDER", mode)
    assert Settings().OCR_PROVIDER == mode.lower()


def test_no_anthropic_ocr_adapter_remains() -> None:
    """The always-raising stub adapter and its export are removed."""
    import importlib.util

    import services.ocr_provider as ocr

    assert importlib.util.find_spec("services.ocr_provider.real") is None
    assert not hasattr(ocr, "RealOcrProvider")


# ---- OpenRouter vision OCR (hermetic; httpx monkeypatched, no network) -------

# Minimal valid PNG header so MIME-sniffing/data-URL building has real bytes.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _select_openrouter_ocr(
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str = "test-ocr-key",
) -> None:
    import config.settings as cs

    monkeypatch.setenv("OCR_PROVIDER", "openrouter")
    monkeypatch.setenv("OCR_API_KEY", key)
    monkeypatch.setenv("LLM_API_KEY", "")
    cs.get_settings.cache_clear()


def _restore_settings() -> None:
    import config.settings as cs

    cs.get_settings.cache_clear()


def _ocr_response(content: str, *, status: int = 200) -> object:
    class _Resp:
        status_code = status

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": content}}]}

    return _Resp()


def test_openrouter_factory_returns_vision_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _select_openrouter_ocr(monkeypatch)
    from services.ocr_provider import OpenRouterVisionOcrProvider, get_ocr_provider

    assert isinstance(get_ocr_provider(), OpenRouterVisionOcrProvider)
    _restore_settings()


def test_openrouter_empty_key_raises_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OCR/LLM key -> ProviderError at call time, not construction."""
    _select_openrouter_ocr(monkeypatch, key="")

    from services.exceptions import ProviderError
    from services.ocr_provider import get_ocr_provider

    p = get_ocr_provider()  # must NOT raise
    with pytest.raises(ProviderError):
        p.extract(_PNG_BYTES)
    _restore_settings()


def test_openrouter_placeholder_key_is_treated_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The .env.example placeholder key must be rejected like an empty key."""
    _select_openrouter_ocr(monkeypatch, key="your-ocr-api-key-here")

    from services.exceptions import ProviderError
    from services.ocr_provider import get_ocr_provider

    p = get_ocr_provider()
    with pytest.raises(ProviderError):
        p.extract(_PNG_BYTES)
    _restore_settings()


def test_openrouter_str_input_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production passes bytes; a str (object key) must raise ProviderError."""
    _select_openrouter_ocr(monkeypatch)

    from services.exceptions import ProviderError
    from services.ocr_provider import get_ocr_provider

    p = get_ocr_provider()
    with pytest.raises(ProviderError):
        p.extract("receipts/seed/employee-airfare-001.png")
    _restore_settings()


def test_openrouter_canned_json_returns_all_six_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed JSON body yields an OcrResult over all 6 keys with high conf."""
    _select_openrouter_ocr(monkeypatch)
    import httpx

    body = json.dumps(
        {
            "vendor": {"value": "United Airlines", "confidence": 0.97},
            "date": {"value": "2024-11-15", "confidence": 0.95},
            "amount": {"value": "$1,450.00", "confidence": 0.96},
            "currency": {"value": "USD", "confidence": 0.99},
            "location": {"value": "San Francisco, CA", "confidence": 0.9},
            "expense_type": {"value": "airfare", "confidence": 0.93},
        }
    )

    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _ocr_response(body)

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.ocr_provider import RECEIPT_FIELD_KEYS, get_ocr_provider

    r = get_ocr_provider().extract(_PNG_BYTES)

    assert set(r.fields) >= set(RECEIPT_FIELD_KEYS)
    assert set(r.field_confidence) == set(RECEIPT_FIELD_KEYS)
    assert r.fields["amount"] == 1450.0
    assert isinstance(r.fields["amount"], float)  # coerced, symbol/commas stripped
    assert r.fields["date"] == "2024-11-15"
    assert r.fields["vendor"] == "United Airlines"
    assert r.fields["expense_type"] == "airfare"
    assert all(0.0 <= v <= 1.0 for v in r.field_confidence.values())
    assert all(r.field_confidence[k] >= 0.70 for k in RECEIPT_FIELD_KEYS)

    sent = captured["json"]
    assert isinstance(sent, dict)
    assert str(captured["url"]).endswith("/chat/completions")
    assert sent["temperature"] == 0.0
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["reasoning"] == {"enabled": False}
    # Multimodal user content: text instruction + image_url data URL.
    user_msg = sent["messages"][1]
    assert user_msg["role"] == "user"
    assert user_msg["content"][1]["type"] == "image_url"
    assert user_msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    _restore_settings()


def test_openrouter_present_unscored_gets_default_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field returned as a bare scalar (no confidence) still routes deterministic."""
    _select_openrouter_ocr(monkeypatch)
    import httpx

    body = json.dumps(
        {
            "vendor": "Marriott",
            "date": {"value": "2024-11-18", "confidence": 0.9},
            "amount": {"value": 210.0, "confidence": 0.95},
            "currency": "USD",
            "location": {"value": "New York, NY", "confidence": 0.8},
            "expense_type": {"value": "hotel", "confidence": 0.9},
            "nights": {"value": 2, "confidence": 0.88},
            "per_night_rate": {"value": 105.0, "confidence": 0.85},
        }
    )
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _ocr_response(body))

    from services.ocr_provider import get_ocr_provider

    r = get_ocr_provider().extract(_PNG_BYTES)
    assert r.fields["vendor"] == "Marriott"
    assert r.field_confidence["vendor"] == pytest.approx(0.75)  # default for unscored
    # Lodging extras surface as EXTRA keys.
    assert r.fields["nights"] == 2
    assert r.fields["per_night_rate"] == 105.0
    _restore_settings()


def test_openrouter_unparseable_body_returns_all_zero_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with a non-JSON body degrades to all-0.0-confidence (graceful gap-fill)."""
    _select_openrouter_ocr(monkeypatch)
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *_a, **_k: _ocr_response("sorry, I could not read that")
    )

    from services.ocr_provider import RECEIPT_FIELD_KEYS, get_ocr_provider

    r = get_ocr_provider().extract(_PNG_BYTES)
    assert set(r.field_confidence) == set(RECEIPT_FIELD_KEYS)
    assert all(v == 0.0 for v in r.field_confidence.values())
    assert all(r.fields[k] is None for k in RECEIPT_FIELD_KEYS)
    _restore_settings()


def test_openrouter_strips_markdown_fences(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fenced ```json block is unwrapped before parsing."""
    _select_openrouter_ocr(monkeypatch)
    import httpx

    inner = json.dumps(
        {
            "vendor": {"value": "Cafe", "confidence": 0.9},
            "date": {"value": "2024-11-17", "confidence": 0.9},
            "amount": {"value": 42.5, "confidence": 0.9},
            "currency": {"value": "USD", "confidence": 0.9},
            "location": {"value": "Chicago, IL", "confidence": 0.9},
            "expense_type": {"value": "meals", "confidence": 0.9},
        }
    )
    fenced = f"```json\n{inner}\n```"
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _ocr_response(fenced))

    from services.ocr_provider import get_ocr_provider

    r = get_ocr_provider().extract(_PNG_BYTES)
    assert r.fields["amount"] == 42.5
    assert r.fields["expense_type"] == "meals"
    _restore_settings()


def test_openrouter_http_500_then_fallback_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Primary 500s twice; the one fallback-model attempt succeeds and is parsed."""
    _select_openrouter_ocr(monkeypatch)
    import httpx

    body = json.dumps(
        {
            "vendor": {"value": "Acme", "confidence": 0.9},
            "date": {"value": "2024-11-20", "confidence": 0.9},
            "amount": {"value": 12.0, "confidence": 0.9},
            "currency": {"value": "USD", "confidence": 0.9},
            "location": {"value": "Austin, TX", "confidence": 0.9},
            "expense_type": {"value": "other", "confidence": 0.9},
        }
    )
    calls: dict[str, int] = {"n": 0}

    def fake_post(url: str, **kwargs: object) -> object:
        calls["n"] += 1
        payload = kwargs.get("json")
        model = payload.get("model") if isinstance(payload, dict) else None
        if model == "google/gemini-2.5-flash":  # primary -> 500
            return _ocr_response("{}", status=500)
        return _ocr_response(body)  # fallback -> 200

    monkeypatch.setattr(httpx, "post", fake_post)

    from services.ocr_provider import get_ocr_provider

    r = get_ocr_provider().extract(_PNG_BYTES)
    assert r.fields["vendor"] == "Acme"
    assert r.fields["amount"] == 12.0
    # initial + 1 retry on primary, then 1 fallback attempt.
    assert calls["n"] == 3
    _restore_settings()


def test_openrouter_http_error_propagates_when_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent HTTP error with no usable fallback surfaces as ProviderError."""
    _select_openrouter_ocr(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_FALLBACK", "")
    import config.settings as cs

    cs.get_settings.cache_clear()
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _ocr_response("{}", status=503))

    from services.exceptions import ProviderError
    from services.ocr_provider import get_ocr_provider

    with pytest.raises(ProviderError):
        get_ocr_provider().extract(_PNG_BYTES)
    _restore_settings()


def test_openrouter_oversize_image_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Images above OCR_MAX_FILE_SIZE_MB are rejected before any network call."""
    _select_openrouter_ocr(monkeypatch)
    monkeypatch.setenv("OCR_MAX_FILE_SIZE_MB", "1")
    import config.settings as cs

    cs.get_settings.cache_clear()
    import httpx

    def boom(*_a: object, **_k: object) -> object:  # pragma: no cover - must not run
        raise AssertionError("network must not be touched for oversize input")

    monkeypatch.setattr(httpx, "post", boom)

    from services.exceptions import ProviderError
    from services.ocr_provider import get_ocr_provider

    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024)
    with pytest.raises(ProviderError):
        get_ocr_provider().extract(oversize)
    _restore_settings()


@pytest.mark.parametrize(
    "data", [b"%PDF-1.7\n...", b"\x00" * 200 + b"%PDF-1.4\n..."], ids=["header", "in-window"]
)
def test_openrouter_refuses_pdf_bytes_without_a_network_call(
    monkeypatch: pytest.MonkeyPatch, data: bytes
) -> None:
    """The adapter never forwards raw PDF bytes (e.g. a legacy stored PDF) to the
    vision model; since phase 12 a PDF receipt reaches OCR only as the PNG
    rendered from it at upload."""
    _select_openrouter_ocr(monkeypatch)
    import httpx

    def boom(*_a: object, **_k: object) -> object:  # pragma: no cover - must not run
        raise AssertionError("network must not be touched for a PDF")

    monkeypatch.setattr(httpx, "post", boom)

    from services.exceptions import ProviderError
    from services.ocr_provider import get_ocr_provider
    from services.ocr_provider.openrouter import OpenRouterVisionOcrProvider

    with pytest.raises(ProviderError) as exc:
        get_ocr_provider().extract(data)
    assert exc.value.code == "PROVIDER_UNSUPPORTED_MEDIA"
    assert OpenRouterVisionOcrProvider._sniff_mime(b"%PDF-1.7") != "application/pdf"
    _restore_settings()


@pytest.mark.parametrize("body_kind", ["non_json", "non_object"])
def test_openrouter_non_json_http_body_raises_provider_error(
    monkeypatch: pytest.MonkeyPatch, body_kind: str
) -> None:
    """A 200 whose HTTP body is not a JSON object (HTML proxy page, empty body,
    bare list) raises ProviderError — so the pipeline marks ocr_status=failed —
    never a raw JSONDecodeError/AttributeError that would 500 the upload."""
    _select_openrouter_ocr(monkeypatch)
    import httpx

    class _BadResp:
        status_code = 200

        def json(self) -> object:
            if body_kind == "non_json":
                return json.loads("<html>Bad Gateway</html>")
            return ["not", "an", "object"]

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _BadResp())

    from services.exceptions import ProviderError
    from services.ocr_provider import get_ocr_provider

    with pytest.raises(ProviderError) as ei:
        get_ocr_provider().extract(_PNG_BYTES)
    assert ei.value.code == "PROVIDER_BAD_RESPONSE"
    _restore_settings()
