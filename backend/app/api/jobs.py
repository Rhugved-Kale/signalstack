"""In-process job tracking for the demo reset button.

Deliberately simple: one dict and one lock. There is no queue, no worker and
no persistence, because the only job this runs is "rebuild the demo dataset"
and exactly one may be in flight at a time. A second request while one is
running gets a 409 rather than two pipelines fighting over the same tables.

A restart loses job history. That is acceptable for a demo control; if this
ever needs to survive a deploy it belongs in the database, not here.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

STAGES = ("reset", "ingest", "replay", "attribute")


class JobAlreadyRunning(RuntimeError):
    """Raised when a job is requested while another is still in flight."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"job {job_id} is already running")
        self.job_id = job_id


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class Stage:
    name: str
    status: str = "pending"  # pending | running | success | failed | skipped
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    detail: dict[str, Any] | None = None


@dataclass
class Job:
    job_id: str
    params: dict[str, Any]
    status: str = "running"  # running | success | failed
    started_at: dt.datetime = field(default_factory=_now)
    finished_at: dt.datetime | None = None
    error: str | None = None
    stages: list[Stage] = field(default_factory=lambda: [Stage(name) for name in STAGES])

    def stage(self, name: str) -> Stage:
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise KeyError(name)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 3)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["duration_seconds"] = self.duration_seconds
        return payload


class JobRegistry:
    """Tracks demo jobs and enforces one-at-a-time."""

    def __init__(self, history_limit: int = 20) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._active_job_id: str | None = None
        self._history_limit = history_limit

    # -- lifecycle --------------------------------------------------------

    def start(self, params: dict[str, Any]) -> Job:
        """Register a new running job, or raise `JobAlreadyRunning`."""
        with self._lock:
            if self._active_job_id is not None:
                active = self._jobs.get(self._active_job_id)
                if active is not None and active.status == "running":
                    raise JobAlreadyRunning(self._active_job_id)
                # Stale pointer (shouldn't happen); clear it and continue.
                self._active_job_id = None

            job = Job(job_id=uuid.uuid4().hex[:12], params=dict(params))
            self._jobs[job.job_id] = job
            self._active_job_id = job.job_id
            self._prune_locked()
            return job

    def finish(self, job_id: str, *, error: str | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.finished_at = _now()
            job.status = "failed" if error else "success"
            job.error = error
            if self._active_job_id == job_id:
                self._active_job_id = None

    # -- stage updates ----------------------------------------------------

    def begin_stage(self, job_id: str, name: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            stage = job.stage(name)
            stage.status = "running"
            stage.started_at = _now()

    def end_stage(
        self,
        job_id: str,
        name: str,
        *,
        detail: dict[str, Any] | None = None,
        failed: bool = False,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            stage = job.stage(name)
            stage.status = "failed" if failed else "success"
            stage.finished_at = _now()
            stage.detail = detail

    # -- reads ------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    @property
    def active_job_id(self) -> str | None:
        with self._lock:
            return self._active_job_id

    def reset(self) -> None:
        """Drop all state. For tests."""
        with self._lock:
            self._jobs.clear()
            self._active_job_id = None

    # -- internals --------------------------------------------------------

    def _prune_locked(self) -> None:
        """Keep the most recent jobs only; this dict is unbounded otherwise."""
        if len(self._jobs) <= self._history_limit:
            return
        finished = [
            job_id
            for job_id, job in sorted(self._jobs.items(), key=lambda item: item[1].started_at)
            if job.status != "running"
        ]
        for job_id in finished[: len(self._jobs) - self._history_limit]:
            del self._jobs[job_id]


#: Process-wide registry used by the routes.
registry = JobRegistry()
