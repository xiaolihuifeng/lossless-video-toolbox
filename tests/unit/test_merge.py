"""Unit tests for the lossless concat-demuxer merge plan builder.

These tests exercise the pure argv / concat-list / compatibility builders
against synthetic paths and typed ``StreamInfo`` values. No ffmpeg/ffprobe
process is spawned here (that is the integration test's job), so the suite
stays fast and hermetic while locking the concat-list escaping contract, the
argv shape, the disposition replay and the preflight difference report.
"""

# ruff: noqa: S108 - synthetic absolute paths feed pure builders; no real I/O
# pyright: reportPrivateUsage=false
from pathlib import Path

import pytest

from lossless_toolbox.models import StreamInfo
from lossless_toolbox.ops.merge import (
    MergeError,
    MergeSpec,
    _build_argv,
    _build_concat_list,
    _compare_stream_sets,
    _CompatStream,
    _disposition_args,
    _escape_concat_path,
    build_plan,
)

pytestmark = pytest.mark.unit


def _stream(
    index: int,
    codec_type: str,
    codec_name: str,
    disposition: dict[str, bool],
) -> StreamInfo:
    return StreamInfo(
        index=index,
        codec_type=codec_type,
        codec_name=codec_name,
        width=320 if codec_type == "video" else None,
        height=240 if codec_type == "video" else None,
        sample_rate=44100 if codec_type == "audio" else None,
        channels=1 if codec_type == "audio" else None,
        disposition=disposition,
    )


_VIDEO = _stream(0, "video", "h264", {"default": True, "forced": False})
_AUDIO = _stream(1, "audio", "aac", {"default": True, "forced": False})


# ─────────────────────────────────────────────────────────────────────────────
# concat-list escaping
# ─────────────────────────────────────────────────────────────────────────────


def test_escape_concat_path_prefixes_file_protocol() -> None:
    """Given an absolute path; then the entry URI is ``file:<abs>``."""
    assert _escape_concat_path(Path("/tmp/seg1.mp4")) == "file:/tmp/seg1.mp4"


def test_escape_concat_path_escapes_single_quotes() -> None:
    """Given a path containing a single quote; then it becomes the shell escape."""
    # file:/tmp/a'\''b.mp4  (close-quote, escaped quote, reopen-quote)
    assert _escape_concat_path(Path("/tmp/a'b.mp4")) == "file:/tmp/a'\\''b.mp4"


