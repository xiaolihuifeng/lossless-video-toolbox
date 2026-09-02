"""ffmpeg subprocess runner with streamed progress parsing plus cooperative cancel."""

from __future__ import annotations

import contextlib
import io
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from lossless_toolbox.ffmpeg_locator import resolve

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

_PROGRESS_FLAGS: Final[tuple[str, ...]] = ("-progress", "pipe:1", "-nostats")
_GRACE_SECONDS: Final[float] = 2.0
_STDERR_TAIL_BYTES: Final[int] = 4096
_STDERR_CHUNK_BYTES: Final[int] = 8192
_US_PER_SECOND: Final[int] = 1_000_000
_MS_PER_SECOND: Final[int] = 1_000


class ProgressEvent(BaseModel):
    """One parsed ``-progress`` block emitted by ffmpeg on stdout."""

    model_config = ConfigDict(frozen=True)

    frame: int | None = None
    speed: float | None = None
    out_time: float | None = None
    progress: float | None = None
    end: bool = False


class RunResult(BaseModel):
    """Outcome of one runner execution."""

    model_config = ConfigDict(frozen=True)

    exit_code: int
    cancelled: bool
    progress_events: tuple[ProgressEvent, ...]
    last_out_time: float | None
    duration: float
    stderr_tail: str
    error: str | None = None


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _parse_speed(value: str) -> float | None:
    try:
        return float(value.rstrip("x"))
    except ValueError:
        return None


def _scaled_int(value: str, divisor: int) -> float | None:
    whole = _parse_int(value)
    return None if whole is None else whole / divisor


def _progress_fraction(out_time: float | None, duration: float | None) -> float | None:
    if out_time is None or duration is None or duration <= 0:
        return None
    return min(max(out_time / duration, 0.0), 1.0)


def _iter_progress_events(
    lines: Iterator[str], duration: float | None
) -> Iterator[ProgressEvent]:
    frame: int | None = None
    speed: float | None = None
    out_time: float | None = None
    us_seen = False
    for raw_line in lines:
        key, sep, value = raw_line.strip().partition("=")
        if not sep:
            continue
        if key == "out_time_us":
            us_seen = True
            out_time = _scaled_int(value, _US_PER_SECOND)
        elif key == "out_time_ms":
            # ffmpeg 6.1.1 prints out_time_ms in microseconds (same as out_time_us).
            if not us_seen or out_time is None:
                out_time = _scaled_int(value, _MS_PER_SECOND)
        elif key == "frame":
            frame = _parse_int(value)
        elif key == "speed":
            speed = _parse_speed(value)
        elif key == "progress":
            yield ProgressEvent(
                frame=frame,
                speed=speed,
                out_time=out_time,
                progress=_progress_fraction(out_time, duration),
                end=value == "end",
            )
            frame = None
            speed = None
            out_time = None
            us_seen = False


class Runner:
    """Execute an ffmpeg argv, streaming progress with a thread-safe cancel.

    ``run`` expects a flags-only ``argv`` (the first token is an ffmpeg flag,
    not the binary); the resolved ``ffmpeg`` binary is prepended here, making
    this the single point in the stack that injects the executable. Pass
    ``binary`` to override resolution (unit tests use it to run a fake script).
    """

    def __init__(self, *, binary: str | None = None) -> None:
        """Create a runner with a clear cancel flag and no live process.

        Args:
            binary: Optional executable prepended instead of the resolved
                ``ffmpeg`` (test seam). When ``None``, the ffmpeg_locator
                resolves it on every ``run``.
        """
        self._binary = binary
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        duration: float | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> RunResult:
        """Run ``argv`` to completion (or cancellation) and report the result."""
        binary = (
            self._binary if self._binary is not None else str(resolve("ffmpeg").path)
        )
        full_argv = [binary, *argv, *_PROGRESS_FLAGS]
        self._cancel_event.clear()
        started = time.monotonic()
        events: list[ProgressEvent] = []

        proc = subprocess.Popen(  # noqa: S603 — argv list, shell is never True
            full_argv,
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with self._lock:
            self._proc = proc

        stderr_tail = bytearray()
        threads = [
            threading.Thread(
                target=self._drain_stdout,
                args=(proc, events, on_progress, duration),
                daemon=True,
            ),
            threading.Thread(
                target=self._drain_stderr, args=(proc, stderr_tail), daemon=True
            ),
        ]
        if stdin_bytes is not None:
            threads.append(
                threading.Thread(
                    target=self._write_stdin, args=(proc, stdin_bytes), daemon=True
                )
            )
        for thread in threads:
            thread.start()

        exit_code = proc.wait()
        for thread in threads:
            thread.join()

        with self._lock:
            self._proc = None

        cancelled = self._cancel_event.is_set()
        elapsed = time.monotonic() - started
        stderr_text = stderr_tail.decode("utf-8", errors="replace")
        return RunResult(
            exit_code=exit_code,
            cancelled=cancelled,
            progress_events=tuple(events),
            last_out_time=_last_out_time(events),
            duration=elapsed,
            stderr_tail=stderr_text,
            error=_build_error(exit_code, cancelled, stderr_text, elapsed),
        )

    def cancel(self) -> None:
        """Terminate the child, then hard-kill it after the grace period."""
        self._cancel_event.set()
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _drain_stdout(
        self,
        proc: subprocess.Popen[bytes],
        events: list[ProgressEvent],
        on_progress: Callable[[ProgressEvent], None] | None,
        duration: float | None,
    ) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        with io.TextIOWrapper(stdout, encoding="utf-8", errors="replace") as lines:
            for event in _iter_progress_events(lines, duration):
                events.append(event)
                if on_progress is not None:
                    on_progress(event)

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen[bytes], tail: bytearray) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        for chunk in iter(lambda: stderr.read(_STDERR_CHUNK_BYTES), b""):
            tail.extend(chunk)
            del tail[:-_STDERR_TAIL_BYTES]

    @staticmethod
    def _write_stdin(proc: subprocess.Popen[bytes], data: bytes) -> None:
        stdin = proc.stdin
        if stdin is None:
            return
        with contextlib.suppress(OSError):
            stdin.write(data)
        with contextlib.suppress(OSError):
            stdin.close()


def _last_out_time(events: list[ProgressEvent]) -> float | None:
    for event in reversed(events):
        if event.out_time is not None:
            return event.out_time
    return None


def _build_error(
    exit_code: int, cancelled: bool, stderr_tail: str, elapsed: float
) -> str | None:
    if cancelled:
        return f"cancelled after {elapsed:.3f}s"
    if exit_code != 0:
        detail = f": {stderr_tail.strip()}" if stderr_tail.strip() else ""
        return f"ffmpeg exited with code {exit_code}{detail}"
    return None
