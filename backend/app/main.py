"""SignalStack API entrypoint."""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from app.api import router as api_router
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
access_logger = logging.getLogger("signalstack.access")

API_DESCRIPTION = """
**SignalStack** is an ad performance attribution pipeline.

It ingests campaign, event and revenue data from three deliberately unreliable
upstream APIs, validates and quarantines what arrives broken, then runs
multi-touch attribution over the reconstructed customer journeys and serves the
results here.

The point of the API is the *disagreement*: the same conversions are scored
under five attribution models simultaneously, so you can see how much a
channel's apparent value depends on which model you believe. `last_touch` and
`first_touch` routinely differ by an order of magnitude on the same data.

* `/api/models` — the five models and what each assumes
* `/api/channels` — per-channel revenue, spend and ROAS for one model
* `/api/model-comparison` — the same revenue under every model, plus the swing
* `/api/timeseries` — attributed revenue over time
* `/api/journeys` — individual customer paths with per-touch credit
* `/api/pipeline/health` — ingestion runs, quarantine reasons, row counts
* `/api/summary` — headline numbers for the dashboard
* `/api/demo/reset` — rebuild the entire dataset from scratch (background job)

Money is `Decimal` end to end internally and is serialised as a JSON number.
`roas` is `null`, never `0`, for channels with no spend.
""".strip()

app = FastAPI(
    title="SignalStack API",
    version="0.6.0",
    description=API_DESCRIPTION,
    openapi_tags=[
        {"name": "attribution", "description": "Attribution analytics"},
        {"name": "pipeline", "description": "Data pipeline health and controls"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log method, path, status and duration for every request."""
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # The exception handlers below build the response; log the timing here
        # so a failed request still leaves an access-log line.
        duration_ms = (time.perf_counter() - started) * 1000
        access_logger.warning(
            "%s %s -> unhandled exception in %.1fms",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = (time.perf_counter() - started) * 1000
    access_logger.info(
        "%s %s -> %s in %.1fms",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    response.headers["X-Response-Time-ms"] = f"{duration_ms:.1f}"
    return response


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
async def database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """A database that cannot be reached is a 503, not a 500.

    The client did nothing wrong and the request is worth retrying, which is
    exactly what 503 means.
    """
    logger.error("database unavailable on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "error": "database_unavailable",
            "detail": (
                "The database is not reachable. Check that Postgres is running "
                "(docker compose up -d) and that DATABASE_URL points at it."
            ),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log the traceback, return a clean body.

    The stack trace goes to the log, never to the client.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_server_error",
            "detail": (
                f"{type(exc).__name__} while handling {request.method} "
                f"{request.url.path}. See server logs for the traceback."
            ),
        },
    )


app.include_router(api_router)


@app.get("/health", tags=["pipeline"], summary="Liveness check")
def health() -> dict[str, str]:
    """Unchanged from Phase 1 — deployment health checks depend on this shape."""
    return {
        "status": "ok",
        "service": "signalstack-api",
        "environment": settings.ENVIRONMENT,
    }
