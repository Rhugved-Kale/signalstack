"""Paginated fetching with a retry policy for transport failures.

The split that matters here: a 500, a timeout or a 429 is a *transport*
problem and worth retrying, whereas a malformed record is a *data* problem and
retrying it will produce the same malformed record forever. So this layer
retries the former and never the latter — validation happens downstream, after
the bytes are safely in hand.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.generator.fake_apis import FakeTimeoutError, RateLimitError, ServerError

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_INITIAL = 0.5
BACKOFF_MAX = 8.0
BACKOFF_JITTER = 0.25

# Transport failures worth retrying. Anything else (a ValueError, a
# ValidationError) propagates on the first raise.
RETRYABLE_ERRORS = (ServerError, FakeTimeoutError, RateLimitError)

# Belt-and-braces guard against an upstream that never sets has_more=False.
MAX_PAGES = 100_000

_BACKOFF = wait_exponential_jitter(
    initial=BACKOFF_INITIAL, max=BACKOFF_MAX, jitter=BACKOFF_JITTER
)


class PipelineFetchError(Exception):
    """Raised when a page could not be fetched within `max_attempts`."""

    def __init__(
        self,
        message: str,
        *,
        source: str,
        page: int,
        attempts: int,
        last_exception: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.source = source
        self.page = page
        self.attempts = attempts
        self.last_exception = last_exception


@dataclass
class FetchStats:
    """Mutable counters the caller reads after iterating.

    `fetch_all_pages` is a generator, so it cannot return a value; the caller
    passes one of these in and reads it once iteration finishes. `retries` is
    what lands on `IngestionRun.retry_count`.
    """

    pages_fetched: int = 0
    retries: int = 0
    records_received: int = 0
    last_error: str | None = None


def _describe(exc: BaseException) -> str:
    """Short, loggable reason for a retry."""
    if isinstance(exc, RateLimitError):
        return f"RateLimitError(429, retry_after={exc.retry_after}s)"
    if isinstance(exc, ServerError):
        return f"ServerError({exc.status_code})"
    if isinstance(exc, FakeTimeoutError):
        return "FakeTimeoutError(no response)"
    return f"{type(exc).__name__}: {exc}"


def _wait_policy(retry_state: RetryCallState) -> float:
    """Exponential backoff with jitter, except for 429s.

    A rate limit tells us exactly how long to wait, so honouring
    `retry_after` is both politer and faster than guessing with a backoff
    curve.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, RateLimitError):
        return float(exc.retry_after)
    return _BACKOFF(retry_state)


def fetch_page(
    fetch_fn: Callable[..., dict[str, Any]],
    *,
    source: str,
    page: int,
    sleep_fn: Callable[[float], None] = time.sleep,
    stats: FetchStats | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch one page, retrying transport failures.

    Raises `PipelineFetchError` once `max_attempts` is exhausted.
    """
    stats = stats if stats is not None else FetchStats()

    def _before_sleep(retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        stats.retries += 1
        stats.last_error = _describe(exc) if exc else None
        logger.info(
            "retrying source=%s page=%s attempt=%s/%s reason=%s",
            source,
            page,
            retry_state.attempt_number,
            max_attempts,
            stats.last_error,
        )

    retryer = Retrying(
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        wait=_wait_policy,
        stop=stop_after_attempt(max_attempts),
        sleep=sleep_fn,
        before_sleep=_before_sleep,
        reraise=False,
    )

    try:
        envelope = retryer(fetch_fn, page=page, **kwargs)
    except RetryError as retry_error:
        last = retry_error.last_attempt.exception()
        stats.last_error = _describe(last) if last else None
        raise PipelineFetchError(
            f"source={source} page={page} failed after {max_attempts} attempts: "
            f"{stats.last_error}",
            source=source,
            page=page,
            attempts=max_attempts,
            last_exception=last,
        ) from last

    stats.pages_fetched += 1
    return envelope


def fetch_all_pages(
    fetch_fn: Callable[..., dict[str, Any]],
    *,
    source: str = "unknown",
    sleep_fn: Callable[[float], None] = time.sleep,
    stats: FetchStats | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    **kwargs: Any,
) -> Iterator[dict[str, Any]]:
    """Yield every record across every page, retrying each page independently.

    One bad page does not lose the pages already yielded; the caller has them
    already. Reserved keyword arguments (`source`, `sleep_fn`, `stats`,
    `max_attempts`) are consumed here — everything else is forwarded to
    `fetch_fn` alongside `page`.
    """
    stats = stats if stats is not None else FetchStats()
    page = 1

    while page <= MAX_PAGES:
        envelope = fetch_page(
            fetch_fn,
            source=source,
            page=page,
            sleep_fn=sleep_fn,
            stats=stats,
            max_attempts=max_attempts,
            **kwargs,
        )

        records = envelope.get("data") or []
        stats.records_received += len(records)
        for record in records:
            yield record

        if not envelope.get("has_more"):
            return
        page += 1

    logger.warning(
        "source=%s stopped at the %s page safety limit", source, MAX_PAGES
    )
