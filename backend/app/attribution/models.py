"""The five attribution models.

Each model answers the same question differently: of the touchpoints that
preceded a conversion, which ones deserve the credit? They disagree by design,
and the point of storing all five is to make that disagreement visible.

Exactness
---------
Every number in the credit path is a `Decimal`; there is no float anywhere,
including in the timestamp arithmetic that drives time decay.

Credits are apportioned with the **largest-remainder method**: convert the
weights into integer units (10^6 of them, i.e. 6 decimal places), floor each
share, then hand the leftover units out one at a time to the largest truncated
remainders, ties broken by touchpoint id. Credits therefore sum to exactly
`Decimal("1.000000")` — not 0.999999, not 1.000001.

Attributed revenue uses the same method at 2 decimal places, apportioning the
conversion's exact cent total. Multiplying each credit by the revenue and
rounding independently would leak pennies; this cannot.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Callable, Iterable, Protocol, Sequence

from app.db.models import Touchpoint
from app.attribution.journeys import Journey

# 6 decimal places of credit, matching AttributionResult.credit NUMERIC(8,6).
CREDIT_PLACES = 6
CREDIT_UNITS = 10**CREDIT_PLACES
CREDIT_EXPONENT = Decimal(1).scaleb(-CREDIT_PLACES)  # Decimal("0.000001")

# Money is apportioned in whole cents.
MONEY_PLACES = 2
MONEY_UNITS = 100
MONEY_EXPONENT = Decimal("0.01")

# Enough precision that the intermediate division is exact for any realistic
# journey length; the apportionment itself is integer arithmetic.
WORKING_PRECISION = 60

DEFAULT_HALF_LIFE_DAYS = 7
HOURS_PER_DAY = 24

HALF = Decimal("0.5")
POSITION_ENDPOINT_SHARE = Decimal("0.4")  # first and last
POSITION_MIDDLE_SHARE = Decimal("0.2")  # split across the middle


class AttributionModel(Protocol):
    """The common interface: a journey in, (touchpoint, credit) pairs out."""

    def __call__(
        self, journey: Journey, conversion_revenue: Decimal
    ) -> list[tuple[Touchpoint, Decimal]]: ...


# ---------------------------------------------------------------------------
# Largest-remainder apportionment
# ---------------------------------------------------------------------------


def _allocate_units(
    weights: Sequence[Decimal], total_units: int, tie_keys: Sequence[int]
) -> list[int]:
    """Split `total_units` across `weights` so the parts sum to it exactly.

    Floor every share, then give the remaining units to the largest truncated
    remainders first. `tie_keys` (touchpoint ids) break ties so the result is
    deterministic rather than dependent on iteration order.
    """
    count = len(weights)
    if count == 0:
        return []
    if total_units <= 0:
        return [0] * count

    with localcontext() as ctx:
        ctx.prec = WORKING_PRECISION
        total_weight = sum(weights, Decimal(0))
        if total_weight <= 0:
            # Degenerate input: fall back to an equal split rather than
            # dividing by zero. No model should produce this.
            weights = [Decimal(1)] * count
            total_weight = Decimal(count)

        raw = [Decimal(total_units) * weight / total_weight for weight in weights]

    floors = [int(value.to_integral_value(rounding=ROUND_DOWN)) for value in raw]
    remainders = [raw[index] - floors[index] for index in range(count)]
    leftover = total_units - sum(floors)

    if leftover > 0:
        order = sorted(
            range(count), key=lambda index: (-remainders[index], tie_keys[index])
        )
        for step in range(leftover):
            # The modulo only matters if precision ever made leftover > count,
            # which it should not; it keeps the invariant safe regardless.
            floors[order[step % count]] += 1
    elif leftover < 0:
        # Equally defensive: claw back from the smallest remainders.
        order = sorted(
            range(count), key=lambda index: (remainders[index], tie_keys[index])
        )
        for step in range(-leftover):
            index = order[step % count]
            if floors[index] > 0:
                floors[index] -= 1

    return floors


def allocate_credits(
    touchpoints: Sequence[Touchpoint], weights: Sequence[Decimal]
) -> list[tuple[Touchpoint, Decimal]]:
    """Turn raw weights into credits summing to exactly Decimal("1.000000")."""
    if not touchpoints:
        return []
    units = _allocate_units(
        weights, CREDIT_UNITS, [touchpoint.id for touchpoint in touchpoints]
    )
    return [
        (
            touchpoint,
            (Decimal(unit) / CREDIT_UNITS).quantize(CREDIT_EXPONENT),
        )
        for touchpoint, unit in zip(touchpoints, units)
    ]


def allocate_revenue(
    credits: Sequence[tuple[Touchpoint, Decimal]], conversion_revenue: Decimal
) -> list[Decimal]:
    """Split `conversion_revenue` by credit so the parts sum to it to the cent."""
    if not credits:
        return []
    total_cents = int(
        (conversion_revenue * MONEY_UNITS).to_integral_value(rounding=ROUND_HALF_UP)
    )
    units = _allocate_units(
        [credit for _, credit in credits],
        total_cents,
        [touchpoint.id for touchpoint, _ in credits],
    )
    return [(Decimal(unit) / MONEY_UNITS).quantize(MONEY_EXPONENT) for unit in units]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def last_touch(
    journey: Journey, conversion_revenue: Decimal
) -> list[tuple[Touchpoint, Decimal]]:
    """All credit to the final touch.

    Assumes the thing that closed the sale is the thing that caused it. Cheap
    to compute, systematically flatters late-funnel channels (paid search,
    email) and makes awareness spend look worthless.
    """
    touchpoints = journey.touchpoints
    if not touchpoints:
        return []
    weights = [Decimal(0)] * len(touchpoints)
    weights[-1] = Decimal(1)
    return allocate_credits(touchpoints, weights)


def first_touch(
    journey: Journey, conversion_revenue: Decimal
) -> list[tuple[Touchpoint, Decimal]]:
    """All credit to the first touch.

    The mirror-image bias: assumes discovery is everything and nurture is
    free. Flatters early-funnel channels (display, social).
    """
    touchpoints = journey.touchpoints
    if not touchpoints:
        return []
    weights = [Decimal(0)] * len(touchpoints)
    weights[0] = Decimal(1)
    return allocate_credits(touchpoints, weights)


def linear(
    journey: Journey, conversion_revenue: Decimal
) -> list[tuple[Touchpoint, Decimal]]:
    """Equal credit to every touch.

    Assumes no touch matters more than another — wrong, but unbiased about
    *which* direction it is wrong in, which makes it a useful baseline.
    """
    touchpoints = journey.touchpoints
    if not touchpoints:
        return []
    return allocate_credits(touchpoints, [Decimal(1)] * len(touchpoints))


def position_based(
    journey: Journey, conversion_revenue: Decimal
) -> list[tuple[Touchpoint, Decimal]]:
    """40% first, 40% last, 20% shared by the middle (a.k.a. U-shaped).

    Assumes discovery and closing are the hard parts and the middle is
    nurture. One touch takes 100%; two split 50/50.
    """
    touchpoints = journey.touchpoints
    count = len(touchpoints)
    if count == 0:
        return []
    if count == 1:
        return allocate_credits(touchpoints, [Decimal(1)])
    if count == 2:
        return allocate_credits(touchpoints, [Decimal(1), Decimal(1)])

    middle_each = POSITION_MIDDLE_SHARE / Decimal(count - 2)
    weights = [POSITION_ENDPOINT_SHARE]
    weights.extend([middle_each] * (count - 2))
    weights.append(POSITION_ENDPOINT_SHARE)
    return allocate_credits(touchpoints, weights)


class TimeDecay:
    """Exponential decay toward the conversion: 0.5 ** (hours_before / half_life).

    Assumes influence fades, so a touch an hour before the order matters more
    than one three weeks earlier. The half-life is the only knob: 7 days by
    default, meaning a touch one week out carries half the weight of one at
    the moment of conversion.

    Implemented as a class so the half-life can be configured while keeping
    the plain `(journey, conversion_revenue)` call signature.
    """

    def __init__(self, half_life_days: float | int | Decimal = DEFAULT_HALF_LIFE_DAYS):
        half_life_days = Decimal(str(half_life_days))
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        self.half_life_days = half_life_days
        self.half_life_hours = half_life_days * HOURS_PER_DAY

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TimeDecay(half_life_days={self.half_life_days})"

    @staticmethod
    def _hours_between(earlier: dt.datetime, later: dt.datetime) -> Decimal:
        """Elapsed hours as a Decimal.

        `timedelta.total_seconds()` returns a float, which would put a float
        in the credit path, so the components are combined by hand.
        """
        delta = later - earlier
        seconds = (
            Decimal(delta.days) * 86400
            + Decimal(delta.seconds)
            + Decimal(delta.microseconds) / 1_000_000
        )
        return seconds / 3600

    def __call__(
        self, journey: Journey, conversion_revenue: Decimal
    ) -> list[tuple[Touchpoint, Decimal]]:
        touchpoints = journey.touchpoints
        if not touchpoints:
            return []

        converted_at = journey.conversion.occurred_at
        with localcontext() as ctx:
            ctx.prec = WORKING_PRECISION
            weights = []
            for touchpoint in touchpoints:
                hours_before = self._hours_between(touchpoint.occurred_at, converted_at)
                if hours_before < 0:
                    # Should not happen (journeys only include prior touches),
                    # but a future touch must not out-weigh the present.
                    hours_before = Decimal(0)
                weights.append(HALF ** (hours_before / self.half_life_hours))

        return allocate_credits(touchpoints, weights)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

MODEL_NAMES: tuple[str, ...] = (
    "last_touch",
    "first_touch",
    "linear",
    "time_decay",
    "position_based",
)

#: Ready-to-use models with default settings. New models go here and are
#: picked up by the engine, the CLI and the analytics layer automatically.
MODELS: dict[str, AttributionModel] = {
    "last_touch": last_touch,
    "first_touch": first_touch,
    "linear": linear,
    "time_decay": TimeDecay(),
    "position_based": position_based,
}


def get_models(
    half_life_days: float | int | Decimal = DEFAULT_HALF_LIFE_DAYS,
) -> dict[str, AttributionModel]:
    """The registry, with `time_decay` configured for `half_life_days`."""
    registry = dict(MODELS)
    registry["time_decay"] = TimeDecay(half_life_days=half_life_days)
    return registry
