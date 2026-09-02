"""Integration tests: the runner driving real ffmpeg on synthetic media.

These tests reuse the media factory fixtures from ``tests/conftest.py`` (todo 2)
and the ``CutSpec`` argv builder (todo 6), then execute the cut through
:class:`~lossless_toolbox.runner.Runner` to assert streamed progress is
monotonic, the final timestamp matches the snapped cut range, and the output
probes as the expected duration.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from lossless_toolbox.ops.cut import CutSpec
from lossless_toolbox.probe import keyframes, probe
from lossless_toolbox.runner import ProgressEvent, Runner

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Protocol

    class _MediaSample(Protocol):
        path: Path
        codec: str
        duration: float

pytestmark = pytest.mark.integration


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    assert binary is not None, "ffmpeg not found on PATH"
    return binary


def test_runner_cut_h264_progress_monotonic_and_duration(
    tmp_path: Path, h264_aac_mp4: _MediaSample
) -> None:
    """Given a keyframe cut; then progress rises monotonically to completion."""
    source = probe(h264_aac_mp4.path)
    kf = keyframes(h264_aac_mp4.path)
    out = tmp_path / "cut.mp4"
    spec = CutSpec(
        in_path=h264_aac_mp4.path,
        start=1.3,
        end=5.1,
        out_path=out,
        keyframe_index=kf.times,
        duration=source.duration,
    )
    plan = spec.build_plan()
    expected = plan.actual_end - plan.actual_start

    received: list[ProgressEvent] = []
    result = Runner().run(
        [_ffmpeg(), *plan.argv], duration=expected, on_progress=received.append
    )

    assert result.exit_code == 0, result.error
    assert result.cancelled is False
    assert result.error is None
    assert out.is_file()

    progress_values = [e.progress for e in received if e.progress is not None]
    assert progress_values
    assert progress_values == sorted(progress_values)  # monotonic
    assert progress_values[-1] == pytest.approx(1.0, abs=0.1)

    assert result.last_out_time is not None
    assert result.last_out_time == pytest.approx(expected, abs=0.6)
    assert result.progress_events[-1].end is True
    assert result.duration >= 0

    out_media = probe(out)
    assert out_media.duration == pytest.approx(expected, abs=0.6)
