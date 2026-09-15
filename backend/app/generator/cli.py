"""Inspection CLI for the synthetic data generator.

    python -m app.generator.cli --seed 42 --out samples/

Builds a world, prints what it contains, and writes one sample JSON page per
API method so the payload shapes can be eyeballed before ingestion is written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from app.generator.fake_apis import (
    FailureConfig,
    FakeAdPlatformAPI,
    FakeEventStreamAPI,
    FakePaymentAPI,
)
from app.generator.world import World, WorldConfig, build_world


def _print_header(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


def _print_summary(world: World) -> None:
    _print_header("World summary")
    summary = world.summary()
    width = max(len(k) for k in summary)
    for key, value in summary.items():
        print(f"  {key.replace('_', ' '):<{width}}  {value}")


def _print_channel_table(world: World) -> None:
    _print_header("Channels")
    rows = world.channel_breakdown()
    print(
        f"  {'channel':<12} {'funnel':<10} {'campaigns':>9} "
        f"{'touchpoints':>11} {'last touch':>10} {'spend':>10}"
    )
    print(f"  {'-' * 12} {'-' * 10} {'-' * 9} {'-' * 11} {'-' * 10} {'-' * 10}")
    for row in rows:
        print(
            f"  {row['channel']:<12} {row['funnel_position']:<10} "
            f"{row['campaigns']:>9} {row['touchpoints']:>11} "
            f"{row['last_touches']:>10} {row['spend']:>10,.2f}"
        )
    totals_spend = sum(r["spend"] for r in rows)
    totals_touch = sum(r["touchpoints"] for r in rows)
    print(f"  {'-' * 12} {'-' * 10} {'-' * 9} {'-' * 11} {'-' * 10} {'-' * 10}")
    print(
        f"  {'TOTAL':<12} {'':<10} {len(world.campaigns):>9} "
        f"{totals_touch:>11} {'':>10} {totals_spend:>10,.2f}"
    )


def _print_touchpoint_distribution(world: World) -> None:
    _print_header("Touchpoints before each conversion")
    buckets = world.touchpoints_before_conversion()
    total = sum(buckets.values()) or 1
    for label in ("1", "2", "3+"):
        count = buckets[label]
        bar = "#" * round(40 * count / total)
        print(f"  {label:>2} touchpoint(s)  {count:>5}  ({count / total:6.1%})  {bar}")
    print(f"  {'total':>14}  {sum(buckets.values()):>5}")


def _write_samples(world: World, out_dir: Path, seed: int) -> list[Path]:
    """Write one clean sample page per API method, plus a corrupted sample."""
    out_dir.mkdir(parents=True, exist_ok=True)

    clean = FailureConfig.none()
    ad_api = FakeAdPlatformAPI(world, clean, seed=seed)
    event_api = FakeEventStreamAPI(world, clean, seed=seed)
    payment_api = FakePaymentAPI(world, clean, seed=seed)

    start = world.config.start_date
    end = world.config.end_date

    samples: dict[str, dict[str, Any]] = {
        "ad_platform_list_campaigns": ad_api.list_campaigns(page=1, per_page=5),
        "ad_platform_list_ad_spend": ad_api.list_ad_spend(start, end, page=1, per_page=5),
        "event_stream_list_touchpoints": event_api.list_touchpoints(
            start, end, page=1, per_page=5
        ),
        "payment_list_conversions": payment_api.list_conversions(
            start, end, page=1, per_page=5
        ),
    }

    # A fifth sample showing what corruption looks like on the wire: every
    # record malformed, but no transport failures so the call always returns.
    messy_api = FakePaymentAPI(
        world,
        FailureConfig(
            server_error_rate=0.0,
            rate_limit_rate=0.0,
            timeout_rate=0.0,
            malformed_record_rate=1.0,
            duplicate_record_rate=0.5,
        ),
        seed=seed,
    )
    samples["payment_list_conversions_malformed"] = messy_api.list_conversions(
        start, end, page=1, per_page=8
    )

    written: list[Path] = []
    for name, payload in samples.items():
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n")
        written.append(path)
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.generator.cli",
        description="Build and inspect a synthetic SignalStack world.",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default: 42)")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Directory to write sample JSON pages into (default: none)",
    )
    parser.add_argument("--campaigns", type=int, default=None, help="Override n_campaigns")
    parser.add_argument("--users", type=int, default=None, help="Override n_users")
    parser.add_argument("--days", type=int, default=None, help="Window length in days")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    kwargs: dict[str, Any] = {"seed": args.seed}
    if args.campaigns is not None:
        kwargs["n_campaigns"] = args.campaigns
    if args.users is not None:
        kwargs["n_users"] = args.users
    if args.days is not None:
        end = dt.datetime.now(dt.timezone.utc).date()
        kwargs["end_date"] = end
        kwargs["start_date"] = end - dt.timedelta(days=args.days - 1)

    config = WorldConfig(**kwargs)
    world = build_world(config)

    _print_summary(world)
    _print_channel_table(world)
    _print_touchpoint_distribution(world)

    if args.out is not None:
        written = _write_samples(world, args.out, args.seed)
        _print_header(f"Sample payloads -> {args.out}")
        for path in written:
            print(f"  {path}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
