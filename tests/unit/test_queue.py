"""Unit tests for the sequential batch job queue (plan todo 12 / C3b).

A scripted fake runner (success / fail / hang / block / raise outcomes) is
injected so the full state machine is exercised with no real process: every
``queued -> running -> done|failed|cancelled`` transition, the
failure-continue rule (F2), cancel propagation, id stability, dynamic submit
and callback ordering.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from lossless_toolbox.queue import JobQueue, JobRecord, RunResult

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

pytestmark = pytest.mark.unit


class _FakeRunner:
    """Scripted runner: consumes shared outcomes and records each invocation."""

    def __init__(
        self,
        outcomes: list[str],
        log: list[tuple[str, tuple[str, ...]]],
        cancel_event: threading.Event,
        release_event: threading.Event,
    ) -> None:
        self._outcomes = outcomes
        self._log = log
        self._cancel_event = cancel_event
        self._release_event = release_event

    def run(self, argv: list[str]) -> RunResult:
        self._log.append(("run", tuple(argv)))
        outcome = self._outcomes.pop(0)
        if outcome == "success":
            return RunResult(exit_code=0)
        if outcome == "fail":
            return RunResult(exit_code=1, stderr_tail="boom")
        if outcome == "fail_quiet":
            return RunResult(exit_code=2)
        if outcome == "hang":
            self._cancel_event.wait(timeout=10.0)
            return RunResult(exit_code=1, stderr_tail="terminated")
        if outcome == "block":
            self._release_event.wait(timeout=10.0)
            return RunResult(exit_code=0)
        if outcome == "raise":
            message = "runner exploded"
            raise OSError(message)
        message = f"unexpected outcome: {outcome!r}"
        raise AssertionError(message)

    def cancel(self) -> None:
        self._cancel_event.set()


@dataclass
class _Harness:
    """A wired queue plus the shared scripting channels the fake runner uses."""

    queue: JobQueue[str]
    log: list[tuple[str, tuple[str, ...]]]
    cancel_event: threading.Event
    release_event: threading.Event


def _identity_argv(spec: str) -> list[str]:
    """Default argv builder: wrap the spec in a single-token argv."""
    return [spec]


def _harness(
    outcomes: list[str],
    *,
    on_job_started: Callable[[JobRecord[str]], None] | None = None,
    on_job_finished: Callable[[JobRecord[str]], None] | None = None,
    on_all_done: Callable[[Sequence[JobRecord[str]]], None] | None = None,
    argv_builder: Callable[[str], list[str]] | None = None,
) -> _Harness:
    log: list[tuple[str, tuple[str, ...]]] = []
    cancel_event = threading.Event()
    release_event = threading.Event()

    def factory() -> _FakeRunner:
        return _FakeRunner(outcomes, log, cancel_event, release_event)

    if argv_builder is None:
        argv_builder = _identity_argv

    queue = JobQueue[str](
        runner_factory=factory,
        argv_builder=argv_builder,
        on_job_started=on_job_started,
        on_job_finished=on_job_finished,
        on_all_done=on_all_done,
    )
    return _Harness(queue, log, cancel_event, release_event)


def _run_async(queue: JobQueue[str]) -> threading.Thread:
    """Start ``queue.run()`` on a background thread and return the thread."""
    thread = threading.Thread(target=queue.run)
    thread.start()
    return thread


def test_submit_assigns_incrementing_ids_and_queued_status() -> None:
    """Given three specs; when submitted; then ids 1..3 and all queued."""
    harness = _harness([])
    records = harness.queue.submit(["a", "b", "c"])
    assert [r.id for r in records] == [1, 2, 3]
    assert [r.status for r in records] == ["queued", "queued", "queued"]
    assert harness.queue.jobs == tuple(records)
    assert harness.queue.current is None
    assert harness.queue.is_running is False


def test_success_transitions_through_running_to_done() -> None:
    """Given two successes; when run; then both done with timestamps and argv."""
    harness = _harness(["success", "success"])
    records = harness.queue.submit(["a", "b"])
    result = harness.queue.run()

    assert [r.id for r in result] == [1, 2]
    assert [r.status for r in records] == ["done", "done"]
    for record in records:
        assert record.error is None
        assert record.result is not None
        assert record.result.exit_code == 0
        assert record.started_at is not None
        assert record.ended_at is not None
        assert record.started_at <= record.ended_at
    assert harness.log == [("run", ("a",)), ("run", ("b",))]


def test_failure_records_error_and_continues() -> None:
    """Given fail in the middle; when run; then error lands and next runs."""
    harness = _harness(["success", "fail", "success"])
    records = harness.queue.submit(["a", "b", "c"])
    harness.queue.run()

    assert [r.status for r in records] == ["done", "failed", "done"]
    assert records[1].error == "boom"
    assert records[1].result is not None
    assert records[1].result.exit_code == 1
    assert records[0].error is None
    assert records[2].error is None
    assert harness.log == [
        ("run", ("a",)),
        ("run", ("b",)),
        ("run", ("c",)),
    ]


def test_failure_with_empty_stderr_falls_back_to_exit_code() -> None:
    """Given a silent nonzero exit; when run; then error is the exit code."""
    harness = _harness(["fail_quiet"])
    records = harness.queue.submit(["a"])
    harness.queue.run()

    assert records[0].status == "failed"
    assert records[0].error == "exit code 2"


def test_argv_builder_exception_is_recorded_as_failure() -> None:
    """Given an argv builder that raises; when run; then job fails, rest run."""

    def builder(spec: str) -> list[str]:
        if spec == "bad":
            message = "bad spec"
            raise ValueError(message)
        return [spec]

    harness = _harness(["success", "success"], argv_builder=builder)
    records = harness.queue.submit(["ok", "bad", "ok"])
    harness.queue.run()

    assert [r.status for r in records] == ["done", "failed", "done"]
    assert records[1].error is not None
    assert "ValueError" in records[1].error
    assert "bad spec" in records[1].error
    assert harness.log == [("run", ("ok",)), ("run", ("ok",))]


def test_runner_exception_is_recorded_as_failure() -> None:
    """Given a runner that raises; when run; then job fails, rest continue."""
    harness = _harness(["raise", "success"])
    records = harness.queue.submit(["a", "b"])
    harness.queue.run()

    assert [r.status for r in records] == ["failed", "done"]
    assert records[0].error is not None
    assert "OSError" in records[0].error
    assert "runner exploded" in records[0].error


def test_cancel_current_cancels_running_job_only() -> None:
    """Given a hanging job; when cancel_current; then only that job cancels."""
    started = threading.Event()

    def on_started(job: JobRecord[str]) -> None:
        started.set()

    harness = _harness(["hang", "success", "success"], on_job_started=on_started)
    records = harness.queue.submit(["a", "b", "c"])
    thread = _run_async(harness.queue)

    assert started.wait(timeout=10.0)
    harness.queue.cancel_current()
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    assert [r.status for r in records] == ["cancelled", "done", "done"]
    assert records[0].started_at is not None
    assert records[0].ended_at is not None
    assert records[1].status == "done"
    assert records[2].status == "done"


def test_cancel_all_cancels_running_and_clears_queued() -> None:
    """Given a hanging job; when cancel_all; then queued jobs never run."""
    started = threading.Event()
    harness = _harness(["hang"], on_job_started=lambda _: started.set())
    records = harness.queue.submit(["a", "b", "c"])
    thread = _run_async(harness.queue)

    assert started.wait(timeout=10.0)
    harness.queue.cancel_all()
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    assert [r.status for r in records] == ["cancelled", "cancelled", "cancelled"]
    assert harness.log == [("run", ("a",))]


def test_submit_during_run_is_executed_after_pending() -> None:
    """Given a blocked job; when submit during run; then the new job executes."""
    started = threading.Event()
    harness = _harness(["block", "success"], on_job_started=lambda _: started.set())
    first = harness.queue.submit(["a"])
    thread = _run_async(harness.queue)

    assert started.wait(timeout=10.0)
    extra = harness.queue.submit(["b"])
    harness.release_event.set()
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    assert [r.id for r in harness.queue.jobs] == [1, 2]
    assert first[0].status == "done"
    assert extra[0].status == "done"
    assert harness.log == [("run", ("a",)), ("run", ("b",))]


def test_callback_order_started_finished_all_done() -> None:
    """Given two jobs; when run; then callbacks fire started/finished per job."""
    events: list[tuple[str, int]] = []

    def on_started(job: JobRecord[str]) -> None:
        events.append(("started", job.id))

    def on_finished(job: JobRecord[str]) -> None:
        events.append(("finished", job.id))

    def on_all_done(jobs: Sequence[JobRecord[str]]) -> None:
        events.append(("all_done", len(jobs)))

    harness = _harness(
        ["success", "success"],
        on_job_started=on_started,
        on_job_finished=on_finished,
        on_all_done=on_all_done,
    )
    harness.queue.submit(["a", "b"])
    harness.queue.run()

    assert events == [
        ("started", 1),
        ("finished", 1),
        ("started", 2),
        ("finished", 2),
        ("all_done", 2),
    ]


def test_concurrent_run_raises() -> None:
    """Given run() already executing; when run() again; then RuntimeError."""
    started = threading.Event()
    harness = _harness(["hang"], on_job_started=lambda _: started.set())
    harness.queue.submit(["a"])
    thread = _run_async(harness.queue)

    assert started.wait(timeout=10.0)
    with pytest.raises(RuntimeError, match="already running"):
        harness.queue.run()
    harness.queue.cancel_all()
    thread.join(timeout=10.0)
    assert not thread.is_alive()


def test_cancel_with_no_running_job_is_noop() -> None:
    """Given an idle queue; when cancel_current/cancel_all; then no error."""
    harness = _harness([])
    harness.queue.cancel_current()
    harness.queue.cancel_all()
    assert harness.queue.is_running is False
