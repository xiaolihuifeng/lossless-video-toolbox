"""Unit tests for the keyframe-aligned lossless cut plan builder.

These tests exercise the pure snapping / validation / argv assembly of
:class:`CutSpec.build_plan` against synthetic paths and a synthetic keyframe
index. No ffmpeg process is spawned here (that is the integration test's job),
so the suite stays fast and hermetic while locking the keyframe-snap contract,
the ``-ss``-before-``-i`` argv shape, the ``avoid_negative_ts`` degradation and
the two error classes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lossless_toolbox.ops.cut import (
    CutRangeError,
    CutSpec,
    UnsupportedInputError,
)

pytestmark = pytest.mark.unit

# GOP = 2s over a 12s synthetic fixture (todo 2): keyframes every 2 seconds.
_KEYFRAMES = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]


def _spec(start: float, end: float, **overrides: object) -> CutSpec:
    """Build a CutSpec over the synthetic 12s H.264 corpus, with overrides."""
    fields: dict[str, object] = {
        "in_path": Path("/in.mp4"),
        "start": start,
        "end": end,
        "out_path": Path("/out.mp4"),
        "keyframe_index": _KEYFRAMES,
        "duration": 12.0,
    }
    fields.update(overrides)
    return CutSpec(**fields)  # type: ignore[arg-type]


def test_snap_cut_points_forward_to_keyframes() -> None:
    """Given cut(1.3, 5.1) with a 2s GOP; then points snap forward to (2.0, 6.0)."""
    plan = _spec(1.3, 5.1).build_plan()
    assert plan.actual_start == 2.0
    assert plan.actual_end == 6.0


def test_argv_ss_precedes_input_and_carries_snap() -> None:
    """Given cut(1.3, 5.1); then -ss comes before -i and the argv is exact."""
    plan = _spec(1.3, 5.1).build_plan()
    assert plan.argv.index("-ss") < plan.argv.index("-i")
    assert plan.argv == [
        "-hide_banner", "-nostdin", "-y",
        "-ss", "2",
        "-avoid_negative_ts", "make_zero",
        "-i", "/in.mp4",
        "-c", "copy",
        "-t", "4",
        "-movflags", "+faststart",
        "/out.mp4",
    ]


def test_end_beyond_duration_clamps_to_last_keyframe() -> None:
    """Given end > duration; then actual_end clamps to the last keyframe."""
    plan = _spec(1.3, 13.0).build_plan()
    assert plan.actual_end == 10.0


def test_mov_output_adds_faststart() -> None:
    """Given a .mov output; then -movflags +faststart is emitted."""
    plan = _spec(1.3, 5.1, out_path=Path("/out.mov")).build_plan()
    assert plan.argv[-3:] == ["-movflags", "+faststart", "/out.mov"]


def test_mkv_output_omits_faststart() -> None:
    """Given an .mkv output; then -movflags is omitted."""
    plan = _spec(1.3, 5.1, out_path=Path("/out.mkv")).build_plan()
    assert "-movflags" not in plan.argv


def test_attached_pic_degrades_avoid_negative_ts_to_auto() -> None:
    """Given has_attached_pic; then -avoid_negative_ts is auto, not make_zero."""
    plan = _spec(1.3, 5.1, has_attached_pic=True).build_plan()
    assert plan.argv[plan.argv.index("-avoid_negative_ts") + 1] == "auto"


def test_no_head_cut_omits_avoid_negative_ts() -> None:
    """Given a cut starting at zero; then no timestamp-shift flag is emitted."""
    plan = _spec(0.0, 5.1).build_plan()
    assert "-avoid_negative_ts" not in plan.argv
    assert plan.actual_start == 0.0


def test_negative_start_raises_cut_range_error() -> None:
    """Given start < 0; then CutRangeError."""
    with pytest.raises(CutRangeError):
        _spec(-1.0, 5.1).build_plan()


def test_end_at_or_before_start_raises_cut_range_error() -> None:
    """Given end <= start; then CutRangeError (the failure-path fixture case)."""
    with pytest.raises(CutRangeError):
        _spec(9.0, 3.0).build_plan()


def test_start_at_duration_raises_cut_range_error() -> None:
    """Given start >= duration; then CutRangeError."""
    with pytest.raises(CutRangeError):
        _spec(12.0, 13.0).build_plan()


def test_nonzero_format_start_time_raises_unsupported_input() -> None:
    """Given format_start_time > 0; then UnsupportedInputError with nonzero hint."""
    with pytest.raises(UnsupportedInputError) as exc:
        _spec(1.3, 5.1, format_start_time=31.376778).build_plan()
    assert "nonzero" in str(exc.value)
