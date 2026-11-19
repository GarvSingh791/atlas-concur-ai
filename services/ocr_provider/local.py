"""Local (deterministic, no-key) OCR adapter for dev/test."""

from __future__ import annotations

import logging

from services.ocr_provider.interface import OcrProvider, OcrResult

logger = logging.getLogger(__name__)

# Canned extraction results for P3-T5 seed receipts.
_CANNED: dict[str, dict] = {
    "airfare": {
        "fields": {
            "amount": 450.00,
            "currency": "USD",
            "date": "2024-11-15",
            "vendor": "United Airlines",
            "location": "San Francisco, CA",
            "expense_type": "airfare",
        },
        "field_confidence": {
            "amount": 0.98,
            "currency": 0.99,
            "date": 0.97,
            "vendor": 0.96,
            "location": 0.90,
            "expense_type": 0.95,
        },
    },
    "hotel": {
        "fields": {
            "amount": 210.00,
            "currency": "USD",
            "date": "2024-11-18",
            "vendor": "Marriott New York",
            "location": "New York, NY",
            "expense_type": "hotel",
        },
        "field_confidence": {
            "amount": 0.97,
            "currency": 0.99,
            "date": 0.96,
            "vendor": 0.94,
            "location": 0.93,
            "expense_type": 0.96,
        },
    },
    "meal": {
        "fields": {
            "amount": 42.50,
            "currency": "USD",
            "date": "2024-11-17",
            "vendor": "The Capital Grille",
            "location": "Chicago, IL",
            "expense_type": "meals",
        },
        "field_confidence": {
            "amount": 0.95,
            "currency": 0.99,
            "date": 0.94,
            "vendor": 0.88,
            "location": 0.85,
            "expense_type": 0.91,
        },
    },
}

_KEYS = ("amount", "currency", "date", "vendor", "location", "expense_type")
_FALLBACK: dict[str, dict] = {
    "fields": {k: "unknown" for k in _KEYS},
    "field_confidence": {k: 0.5 for k in _KEYS},
}


class LocalOcrProvider(OcrProvider):
    """Deterministic canned OCR for dev/CI. No key or network required."""

    def extract(self, image: bytes | str) -> OcrResult:
        key = image if isinstance(image, str) else image.decode("utf-8", "ignore")
        logger.debug(
            "LocalOcrProvider.extract called (local mode), key=%r",
            key[:60] if key else "<bytes>",
        )

        for hint, data in _CANNED.items():
            if hint in key:
                return OcrResult(
                    fields=dict(data["fields"]),
                    field_confidence=dict(data["field_confidence"]),
                )

        # Unknown input — return generic placeholder
        return OcrResult(
            fields=dict(_FALLBACK["fields"]),
            field_confidence=dict(_FALLBACK["field_confidence"]),
        )
