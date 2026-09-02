"""Unit tests for the ffmpeg subprocess runner.

The runner is exercised against fake ``#!/bin/sh`` scripts installed under
``tmp_path`` that emit fixed ``-progress`` line sequences, inject exit codes,
hang, or validate stdin — no real ffmpeg binary is required.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import pytest

from lossless_toolbox.runner import ProgressEvent, Runner, RunResult

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


def _script(tmp_path: Path, body: str) -> Path:
    """Write an executable fake-ffmpeg shell script and return its path."""
    path = tmp_path / "fake_ffmpeg"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    """Block until ``path`` exists, failing the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.025)
    pytest.fail(f"{path} was not created within {timeout:g}s")


def test_progress_parsing_last_out_time_and_end(tmp_path: Path) -> None:
    """Given three progress blocks; then events are parsed, ordered and ended."""
    body = (
        "printf 'frame=100\\nfps=25.0\\nout_time_us=2000000\\n"
        "speed=1.0x\\nprogress=continue\\n'\n"
        "printf 'frame=200\\nout_time_us=4000000\\n"
        "speed=1.0x\\nprogress=continue\\n'\n"
        "printf 'frame=300\\nout_time_us=6000000\\n"
        "speed=1.0x\\nprogress=end\\n'\n"
    )
    received: list[ProgressEvent] = []

    result = Runner(binary=str(_script(tmp_path, body))).run(
        [], duration=6.0, on_progress=received.append
    )

    assert result.exit_code == 0
    assert result.cancelled is False
    assert result.error is None
    assert result.last_out_time == pytest.approx(6.0)
    assert [e.frame for e in result.progress_events] == [100, 200, 300]
    assert [e.out_time for e in result.progress_events] == pytest.approx(
        [2.0, 4.0, 6.0]
    )
    assert [e.end for e in result.progress_events] == [False, False, True]
    progress_values = [e.progress for e in result.progress_events]
    assert progress_values == pytest.approx([2.0 / 6.0, 4.0 / 6.0, 1.0])
    assert progress_values == sorted(progress_values)
    assert received == list(result.progress_events)


def test_out_time_ms_compatibility(tmp_path: Path) -> None:
    """Given an out_time_ms block; then it is interpreted as milliseconds."""
    body = "printf 'out_time_ms=2500\\nprogress=end\\n'\n"

    result = Runner(binary=str(_script(tmp_path, body))).run([], duration=10.0)

    assert result.exit_code == 0
    assert result.last_out_time == pytest.approx(2.5)
    assert result.progress_events[0].out_time == pytest.approx(2.5)
    assert result.progress_events[0].progress == pytest.approx(0.25)


def test_out_time_us_preferred_over_ms_quirk(tmp_path: Path) -> None:
    """Given both fields (ffmpeg 6.1.1 prints out_time_ms in microseconds)."""
    body = (
        "printf 'out_time_us=3990748\\nout_time_ms=3990748\\nprogress=end\\n'\n"
    )

    result = Runner(binary=str(_script(tmp_path, body))).run([], duration=4.0)

    assert result.last_out_time == pytest.approx(3.990748)
    assert result.progress_events[0].out_time == pytest.approx(3.990748)


def test_normal_completion_exit_zero(tmp_path: Path) -> None:
    """Given a script that exits 0 with no progress; then a clean result."""
    result = Runner(binary=str(_script(tmp_path, "exit 0"))).run([])

    assert result.exit_code == 0
    assert result.cancelled is False
    assert result.error is None
    assert result.progress_events == ()
    assert result.last_out_time is None
    assert result.duration >= 0


def test_nonzero_exit_captures_stderr_tail(tmp_path: Path) -> None:
    """Given exit 1 with a stderr diagnostic; then it lands in error + tail."""
    result = Runner(
        binary=str(_script(tmp_path, "echo 'ERROR: something broke' >&2\nexit 1"))
    ).run([])

    assert result.exit_code == 1
    assert result.error is not None
    assert "ERROR: something broke" in result.error
    assert "ERROR: something broke" in result.stderr_tail


def test_cancel_terminates_hung_process(tmp_path: Path) -> None:
    """Given a sleeping child; then cancel terminates it within the grace."""
    ready = tmp_path / "ready"
    script = _script(tmp_path, f"touch {ready}\nexec sleep 30")
    runner = Runner(binary=str(script))
    result: dict[str, RunResult] = {}

    def target() -> None:
        result["value"] = runner.run([])

    thread = threading.Thread(target=target)
    thread.start()
    _wait_for(ready)
    started = time.monotonic()
    runner.cancel()
    elapsed = time.monotonic() - started
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    value = result["value"]
    assert value.cancelled is True
    assert value.exit_code < 0  # died from SIGTERM (-15)
    assert elapsed < 2.0  # terminated inside the grace period


def test_cancel_kills_process_that_ignores_sigterm(tmp_path: Path) -> None:
    """Given a child that ignores SIGTERM; then cancel kills it after grace."""
    ready = tmp_path / "ready"
    script = _script(tmp_path, f"trap '' TERM\ntouch {ready}\nwhile :; do :; done")
    runner = Runner(binary=str(script))
    result: dict[str, RunResult] = {}

    def target() -> None:
        result["value"] = runner.run([])

    thread = threading.Thread(target=target)
    thread.start()
    _wait_for(ready)
    started = time.monotonic()
    runner.cancel()
    elapsed = time.monotonic() - started
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    value = result["value"]
    assert value.cancelled is True
    assert value.exit_code < 0  # died from SIGKILL (-9)
    assert 1.5 <= elapsed < 3.5  # grace period elapsed before the hard kill


def test_stdin_bytes_passed_to_child(tmp_path: Path) -> None:
    """Given stdin_bytes; then the child reads them verbatim over stdin."""
    body = (
        "line=$(head -n 1)\n"
        "if [ \"$line\" = \"file '/tmp/a.mp4'\" ]; then\n"
        "  printf 'progress=end\\n'\n"
        "  exit 0\n"
        "else\n"
        "  printf 'BAD-STDIN: %s\\n' \"$line\" >&2\n"
        "  exit 3\n"
        "fi\n"
    )

    result = Runner(binary=str(_script(tmp_path, body))).run(
        [], stdin_bytes=b"file '/tmp/a.mp4'\n"
    )

    assert result.exit_code == 0
    assert result.error is None


def test_progress_flags_appended_to_argv(tmp_path: Path) -> None:
    """Given any argv; then -progress pipe:1 -nostats are appended, argv intact."""
    body = 'for a in "$@"; do echo "ARG:$a" >&2; done\nexit 0\n'

    result = Runner(binary=str(_script(tmp_path, body))).run(
        ["-i", "in.mp4", "out.mp4"]
    )

    assert result.exit_code == 0
    assert "ARG:-i" in result.stderr_tail
    assert "ARG:in.mp4" in result.stderr_tail
    assert "ARG:out.mp4" in result.stderr_tail
    assert "ARG:-progress" in result.stderr_tail
    assert "ARG:pipe:1" in result.stderr_tail
    assert "ARG:-nostats" in result.stderr_tail
