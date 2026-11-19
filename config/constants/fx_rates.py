"""Static FX reference table + pure Decimal conversion (product gap 8).

SIMULATED REFERENCE RATES — NOT LIVE MARKET DATA.
The figures below are rounded, illustrative rates fixed as of ``RATES_AS_OF``.
Atlas has no market-data feed: the table exists so a foreign-currency receipt is
never compared against a USD cap unconverted.  Every converted figure shown to a
user carries the rate used, so the approver can see exactly what was assumed.

Table semantics: ``USD_PER_UNIT[ccy]`` = how many US dollars one unit of ``ccy``
buys (USD base).  Cross rates are derived through USD.

Rules
  * Money is ``Decimal`` end to end; results are rounded ROUND_HALF_UP to the
    target currency's minor unit (cents; whole units for zero-decimal JPY).
  * An unsupported / malformed code raises ``UnknownCurrency`` — it is never
    silently treated as USD.
  * Same-currency conversion returns the amount unchanged (no re-rounding), so
    base-currency data is byte-for-byte untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

RATES_AS_OF: Final[str] = "2026-11-01"
RATES_SOURCE: Final[str] = "simulated reference rates (not live market data)"

USD_PER_UNIT: Final[dict[str, Decimal]] = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.085"),
    "GBP": Decimal("1.27"),
    "CAD": Decimal("0.73"),
    "JPY": Decimal("0.0067"),
    "INR": Decimal("0.012"),
    "AUD": Decimal("0.66"),
    "CHF": Decimal("1.13"),
    "MXN": Decimal("0.055"),
    "SGD": Decimal("0.75"),
    "CNY": Decimal("0.14"),
}

# ISO-4217 minor units; every supported currency not listed has 2.
_MINOR_UNITS: Final[dict[str, int]] = {"JPY": 0}

SUPPORTED_CURRENCIES: Final[tuple[str, ...]] = tuple(sorted(USD_PER_UNIT))

# Displayed cross rates: 6 significant decimals is plenty for a reason string.
_RATE_DISPLAY = Decimal("0.000001")


class UnknownCurrency(ValueError):
    """A currency code outside the supported reference table."""

    def __init__(self, code: object) -> None:
        self.code = code
        super().__init__(f"currency not supported: {code!r}")


def normalize_currency(code: object) -> str:
    """Upper-cased, stripped supported ISO code, else ``UnknownCurrency``."""
    if not isinstance(code, str):
        raise UnknownCurrency(code)
    normalized = code.strip().upper()
    if normalized not in USD_PER_UNIT:
        raise UnknownCurrency(code)
    return normalized


def is_supported(code: object) -> bool:
    try:
        normalize_currency(code)
    except UnknownCurrency:
        return False
    return True


def minor_units(code: str) -> int:
    return _MINOR_UNITS.get(normalize_currency(code), 2)


def quantize_money(amount: Decimal, code: str) -> Decimal:
    """Round *amount* to *code*'s minor unit, ROUND_HALF_UP."""
    exponent = Decimal(1).scaleb(-minor_units(code))
    return amount.quantize(exponent, rounding=ROUND_HALF_UP)


def _as_decimal(amount: object) -> Decimal:
    if isinstance(amount, bool):
        raise TypeError("amount must be a number, not a bool")
    if isinstance(amount, Decimal):
        value = amount
    elif isinstance(amount, int | str):
        value = Decimal(amount)
    elif isinstance(amount, float):
        value = Decimal(str(amount))  # the shortest repr, never the binary expansion
    else:
        raise TypeError(f"amount must be a number, got {type(amount).__name__}")
    if not value.is_finite():
        raise InvalidOperation("amount must be finite")
    return value


def rate(from_ccy: str, to_ccy: str) -> Decimal:
    """Units of *to_ccy* per unit of *from_ccy* (exact Decimal division)."""
    src, dst = normalize_currency(from_ccy), normalize_currency(to_ccy)
    if src == dst:
        return Decimal("1")
    return USD_PER_UNIT[src] / USD_PER_UNIT[dst]


def display_rate(from_ccy: str, to_ccy: str) -> str:
    """The rate as shown to users: '1.085', '0.854331' (trailing zeros dropped)."""
    shown = rate(from_ccy, to_ccy).quantize(_RATE_DISPLAY, rounding=ROUND_HALF_UP)
    text = format(shown.normalize(), "f")
    return text


def convert(amount: object, from_ccy: str, to_ccy: str) -> Decimal:
    """Convert *amount* from one supported currency to another.

    Same currency -> the amount unchanged (as a Decimal, not re-rounded).
    Otherwise -> amount x rate, rounded ROUND_HALF_UP to the target's minor unit.
    Raises ``UnknownCurrency`` for an unsupported code on either side.
    """
    value = _as_decimal(amount)
    src, dst = normalize_currency(from_ccy), normalize_currency(to_ccy)
    if src == dst:
        return value
    return quantize_money(value * USD_PER_UNIT[src] / USD_PER_UNIT[dst], dst)


def format_money(amount: object, code: str) -> str:
    """'EUR 220.00', 'JPY 25000' — the currency's own minor unit."""
    value = quantize_money(_as_decimal(amount), code)
    return f"{normalize_currency(code)} {value:.{minor_units(code)}f}"


@dataclass(frozen=True)
class Conversion:
    """One conversion, kept together so a message can quote every figure."""

    original_amount: Decimal
    original_currency: str
    converted_amount: Decimal
    currency: str
    rate: str  # display rate (units of ``currency`` per unit of original)

    def describe(self) -> str:
        """'EUR 220.00 ≈ USD 238.70 at 1.085'."""
        return (
            f"{format_money(self.original_amount, self.original_currency)} ≈ "
            f"{format_money(self.converted_amount, self.currency)} at {self.rate}"
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "original_amount": str(self.original_amount),
            "original_currency": self.original_currency,
            "converted_amount": str(self.converted_amount),
            "currency": self.currency,
            "rate": self.rate,
            "rates_as_of": RATES_AS_OF,
            "rates_source": RATES_SOURCE,
        }


def conversion(amount: object, from_ccy: str, to_ccy: str) -> Conversion:
    """``convert`` plus the figures needed to explain it."""
    src, dst = normalize_currency(from_ccy), normalize_currency(to_ccy)
    value = _as_decimal(amount)
    return Conversion(
        original_amount=value,
        original_currency=src,
        converted_amount=convert(value, src, dst),
        currency=dst,
        rate=display_rate(src, dst),
    )
