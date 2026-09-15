"""A coherent synthetic marketing world.

The generator's whole reason for existing is that attribution cannot be tested
against random unrelated rows. It needs *journeys*: several touchpoints for the
same user, ordered in time, followed by a conversion the touchpoints could
plausibly have caused. So this module builds one internally consistent world up
front, and `fake_apis` serves slices of it afterwards.

Everything derives from a single seeded `random.Random` (plus a Faker seeded
identically), so the same `WorldConfig.seed` always yields a byte-identical
world.

No database, no ORM, no I/O — just plain dicts.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from faker import Faker
import random

# ---------------------------------------------------------------------------
# Channel definitions
# ---------------------------------------------------------------------------

FunnelPosition = Literal["early", "early_mid", "mid", "late", "any"]

# How tightly a channel clusters around its funnel position. Smaller = stricter.
FUNNEL_SIGMA = 0.25


@dataclass(frozen=True)
class ChannelSpec:
    """Everything that makes one channel behave differently from the others.

    `funnel_score` is the channel's preferred position within a journey on a
    0.0 (first touch) .. 1.0 (last touch) scale. `None` means "any position"
    and produces a flat distribution — that is what organic traffic does.
    """

    name: str
    platform: str
    funnel_position: FunnelPosition
    funnel_score: float | None
    # Relative share of all touchpoints; display is high-volume, affiliate is not.
    volume_weight: float
    # (touch_type, weight) pairs valid for this channel.
    touch_types: tuple[tuple[str, float], ...]
    # "cpm" | "cpc" | "near_zero" | "none"
    cost_model: str
    cpm: float = 0.0
    cpc: float = 0.0
    # Click-through rate, used to reconcile impressions against clicks.
    ctr: float = 0.01
    # Typical per-campaign daily volume. CPM channels are driven by
    # impressions, CPC channels by clicks; the other metric is derived via ctr.
    daily_impression_base: int = 0
    daily_click_base: int = 0


CHANNELS: dict[str, ChannelSpec] = {
    "display": ChannelSpec(
        name="display",
        platform="meta_ads",
        funnel_position="early",
        funnel_score=0.10,
        volume_weight=3.0,
        touch_types=(("impression", 0.92), ("click", 0.08)),
        cost_model="cpm",
        cpm=4.50,
        ctr=0.008,
        daily_impression_base=4000,
    ),
    "social": ChannelSpec(
        name="social",
        platform="meta_ads",
        funnel_position="early_mid",
        funnel_score=0.30,
        volume_weight=2.5,
        touch_types=(("impression", 0.60), ("click", 0.40)),
        cost_model="cpm",
        cpm=8.00,
        ctr=0.020,
        daily_impression_base=2500,
    ),
    "affiliate": ChannelSpec(
        name="affiliate",
        platform="impact",
        funnel_position="mid",
        funnel_score=0.50,
        volume_weight=1.0,
        touch_types=(("click", 1.0),),
        cost_model="cpc",
        cpc=0.85,
        ctr=0.040,
        daily_click_base=15,
    ),
    "email": ChannelSpec(
        name="email",
        platform="klaviyo",
        funnel_position="late",
        funnel_score=0.80,
        volume_weight=1.2,
        touch_types=(("email_open", 0.70), ("click", 0.30)),
        cost_model="near_zero",
        ctr=0.220,
        daily_click_base=40,
    ),
    "paid_search": ChannelSpec(
        name="paid_search",
        platform="google_ads",
        funnel_position="late",
        funnel_score=0.90,
        volume_weight=1.5,
        touch_types=(("click", 1.0),),
        cost_model="cpc",
        cpc=2.40,
        ctr=0.060,
        daily_click_base=30,
    ),
    "organic": ChannelSpec(
        name="organic",
        platform="organic",
        funnel_position="any",
        funnel_score=None,
        volume_weight=1.0,
        touch_types=(("visit", 0.65), ("click", 0.35)),
        cost_model="none",
        ctr=0.0,
    ),
}

EARLY_FUNNEL_CHANNELS = ("display", "social")
LATE_FUNNEL_CHANNELS = ("email", "paid_search")

# Which ad-platform metric a touch_type contributes to.
TOUCH_TYPE_METRIC = {
    "impression": "impressions",
    "email_open": "impressions",
    "click": "clicks",
    "visit": "clicks",
}

# Journey-length distribution for 1..6 touchpoints. Converters skew longer.
NON_CONVERTER_LENGTH_WEIGHTS = (0.30, 0.28, 0.20, 0.12, 0.07, 0.03)
CONVERTER_LENGTH_WEIGHTS = (0.12, 0.18, 0.22, 0.20, 0.16, 0.12)

# How many days a journey spans, and how likely each span is.
JOURNEY_SPAN_DAYS = (0, 1, 2, 3, 5, 7, 10, 14, 21)
JOURNEY_SPAN_WEIGHTS = (0.10, 0.15, 0.15, 0.15, 0.13, 0.12, 0.10, 0.06, 0.04)

# Days reserved at the end of the window so a conversion still fits inside it.
CONVERSION_LAG_BUFFER_DAYS = 3

WEEKEND_FACTOR = 0.80


# ---------------------------------------------------------------------------
# Config and result types
# ---------------------------------------------------------------------------


def _default_end_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _default_start_date() -> dt.date:
    return _default_end_date() - dt.timedelta(days=59)


@dataclass
class WorldConfig:
    """Knobs for `build_world`. The defaults describe a 60-day window."""

    seed: int = 42
    n_campaigns: int = 12
    n_users: int = 800
    start_date: dt.date = field(default_factory=_default_start_date)
    end_date: dt.date = field(default_factory=_default_end_date)
    conversion_rate: float = 0.22

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if not 0.0 <= self.conversion_rate <= 1.0:
            raise ValueError("conversion_rate must be between 0.0 and 1.0")
        if self.n_campaigns < 1:
            raise ValueError("n_campaigns must be at least 1")
        if self.n_users < 1:
            raise ValueError("n_users must be at least 1")

    @property
    def n_days(self) -> int:
        return (self.end_date - self.start_date).days + 1


@dataclass
class World:
    """A fully materialised synthetic world, as plain dicts."""

    config: WorldConfig
    campaigns: list[dict[str, Any]]
    users: list[str]
    touchpoints: list[dict[str, Any]]
    conversions: list[dict[str, Any]]
    daily_spend: list[dict[str, Any]]

    def summary(self) -> dict[str, Any]:
        """Headline counts, used by the CLI and by tests."""
        total_spend = round(sum(row["spend"] for row in self.daily_spend), 2)
        total_revenue = round(sum(c["revenue"] for c in self.conversions), 2)
        converting_users = {c["user_id"] for c in self.conversions}
        return {
            "seed": self.config.seed,
            "campaigns": len(self.campaigns),
            "users": len(self.users),
            "touchpoints": len(self.touchpoints),
            "conversions": len(self.conversions),
            "daily_spend_rows": len(self.daily_spend),
            "converting_users": len(converting_users),
            "observed_conversion_rate": (
                round(len(converting_users) / len(self.users), 4) if self.users else 0.0
            ),
            "total_spend": total_spend,
            "total_revenue": total_revenue,
            "blended_roas": (
                round(total_revenue / total_spend, 2) if total_spend else None
            ),
            "window_start": self.config.start_date.isoformat(),
            "window_end": self.config.end_date.isoformat(),
        }

    def campaigns_by_channel(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for campaign in self.campaigns:
            grouped.setdefault(campaign["channel"], []).append(campaign)
        return grouped

    def channel_breakdown(self) -> list[dict[str, Any]]:
        """Per-channel campaign / touchpoint / spend rollup for the CLI table."""
        spend_by_campaign: dict[str, float] = {}
        for row in self.daily_spend:
            spend_by_campaign[row["campaign_external_id"]] = (
                spend_by_campaign.get(row["campaign_external_id"], 0.0) + row["spend"]
            )
        channel_of_campaign = {c["external_id"]: c["channel"] for c in self.campaigns}

        rows: list[dict[str, Any]] = []
        for name, spec in CHANNELS.items():
            campaigns = [c for c in self.campaigns if c["channel"] == name]
            touchpoints = [t for t in self.touchpoints if t["channel"] == name]
            spend = sum(
                amount
                for ext_id, amount in spend_by_campaign.items()
                if channel_of_campaign.get(ext_id) == name
            )
            last_touches = sum(1 for t in touchpoints if t["is_last_touch"])
            rows.append(
                {
                    "channel": name,
                    "funnel_position": spec.funnel_position,
                    "campaigns": len(campaigns),
                    "touchpoints": len(touchpoints),
                    "last_touches": last_touches,
                    "spend": round(spend, 2),
                }
            )
        return rows

    def touchpoints_before_conversion(self) -> dict[str, int]:
        """How many conversions were preceded by 1, 2, or 3+ touchpoints."""
        touches_by_user: dict[str, list[dt.datetime]] = {}
        for touch in self.touchpoints:
            touches_by_user.setdefault(touch["user_id"], []).append(touch["occurred_at"])

        buckets = {"1": 0, "2": 0, "3+": 0}
        for conversion in self.conversions:
            prior = sum(
                1
                for occurred_at in touches_by_user.get(conversion["user_id"], [])
                if occurred_at < conversion["occurred_at"]
            )
            if prior <= 1:
                buckets["1"] += 1
            elif prior == 2:
                buckets["2"] += 1
            else:
                buckets["3+"] += 1
        return buckets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hex_id(rng: random.Random, n_chars: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n_chars))


def _weighted_choice(rng: random.Random, options: Sequence[Any], weights: Sequence[float]) -> Any:
    return rng.choices(list(options), weights=list(weights), k=1)[0]


def _funnel_affinity(spec: ChannelSpec, position: float) -> float:
    """How well a channel fits `position` (0.0 = first touch, 1.0 = last touch).

    A Gaussian around the channel's funnel score. "any"-position channels are
    flat, so organic stays equally likely anywhere in a journey.
    """
    if spec.funnel_score is None:
        return 1.0
    delta = spec.funnel_score - position
    return math.exp(-(delta**2) / (2 * FUNNEL_SIGMA**2))


def _pick_channel(rng: random.Random, position: float) -> ChannelSpec:
    specs = list(CHANNELS.values())
    weights = [spec.volume_weight * _funnel_affinity(spec, position) for spec in specs]
    return _weighted_choice(rng, specs, weights)


def _pick_touch_type(rng: random.Random, spec: ChannelSpec) -> str:
    types = [t for t, _ in spec.touch_types]
    weights = [w for _, w in spec.touch_types]
    return _weighted_choice(rng, types, weights)


def _is_weekend(day: dt.date) -> bool:
    return day.weekday() >= 5


# ---------------------------------------------------------------------------
# Campaign generation
# ---------------------------------------------------------------------------

_NAME_TEMPLATES: dict[str, tuple[str, ...]] = {
    "display": (
        "Prospecting Display — {geo} {theme}",
        "Display Retargeting — {audience}",
        "Display Awareness — {quarter} {theme}",
    ),
    "social": (
        "{quarter} Social {objective} — {audience}",
        "Social Lookalike 1% — {geo}",
        "Social UGC Creative — {theme}",
    ),
    "paid_search": (
        "Brand Search — Exact {geo}",
        "Non-Brand Search — {theme}",
        "Shopping — {theme} {geo}",
    ),
    "email": (
        "Lifecycle Email — {flow}",
        "{quarter} Email Promo — {theme}",
    ),
    "affiliate": (
        "Affiliate — {partner}",
        "Affiliate Coupon — {partner}",
    ),
    "organic": (
        "Organic / Direct",
        "Organic Search — Unpaid",
    ),
}

_GEOS = ("US", "CA", "UK", "DACH", "APAC", "US-West", "US-East")
_AUDIENCES = ("Cart Abandoners", "Site Visitors 30d", "Lookalike 2%", "New Customers", "VIP Segment")
_QUARTERS = ("Q1", "Q2", "Q3", "Q4", "Spring", "Summer", "Fall", "Holiday")
_OBJECTIVES = ("Prospecting", "Retargeting", "Conversions", "Reach")
_FLOWS = ("Welcome Series", "Abandoned Cart", "Win-Back", "Post-Purchase", "Browse Abandon")
# Fed to Faker as an ext_word_list so the themes stay seeded/deterministic but
# read like product lines instead of arbitrary dictionary words.
_PRODUCT_NOUNS = (
    "Sneakers", "Outerwear", "Denim", "Skincare", "Fragrance", "Homeware",
    "Accessories", "Footwear", "Athleisure", "Wellness", "Bedding", "Eyewear",
)


def _allocate_campaigns_per_channel(rng: random.Random, n_campaigns: int) -> dict[str, int]:
    """Spread `n_campaigns` across channels, giving every channel at least one."""
    names = list(CHANNELS)
    if n_campaigns <= len(names):
        chosen = rng.sample(names, n_campaigns)
        return {name: 1 for name in names if name in chosen}

    allocation = {name: 1 for name in names}
    weights = [CHANNELS[name].volume_weight for name in names]
    for _ in range(n_campaigns - len(names)):
        allocation[_weighted_choice(rng, names, weights)] += 1
    return allocation


def _build_campaigns(
    rng: random.Random, fake: Faker, config: WorldConfig
) -> list[dict[str, Any]]:
    allocation = _allocate_campaigns_per_channel(rng, config.n_campaigns)
    campaigns: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for channel, count in allocation.items():
        spec = CHANNELS[channel]
        for _ in range(count):
            while True:
                external_id = f"cmp_{_hex_id(rng, 8)}"
                if external_id not in seen_ids:
                    seen_ids.add(external_id)
                    break

            template = rng.choice(_NAME_TEMPLATES[channel])
            name = template.format(
                geo=rng.choice(_GEOS),
                audience=rng.choice(_AUDIENCES),
                quarter=rng.choice(_QUARTERS),
                objective=rng.choice(_OBJECTIVES),
                flow=rng.choice(_FLOWS),
                # Faker supplies the varying product/partner nouns.
                theme=fake.word(ext_word_list=_PRODUCT_NOUNS),
                partner=fake.company(),
            )

            created_at = dt.datetime.combine(
                config.start_date - dt.timedelta(days=rng.randint(1, 120)),
                dt.time(hour=rng.randint(0, 23), minute=rng.randint(0, 59)),
                tzinfo=dt.timezone.utc,
            )

            campaigns.append(
                {
                    "external_id": external_id,
                    "name": name,
                    "channel": channel,
                    "platform": spec.platform,
                    "created_at": created_at,
                }
            )

    return campaigns


# ---------------------------------------------------------------------------
# Journeys and conversions
# ---------------------------------------------------------------------------


def _journey_length(rng: random.Random, converts: bool) -> int:
    weights = CONVERTER_LENGTH_WEIGHTS if converts else NON_CONVERTER_LENGTH_WEIGHTS
    return _weighted_choice(rng, range(1, 7), weights)


def _build_journeys(
    rng: random.Random,
    config: WorldConfig,
    campaigns: list[dict[str, Any]],
    users: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build every user's ordered touchpoints, plus conversions for converters."""
    campaigns_by_channel: dict[str, list[dict[str, Any]]] = {}
    for campaign in campaigns:
        campaigns_by_channel.setdefault(campaign["channel"], []).append(campaign)

    window_start = dt.datetime.combine(
        config.start_date, dt.time(0, 0), tzinfo=dt.timezone.utc
    )
    window_end = dt.datetime.combine(
        config.end_date, dt.time(23, 59, 59), tzinfo=dt.timezone.utc
    )

    touchpoints: list[dict[str, Any]] = []
    conversions: list[dict[str, Any]] = []
    seen_touch_ids: set[str] = set()
    seen_order_ids: set[str] = set()

    for user_id in users:
        converts = rng.random() < config.conversion_rate
        n_touches = _journey_length(rng, converts)
        span_days = _weighted_choice(rng, JOURNEY_SPAN_DAYS, JOURNEY_SPAN_WEIGHTS)

        # Reserve room at the end of the window for the conversion lag.
        latest_start = window_end - dt.timedelta(
            days=span_days + CONVERSION_LAG_BUFFER_DAYS
        )
        usable_seconds = max(0, int((latest_start - window_start).total_seconds()))
        journey_start = window_start + dt.timedelta(
            seconds=rng.randint(0, usable_seconds) if usable_seconds else 0
        )

        span_seconds = span_days * 86_400
        offsets = sorted(rng.randint(0, span_seconds) for _ in range(n_touches))

        journey: list[dict[str, Any]] = []
        for index, offset in enumerate(offsets):
            # Keep timestamps strictly increasing even when offsets collide.
            occurred_at = journey_start + dt.timedelta(seconds=offset + index * 61)

            position = 0.5 if n_touches == 1 else index / (n_touches - 1)
            spec = _pick_channel(rng, position)

            # Organic traffic belongs to no campaign, by design.
            if spec.name == "organic":
                campaign_external_id = None
            else:
                campaign_external_id = rng.choice(
                    campaigns_by_channel[spec.name]
                )["external_id"]

            while True:
                external_id = f"evt_{_hex_id(rng, 12)}"
                if external_id not in seen_touch_ids:
                    seen_touch_ids.add(external_id)
                    break

            journey.append(
                {
                    "external_id": external_id,
                    "user_id": user_id,
                    "campaign_external_id": campaign_external_id,
                    "channel": spec.name,
                    "touch_type": _pick_touch_type(rng, spec),
                    "occurred_at": occurred_at,
                    "position": index,
                    "is_last_touch": False,
                }
            )

        journey[-1]["is_last_touch"] = True
        touchpoints.extend(journey)

        if not converts:
            continue

        # Conversion lands 1-2 days after the LAST touch, so it can never
        # precede the first one.
        last_touch_at = journey[-1]["occurred_at"]
        occurred_at = last_touch_at + dt.timedelta(
            days=rng.choice((1, 2)),
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
        )
        occurred_at = min(occurred_at, window_end)

        # Lognormal revenue, clamped to a believable order-value range.
        revenue = rng.lognormvariate(math.log(140.0), 0.70)
        revenue = round(min(max(revenue, 25.0), 900.0), 2)

        while True:
            order_id = f"ord_{_hex_id(rng, 10)}"
            if order_id not in seen_order_ids:
                seen_order_ids.add(order_id)
                break

        conversions.append(
            {
                "external_id": order_id,
                "user_id": user_id,
                "occurred_at": occurred_at,
                "revenue": revenue,
                "currency": "USD",
            }
        )

    touchpoints.sort(key=lambda t: (t["occurred_at"], t["external_id"]))
    conversions.sort(key=lambda c: (c["occurred_at"], c["external_id"]))
    return touchpoints, conversions


