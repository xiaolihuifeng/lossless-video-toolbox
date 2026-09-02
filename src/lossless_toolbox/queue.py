"""Sequential batch job queue with a per-job state machine (plan todo 12 / C3b).

Pure orchestration — no ffmpeg process and no argv construction. Callers inject
a ``runner_factory`` (one fresh cancelable runner per job) and an
``argv_builder`` (spec -> argv; the ops engine is the sole argv authority).
Execution is single-concurrency: ffmpeg saturates IO/CPU on its own, so a
failed job just records its error and the run continues (F2). Two cancel
strengths: :meth:`JobQueue.cancel_current` stops only the running job (the rest
of the batch still runs), while :meth:`JobQueue.cancel_all` also clears every
remaining queued job. A spec may expose ``build_stdin_data()``; its bytes are
passed to the runner as ``stdin_bytes`` (the concat-demuxer merge feeds its
file list this way). All shared state is lock-guarded so submit/cancel may be
called from any thread while :meth:`JobQueue.run` executes in another.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Generic, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

SpecT = TypeVar("SpecT")

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp for job start/end times."""
    return datetime.now(timezone.utc)


class RunResult(BaseModel):
    """Minimal runner outcome the queue reads back: exit code + stderr tail.

    ``exit_code`` 0 means success; ``stderr_tail`` becomes the job error on
    failure. Richer runner payloads (progress, out-time, duration) live in the
    runner module and stay out of this contract.
    """

    model_config = ConfigDict(frozen=True)

    exit_code: int
    stderr_tail: str = ""


class Runner(Protocol):
    """Structural contract for a cancelable ffmpeg runner (todo 11).

    ``run`` must not raise for a failed process (nonzero exit code is the
    failure signal); ``cancel`` must be safe to call cross-thread.
    ``stdin_bytes`` feeds a spec's stdin payload (e.g. the concat demuxer file
    list for merges); runners that need no stdin simply ignore the keyword.
    """

    def run(
        self, argv: list[str], *, stdin_bytes: bytes | None = None
    ) -> RunResult:
        """Execute ``argv`` to completion and return the exit-code outcome."""
        ...

    def cancel(self) -> None:
        """Request cancellation of the in-flight ``run`` (thread-safe)."""
        ...


def _spec_stdin_bytes(spec: object) -> bytes | None:
    """Return ``spec``'s stdin payload via ``build_stdin_data``, or None.

    Only specs that feed data over stdin (e.g. the concat-demuxer merge) expose
    ``build_stdin_data``; every other spec yields None so the runner is called
    with its plain ``argv``.
    """
    builder = getattr(spec, "build_stdin_data", None)
    return cast("bytes | None", builder()) if callable(builder) else None


