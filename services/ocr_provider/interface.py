"""OcrProvider interface and data types."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

RECEIPT_FIELD_KEYS = ("amount", "currency", "date", "vendor", "location", "expense_type")


@dataclass
class OcrResult:
    """Result from OCR extraction.

    fields: extracted field values (amount, currency, date, vendor, location, expense_type)
    field_confidence: per-field confidence score (0.0–1.0)
    """

    fields: dict[str, str | float | None] = field(default_factory=dict)
    field_confidence: dict[str, float] = field(default_factory=dict)


class OcrProvider(ABC):
    """Model-agnostic OCR interface. Returns structured receipt fields."""

    @abstractmethod
    def extract(self, image: bytes | str) -> OcrResult:
        """Extract structured fields from a receipt image.

        image: raw image bytes OR an object-store key
            (e.g. 'receipts/seed/employee-airfare-001.png')
        Returns OcrResult with fields + per-field confidence scores.

        NOTE: This is the deterministic provider core only.
        Confidence routing, gap-fill, and reflection are P4-T11 concerns.
        """
