"""Tests for the attribution engine.

The model tests are pure: hand-built journeys with credit vectors worked out
by hand. The engine and analytics tests hit the real local Postgres against a
small, deliberately-shaped dataset, and truncate before and after themselves.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.db.models import (
    AdSpend,
    AttributionResult,
    Campaign,
    Conversion,
    Touchpoint,
)
from app.db.session import SessionLocal
from app.attribution.analytics import (
    channel_performance,
    model_comparison,
    pipeline_health,
    timeseries,
    top_journeys,
)
from app.attribution.engine import run_attribution
from app.attribution.journeys import Journey, load_journeys
from app.attribution.models import (
    MODEL_NAMES,
    TimeDecay,
    allocate_revenue,
    get_models,
)
from app.pipeline.cli import DATA_TABLES

UTC = dt.timezone.utc
CONVERTED_AT = dt.datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ONE = Decimal("1.000000")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _touchpoint(
    touchpoint_id: int,
    *,
    hours_before: float = 0,
    days_before: float = 0,
    channel: str = "display",
    user_id: str = "usr_test",
) -> Touchpoint:
    """An unpersisted Touchpoint; the models only read its fields."""
    return Touchpoint(
        id=touchpoint_id,
        external_id=f"evt_{touchpoint_id}",
        user_id=user_id,
        campaign_id=None,
        channel=channel,
        occurred_at=CONVERTED_AT
        - dt.timedelta(hours=hours_before, days=days_before),
        touch_type="click",
    )


def _journey(touchpoints, revenue: str = "400.00", conversion_id: int = 1) -> Journey:
    conversion = Conversion(
        id=conversion_id,
        external_id=f"ord_{conversion_id}",
        user_id="usr_test",
        occurred_at=CONVERTED_AT,
        revenue=Decimal(revenue),
        currency="USD",
    )
    return Journey(conversion=conversion, touchpoints=list(touchpoints))


def _credits(model_name: str, journey: Journey, half_life_days: float = 7):
    model = get_models(half_life_days)[model_name]
    return model(journey, journey.revenue)


def _vector(result) -> list[Decimal]:
    return [credit for _, credit in result]


@pytest.fixture
def db():
    session = SessionLocal()
    _truncate(session)
    try:
        yield session
    finally:
        _truncate(session)
        session.close()


def _truncate(session) -> None:
    session.rollback()
    session.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    session.commit()


# ---------------------------------------------------------------------------
# Exact credit vectors
# ---------------------------------------------------------------------------

# A 4-touchpoint journey: 20 days, 10 days, 2 days, 1 hour before the order.
FOUR_TOUCH = [
    _touchpoint(1, days_before=20),
    _touchpoint(2, days_before=10),
    _touchpoint(3, days_before=2),
    _touchpoint(4, hours_before=1),
]


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        (
            "last_touch",
            ["0.000000", "0.000000", "0.000000", "1.000000"],
        ),
        (
            "first_touch",
            ["1.000000", "0.000000", "0.000000", "0.000000"],
        ),
        (
            "linear",
            ["0.250000", "0.250000", "0.250000", "0.250000"],
        ),
        (
            # 40% first, 20% shared by the two middles, 40% last.
            "position_based",
            ["0.400000", "0.100000", "0.100000", "0.400000"],
        ),
        (
            # 0.5 ** (hours_before / 168), normalised by largest remainder.
            "time_decay",
            ["0.059341", "0.159734", "0.352722", "0.428203"],
        ),
    ],
)
def test_four_touchpoint_journey_credit_vectors(model_name, expected):
    journey = _journey(FOUR_TOUCH)
    assert _vector(_credits(model_name, journey)) == [Decimal(v) for v in expected]


def test_single_touchpoint_journey_gives_every_model_full_credit():
    journey = _journey([_touchpoint(1, days_before=3)])
    for model_name in MODEL_NAMES:
        vector = _vector(_credits(model_name, journey))
        assert vector == [ONE], f"{model_name} gave {vector}"


def test_two_touchpoint_journey_position_based_is_exactly_fifty_fifty():
    journey = _journey(
        [_touchpoint(1, days_before=5), _touchpoint(2, hours_before=2)]
    )
    assert _vector(_credits("position_based", journey)) == [
        Decimal("0.500000"),
        Decimal("0.500000"),
    ]


@pytest.mark.parametrize("length", list(range(1, 11)))
@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_credits_sum_to_exactly_one(model_name, length):
    """Not 0.999999, not 1.000001 — exactly Decimal("1.000000")."""
    touchpoints = [
        _touchpoint(index + 1, hours_before=(length - index) * 30)
        for index in range(length)
    ]
    journey = _journey(touchpoints, revenue="77.77")
    vector = _vector(_credits(model_name, journey))

    assert len(vector) == length
    assert sum(vector, Decimal(0)) == ONE
    assert all(Decimal(0) <= credit <= ONE for credit in vector)
    # Every credit carries exactly 6 decimal places.
    assert all(-credit.as_tuple().exponent == 6 for credit in vector)


# ---------------------------------------------------------------------------
# Penny-leak
# ---------------------------------------------------------------------------


def test_attributed_revenue_sums_exactly_for_an_awkward_amount():
    """100.01 split three ways is the classic penny leak.

    Naive rounding gives 33.34 + 33.34 + 33.34 = 100.02 or
    33.33 * 3 = 99.99. Largest-remainder gives exactly 100.01.
    """
    revenue = Decimal("100.01")
    journey = _journey(
        [
            _touchpoint(1, days_before=3),
            _touchpoint(2, days_before=2),
            _touchpoint(3, days_before=1),
        ],
        revenue=str(revenue),
    )
    credits = _credits("linear", journey)
    allocated = allocate_revenue(credits, revenue)

    assert sum(allocated, Decimal(0)) == revenue
    assert allocated == [Decimal("33.34"), Decimal("33.34"), Decimal("33.33")]


@pytest.mark.parametrize(
    "revenue",
    ["100.01", "0.01", "0.02", "1.00", "999999.99", "33.33", "7.77"],
)
@pytest.mark.parametrize("length", [1, 2, 3, 7, 10])
@pytest.mark.parametrize("model_name", MODEL_NAMES)
def test_attributed_revenue_never_leaks_a_cent(model_name, length, revenue):
    amount = Decimal(revenue)
    touchpoints = [
        _touchpoint(index + 1, hours_before=(length - index) * 19)
        for index in range(length)
    ]
    journey = _journey(touchpoints, revenue=revenue)
    credits = _credits(model_name, journey)
    allocated = allocate_revenue(credits, amount)

    assert sum(allocated, Decimal(0)) == amount, (
        f"{model_name} leaked on {revenue} across {length} touchpoints"
    )
    assert all(part >= 0 for part in allocated)


# ---------------------------------------------------------------------------
# Time decay behaviour
# ---------------------------------------------------------------------------


def test_time_decay_favours_recent_touchpoints():
    """A touch an hour before the order must beat one 20 days before."""
    journey = _journey(
        [_touchpoint(1, days_before=20), _touchpoint(2, hours_before=1)]
    )
    old_credit, recent_credit = _vector(_credits("time_decay", journey))

    assert recent_credit > old_credit
    assert old_credit > 0, "an old touch still earns something"
    assert old_credit + recent_credit == ONE


def test_time_decay_is_monotonic_across_a_journey():
    touchpoints = [
        _touchpoint(1, days_before=21),
        _touchpoint(2, days_before=14),
        _touchpoint(3, days_before=7),
        _touchpoint(4, days_before=1),
    ]
    vector = _vector(_credits("time_decay", _journey(touchpoints)))
    assert vector == sorted(vector), f"credit should rise toward the conversion: {vector}"


def test_time_decay_half_life_halves_the_weight():
    """A touch exactly one half-life out carries half the weight of now."""
    journey = _journey(
        [_touchpoint(1, days_before=7), _touchpoint(2, hours_before=0)]
    )
    older, newest = _vector(_credits("time_decay", journey, half_life_days=7))
    # weights 0.5 and 1.0 -> credits 1/3 and 2/3
    assert newest == Decimal("0.666667")
    assert older == Decimal("0.333333")
    assert older + newest == ONE


def test_time_decay_rejects_a_non_positive_half_life():
    with pytest.raises(ValueError):
        TimeDecay(half_life_days=0)


# ---------------------------------------------------------------------------
# Empty journeys
# ---------------------------------------------------------------------------


def test_empty_journey_produces_an_empty_result_for_every_model():
    journey = _journey([])
    assert journey.is_empty
    for model_name in MODEL_NAMES:
        assert _credits(model_name, journey) == []
    assert allocate_revenue([], Decimal("50.00")) == []


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


def _seed_dataset(db) -> dict[str, int]:
    """A small dataset with a known shape.

    One journey that starts on display and ends on paid_search, so
    first_touch and last_touch are guaranteed to disagree, plus an organic
    touch (zero spend) and a touchpoint deliberately outside the lookback
    window.
    """
    display = Campaign(
        external_id="cmp_display",
        name="Display Prospecting",
        channel="display",
        platform="meta_ads",
    )
    search = Campaign(
        external_id="cmp_search",
        name="Brand Search",
        channel="paid_search",
        platform="google_ads",
    )
    organic = Campaign(
        external_id="cmp_organic",
        name="Organic / Direct",
        channel="organic",
        platform="organic",
    )
    db.add_all([display, search, organic])
    db.flush()

    # Display and search cost money; organic is free.
    db.add_all(
        [
            AdSpend(
                campaign_id=display.id,
                date=dt.date(2026, 8, 30),
                spend=Decimal("100.00"),
                impressions=10000,
                clicks=80,
            ),
            AdSpend(
                campaign_id=search.id,
                date=dt.date(2026, 8, 31),
                spend=Decimal("50.00"),
                impressions=800,
                clicks=48,
            ),
            AdSpend(
                campaign_id=organic.id,
                date=dt.date(2026, 8, 31),
                spend=Decimal("0.00"),
                impressions=0,
                clicks=0,
            ),
        ]
    )

    conversion = Conversion(
        external_id="ord_seed_1",
        user_id="usr_seed",
        occurred_at=CONVERTED_AT,
        revenue=Decimal("300.00"),
        currency="USD",
    )
    db.add(conversion)

    touchpoints = [
        Touchpoint(
            external_id="evt_outside",
            user_id="usr_seed",
            campaign_id=display.id,
            channel="display",
            occurred_at=CONVERTED_AT - dt.timedelta(days=40),
            touch_type="impression",
        ),
        Touchpoint(
            external_id="evt_first",
            user_id="usr_seed",
            campaign_id=display.id,
            channel="display",
            occurred_at=CONVERTED_AT - dt.timedelta(days=10),
            touch_type="impression",
        ),
        Touchpoint(
            external_id="evt_middle",
            user_id="usr_seed",
            campaign_id=None,
            channel="organic",
            occurred_at=CONVERTED_AT - dt.timedelta(days=4),
            touch_type="visit",
        ),
        Touchpoint(
            external_id="evt_last",
            user_id="usr_seed",
            campaign_id=search.id,
            channel="paid_search",
            occurred_at=CONVERTED_AT - dt.timedelta(hours=2),
            touch_type="click",
        ),
    ]
    db.add_all(touchpoints)

    # A second conversion with no touchpoints at all.
    db.add(
        Conversion(
            external_id="ord_seed_orphan",
            user_id="usr_no_touches",
            occurred_at=CONVERTED_AT,
            revenue=Decimal("42.00"),
            currency="USD",
        )
    )
    db.commit()

    return {"conversion_id": conversion.id}


# ---------------------------------------------------------------------------
# Journey loading
# ---------------------------------------------------------------------------


def test_touchpoint_outside_the_lookback_window_is_excluded(db):
    _seed_dataset(db)

    journeys = {
        journey.conversion.external_id: journey
        for journey in load_journeys(db, lookback_days=30)
    }
    seeded = journeys["ord_seed_1"]
    external_ids = [touchpoint.external_id for touchpoint in seeded.touchpoints]

    assert external_ids == ["evt_first", "evt_middle", "evt_last"]
    assert "evt_outside" not in external_ids

    # Widening the window pulls it back in.
    wider = {
        journey.conversion.external_id: journey
        for journey in load_journeys(db, lookback_days=60)
    }["ord_seed_1"]
    assert [t.external_id for t in wider.touchpoints] == [
        "evt_outside",
        "evt_first",
        "evt_middle",
        "evt_last",
    ]


def test_conversion_with_no_touchpoints_yields_an_empty_journey(db):
    _seed_dataset(db)

    journeys = {
        journey.conversion.external_id: journey
        for journey in load_journeys(db, lookback_days=30)
    }
    orphan = journeys["ord_seed_orphan"]

    assert orphan.touchpoints == []
    assert orphan.is_empty
    assert len(orphan) == 0

    # And the engine handles it without crashing.
    result = run_attribution(db, rebuild=True)
    for stats in result["models"].values():
        assert stats["conversions_with_empty_journey"] == 1


def test_journeys_are_ordered_and_batched(db):
    _seed_dataset(db)
    journeys = list(load_journeys(db, lookback_days=30, batch_size=1))
    assert len(journeys) == 2
    for journey in journeys:
        times = [touchpoint.occurred_at for touchpoint in journey.touchpoints]
        assert times == sorted(times)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def test_run_attribution_writes_expected_rows(db):
    _seed_dataset(db)
    result = run_attribution(db, rebuild=True)

    # 3 touchpoints in the window: single-touch models write 1 row per
    # conversion, multi-touch models write 3.
    assert result["models"]["last_touch"]["rows_written"] == 1
    assert result["models"]["first_touch"]["rows_written"] == 1
    assert result["models"]["linear"]["rows_written"] == 3
    assert result["models"]["time_decay"]["rows_written"] == 3
    assert result["models"]["position_based"]["rows_written"] == 3

    # Every model attributes the whole (non-empty-journey) revenue, exactly.
    for name, stats in result["models"].items():
        assert stats["total_attributed_revenue"] == Decimal("300.00"), name

    # Per-model revenue in the table agrees with the reported totals.
    rows = db.execute(
        select(
            AttributionResult.model_name,
            func.sum(AttributionResult.attributed_revenue),
            func.sum(AttributionResult.credit),
        ).group_by(AttributionResult.model_name)
    ).all()
    assert len(rows) == 5
    for _model_name, revenue, credit in rows:
        assert revenue == Decimal("300.00")
        assert credit == ONE


def test_run_attribution_is_idempotent(db):
    _seed_dataset(db)

    first = run_attribution(db, rebuild=True)
    count_after_first = db.scalar(select(func.count()).select_from(AttributionResult))
    revenue_after_first = db.scalar(
        select(func.sum(AttributionResult.attributed_revenue))
    )

    second = run_attribution(db)
    count_after_second = db.scalar(select(func.count()).select_from(AttributionResult))
    revenue_after_second = db.scalar(
        select(func.sum(AttributionResult.attributed_revenue))
    )

    assert count_after_second == count_after_first
    assert revenue_after_second == revenue_after_first

    # The second pass updated rather than inserted.
    assert first["totals"]["rows_inserted"] > 0
    assert second["totals"]["rows_inserted"] == 0
    assert second["totals"]["rows_updated"] == first["totals"]["rows_inserted"]

    # No duplicates on the natural key.
    duplicates = db.execute(
        select(
            AttributionResult.conversion_id,
            AttributionResult.touchpoint_id,
            AttributionResult.model_name,
            func.count(),
        )
        .group_by(
            AttributionResult.conversion_id,
            AttributionResult.touchpoint_id,
            AttributionResult.model_name,
        )
        .having(func.count() > 1)
    ).all()
    assert not duplicates


def test_rebuild_removes_rows_for_the_selected_models_only(db):
    _seed_dataset(db)
    run_attribution(db, rebuild=True)

    before = db.scalar(
        select(func.count())
        .select_from(AttributionResult)
        .where(AttributionResult.model_name == "first_touch")
    )
    assert before > 0

    run_attribution(db, models=["last_touch"], rebuild=True)

    # first_touch rows survived a last_touch-only rebuild.
    assert (
        db.scalar(
            select(func.count())
            .select_from(AttributionResult)
            .where(AttributionResult.model_name == "first_touch")
        )
        == before
    )


def test_unknown_model_is_rejected(db):
    with pytest.raises(ValueError, match="unknown models"):
        run_attribution(db, models=["not_a_model"])


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def test_channel_performance_returns_none_roas_for_organic(db):
    _seed_dataset(db)
    run_attribution(db, rebuild=True)

    rows = {row["channel"]: row for row in channel_performance(db, model_name="linear")}

    organic = rows["organic"]
    assert organic["spend"] == Decimal("0.00")
    assert organic["roas"] is None, "zero spend must give None, not a divide error"
    assert organic["attributed_revenue"] > 0

    # A paid channel does get a ROAS.
    display = rows["display"]
    assert display["spend"] == Decimal("100.00")
    assert display["roas"] is not None
    assert display["roas"] == (
        display["attributed_revenue"] / display["spend"]
    ).quantize(Decimal("0.01"))

    # Sorted by attributed revenue, descending.
    revenues = [
        row["attributed_revenue"] for row in channel_performance(db, model_name="linear")
    ]
    assert revenues == sorted(revenues, reverse=True)


def test_channel_performance_attributed_conversions_are_fractional(db):
    _seed_dataset(db)
    run_attribution(db, rebuild=True)

    rows = channel_performance(db, model_name="linear")
    total_conversions = sum(row["attributed_conversions"] for row in rows)
    # One conversion, split three ways: the parts sum back to exactly 1.
    assert total_conversions == ONE
    assert any(row["attributed_conversions"] < 1 for row in rows)


def test_model_comparison_shows_a_nonzero_swing(db):
    """The models must genuinely disagree, not just exist."""
    _seed_dataset(db)
    run_attribution(db, rebuild=True)

    comparison = model_comparison(db)
    assert comparison["channels"]
    assert any(row["swing"] > 0 for row in comparison["channels"])
    assert comparison["total_swing"] > 0

    by_channel = {row["channel"]: row for row in comparison["channels"]}

    # display is the first touch and paid_search the last, so the two
    # single-touch models must sit at opposite extremes.
    assert by_channel["display"]["by_model"]["first_touch"] == Decimal("300.00")
    assert by_channel["display"]["by_model"]["last_touch"] == Decimal("0.00")
    assert by_channel["paid_search"]["by_model"]["last_touch"] == Decimal("300.00")
    assert by_channel["paid_search"]["by_model"]["first_touch"] == Decimal("0.00")
    assert by_channel["display"]["swing"] == Decimal("300.00")
    assert by_channel["display"]["most_generous_model"] == "first_touch"
    assert by_channel["paid_search"]["most_generous_model"] == "last_touch"

    # Every model still totals the same revenue.
    totals = set(comparison["totals_by_model"].values())
    assert totals == {Decimal("300.00")}

    # Sorted by swing, descending.
    swings = [row["swing"] for row in comparison["channels"]]
    assert swings == sorted(swings, reverse=True)


def test_timeseries_buckets_revenue_by_period_and_channel(db):
    _seed_dataset(db)
    run_attribution(db, rebuild=True)

    rows = timeseries(db, model_name="linear", granularity="day")
    assert rows
    assert {row["channel"] for row in rows} == {"display", "organic", "paid_search"}
    assert all(row["period"] == CONVERTED_AT.date() for row in rows)
    assert sum(row["attributed_revenue"] for row in rows) == Decimal("300.00")

    with pytest.raises(ValueError, match="unknown granularity"):
        timeseries(db, model_name="linear", granularity="fortnight")


def test_top_journeys_returns_the_full_path_with_credits(db):
    _seed_dataset(db)
    run_attribution(db, rebuild=True)

    journeys = top_journeys(db, model_name="linear", limit=5)
    assert journeys
    journey = journeys[0]

    assert journey["touchpoint_count"] == 3
    assert journey["path"] == "display -> organic -> paid_search"
    assert journey["revenue"] == Decimal("300.00")
    assert sum(touch["credit"] for touch in journey["touchpoints"]) == ONE
    assert sum(
        touch["attributed_revenue"] for touch in journey["touchpoints"]
    ) == Decimal("300.00")
    # Ordered chronologically.
    times = [touch["occurred_at"] for touch in journey["touchpoints"]]
    assert times == sorted(times)


def test_pipeline_health_reports_counts_and_runs(db):
    _seed_dataset(db)
    run_attribution(db, rebuild=True)

    health = pipeline_health(db)
    counts = health["row_counts"]

    assert counts["campaigns"] == 3
    assert counts["conversions"] == 2
    assert counts["touchpoints"] == 4
    assert counts["attribution_results"] > 0
    assert health["quarantine"]["total"] == 0
    assert health["quarantine"]["by_source"] == {}
    assert isinstance(health["recent_runs"], list)