class JobRecord(BaseModel, Generic[SpecT]):
    """One submitted job and its state-machine result.

    Transitions: ``queued -> running -> done|failed|cancelled``, plus
    ``queued -> cancelled`` when :meth:`JobQueue.cancel_all` clears jobs that
    never started. The queue mutates this record in place; the UI reads it.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int
    spec: SpecT
    status: JobStatus = "queued"
    error: str | None = None
    result: RunResult | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class JobQueue(Generic[SpecT]):
    """Sequential, single-concurrency batch queue over injected specs.

    Submit ops specs, then call :meth:`run` once (typically on a worker
    thread). Each spec becomes argv via ``argv_builder`` and is handed to a
    fresh runner; a nonzero exit or exception lands on the job and the queue
    moves on. See the module docstring for threading/cancellation semantics.
    """

    def __init__(
        self,
        runner_factory: Callable[[], Runner],
        argv_builder: Callable[[SpecT], list[str]],
        *,
        on_job_started: Callable[[JobRecord[SpecT]], None] | None = None,
        on_job_finished: Callable[[JobRecord[SpecT]], None] | None = None,
        on_all_done: Callable[[Sequence[JobRecord[SpecT]]], None] | None = None,
    ) -> None:
        """Wire the runner factory, argv builder and optional GUI callbacks."""
        self._runner_factory = runner_factory
        self._argv_builder = argv_builder
        self._on_job_started = on_job_started
        self._on_job_finished = on_job_finished
        self._on_all_done = on_all_done

        self._lock = threading.Lock()
        self._jobs: list[JobRecord[SpecT]] = []
        self._pending: deque[JobRecord[SpecT]] = deque()
        self._current: JobRecord[SpecT] | None = None
        self._current_runner: Runner | None = None
        self._cancel_requested = False
        self._cancel_all = False
        self._running = False
        self._next_id = 1

    @property
    def jobs(self) -> tuple[JobRecord[SpecT], ...]:
        """A snapshot of every submitted job, in submission order."""
        with self._lock:
            return tuple(self._jobs)

    @property
    def current(self) -> JobRecord[SpecT] | None:
        """The currently running job, or ``None`` when idle."""
        with self._lock:
            return self._current

    @property
    def is_running(self) -> bool:
        """Whether :meth:`run` is currently executing in some thread."""
        with self._lock:
            return self._running

    def submit(self, specs: Iterable[SpecT]) -> list[JobRecord[SpecT]]:
        """Append ``specs`` as queued jobs and return their records.

        Safe from any thread, including while :meth:`run` executes elsewhere:
        new jobs join the tail of the pending queue.
        """
        created: list[JobRecord[SpecT]] = []
        with self._lock:
            for spec in specs:
                job = JobRecord(id=self._next_id, spec=spec)
                self._next_id += 1
                self._jobs.append(job)
                self._pending.append(job)
                created.append(job)
        return created

    def cancel_current(self) -> None:
        """Cancel only the running job; remaining queued jobs still run.

        No-op when nothing runs. Per F2, cancelling one job does not interrupt
        the rest of the batch.
        """
        with self._lock:
            runner = self._current_runner
            if runner is None:
                return
            self._cancel_requested = True
        runner.cancel()

    def cancel_all(self) -> None:
        """Cancel the running job and clear every remaining queued job.

        The running job is cancelled via its runner; every still-queued job is
        marked ``cancelled`` without running, and :meth:`run` returns once the
        current runner stops.
        """
        with self._lock:
            self._cancel_all = True
            runner = self._current_runner
            if runner is not None:
                self._cancel_requested = True
        if runner is not None:
            runner.cancel()

    def run(self) -> list[JobRecord[SpecT]]:
        """Execute every pending job sequentially and return all job records.

        Blocks until the queue drains or :meth:`cancel_all` clears it. A failed
        job does not stop the run — the next queued job still executes.
        """
        with self._lock:
            if self._running:
                message = "JobQueue.run() is already running"
                raise RuntimeError(message)
            self._running = True
            self._cancel_all = False

        try:
            while True:
                with self._lock:
                    if self._cancel_all or not self._pending:
                        break
                    job = self._pending.popleft()
                self._run_job(job)
        finally:
            with self._lock:
                while self._pending:
                    leftover = self._pending.popleft()
                    leftover.status = "cancelled"
                    leftover.ended_at = _utcnow()
                self._current = None
                self._current_runner = None
                self._running = False
                self._cancel_all = False

        snapshot = list(self._jobs)
        if self._on_all_done is not None:
            self._on_all_done(snapshot)
        return snapshot

    def _run_job(self, job: JobRecord[SpecT]) -> None:
        """Drive one job through ``queued -> running -> terminal`` status."""
        runner = self._runner_factory()
        with self._lock:
            job.status = "running"
            job.started_at = _utcnow()
            self._current = job
            self._current_runner = runner
            self._cancel_requested = False
            skip = self._cancel_all

        if skip:
            with self._lock:
                job.status = "cancelled"
                job.ended_at = _utcnow()
                self._current = None
                self._current_runner = None
            if self._on_job_finished is not None:
                self._on_job_finished(job)
            return

        if self._on_job_started is not None:
            self._on_job_started(job)

        try:
            argv = self._argv_builder(job.spec)
            stdin_bytes = _spec_stdin_bytes(job.spec)
            result = (
                runner.run(argv, stdin_bytes=stdin_bytes)
                if stdin_bytes is not None
                else runner.run(argv)
            )
        except Exception as exc:  # noqa: BLE001 - job boundary: record, never raise
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        else:
            with self._lock:
                cancelled = self._cancel_requested
            if cancelled:
                job.status = "cancelled"
            elif result.exit_code == 0:
                job.status = "done"
                job.result = result
            else:
                job.status = "failed"
                job.result = result
                job.error = result.stderr_tail or f"exit code {result.exit_code}"
        finally:
            job.ended_at = _utcnow()
            with self._lock:
                self._current = None
                self._current_runner = None
                self._cancel_requested = False

        if self._on_job_finished is not None:
            self._on_job_finished(job)
