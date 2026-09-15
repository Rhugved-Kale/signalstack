"""HTTP endpoints, mounted under /api.

Deliberately thin: validate the query string, call an existing analytics or
runner function, serialise the result. There is no attribution or pipeline
logic in this module — if something here looks like a calculation, it belongs
in `app.attribution.analytics` instead.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.attribution.analytics import (
    channel_performance,
    model_comparison,
    pipeline_health,
    timeseries,
    top_journeys,
)
from app.attribution.engine import run_attribution
from app.db.models import Conversion
from app.db.session import SessionLocal, get_db
from app.generator.world import WorldConfig
from app.pipeline.cli import FAILURE_PROFILES, reset_data
from app.pipeline.replay import replay_quarantine
from app.pipeline.runner import run_ingestion
from app.api import jobs
from app.api.schemas import (
    ChannelsResponse,
    DateRange,
    DemoJobResponse,
    DemoResetRequest,
    Granularity,
    JourneysResponse,
    LargestDisagreement,
    ModelComparisonResponse,
    ModelInfo,
    ModelName,
    ModelsResponse,
    PipelineHealthResponse,
    SummaryResponse,
    TimeseriesResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

MAX_JOURNEY_LIMIT = 100

# Descriptions live here rather than in the engine because they exist for the
# UI's benefit; the engine only needs the maths.
MODEL_CATALOGUE: list[dict[str, str]] = [
    {
        "name": "last_touch",
        "label": "Last touch",
        "description": (
            "All credit to the final touchpoint. Assumes whatever closed the "
            "sale is what caused it."
        ),
        "favours": "Late funnel — paid search, email",
    },
    {
        "name": "first_touch",
        "label": "First touch",
        "description": (
            "All credit to the first touchpoint. Assumes discovery is "
            "everything and nurture is free."
        ),
        "favours": "Early funnel — display, social",
    },
    {
        "name": "linear",
        "label": "Linear",
        "description": (
            "Equal credit to every touchpoint. Assumes no touch matters more "
            "than another — a useful unbiased baseline."
        ),
        "favours": "Long journeys and mid funnel",
    },
    {
        "name": "time_decay",
        "label": "Time decay",
        "description": (
            "Credit decays exponentially with time before the conversion "
            "(7-day half-life). Assumes influence fades."
        ),
        "favours": "Recent touchpoints, whatever the channel",
    },
    {
        "name": "position_based",
        "label": "Position based (U-shaped)",
        "description": (
            "40% to the first touch, 40% to the last, 20% shared by the "
            "middle. Assumes discovery and closing are the hard parts."
        ),
        "favours": "Both ends of the journey",
    },
]


# ---------------------------------------------------------------------------
# Shared dependencies
# ---------------------------------------------------------------------------


class DateRangeParams:
    """Validates an optional inclusive date range.

    Cross-field validation cannot live on the individual Query params, so it
    happens here and raises a 422 that names the problem.
    """

    def __init__(
        self,
        start_date: Annotated[
            dt.date | None, Query(description="Inclusive start date (YYYY-MM-DD)")
        ] = None,
        end_date: Annotated[
            dt.date | None, Query(description="Inclusive end date (YYYY-MM-DD)")
        ] = None,
    ) -> None:
        if start_date and end_date and start_date > end_date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"start_date ({start_date.isoformat()}) must not be after "
                    f"end_date ({end_date.isoformat()})"
                ),
            )
        self.start_date = start_date
        self.end_date = end_date


DateRangeDep = Annotated[DateRangeParams, Depends()]
DbDep = Annotated[Session, Depends(get_db)]


def _blended_roas(revenue: Decimal, spend: Decimal) -> float | None:
    """Blended ROAS, or None when nothing was spent."""
    if spend == 0:
        return None
    return float((revenue / spend).quantize(Decimal("0.01")))


def _conversion_date_range(db: Session) -> DateRange:
    """Min/max conversion date.

    A metadata lookup rather than an analytics rollup — it describes the
    dataset's extent, not its performance — so it stays here instead of
    bloating `analytics.py`.
    """
    first, last = db.execute(
        select(func.min(Conversion.occurred_at), func.max(Conversion.occurred_at))
    ).one()
    return DateRange(
        start=first.date() if first else None,
        end=last.date() if last else None,
    )


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@router.get(
    "/models",
    response_model=ModelsResponse,
    summary="List the attribution models",
    description="The five models, with what each one assumes. Used to build the UI selector.",
)
def list_models() -> ModelsResponse:
    return ModelsResponse(models=[ModelInfo(**entry) for entry in MODEL_CATALOGUE])


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@router.get(
    "/channels",
    response_model=ChannelsResponse,
    summary="Channel performance for one model",
    description=(
        "Attributed revenue, fractional conversions, spend and ROAS per channel. "
        "`roas` is null for channels with no spend (organic)."
    ),
)
def get_channels(
    db: DbDep,
    dates: DateRangeDep,
    model: Annotated[ModelName, Query(description="Attribution model")] = ModelName.last_touch,
) -> ChannelsResponse:
    rows = channel_performance(
        db,
        model_name=model.value,
        start_date=dates.start_date,
        end_date=dates.end_date,
    )
    return ChannelsResponse(
        model=model,
        start_date=dates.start_date,
        end_date=dates.end_date,
        data=rows,
    )


@router.get(
    "/model-comparison",
    response_model=ModelComparisonResponse,
    summary="How much the models disagree",
    description=(
        "Attributed revenue per channel under every model, plus the swing "
        "(max minus min) — the revenue whose owner the models dispute."
    ),
)
def get_model_comparison(db: DbDep, dates: DateRangeDep) -> ModelComparisonResponse:
    result = model_comparison(
        db, start_date=dates.start_date, end_date=dates.end_date
    )
    return ModelComparisonResponse(
        models=result["models"],
        start_date=dates.start_date,
        end_date=dates.end_date,
        channels=result["channels"],
        totals_by_model=result["totals_by_model"],
        total_swing=result["total_swing"],
    )


@router.get(
    "/timeseries",
    response_model=TimeseriesResponse,
    summary="Attributed revenue over time by channel",
)
def get_timeseries(
    db: DbDep,
    dates: DateRangeDep,
    model: Annotated[ModelName, Query(description="Attribution model")] = ModelName.linear,
    granularity: Annotated[Granularity, Query()] = Granularity.day,
) -> TimeseriesResponse:
    rows = timeseries(
        db,
        model_name=model.value,
        granularity=granularity.value,
        start_date=dates.start_date,
        end_date=dates.end_date,
    )
    return TimeseriesResponse(
        model=model,
        granularity=granularity,
        start_date=dates.start_date,
        end_date=dates.end_date,
        data=rows,
    )


@router.get(
    "/journeys",
    response_model=JourneysResponse,
    summary="Sample customer journeys with per-touch credit",
    description="Journeys with 3+ touchpoints are preferred — they are the interesting ones.",
)
def get_journeys(
    db: DbDep,
    model: Annotated[ModelName, Query(description="Attribution model")] = ModelName.linear,
    limit: Annotated[
        int, Query(ge=1, le=MAX_JOURNEY_LIMIT, description="Max journeys (1-100)")
    ] = 20,
) -> JourneysResponse:
    rows = top_journeys(db, model_name=model.value, limit=limit)
    return JourneysResponse(model=model, limit=limit, data=rows)


@router.get(
    "/pipeline/health",
    response_model=PipelineHealthResponse,
    summary="Ingestion runs, quarantine breakdown and row counts",
    description="Surfaces data-quality work rather than pretending the numbers arrived clean.",
)
def get_pipeline_health(db: DbDep) -> PipelineHealthResponse:
    return PipelineHealthResponse(**pipeline_health(db))


@router.get(
    "/summary",
    response_model=SummaryResponse,
    summary="Headline numbers for the dashboard hero",
)
def get_summary(
    db: DbDep,
    model: Annotated[ModelName, Query(description="Attribution model")] = ModelName.last_touch,
) -> SummaryResponse:
    channels = channel_performance(db, model_name=model.value)
    comparison = model_comparison(db)
    health = pipeline_health(db, recent_runs=1)

    total_revenue = sum((row["attributed_revenue"] for row in channels), Decimal("0.00"))
    total_spend = sum((row["spend"] for row in channels), Decimal("0.00"))

    # channels is already sorted by swing descending inside model_comparison.
    disagreement = LargestDisagreement()
    if comparison["channels"]:
        worst = comparison["channels"][0]
        disagreement = LargestDisagreement(
            channel=worst["channel"],
            swing=worst["swing"],
            most_generous_model=worst["most_generous_model"],
            least_generous_model=worst["least_generous_model"],
        )

    return SummaryResponse(
        model=model,
        total_attributed_revenue=total_revenue,
        total_spend=total_spend,
        blended_roas=_blended_roas(total_revenue, total_spend),
        conversion_count=health["row_counts"]["conversions"],
        touchpoint_count=health["row_counts"]["touchpoints"],
        channel_count=len(channels),
        date_range=_conversion_date_range(db),
        largest_disagreement=disagreement,
    )


# ---------------------------------------------------------------------------
# Demo reset
# ---------------------------------------------------------------------------


def _run_demo_pipeline(job_id: str, params: dict[str, Any]) -> None:
    """Background worker: reset -> ingest -> replay -> attribute.

    Runs in Starlette's threadpool with its own session, because the request's
    session is closed by the time this executes.
    """
    failure_config = FAILURE_PROFILES[params["failure_profile"]]
    world_config = WorldConfig(seed=params["seed"], n_users=params["users"])
    db = SessionLocal()
    try:
        jobs.registry.begin_stage(job_id, "reset")
        reset_data(db)
        jobs.registry.end_stage(job_id, "reset", detail={"tables_truncated": True})

        jobs.registry.begin_stage(job_id, "ingest")
        ingest = run_ingestion(
            db, world_config=world_config, failure_config=failure_config
        )
        jobs.registry.end_stage(
            job_id,
            "ingest",
            detail={
                "received": ingest["totals"]["received"],
                "ingested": ingest["totals"]["ingested"],
                "quarantined": ingest["totals"]["quarantined"],
                "retries": ingest["totals"]["retries"],
            },
        )

        jobs.registry.begin_stage(job_id, "replay")
        replay = replay_quarantine(
            db, world_config=world_config, failure_config=failure_config
        )
        jobs.registry.end_stage(
            job_id,
            "replay",
            detail={
                "recovered": replay["totals"]["recovered"],
                "still_quarantined": replay["totals"]["still_quarantined"],
                "parent_campaigns_recovered": replay["parent_campaigns_recovered"],
            },
        )

        jobs.registry.begin_stage(job_id, "attribute")
        attribution = run_attribution(db, rebuild=True)
        jobs.registry.end_stage(
            job_id,
            "attribute",
            detail={
                "journeys": attribution["journeys"],
                "empty_journeys": attribution["empty_journeys"],
                "rows_written": attribution["totals"]["rows_written"],
            },
        )

        jobs.registry.finish(job_id)
        logger.info("demo job %s finished successfully", job_id)
    except Exception as exc:  # noqa: BLE001 - the job records its own failure
        db.rollback()
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("demo job %s failed", job_id)
        for stage in jobs.STAGES:
            state = jobs.registry.get(job_id)
            if state and state.stage(stage).status == "running":
                jobs.registry.end_stage(job_id, stage, failed=True)
        jobs.registry.finish(job_id, error=message)
    finally:
        db.close()


@router.post(
    "/demo/reset",
    response_model=DemoJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Rebuild the whole demo dataset",
    description=(
        "Truncates the data tables, then runs generate -> ingest -> replay -> "
        "attribute in the background. Returns 202 with a job id to poll. "
        "Returns 409 if a run is already in flight."
    ),
    responses={409: {"description": "A demo run is already in progress"}},
)
def post_demo_reset(
    payload: DemoResetRequest,
    background_tasks: BackgroundTasks,
) -> DemoJobResponse:
    params = {
        "seed": payload.seed,
        "users": payload.users,
        "failure_profile": payload.failure_profile.value,
    }
    try:
        job = jobs.registry.start(params)
    except jobs.JobAlreadyRunning as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"a demo reset is already running (job {exc.job_id}); "
                "poll /api/demo/status/{job_id} and retry when it finishes"
            ),
        ) from exc

    background_tasks.add_task(_run_demo_pipeline, job.job_id, params)
    logger.info("demo job %s accepted with params %s", job.job_id, params)
    return DemoJobResponse(**job.as_dict())


@router.get(
    "/demo/status/{job_id}",
    response_model=DemoJobResponse,
    summary="Poll a demo reset job",
    responses={404: {"description": "Unknown job id"}},
)
def get_demo_status(job_id: str) -> DemoJobResponse:
    job = jobs.registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown job id {job_id!r}"
        )
    return DemoJobResponse(**job.as_dict())
