"""Integration tests: real ffmpeg batch remux through the JobQueue.

Three files are remuxed through the queue — the middle one is a deliberately
bad input (a path that does not exist) — to prove the batch survives a
mid-queue failure and preserves submission order. A real ``ffmpeg`` process is
driven through a thin subprocess runner; the argv is built by the ops engine
(:class:`~lossless_toolbox.ops.remux.RemuxSpec.build_argv`), exactly as the
queue's contract requires (the queue never rewrites argv).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, Protocol

import pytest

from lossless_toolbox.ops.cut import CutSpec
from lossless_toolbox.ops.remux import RemuxSpec
from lossless_toolbox.probe import keyframes, probe
from lossless_toolbox.queue import JobQueue, RunResult
from lossless_toolbox.runner import Runner

pytestmark = pytest.mark.integration

if TYPE_CHECKING:
    from pathlib import Path


class _MediaSample(Protocol):
    """Shape of the conftest media fixture objects (todo 2)."""

    path: Path
    codec: str
    duration: float


class _SubprocessRunner:
    """Thin real-process runner: synchronous ffmpeg via ``subprocess.run``."""

    def __init__(self, ffmpeg: str) -> None:
        self._ffmpeg = ffmpeg

    def run(
        self,
        argv: list[str],
        *,
        stdin_bytes: bytes | None = None,
        duration: float | None = None,
    ) -> RunResult:
        proc = subprocess.run(  # noqa: S603 - argv from ops engine, no shell
            [self._ffmpeg, *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return RunResult(
            exit_code=proc.returncode,
            stderr_tail=proc.stderr[-4096:],
        )

    def cancel(self) -> None:
        """Not exercised here; cancellation is covered by the unit tests."""


def test_batch_remux_bad_middle_file_survives(
    tmp_path: Path,
    h264_aac_mp4: _MediaSample,
    hevc_aac_mkv: _MediaSample,
) -> None:
    """Given 3 remux jobs with a bad middle; when run; then 2 done 1 failed."""
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None

    out1 = tmp_path / "one.mkv"
    out2 = tmp_path / "two.mkv"
    out3 = tmp_path / "three.mkv"
    missing = tmp_path / "missing_input.mp4"

    good1 = RemuxSpec(
        in_path=h264_aac_mp4.path,
        out_path=out1,
        streams=probe(h264_aac_mp4.path).streams,
    )
    bad = RemuxSpec(in_path=missing, out_path=out2, streams=[])
    good3 = RemuxSpec(
        in_path=hevc_aac_mkv.path,
        out_path=out3,
        streams=probe(hevc_aac_mkv.path).streams,
    )

    start_order: list[int] = []

    queue = JobQueue[RemuxSpec](
        runner_factory=lambda: _SubprocessRunner(ffmpeg),
        argv_builder=RemuxSpec.build_argv,
        on_job_started=lambda job: start_order.append(job.id),
    )

    queue.submit([good1, bad, good3])
    result = queue.run()

    assert [r.id for r in result] == [1, 2, 3]
    assert start_order == [1, 2, 3]
    assert result[0].status == "done"
    assert result[1].status == "failed"
    assert result[2].status == "done"

    assert result[1].error is not None
    assert "missing_input.mp4" in result[1].error

    assert out1.is_file()
    assert not out2.exists()
    assert out3.is_file()


def test_queue_forwards_cut_duration_to_real_runner(
    tmp_path: Path, h264_aac_mp4: _MediaSample
) -> None:
    """Given a CutSpec through the queue; then the real runner gets its duration.

    Proves the full production chain (queue -> spec duration -> real
    ``Runner.run``) with a genuine ffmpeg process: the job finishes and the
    cut output exists, while the recorded duration equals the probed media
    duration the spec carries (F2-M1).
    """
    source = probe(h264_aac_mp4.path)
    kf = keyframes(h264_aac_mp4.path)
    out = tmp_path / "cut.mkv"
    spec = CutSpec(
        in_path=h264_aac_mp4.path,
        start=1.0,
        end=5.0,
        out_path=out,
        keyframe_index=kf.times,
        duration=source.duration,
    )
    received: list[float | None] = []

    class _RecordingRunner:
        def __init__(self, inner: Runner) -> None:
            self._inner = inner

        def run(
            self,
            argv: list[str],
            *,
            stdin_bytes: bytes | None = None,
            duration: float | None = None,
        ) -> RunResult:
            received.append(duration)
            return self._inner.run(argv, stdin_bytes=stdin_bytes, duration=duration)

        def cancel(self) -> None:
            self._inner.cancel()

    queue = JobQueue[CutSpec](
        runner_factory=lambda: _RecordingRunner(Runner()),
        argv_builder=CutSpec.build_argv,
    )
    queue.submit([spec])
    result = queue.run()

    assert result[0].status == "done"
    assert received == [source.duration]
    assert out.is_file()
