"""Value normalizers for deterministic M-A1 field comparison.

The OCR stub payloads and the gold values in the dataset are written in
human-friendly formats ("$1,234.50", "11/15/2024").  To compare a predicted
field against gold *as a value* (not as a raw string), the metrics layer
normalizes both sides through these functions first.  This is what makes the
M-A1 per-field metrics non-trivial: a case where ``ocr != gold`` as strings can
still be a correct extraction once both are normalized.

Three normalizers:

- ``normalize_money(value, currency)`` -> ``(Decimal, currency_code)``
  Handles ``"$1,234.50"`` (US grouping), ``"1.234,50"`` (EU grouping),
  bare numbers, and numeric inputs.  Currency symbols/codes in the string take
  precedence over the ``currency`` argument; the argument is the fallback.

- ``normalize_date(value)`` -> ISO ``"YYYY-MM-DD"``
  Handles ISO ``2024-11-15``, US ``11/15/2024``, EU ``15/11/2024`` (and ``-``/``.``
  separators).  Ambiguous DMY/MDY is resolved by range (a part > 12 must be the
  day); when both parts are <= 12 the format is assumed **US (MDY)** to match the
  pipeline's en-US OCR locale — documented so gold authors know the convention.

- ``normalize_text(value)`` -> casefolded, whitespace-collapsed string.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

__all__ = [
    "NormalizationError",
    "normalize_money",
    "normalize_date",
    "normalize_text",
]


class NormalizationError(ValueError):
    """Raised when a value cannot be normalized to the target form."""


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

# Common currency symbols -> ISO 4217 code.
_SYMBOL_TO_CODE = {
    "$": "USD",
    "€": "EUR",  # €
    "£": "GBP",  # £
    "¥": "JPY",  # ¥
}
# Recognized ISO codes that may appear inline in the string (e.g. "EUR 100").
_INLINE_CODES = frozenset({"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD"})

_WS_RE = re.compile(r"\s+")
_DIGIT_GROUP_RE = re.compile(r"[\d.,\s]+")


def normalize_money(
    value: str | int | float | Decimal | None,
    currency: str | None = None,
) -> tuple[Decimal, str | None]:
    """Parse a monetary value into ``(Decimal amount, currency_code)``.

    Args:
        value: the amount, as a formatted string ("$1,234.50", "1.234,50"),
            a number, or ``None``.
        currency: fallback currency code if none is embedded in ``value``.

    Returns:
        ``(Decimal, currency_or_None)``.  The currency embedded in ``value``
        (symbol or inline ISO code) wins over the ``currency`` argument.

    Raises:
        NormalizationError: if ``value`` is ``None`` or cannot be parsed.
    """
    if value is None:
        raise NormalizationError("Cannot normalize money from None.")

    code = currency.upper().strip() if currency else None

    if isinstance(value, int | float | Decimal):
        try:
            return Decimal(str(value)), code
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise NormalizationError(f"Invalid numeric money value: {value!r}") from exc

    text = value.strip()
    if not text:
        raise NormalizationError("Cannot normalize money from empty string.")

    # Embedded symbol?
    for sym, sym_code in _SYMBOL_TO_CODE.items():
        if sym in text:
            code = sym_code
            text = text.replace(sym, "")
            break

    # Embedded ISO code (e.g. "EUR 100", "100 GBP")?
    upper = text.upper()
    for iso in _INLINE_CODES:
        if re.search(rf"\b{iso}\b", upper):
            code = iso
            text = re.sub(rf"\b{iso}\b", "", upper, flags=re.IGNORECASE)
            break

    text = _WS_RE.sub("", text).strip()
    match = _DIGIT_GROUP_RE.search(text)
    if not match:
        raise NormalizationError(f"No numeric content in money value: {value!r}")
    digits = match.group(0).strip()

    amount = _parse_grouped_number(digits, original=value)
    return amount, code


def _parse_grouped_number(digits: str, *, original: object) -> Decimal:
    """Disambiguate US (1,234.50) vs EU (1.234,50) grouping into a Decimal."""
    has_comma = "," in digits
    has_dot = "." in digits

    if has_comma and has_dot:
        # The separator that appears LAST is the decimal separator.
        if digits.rfind(",") > digits.rfind("."):
            # EU: dot = thousands, comma = decimal -> 1.234,50
            normalized = digits.replace(".", "").replace(",", ".")
        else:
            # US: comma = thousands, dot = decimal -> 1,234.50
            normalized = digits.replace(",", "")
    elif has_comma:
        # Only commas. If exactly one comma with 1-2 trailing digits, treat as
        # a decimal comma (EU "1234,50"); otherwise thousands grouping.
        parts = digits.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            normalized = digits.replace(",", ".")
        else:
            normalized = digits.replace(",", "")
    else:
        # Only dots (or none). A single dot with !=3 trailing digits is decimal;
        # dots used purely as thousands grouping ("1.234") collapse to "1234".
        parts = digits.split(".")
        if len(parts) == 2 and len(parts[1]) != 3:
            normalized = digits
        elif len(parts) >= 2 and all(len(p) == 3 for p in parts[1:]):
            normalized = digits.replace(".", "")
        else:
            normalized = digits

    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise NormalizationError(f"Could not parse money value: {original!r}") from exc


# ---------------------------------------------------------------------------
# Date
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_SLASH_RE = re.compile(r"^(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{1,4})$")


def normalize_date(value: str | None) -> str:
    """Normalize a date string to ISO ``YYYY-MM-DD``.

    Supported inputs: ISO ``2024-11-15``; US ``11/15/2024``; EU ``15/11/2024``;
    ``.`` or ``-`` separators.  Ambiguous DMY/MDY where both parts are <= 12 is
    assumed **US (MDY)** to match the pipeline's en-US OCR locale.

    Raises:
        NormalizationError: if the value cannot be parsed into a valid date.
    """
    if value is None:
        raise NormalizationError("Cannot normalize date from None.")
    text = value.strip()
    if not text:
        raise NormalizationError("Cannot normalize date from empty string.")

    iso = _ISO_RE.match(text)
    if iso:
        year, month, day = (int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        return _format_date(year, month, day, original=value)

    slash = _SLASH_RE.match(text)
    if slash:
        a, b, c = int(slash.group(1)), int(slash.group(2)), int(slash.group(3))
        # Year-first slash form (rare): YYYY/MM/DD
        if len(slash.group(1)) == 4:
            return _format_date(a, b, c, original=value)
        # a/b/c with a 4-digit year at the end -> day/month or month/day.
        year = c if len(slash.group(3)) == 4 else _expand_2digit_year(c)
        if a > 12 and b <= 12:
            day, month = a, b  # unambiguous DMY
        elif b > 12 and a <= 12:
            month, day = a, b  # unambiguous MDY
        else:
            month, day = a, b  # ambiguous -> assume US MDY
        return _format_date(year, month, day, original=value)

    raise NormalizationError(f"Unrecognized date format: {value!r}")


def _expand_2digit_year(yy: int) -> int:
    """Expand a 2-digit year (00-99) into a 4-digit year (2000-2099 window)."""
    return 2000 + yy if yy < 100 else yy


def _format_date(year: int, month: int, day: int, *, original: object) -> str:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise NormalizationError(f"Date out of range: {original!r}")
    return f"{year:04d}-{month:02d}-{day:02d}"


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def normalize_text(value: str | None) -> str:
    """Casefold and collapse whitespace for text-field comparison.

    Returns the empty string for ``None`` so a missing prediction compares
    cleanly against a missing gold value.
    """
    if value is None:
        return ""
    return _WS_RE.sub(" ", str(value).strip()).casefold()