def test_build_concat_list_joins_file_entries() -> None:
    """Given two paths (one with a quote); then two ``file '...'`` lines."""
    assert _build_concat_list([Path("/tmp/a'b.mp4"), Path("/tmp/c.mp4")]) == (
        "file 'file:/tmp/a'\\''b.mp4'\n"
        "file 'file:/tmp/c.mp4'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# argv assembly
# ─────────────────────────────────────────────────────────────────────────────


def test_build_argv_assembles_concat_command() -> None:
    """Given two streams and an mp4 output; then the full concat argv is built."""
    argv = _build_argv(Path("/out/merged.mp4"), [_VIDEO, _AUDIO], Path("/tmp/seg1.mp4"))
    assert argv == [
        "-hide_banner",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-protocol_whitelist",
        "file,pipe,fd",
        "-i",
        "-",
        "-i",
        "/tmp/seg1.mp4",
        "-map",
        "0:0",
        "-c:0",
        "copy",
        "-map",
        "0:1",
        "-c:1",
        "copy",
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        "-disposition:0",
        "default",
        "-disposition:1",
        "default",
        "-movflags",
        "+faststart",
        "-ignore_unknown",
        "/out/merged.mp4",
    ]


def test_build_argv_omits_movflags_for_mkv_output() -> None:
    """Given an mkv output; then movflags is omitted (mp4/mov only)."""
    argv = _build_argv(Path("/out/merged.mkv"), [_VIDEO, _AUDIO], Path("/tmp/seg1.mp4"))
    assert "-movflags" not in argv
    assert argv[-1] == "/out/merged.mkv"


def test_build_argv_uses_first_real_file_as_metadata_input() -> None:
    """Given segments; then the metadata input is paths[0] (not the concat stdin)."""
    argv = _build_argv(Path("/out/merged.mp4"), [_VIDEO], Path("/data/first.mp4"))
    # input 0 is the concat stdin ("-i -"); input 1 is the first real file for
    # metadata, so the SECOND "-i" must be followed by the real file path.
    first_i = argv.index("-i")
    assert argv[first_i + 1] == "-"
    second_i = argv.index("-i", first_i + 1)
    assert argv[second_i + 1] == "/data/first.mp4"
    assert "-map_metadata" in argv
    assert "-map_chapters" in argv


# ─────────────────────────────────────────────────────────────────────────────
# disposition replay
# ─────────────────────────────────────────────────────────────────────────────


def test_disposition_args_replays_only_enabled_flags() -> None:
    """Given mixed dispositions; then only enabled flags are replayed."""
    streams = [
        _stream(0, "video", "h264", {"default": True, "forced": False}),
        _stream(1, "audio", "aac", {"default": False, "forced": True}),
    ]
    assert _disposition_args(streams) == [
        "-disposition:0",
        "default",
        "-disposition:1",
        "forced",
    ]


def test_disposition_args_skips_stream_with_no_flags() -> None:
    """Given a stream with no enabled dispositions; then nothing is emitted."""
    streams = [_stream(0, "video", "h264", {"default": False, "forced": False})]
    assert _disposition_args(streams) == []


# ─────────────────────────────────────────────────────────────────────────────
# compatibility preflight
# ─────────────────────────────────────────────────────────────────────────────


def _compat(**overrides: object) -> _CompatStream:
    fields: dict[str, object] = {
        "codec_name": "h264",
        "profile": "High",
        "width": 320,
        "height": 240,
        "pix_fmt": "yuv420p",
        "sample_rate": None,
        "channels": None,
    }
    fields.update(overrides)
    return _CompatStream(**fields)  # type: ignore[arg-type]


def test_compare_stream_sets_reports_field_differences() -> None:
    """Given a 320x240 vs 640x480 stream; then width and height differ."""
    ref = [_compat()]
    cand = [_compat(width=640, height=480)]
    assert _compare_stream_sets(ref, cand) == [
        "stream 0 width: 320 != 640",
        "stream 0 height: 240 != 480",
    ]


def test_compare_stream_sets_reports_codec_and_profile() -> None:
    """Given different codec/profile; then both fields are reported."""
    ref = [_compat()]
    cand = [_compat(codec_name="mpeg4", profile="Simple")]
    assert _compare_stream_sets(ref, cand) == [
        "stream 0 codec_name: 'h264' != 'mpeg4'",
        "stream 0 profile: 'High' != 'Simple'",
    ]


def test_compare_stream_sets_identical_is_clean() -> None:
    """Given identical stream sets; then no differences are reported."""
    assert _compare_stream_sets([_compat()], [_compat()]) == []


def test_compare_stream_sets_reports_stream_count_mismatch() -> None:
    """Given a differing stream count; then it is reported and fields skipped."""
    video = _compat()
    audio = _compat(
        codec_name="aac", profile="LC", width=None, height=None,
        pix_fmt=None, sample_rate=44100, channels=1,
    )
    assert _compare_stream_sets([video, audio], [video]) == ["stream count: 2 != 1"]


# ─────────────────────────────────────────────────────────────────────────────
# build_plan guards
# ─────────────────────────────────────────────────────────────────────────────


def test_build_plan_requires_at_least_two_inputs() -> None:
    """Given a single input; then build_plan raises MergeError before probing."""
    spec = MergeSpec(paths=[Path("/tmp/only.mp4")], out_path=Path("/out/merged.mp4"))
    with pytest.raises(MergeError, match="at least 2"):
        build_plan(spec)
