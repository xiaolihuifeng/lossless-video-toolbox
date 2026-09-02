"""Unit tests for the queue worker's RunnerAdapter (F2-M1 duration plumbing).

The adapter is the seam between the queue's minimal ``Runner`` protocol and
the progress-reporting :class:`~lossless_toolbox.runner.Runner`. These tests
prove it forwards the job's media duration (or None), the stdin payload and
the progress callback without spawning any process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lossless_toolbox.queue import RunResult as QueueRunResult
from lossless_toolbox.runner import ProgressEvent, RunResult
from lossless_toolbox.ui.workers import RunnerAdapter

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

pytestmark = pytest.mark.unit


class _ProgressRecorder:
    """Records every ``run`` invocation and replays scripted events."""

    def __init__(self, events: Sequence[ProgressEvent] = ()) -> None:
        self.events = list(events)
        self.calls: list[
            tuple[
                list[str],
                Callable[[ProgressEvent], None] | None,
                bytes | None,
                float | None,
            ]
        ] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        stdin_bytes: bytes | None = None,
        duration: float | None = None,
    ) -> RunResult:
        self.calls.append((list(argv), on_progress, stdin_bytes, duration))
        for event in self.events:
            if on_progress is not None:
                on_progress(event)
        return RunResult(
            exit_code=0,
            cancelled=False,
            progress_events=tuple(self.events),
            last_out_time=None,
            duration=0.0,
            stderr_tail="",
        )

    def cancel(self) -> None:
        """Not exercised here."""


def test_adapter_forwards_known_duration_to_runner() -> None:
    """Given a known duration; then the wrapped runner receives it verbatim."""
    inner = _ProgressRecorder()
    adapter = RunnerAdapter(inner)

    result = adapter.run(["-i", "in.mp4"], duration=12.5)

    assert isinstance(result, QueueRunResult)
    assert result.exit_code == 0
    assert inner.calls == [(["-i", "in.mp4"], None, None, 12.5)]


def test_adapter_forwards_unknown_duration_as_none() -> None:
    """Given no duration; then the runner gets None (indeterminate path)."""
    inner = _ProgressRecorder()
    adapter = RunnerAdapter(inner)

    adapter.run(["-i", "in.mp4"])

    assert inner.calls[0][3] is None


def test_adapter_forwards_progress_stdin_and_duration_together() -> None:
    """Progress callback, stdin payload and duration all reach the runner."""
    event = ProgressEvent(out_time=2.0, progress=0.5)
    inner = _ProgressRecorder(events=[event])
    received: list[ProgressEvent] = []
    adapter = RunnerAdapter(inner, on_progress=received.append)

    result = adapter.run(
        ["-i", "-"], stdin_bytes=b"file 'a.mp4'\n", duration=4.0
    )

    assert result.exit_code == 0
    assert received == [event]
    assert inner.calls == [
        (["-i", "-"], received.append, b"file 'a.mp4'\n", 4.0)
    ]
