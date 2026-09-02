"""Integration tests: real ffmpeg keyframe-aligned lossless cut.

These tests depend on the media factory fixtures defined in
``tests/conftest.py`` (todo 2), consumed as function parameters. Each fixture
is a ``MediaSample`` exposing ``.path``, ``.codec`` and ``.duration``.

The cut is executed with the argv returned by :meth:`CutSpec.build_plan` and
the output is then verified with ffprobe: the first video packet must be a
keyframe with ``pts_time == 0`` (the ``-avoid_negative_ts make_zero`` shift),
the container duration must equal the snapped range, and every output stream
must carry the same codec/geometry as its source (stream copy, no re-encode).
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Protocol

import pytest

from lossless_toolbox.ops.cut import CutSpec, UnsupportedInputError
from lossless_toolbox.probe import keyframes, probe

pytestmark = pytest.mark.integration


class _MediaSample(Protocol):
    """Shape of the conftest media fixture objects (todo 2)."""

    path: Path
    codec: str
    duration: float


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — controlled invocation, no shell
        args, capture_output=True, text=True, check=False
    )


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    assert binary is not None, "ffmpeg not found on PATH"
    return binary


def _ffprobe() -> str:
    binary = shutil.which("ffprobe")
    assert binary is not None, "ffprobe not found on PATH"
    return binary


def _ffprobe_json(path: Path, args: list[str]) -> dict[str, Any]:
    """Run ffprobe and return the parsed JSON object."""
    proc = _run([_ffprobe(), "-v", "error", *args, "-of", "json", str(path)])
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _format_start_time(path: Path) -> float:
    raw = _ffprobe_json(path, ["-show_entries", "format=start_time"])
    return float(raw["format"]["start_time"])


def _first_video_packet(path: Path) -> dict[str, Any]:
    """Return the first video packet (pts_time + flags) of ``path``."""
    raw = _ffprobe_json(
        path,
        ["-select_streams", "v:0", "-show_packets",
         "-show_entries", "packet=pts_time,flags"],
    )
    return raw["packets"][0]


def test_cut_h264_aac_mp4_keyframe_aligned(
    tmp_path: Path, h264_aac_mp4: _MediaSample
) -> None:
    """Given cut(1.3, 5.1); then a 4s keyframe-aligned copy with pts 0."""
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
        format_start_time=_format_start_time(h264_aac_mp4.path),
    )
    plan = spec.build_plan()
    assert plan.actual_start == pytest.approx(2.0, abs=0.1)
    assert plan.actual_end == pytest.approx(6.0, abs=0.1)

    proc = _run([_ffmpeg(), *plan.argv])
    assert proc.returncode == 0, proc.stderr

    first = _first_video_packet(out)
    assert float(first["pts_time"]) == pytest.approx(0.0, abs=0.05)
    assert "K" in str(first["flags"])

    out_media = probe(out)
    assert out_media.duration == pytest.approx(4.0, abs=0.5)
    assert len(out_media.streams) == len(source.streams)
    for out_stream, src_stream in zip(
        out_media.streams, source.streams, strict=True
    ):
        assert out_stream.codec_name == src_stream.codec_name
        assert out_stream.width == src_stream.width
        assert out_stream.height == src_stream.height


def test_cut_nonzero_start_ts_raises_unsupported(
    nonzero_start_ts: _MediaSample, tmp_path: Path
) -> None:
    """Given a nonzero-start TS; then UnsupportedInputError before any command."""
    source = probe(nonzero_start_ts.path)
    kf = keyframes(nonzero_start_ts.path)
    spec = CutSpec(
        in_path=nonzero_start_ts.path,
        start=1.0,
        end=5.0,
        out_path=tmp_path / "unused-cut.mp4",
        keyframe_index=kf.times,
        duration=source.duration,
        format_start_time=_format_start_time(nonzero_start_ts.path),
    )
    with pytest.raises(UnsupportedInputError):
        spec.build_plan()
