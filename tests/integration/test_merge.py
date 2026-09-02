"""Integration tests for the concat-demuxer merge (todo 7).

The unit tests lock the pure argv / concat-list builders; here a real merge is
planned against two losslessly split segments and executed by feeding the
concat list over stdin, exactly as the CLI will. Asserts the preflight accepts
identical segments, the merged duration equals the sum of the segments (±0.5 s),
and every stream parameter survives the stream-copy path unchanged.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, Final, Protocol

import pytest

from lossless_toolbox.ops.merge import (
    MergePlan,
    MergeSpec,
    build_plan,
    check_concat_compatibility,
)
from lossless_toolbox.probe import probe

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

DURATION_TOLERANCE: Final[float] = 0.5
SPLIT_MIDPOINT: Final[float] = 6.0


class _MediaSample(Protocol):
    path: Path
    codec: str
    duration: float


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    assert binary is not None, "ffmpeg not found on PATH"
    return binary


def _split(source: Path, start: float, end: float, out: Path) -> Path:
    """Losslessly cut ``[start, end)`` out of ``source`` via stream copy."""
    proc = subprocess.run(  # noqa: S603 — controlled invocation, no shell
        [
            _ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-t",
            str(end - start),
            "-i",
            str(source),
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"split failed for {out.name}:\n{proc.stderr}"
    return out


def _merge(plan: MergePlan) -> None:
    """Run the planned ffmpeg argv, feeding the concat list over stdin."""
    proc = subprocess.run(  # noqa: S603 — controlled invocation, no shell
        [_ffmpeg(), *plan.argv],
        capture_output=True,
        text=True,
        input=plan.concat_list,
        check=False,
    )
    assert proc.returncode == 0, f"merge failed for {plan.argv[-1]}:\n{proc.stderr}"


def test_merge_two_segments_preserves_streams(
    h264_aac_mp4: _MediaSample, tmp_path: Path
) -> None:
    """Given two segments cut from one source; then the merged duration equals
    their sum and every stream parameter is preserved."""
    source = h264_aac_mp4.path
    seg1 = _split(source, 0.0, SPLIT_MIDPOINT, tmp_path / "seg1.mp4")
    seg2 = _split(source, SPLIT_MIDPOINT, h264_aac_mp4.duration, tmp_path / "seg2.mp4")

    report = check_concat_compatibility([seg1, seg2])
    assert report.ok, f"identical segments reported incompatible: {report.differences}"

    out_path = tmp_path / "merged.mp4"
    plan = build_plan(MergeSpec(paths=[seg1, seg2], out_path=out_path))
    assert plan.concat_list
    _merge(plan)

    seg1_info = probe(seg1)
    merged = probe(out_path)
    expected_duration = seg1_info.duration + probe(seg2).duration
    assert merged.duration == pytest.approx(expected_duration, abs=DURATION_TOLERANCE)

    assert len(merged.streams) == len(seg1_info.streams)
    for merged_stream, source_stream in zip(
        merged.streams, seg1_info.streams, strict=True
    ):
        assert merged_stream.codec_name == source_stream.codec_name
        assert merged_stream.codec_type == source_stream.codec_type
        assert merged_stream.width == source_stream.width
        assert merged_stream.height == source_stream.height
        assert merged_stream.sample_rate == source_stream.sample_rate
        assert merged_stream.channels == source_stream.channels
