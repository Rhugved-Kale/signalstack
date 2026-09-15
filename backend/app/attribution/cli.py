"""Attribution CLI.

    python -m app.attribution.cli --rebuild

Scores every journey, then prints the per-model summary, a side-by-side
channel comparison for `last_touch` vs `linear`, and the full model-comparison
matrix ordered by how much the models disagree.
"""

from __future__ import annotations

import argparse
import logging
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.attribution.analytics import channel_performance, model_comparison
from app.attribution.engine import run_attribution
from app.attribution.journeys import DEFAULT_LOOKBACK_DAYS
from app.attribution.models import DEFAULT_HALF_LIFE_DAYS, MODEL_NAMES

logger = logging.getLogger(__name__)

BAR_WIDTH = 24


def _money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def _roas(value: Decimal | None) -> str:
    # Organic has no spend, so it has no ROAS. Say so rather than printing 0.
    return "n/a" if value is None else f"{value:,.2f}x"


def _bar(value: Decimal, largest: Decimal, width: int = BAR_WIDTH) -> str:
    if largest <= 0:
        return ""
    filled = int((value / largest) * width)
    return "█" * filled


def print_model_summary(result: dict[str, Any]) -> None:
    header = (
        f"{'model':<16}{'conversions':>12}{'touchpoints':>13}{'rows':>8}"
        f"{'inserted':>10}{'updated':>9}{'attributed revenue':>20}"
    )
    print()
    print("Per-model summary")
    print(header)
    print("-" * len(header))
    for name, stats in result["models"].items():
        print(
            f"{name:<16}{stats['conversions_scored']:>12,}"
            f"{stats['touchpoints_credited']:>13,}{stats['rows_written']:>8,}"
            f"{stats['rows_inserted']:>10,}{stats['rows_updated']:>9,}"
            f"{_money(stats['total_attributed_revenue']):>20}"
        )
    print("-" * len(header))
    print(
        f"  journeys={result['journeys']:,}  "
        f"empty journeys={result['empty_journeys']:,}  "
        f"lookback={result['lookback_days']}d  "
        f"half-life={result['half_life_days']}d  "
        f"duration={result['duration_seconds']:.2f}s"
    )
    print(
        "  every model attributes the same total revenue — only the "
        "distribution differs."
    )


def print_side_by_side(db: Session, left: str = "last_touch", right: str = "linear") -> None:
    left_rows = {row["channel"]: row for row in channel_performance(db, model_name=left)}
    right_rows = {row["channel"]: row for row in channel_performance(db, model_name=right)}
    channels = sorted(
        set(left_rows) | set(right_rows),
        key=lambda channel: left_rows.get(channel, {}).get("attributed_revenue", Decimal(0)),
        reverse=True,
    )

    print()
    print(f"Channel performance: {left} vs {right}")
    header = (
        f"{'channel':<13}{'spend':>10} | {left + ' rev':>16}{'roas':>9} | "
        f"{right + ' rev':>16}{'roas':>9} | {'delta':>13}"
    )
    print(header)
    print("-" * len(header))
    for channel in channels:
        left_row = left_rows.get(channel)
        right_row = right_rows.get(channel)
        left_revenue = left_row["attributed_revenue"] if left_row else Decimal(0)
        right_revenue = right_row["attributed_revenue"] if right_row else Decimal(0)
        spend = (left_row or right_row)["spend"]
        delta = right_revenue - left_revenue
        sign = "+" if delta > 0 else ""
        print(
            f"{channel:<13}{_money(spend):>10} | {_money(left_revenue):>16}"
            f"{_roas(left_row['roas'] if left_row else None):>9} | "
            f"{_money(right_revenue):>16}"
            f"{_roas(right_row['roas'] if right_row else None):>9} | "
            f"{sign + _money(delta):>13}"
        )
    print("-" * len(header))
    print(
        f"  delta = {right} minus {left}. Positive means {right} credits the "
        f"channel more."
    )


def print_model_matrix(db: Session, models: Sequence[str]) -> None:
    comparison = model_comparison(db, models=models)
    if not comparison["channels"]:
        print("\nNo attribution results to compare.")
        return

    print()
    print("Model comparison — attributed revenue by channel (sorted by swing)")
    header = f"{'channel':<13}" + "".join(f"{name:>15}" for name in comparison["models"])
    header += f"{'swing':>14}{'':>3}{'disagreement':<{BAR_WIDTH}}"
    print(header)
    print("-" * len(header))

    largest_swing = max(row["swing"] for row in comparison["channels"])
    for row in comparison["channels"]:
        line = f"{row['channel']:<13}"
        for name in comparison["models"]:
            value = row["by_model"][name]
            marker = ""
            if value == row["max_revenue"]:
                marker = "*"
            elif value == row["min_revenue"]:
                marker = "."
            line += f"{_money(value) + marker:>15}"
        line += f"{_money(row['swing']):>14}   "
        line += _bar(row["swing"], largest_swing)
        print(line)

    print("-" * len(header))
    totals_line = f"{'TOTAL':<13}"
    for name in comparison["models"]:
        totals_line += f"{_money(comparison['totals_by_model'][name]):>15}"
    totals_line += f"{_money(comparison['total_swing']):>14}"
    print(totals_line)
    print(
        "\n  * = model crediting this channel most,  . = least.\n"
        "  swing = max minus min: the revenue whose owner the models dispute."
    )
    for row in comparison["channels"][:3]:
        print(
            f"  {row['channel']}: {row['most_generous_model']} credits it "
            f"{_money(row['swing'])} more than {row['least_generous_model']} "
            f"({row['swing_pct']}% of its peak)."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.attribution.cli",
        description="Score conversions under every attribution model.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete existing rows for the selected models before scoring",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Attribution window in days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--half-life-days",
        type=float,
        default=DEFAULT_HALF_LIFE_DAYS,
        help=f"time_decay half-life in days (default: {DEFAULT_HALF_LIFE_DAYS})",
    )
    parser.add_argument(
        "--model",
        action="append",
        choices=list(MODEL_NAMES),
        dest="models",
        help="Model to run; repeatable. Defaults to all five.",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress INFO logging"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    selected = tuple(args.models) if args.models else MODEL_NAMES

    db = SessionLocal()
    try:
        print(
            f"Scoring attribution: models={','.join(selected)} "
            f"lookback={args.lookback_days}d half-life={args.half_life_days}d "
            f"rebuild={args.rebuild}"
        )
        result = run_attribution(
            db,
            models=selected,
            lookback_days=args.lookback_days,
            half_life_days=args.half_life_days,
            rebuild=args.rebuild,
        )
        print_model_summary(result)

        if "last_touch" in selected and "linear" in selected:
            print_side_by_side(db, "last_touch", "linear")
        print_model_matrix(db, selected)
        print()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
