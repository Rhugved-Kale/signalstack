"""Ingestion CLI.

    python -m app.pipeline.cli --reset --users 3000

Runs the pipeline against a generated world and prints a per-source table plus
the most common quarantine reasons.
"""

from __future__ import annotations

import argparse
import logging
import re
from typing import Any, Iterable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import QuarantinedRecord
from app.db.session import SessionLocal
from app.generator.fake_apis import FailureConfig
from app.generator.world import WorldConfig
from app.pipeline.runner import run_ingestion

logger = logging.getLogger(__name__)

# Tables the pipeline owns. attribution_results is included so --reset leaves a
# genuinely clean slate for later phases.
DATA_TABLES = (
    "attribution_results",
    "quarantined_records",
    "ingestion_runs",
    "ad_spend",
    "touchpoints",
    "conversions",
    "campaigns",
)

FAILURE_PROFILES: dict[str, FailureConfig] = {
    "none": FailureConfig.none(),
    "normal": FailureConfig(),
    # Roughly triple the defaults.
    "chaos": FailureConfig(
        server_error_rate=0.24,
        rate_limit_rate=0.15,
        timeout_rate=0.09,
        malformed_record_rate=0.21,
        duplicate_record_rate=0.06,
    ),
}


def reset_data(db: Session) -> None:
    """TRUNCATE every data table, resetting identities."""
    db.execute(
        text(
            f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"
        )
    )
    db.commit()


def _normalise_reason(reason: str) -> str:
    """Collapse quoted literals so reasons group into useful buckets.

    Without this, every "unknown campaign_id 'cmp_abc'" is its own bucket and
    the top-5 list is useless.
    """
    return re.sub(r"'[^']*'", "'?'", reason)


def print_run_table(result: dict[str, Any]) -> None:
    header = (
        f"{'source':<14}{'received':>10}{'ingested':>10}{'quarantined':>13}"
        f"{'retries':>9}{'status':>10}{'duration':>10}"
    )
    print()
    print(header)
    print("-" * len(header))
    for row in result["sources"]:
        print(
            f"{row['source']:<14}{row['received']:>10,}{row['ingested']:>10,}"
            f"{row['quarantined']:>13,}{row['retries']:>9,}{row['status']:>10}"
            f"{row['duration_seconds']:>9.2f}s"
        )
    totals = result["totals"]
    print("-" * len(header))
    print(
        f"{'TOTAL':<14}{totals['received']:>10,}{totals['ingested']:>10,}"
        f"{totals['quarantined']:>13,}{totals['retries']:>9,}{'':>10}"
        f"{totals['duration_seconds']:>9.2f}s"
    )
    print(
        f"\n  rows inserted={totals['rows_inserted']:,}  "
        f"rows updated={totals['rows_updated']:,}  "
        f"in-batch duplicates dropped={totals['duplicates_dropped']:,}"
    )
    print(
        "  identity: received == ingested + quarantined -> "
        f"{totals['received']:,} == {totals['ingested']:,} + {totals['quarantined']:,} "
        f"({totals['received'] == totals['ingested'] + totals['quarantined']})"
    )

    failures = [row for row in result["sources"] if row["error_message"]]
    if failures:
        print("\n  errors:")
        for row in failures:
            print(f"    {row['source']}: {row['error_message']}")


def print_top_quarantine_reasons(
    db: Session, run_ids: Iterable[int], limit: int = 5
) -> None:
    run_ids = list(run_ids)
    print()
    if not run_ids:
        print("No quarantine reasons (no runs).")
        return

    rows = db.execute(
        select(QuarantinedRecord.source, QuarantinedRecord.error_reason).where(
            QuarantinedRecord.ingestion_run_id.in_(run_ids)
        )
    ).all()

    if not rows:
        print("Quarantine: nothing quarantined in this run.")
        return

    counts: dict[tuple[str, str], int] = {}
    for source, reason in rows:
        key = (source, _normalise_reason(reason))
        counts[key] = counts.get(key, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
    print(f"Top {min(limit, len(ranked))} quarantine reasons ({len(rows):,} total)")
    print("-" * 78)
    for (source, reason) in [item[0] for item in ranked]:
        count = counts[(source, reason)]
        display = reason if len(reason) <= 58 else reason[:55] + "..."
        print(f"  {count:>6,}  {source:<12} {display}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline.cli",
        description="Ingest a generated world into Postgres.",
    )
    parser.add_argument("--seed", type=int, default=42, help="World seed (default: 42)")
    parser.add_argument(
        "--users", type=int, default=3000, help="Number of users to generate (default: 3000)"
    )
    parser.add_argument(
        "--failure-profile",
        choices=sorted(FAILURE_PROFILES),
        default="normal",
        help="Upstream failure rates (default: normal)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="TRUNCATE all data tables before ingesting",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress per-source INFO logging"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    failure_config = FAILURE_PROFILES[args.failure_profile]
    world_config = WorldConfig(seed=args.seed, n_users=args.users)

    db = SessionLocal()
    try:
        if args.reset:
            print(f"Resetting {len(DATA_TABLES)} data tables...")
            reset_data(db)

        print(
            f"Ingesting seed={args.seed} users={args.users:,} "
            f"failure-profile={args.failure_profile}"
        )
        result = run_ingestion(
            db, world_config=world_config, failure_config=failure_config
        )
        print_run_table(result)
        print_top_quarantine_reasons(db, result["ingestion_run_ids"])
        print()

        any_failed = any(row["status"] == "failed" for row in result["sources"])
        return 1 if any_failed else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
