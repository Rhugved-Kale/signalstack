"""Tests for the synthetic data generator.

No database and no network: the generator is pure computation over plain dicts,
so these tests only import from `app.generator`.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter

import pytest

from app.generator.fake_apis import (
    FailureConfig,
    FakeAdPlatformAPI,
    FakeAPIError,
    FakeEventStreamAPI,
    FakePaymentAPI,
    FakeTimeoutError,
    RateLimitError,
    RecordShape,
    ServerError,
)
from app.generator.world import (
    CHANNELS,
    EARLY_FUNNEL_CHANNELS,
    LATE_FUNNEL_CHANNELS,
    World,
    WorldConfig,
    build_world,
)

# A smaller world keeps the suite fast; the invariants are size-independent.
TEST_CONFIG = WorldConfig(seed=42, n_campaigns=12, n_users=400)


@pytest.fixture(scope="module")
def world() -> World:
    return build_world(TEST_CONFIG)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def _world_payload(w: World) -> tuple:
    return (w.campaigns, w.users, w.touchpoints, w.conversions, w.daily_spend)


def test_same_seed_produces_identical_world():
    first = build_world(WorldConfig(seed=7, n_campaigns=8, n_users=120))
    second = build_world(WorldConfig(seed=7, n_campaigns=8, n_users=120))

    assert _world_payload(first) == _world_payload(second)
    assert first.summary() == second.summary()


def test_different_seeds_produce_different_worlds():
    first = build_world(WorldConfig(seed=7, n_campaigns=8, n_users=120))
    other = build_world(WorldConfig(seed=8, n_campaigns=8, n_users=120))

    assert _world_payload(first) != _world_payload(other)
    assert first.touchpoints != other.touchpoints


# ---------------------------------------------------------------------------
# World invariants
# ---------------------------------------------------------------------------


def test_every_conversion_has_a_prior_touchpoint(world: World):
    touches_by_user: dict[str, list[dt.datetime]] = {}
    for touch in world.touchpoints:
        touches_by_user.setdefault(touch["user_id"], []).append(touch["occurred_at"])

    assert world.conversions, "expected at least one conversion"
    for conversion in world.conversions:
        prior = [
            occurred_at
            for occurred_at in touches_by_user.get(conversion["user_id"], [])
            if occurred_at < conversion["occurred_at"]
        ]
        assert prior, (
            f"conversion {conversion['external_id']} for user "
            f"{conversion['user_id']} has no touchpoint before it"
        )


def test_external_ids_are_unique_within_a_world(world: World):
    for label, records in (
        ("campaigns", world.campaigns),
        ("touchpoints", world.touchpoints),
        ("conversions", world.conversions),
    ):
        ids = [record["external_id"] for record in records]
        duplicates = [item for item, count in Counter(ids).items() if count > 1]
        assert not duplicates, f"duplicate {label} external_ids: {duplicates[:5]}"
        assert all(ids), f"empty external_id present in {label}"


def test_organic_touchpoints_have_no_campaign_and_organic_spend_is_zero(world: World):
    organic_touches = [t for t in world.touchpoints if t["channel"] == "organic"]
    assert organic_touches, "expected organic touchpoints in the world"
    for touch in organic_touches:
        assert touch["campaign_external_id"] is None

    # Non-organic touchpoints must always carry a campaign.
    for touch in world.touchpoints:
        if touch["channel"] != "organic":
            assert touch["campaign_external_id"] is not None

    organic_campaign_ids = {
        c["external_id"] for c in world.campaigns if c["channel"] == "organic"
    }
    organic_rows = [
        row
        for row in world.daily_spend
        if row["campaign_external_id"] in organic_campaign_ids
    ]
    assert organic_rows, "expected daily_spend rows for organic campaigns"
    for row in organic_rows:
        assert row["spend"] == 0.0
        assert row["impressions"] == 0
        assert row["clicks"] == 0


def test_spend_never_reports_fewer_events_than_the_journeys_contain(world: World):
    """Spend must not contradict the journey data it is supposed to explain."""
    metric_of = {"impression": "impressions", "email_open": "impressions",
                 "click": "clicks", "visit": "clicks"}
    observed: dict[tuple[str, dt.date], Counter] = {}
    for touch in world.touchpoints:
        if touch["campaign_external_id"] is None:
            continue
        key = (touch["campaign_external_id"], touch["occurred_at"].date())
        observed.setdefault(key, Counter())[metric_of[touch["touch_type"]]] += 1

    reported = {
        (row["campaign_external_id"], row["date"]): row for row in world.daily_spend
    }
    for key, counts in observed.items():
        row = reported.get(key)
        assert row is not None, f"no spend row for {key}"
        assert row["impressions"] >= counts["impressions"]
        assert row["clicks"] >= counts["clicks"]
        # A click always implies an impression.
        assert row["impressions"] >= row["clicks"]


def test_late_funnel_channels_end_journeys_more_often_than_early_funnel(world: World):
    """Proves the funnel bias is real and not decorative."""
    journeys: dict[str, list[dict]] = {}
    for touch in world.touchpoints:
        journeys.setdefault(touch["user_id"], []).append(touch)

    first_counts: Counter = Counter()
    last_counts: Counter = Counter()
    multi_touch = 0
    for touches in journeys.values():
        if len(touches) < 2:
            continue
        multi_touch += 1
        ordered = sorted(touches, key=lambda t: t["occurred_at"])
        first_counts[ordered[0]["channel"]] += 1
        last_counts[ordered[-1]["channel"]] += 1

    assert multi_touch > 50, "need multi-touch journeys to judge ordering"

    late_as_last = sum(last_counts[c] for c in LATE_FUNNEL_CHANNELS)
    early_as_last = sum(last_counts[c] for c in EARLY_FUNNEL_CHANNELS)
    assert late_as_last > early_as_last, (
        f"late-funnel channels should close journeys more often: "
        f"late={late_as_last} early={early_as_last}"
    )

    # And the mirror image: early-funnel channels should open them.
    early_as_first = sum(first_counts[c] for c in EARLY_FUNNEL_CHANNELS)
    late_as_first = sum(first_counts[c] for c in LATE_FUNNEL_CHANNELS)
    assert early_as_first > late_as_first, (
        f"early-funnel channels should open journeys more often: "
        f"early={early_as_first} late={late_as_first}"
    )


def test_touchpoints_within_a_journey_are_strictly_ordered(world: World):
    journeys: dict[str, list[dict]] = {}
    for touch in world.touchpoints:
        journeys.setdefault(touch["user_id"], []).append(touch)

    for user_id, touches in journeys.items():
        ordered = sorted(touches, key=lambda t: t["position"])
        times = [t["occurred_at"] for t in ordered]
        assert times == sorted(times), f"journey for {user_id} is out of order"
        assert len(set(times)) == len(times), f"duplicate timestamps for {user_id}"
        assert 1 <= len(touches) <= 6
        for touch in touches:
            assert touch["occurred_at"].tzinfo is not None
            valid_types = {t for t, _ in CHANNELS[touch["channel"]].touch_types}
            assert touch["touch_type"] in valid_types


def test_conversion_revenue_is_in_range_and_two_decimal_places(world: World):
    for conversion in world.conversions:
        revenue = conversion["revenue"]
        assert 25.0 <= revenue <= 900.0
        assert round(revenue, 2) == revenue
        assert conversion["currency"] == "USD"


# ---------------------------------------------------------------------------
# Strict payload validation
# ---------------------------------------------------------------------------


def _parse_timestamp(value: str) -> dt.datetime:
    """Accept either an ISO-Z timestamp or a plain YYYY-MM-DD date.

    Any unparseable value is reported as an AssertionError so that callers can
    treat "invalid record" uniformly. (Phase 4's real parser will need to catch
    ValueError from fromisoformat for exactly these inputs.)
    """
    assert isinstance(value, str), f"timestamp must be a string, got {type(value)}"
    try:
        if value.endswith("Z"):
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise AssertionError(f"unparseable timestamp {value!r}: {exc}") from exc


def assert_strict_record(record: dict, shape: RecordShape, nullable_ids=("campaign_id",)) -> None:
    """A record that a clean API must produce. Any deviation is a failure."""
    assert set(record) == set(shape.required), (
        f"unexpected key set: {sorted(record)} != {sorted(shape.required)}"
    )
    for key in shape.numeric:
        value = record[key]
        assert isinstance(value, (int, float)) and not isinstance(value, bool), (
            f"{key} must be numeric, got {value!r}"
        )
    for key in shape.money:
        assert record[key] >= 0, f"{key} must not be negative, got {record[key]!r}"
    for key in shape.timestamps:
        _parse_timestamp(record[key])
    for key in shape.ids:
        value = record[key]
        if value is None:
            assert key in nullable_ids, f"{key} must not be null"
        else:
            assert isinstance(value, str) and value, f"{key} must be a non-empty string"
    for key in shape.currency:
        assert record[key] == "USD"


def test_clean_config_never_fails_and_always_returns_valid_records(world: World):
    clean = FailureConfig.none()
    ad = FakeAdPlatformAPI(world, clean, seed=1)
    events = FakeEventStreamAPI(world, clean, seed=2)
    payments = FakePaymentAPI(world, clean, seed=3)
    start, end = world.config.start_date, world.config.end_date

    calls = [
        (lambda page: ad.list_campaigns(page=page, per_page=5), ad.CAMPAIGN_SHAPE),
        (lambda page: ad.list_ad_spend(start, end, page=page, per_page=25), ad.SPEND_SHAPE),
        (
            lambda page: events.list_touchpoints(start, end, page=page, per_page=25),
            events.TOUCHPOINT_SHAPE,
        ),
        (
            lambda page: payments.list_conversions(start, end, page=page, per_page=25),
            payments.CONVERSION_SHAPE,
        ),
    ]

    checked = 0
    for index in range(200):
        call, shape = calls[index % len(calls)]
        page = (index // len(calls)) % 3 + 1
        # Any raised exception fails the test — that is the assertion.
        response = call(page)
        assert set(response) == {
            "data",
            "page",
            "per_page",
            "total",
            "total_pages",
            "has_more",
        }
        for record in response["data"]:
            assert_strict_record(record, shape)
            checked += 1

    assert checked > 0, "expected to validate at least some records"


def test_clean_config_emits_no_duplicates(world: World):
    api = FakeEventStreamAPI(world, FailureConfig.none(), seed=5)
    response = api.list_touchpoints(per_page=100, page=1)
    ids = [record["event_id"] for record in response["data"]]
    assert len(ids) == len(set(ids))
    assert len(ids) == 100


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_high_failure_rates_produce_all_three_exception_types(world: World):
    noisy = FailureConfig(
        server_error_rate=0.25,
        rate_limit_rate=0.25,
        timeout_rate=0.25,
        malformed_record_rate=0.20,
        duplicate_record_rate=0.10,
    )
    api = FakeEventStreamAPI(world, noisy, seed=11)

    seen: Counter = Counter()
    for index in range(300):
        try:
            api.list_touchpoints(page=(index % 4) + 1, per_page=10)
            seen["ok"] += 1
        except ServerError as exc:
            assert exc.status_code in (500, 503)
            seen["server"] += 1
        except RateLimitError as exc:
            assert exc.status_code == 429
            assert exc.retry_after > 0
            seen["rate_limit"] += 1
        except FakeTimeoutError as exc:
            # A timeout produced no response, so there is no status code.
            assert exc.status_code is None
            seen["timeout"] += 1

    assert seen["server"] > 0
    assert seen["rate_limit"] > 0
    assert seen["timeout"] > 0
    assert seen["ok"] > 0, "not every call should fail"

    # All of them are catchable as the common base class.
    for exc_type in (ServerError, RateLimitError, FakeTimeoutError):
        assert issubclass(exc_type, FakeAPIError)


def test_failure_sequence_is_reproducible_for_a_given_seed(world: World):
    noisy = FailureConfig(0.2, 0.2, 0.2, 0.2, 0.1)

    def run(seed: int) -> list[str]:
        api = FakePaymentAPI(world, noisy, seed=seed)
        outcomes: list[str] = []
        for index in range(60):
            try:
                api.list_conversions(page=(index % 3) + 1, per_page=10)
                outcomes.append("ok")
            except FakeAPIError as exc:
                outcomes.append(type(exc).__name__)
        return outcomes

    assert run(99) == run(99)
    assert run(99) != run(100)


def test_malformed_records_actually_deviate_from_the_strict_shape(world: World):
    api = FakePaymentAPI(
        world,
        FailureConfig(0.0, 0.0, 0.0, 1.0, 0.0),
        seed=13,
    )
    response = api.list_conversions(per_page=40)
    bad = 0
    for record in response["data"]:
        try:
            assert_strict_record(record, api.CONVERSION_SHAPE)
        except AssertionError:
            bad += 1
    assert bad >= 30, f"expected nearly every record corrupted, got {bad}/40"


def test_duplicates_appear_when_the_duplicate_rate_is_high(world: World):
    api = FakeEventStreamAPI(
        world,
        FailureConfig(0.0, 0.0, 0.0, 0.0, 1.0),
        seed=17,
    )
    response = api.list_touchpoints(per_page=30)
    ids = [record["event_id"] for record in response["data"]]
    assert len(ids) > len(set(ids)), "expected repeated records"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("per_page", [7, 25, 100])
def test_walking_all_pages_yields_exactly_total_records(world: World, per_page: int):
    api = FakeEventStreamAPI(world, FailureConfig.none(), seed=23)

    collected: list[str] = []
    page = 1
    reported_total = None
    while True:
        response = api.list_touchpoints(page=page, per_page=per_page)
        if reported_total is None:
            reported_total = response["total"]
            expected_pages = response["total_pages"]
        assert response["total"] == reported_total
        assert response["page"] == page
        assert response["per_page"] == per_page
        assert len(response["data"]) <= per_page
        collected.extend(record["event_id"] for record in response["data"])
        if not response["has_more"]:
            break
        page += 1

    assert page == expected_pages
    assert len(collected) == reported_total
    assert len(set(collected)) == reported_total, "pages overlapped"
    assert reported_total == len(world.touchpoints)


def test_page_beyond_the_end_is_empty(world: World):
    api = FakeAdPlatformAPI(world, FailureConfig.none(), seed=29)
    response = api.list_campaigns(page=99, per_page=10)
    assert response["data"] == []
    assert response["has_more"] is False


def test_invalid_pagination_arguments_are_rejected(world: World):
    api = FakeAdPlatformAPI(world, FailureConfig.none(), seed=31)
    with pytest.raises(ValueError):
        api.list_campaigns(page=0)
    with pytest.raises(ValueError):
        api.list_campaigns(per_page=0)


def test_date_filters_narrow_the_result_set(world: World):
    api = FakePaymentAPI(world, FailureConfig.none(), seed=37)
    full = api.list_conversions(per_page=1)["total"]

    midpoint = world.config.start_date + dt.timedelta(
        days=(world.config.end_date - world.config.start_date).days // 2
    )
    later_half = api.list_conversions(start_date=midpoint, per_page=1)["total"]

    assert 0 < later_half < full
