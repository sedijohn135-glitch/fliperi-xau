"""Price / money scaling helpers.

cTrader's API speaks in *relative* integers, not human prices: XAUUSD at 4431.23
arrives as ``443123000`` (price x 1e5) and account money arrives in cents
(money x 100).  Feeding a raw 443123000 into position sizing would compute a lot
size ~100,000x too small - or, worse, a stop distance that makes the risk maths
meaningless - so every number crossing the API boundary is normalised here, once.

Scaling is auto-detected against a plausibility band and then logged, so the
operator can always see which convention was applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from utils.logging import get_logger

log = get_logger("prices")

#: Candidate divisors, most-likely first.  1e5 is the documented cTrader
#: "relative price" convention; 1 covers servers that already return real prices.
PRICE_SCALE_CANDIDATES: tuple[float, ...] = (1e5, 1.0, 1e3, 1e2, 1e7)

#: cTrader money is normally in cents (moneyDigits = 2).
MONEY_SCALE_CANDIDATES: tuple[float, ...] = (100.0, 1.0, 1e5)

#: A balance outside this band is almost certainly a scaling mistake.
MONEY_SANE_MIN = 0.01
MONEY_SANE_MAX = 1e9


@dataclass(frozen=True)
class Scale:
    """A resolved divisor plus how we arrived at it (for logging)."""

    divisor: float
    source: str  # "configured" | "detected" | "fallback"

    def apply(self, raw: float | int | None) -> Optional[float]:
        if raw is None:
            return None
        return float(raw) / self.divisor


def detect_scale(
    samples: Sequence[float],
    candidates: Iterable[float],
    sane_min: float,
    sane_max: float,
    *,
    label: str = "value",
) -> Scale:
    """Pick the first candidate divisor that lands every sample in the sane band."""
    usable = [float(s) for s in samples if s is not None and float(s) != 0.0]
    if not usable:
        return Scale(1.0, "fallback")

    for divisor in candidates:
        if all(sane_min <= abs(value) / divisor <= sane_max for value in usable):
            return Scale(divisor, "detected")

    log.warning(
        "Could not auto-detect %s scale from samples %s (band %.4g..%.4g); "
        "using 1.0 - set the matching *_SCALE env var if numbers look wrong.",
        label, usable[:4], sane_min, sane_max,
    )
    return Scale(1.0, "fallback")


def resolve_price_scale(
    configured: Optional[float],
    samples: Sequence[float],
    sane_min: float,
    sane_max: float,
) -> Scale:
    if configured is not None:
        return Scale(configured, "configured")
    return detect_scale(samples, PRICE_SCALE_CANDIDATES, sane_min, sane_max, label="price")


def resolve_money_scale(configured: Optional[float], samples: Sequence[float]) -> Scale:
    if configured is not None:
        return Scale(configured, "configured")
    return detect_scale(
        samples, MONEY_SCALE_CANDIDATES, MONEY_SANE_MIN, MONEY_SANE_MAX, label="money"
    )


def normalize_price(raw: Optional[float], scale: Scale,
                    sane_min: float, sane_max: float) -> Optional[float]:
    """Unscale a price only if it actually needs it.

    The server is not consistent: ``get_spot_prices`` and ``get_trendbars``
    return integers scaled by 1e5, while ``get_positions`` returns prices
    already in human units. Dividing those a second time turned an 79,851.50
    entry into 0.80 - three different levels all collapsing onto the same
    number, which is what gave it away.

    So the value decides. If it is already inside the instrument's plausible
    band it is taken as-is; otherwise the detected scale is applied. That is
    self-correcting whichever convention a given payload happens to use.
    """
    if raw is None:
        return None
    value = float(raw)
    if value == 0.0:
        return 0.0
    if sane_min <= abs(value) <= sane_max:
        return value
    return scale.apply(value)


def round_to_step(value: float, step: float, *, mode: str = "down") -> float:
    """Round ``value`` onto the broker's volume grid.

    Position sizing always rounds *down*: rounding up would silently breach the
    configured risk-per-trade limit.
    """
    if step <= 0:
        return value
    quotient = value / step
    if mode == "down":
        steps = int(quotient + 1e-9)
    elif mode == "up":
        steps = int(-(-quotient // 1))
    else:
        steps = int(round(quotient))
    result = steps * step
    # Kill binary-float dust like 0.30000000000000004.
    decimals = max(0, len(f"{step:.10f}".rstrip("0").split(".")[-1]))
    return round(result, decimals)


def price_decimals(point_size: float) -> int:
    """Number of decimals implied by a point size (0.01 -> 2)."""
    text = f"{point_size:.10f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".")[1])


def round_price(price: float, point_size: float) -> float:
    return round(price, price_decimals(point_size))
