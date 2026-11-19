"""Display-side currency conversion for API responses (product gap 8).

Wraps the static reference table (``config.constants.fx_rates`` — SIMULATED
reference rates, not live market data) in a response model, so a receipt or an
entry in a foreign currency is shown with its original amount AND the amount in
the base currency it is reported in, plus the rate that was assumed.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from config.constants.fx_rates import (
    RATES_AS_OF,
    RATES_SOURCE,
    UnknownCurrency,
    conversion,
    normalize_currency,
)


class AmountConversion(BaseModel):
    """An amount in its original currency and in the base currency it reports in.

    ``supported`` False: the original currency is outside the reference table —
    ``converted_amount`` / ``rate`` are then None (never a silent 1:1 guess).
    """

    original_amount: Decimal
    original_currency: str
    converted_amount: Decimal | None
    currency: str
    rate: str | None
    supported: bool
    rates_as_of: str = RATES_AS_OF
    rates_source: str = RATES_SOURCE


def amount_conversion(amount: object, currency: object, base: object) -> AmountConversion | None:
    """The conversion of *amount* (*currency*) into *base*, for display.

    None when there is nothing to explain: no amount / currency, an unusable base,
    or the amount is already in the base currency.
    """
    if amount is None or isinstance(amount, bool):
        return None
    if not isinstance(currency, str) or not currency.strip():
        return None
    try:
        target = normalize_currency(base)
    except UnknownCurrency:
        return None
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    original = currency.strip().upper()
    if original == target:
        return None
    try:
        conv = conversion(value, original, target)
    except UnknownCurrency:
        return AmountConversion(
            original_amount=value,
            original_currency=original,
            converted_amount=None,
            currency=target,
            rate=None,
            supported=False,
        )
    return AmountConversion(
        original_amount=conv.original_amount,
        original_currency=conv.original_currency,
        converted_amount=conv.converted_amount,
        currency=conv.currency,
        rate=conv.rate,
        supported=True,
    )
