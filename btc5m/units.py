"""
How the venue writes numbers: 18-decimal wei, and strings that may or
may not be a number at all.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN

WEI = Decimal(10) ** 18


def to_wei(amount_usdt: float | Decimal) -> str:
    """
    USDT -> wei string, truncated (never rounded up past the balance).

    Decimal throughout: float arithmetic on 18 decimals loses precision and
    would produce off-by-a-few-wei amounts the venue may reject.
    """
    d = Decimal(str(amount_usdt))
    if d <= 0:
        raise ValueError("amount must be positive")
    return str(int((d * WEI).to_integral_value(rounding=ROUND_DOWN)))


def from_wei(amount_wei: str | int) -> Decimal:
    """wei -> USDT as Decimal."""
    return Decimal(str(amount_wei)) / WEI


def _as_float_or_none(value: object) -> float | None:
    """Parse a numeric field, or None when it is absent or unusable."""
    try:
        parsed = float(value)          # float(None) raises TypeError too
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