# ---------------------------------------------------------------------------
# Daily spend
# ---------------------------------------------------------------------------


def _build_daily_spend(
    rng: random.Random,
    config: WorldConfig,
    campaigns: list[dict[str, Any]],
    touchpoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Daily spend per campaign, reconciled against the journey data.

    The touchpoint stream is a *tracked sample* of activity, so reported
    impressions and clicks are always >= what the journeys actually contain.
    They are never less, which is what would make spend contradict the
    journeys.
    """
    observed: dict[tuple[str, dt.date], dict[str, int]] = {}
    for touch in touchpoints:
        campaign_id = touch["campaign_external_id"]
        if campaign_id is None:
            continue
        key = (campaign_id, touch["occurred_at"].date())
        bucket = observed.setdefault(key, {"impressions": 0, "clicks": 0})
        bucket[TOUCH_TYPE_METRIC[touch["touch_type"]]] += 1

    rows: list[dict[str, Any]] = []
    all_days = [
        config.start_date + dt.timedelta(days=offset) for offset in range(config.n_days)
    ]

    for campaign in campaigns:
        spec = CHANNELS[campaign["channel"]]
        # Per-campaign scale factor so campaigns are not all the same size.
        campaign_scale = rng.uniform(0.6, 1.8)

        for day in all_days:
            seen = observed.get((campaign["external_id"], day), {"impressions": 0, "clicks": 0})

            if spec.cost_model == "none":
                # Organic campaigns cost nothing and buy no inventory.
                rows.append(
                    {
                        "campaign_external_id": campaign["external_id"],
                        "date": day,
                        "spend": 0.0,
                        "impressions": 0,
                        "clicks": 0,
                    }
                )
                continue

            # The weekend factor is applied exactly once, to volume; spend is
            # derived from volume, so it dips by the same ~20% and no more.
            weekend_factor = WEEKEND_FACTOR if _is_weekend(day) else 1.0
            daily_noise = rng.uniform(0.75, 1.25)
            volume = campaign_scale * weekend_factor * daily_noise

            if spec.cost_model == "cpm":
                impressions = int(round(spec.daily_impression_base * volume))
                clicks = int(round(impressions * spec.ctr))
            else:
                clicks = int(round(spec.daily_click_base * volume))
                impressions = int(round(clicks / spec.ctr)) if spec.ctr else clicks

            # Never report less than the journeys actually contain, and never
            # report more clicks than impressions.
            clicks = max(clicks, seen["clicks"])
            impressions = max(impressions, seen["impressions"], clicks)

            if spec.cost_model == "cpm":
                spend = impressions / 1000.0 * spec.cpm
            elif spec.cost_model == "cpc":
                spend = clicks * spec.cpc
            else:  # near_zero: ESP cost is per-send, effectively rounding error
                spend = clicks * 0.01 + 0.25
            spend = round(spend * rng.uniform(0.95, 1.05), 2)

            rows.append(
                {
                    "campaign_external_id": campaign["external_id"],
                    "date": day,
                    "spend": spend,
                    "impressions": impressions,
                    "clicks": clicks,
                }
            )

    rows.sort(key=lambda r: (r["date"], r["campaign_external_id"]))
    return rows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_world(config: WorldConfig | None = None) -> World:
    """Build a complete, internally consistent world from `config`."""
    config = config or WorldConfig()

    rng = random.Random(config.seed)
    fake = Faker()
    # seed_instance (not Faker.seed) keeps this generator isolated from any
    # other Faker user in the process.
    fake.seed_instance(config.seed)

    campaigns = _build_campaigns(rng, fake, config)

    users: list[str] = []
    seen_users: set[str] = set()
    while len(users) < config.n_users:
        user_id = f"usr_{_hex_id(rng, 12)}"
        if user_id not in seen_users:
            seen_users.add(user_id)
            users.append(user_id)

    touchpoints, conversions = _build_journeys(rng, config, campaigns, users)
    daily_spend = _build_daily_spend(rng, config, campaigns, touchpoints)

    return World(
        config=config,
        campaigns=campaigns,
        users=users,
        touchpoints=touchpoints,
        conversions=conversions,
        daily_spend=daily_spend,
    )
