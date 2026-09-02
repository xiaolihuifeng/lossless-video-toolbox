"""QThread bridges connecting the GUI to ffprobe and the batch job queue.

Two worker threads keep every external process off the UI thread:

* :class:`ProbeWorker` runs one :func:`lossless_toolbox.probe.probe` call and
  emits ``probe_finished`` with the typed :class:`MediaFile` or the
  :class:`ProbeError`.
* :class:`QueueWorker` owns one :class:`JobQueue` and replays its callbacks as
  Qt signals (``job_started`` / ``job_progress`` / ``job_finished`` /
  ``all_done``). A :class:`RunnerAdapter` adapts the progress-reporting
  :class:`lossless_toolbox.runner.Runner` to the queue's minimal protocol
  while forwarding each :class:`ProgressEvent` as ``job_progress``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject, QThread, Signal

from lossless_toolbox.probe import ProbeError, probe
from lossless_toolbox.queue import JobQueue
from lossless_toolbox.queue import RunResult as QueueRunResult

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from lossless_toolbox.queue import JobRecord
    from lossless_toolbox.runner import ProgressEvent, RunResult

logger = logging.getLogger(__name__)


class ProgressRunner(Protocol):
    """Structural contract of :class:`lossless_toolbox.runner.Runner`.

    ``run`` executes argv with an optional progress callback; ``cancel`` is
    safe to call from any thread.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> RunResult:
        """Execute ``argv`` to completion and report the run outcome."""
        ...

    def cancel(self) -> None:
        """Request cancellation of the in-flight run."""
        ...


class RunnerAdapter:
    """Adapt a progress runner to the queue's minimal ``Runner`` protocol."""

    def __init__(
        self,
        runner: ProgressRunner,
        *,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        """Wrap ``runner`` and forward its progress events to ``on_progress``."""
        self._runner = runner
        self._on_progress = on_progress

    def run(self, argv: Sequence[str]) -> QueueRunResult:
        """Run argv via the wrapped runner, keeping only the queue contract."""
        result = self._runner.run(argv, on_progress=self._on_progress)
        return QueueRunResult(
            exit_code=result.exit_code, stderr_tail=result.stderr_tail
        )

    def cancel(self) -> None:
        """Forward cancellation to the wrapped runner."""
        self._runner.cancel()


class ProbeWorker(QThread):
    """Probe one media file off the UI thread and report the outcome."""

    probe_finished = Signal(Path, object)

    def __init__(self, path: Path, parent: QObject | None = None) -> None:
        """Create a worker that probes ``path`` when started."""
        super().__init__(parent)
        self._path = path

    def run(self) -> None:
        """Run ffprobe once and emit the MediaFile or the ProbeError."""
        try:
            result: object = probe(self._path)
        except ProbeError as exc:
            logger.debug("probe failed for %s: %s", self._path, exc)
            result = exc
        self.probe_finished.emit(self._path, result)


class QueueWorker(QThread):
    """Run a JobQueue over submitted specs, replaying events as Qt signals."""

    job_started = Signal(object)  # JobRecord
    job_progress = Signal(object, object)  # JobRecord, ProgressEvent
    job_finished = Signal(object)  # JobRecord
    all_done = Signal(object)  # list[JobRecord]

    def __init__(
        self,
        specs: Sequence[object],
        *,
        runner_factory: Callable[[], ProgressRunner],
        argv_builder: Callable[[object], list[str]],
        parent: QObject | None = None,
    ) -> None:
        """Create a worker that runs ``specs`` through one JobQueue."""
        super().__init__(parent)
        self._specs = list(specs)
        self._runner_factory = runner_factory
        self._argv_builder = argv_builder
        self._queue: JobQueue[object] | None = None
        self._results: list[JobRecord[object]] = []

    def run(self) -> None:
        """Submit the specs and run the queue to completion (blocking)."""
        self._queue = JobQueue[object](
            runner_factory=lambda: RunnerAdapter(
                self._runner_factory(),
                on_progress=self._relay_progress,
            ),
            argv_builder=self._argv_builder,
            on_job_started=self.job_started.emit,
            on_job_finished=self.job_finished.emit,
            on_all_done=self.all_done.emit,
        )
        self._queue.submit(self._specs)
        self._results = list(self._queue.run())
        logger.info("queue worker finished %d job(s)", len(self._results))

    def _relay_progress(self, event: ProgressEvent) -> None:
        """Emit ``job_progress`` for the queue's currently running job."""
        queue = self._queue
        if queue is None:
            return
        job = queue.current
        if job is not None:
            self.job_progress.emit(job, event)

    def cancel_current(self) -> None:
        """Cancel only the running job (remaining queued jobs still run)."""
        if self._queue is not None:
            self._queue.cancel_current()

    def cancel_all(self) -> None:
        """Cancel the running job and clear every remaining queued job."""
        if self._queue is not None:
            self._queue.cancel_all()

    @property
    def results(self) -> tuple[JobRecord[object], ...]:
        """The completed job records, valid once the thread has finished."""
        return tuple(self._results)
