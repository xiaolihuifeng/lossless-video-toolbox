"""Zero-re-encode safety net: scan every ops argv builder (plan todo 5).

The lossless toolbox has one iron rule (plan "Must NOT have"): no operation may
emit a video or audio encoder. Every ``ffmpeg`` argv produced by the six ops
modules (cut / remux / merge / tracks / subtitles / meta) is therefore scanned
here for ``-c:v`` / ``-c:a`` (plus the global ``-c`` and positional ``-c:N``)
flags whose value is anything other than ``copy``.

The only two sanctioned exceptions are:

* the MP4 subtitle text transcode (``-c:s mov_text``), emitted only when the
  :class:`~lossless_toolbox.ops.subtitles.MuxSpec` ``transcode_warning`` is set;
* the one-shot PNG cover-art wrap (``-c:v:1 png``) from
  :class:`~lossless_toolbox.ops.meta.CoverSpec`.

A video/audio encoder outside these two is a test failure ("白名单外出现任何
-c:v/-c:a 非 copy 值即测试失败"). This file also locks the srt/mkv -> mp4 remux
block: instead of silently transcoding the subtitle, the remux spec must raise
:class:`~lossless_toolbox.ops.remux.SubtitleIncompatibleError`.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from lossless_toolbox.models import StreamInfo
from lossless_toolbox.ops.cut import CutSpec
from lossless_toolbox.ops.merge import _build_argv
from lossless_toolbox.ops.meta import (
    ChapterArg,
    ChaptersSpec,
    CoverSpec,
    MetadataEditSpec,
    RotateSpec,
)
from lossless_toolbox.ops.remux import RemuxSpec, SubtitleIncompatibleError
from lossless_toolbox.ops.subtitles import DetachSpec, MuxSpec
from lossless_toolbox.ops.tracks import ExtractSpec, ReplaceSpec, StripSpec

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.unit


def _stream(index: int, codec_type: str, codec_name: str) -> StreamInfo:
    """Build a minimal typed stream for argv construction (no ffprobe)."""
    return StreamInfo(
        index=index,
        codec_type=codec_type,
        codec_name=codec_name,
        disposition={},
    )


H264_AAC = [_stream(0, "video", "h264"), _stream(1, "audio", "aac")]
H264_AAC_SRT = [*H264_AAC, _stream(2, "subtitle", "subrip")]


def _codec_pairs(argv: list[str]) -> list[tuple[str, str]]:
    """Return every ``-c ...`` / ``-c:<spec> ...`` codec (flag, value) pair."""
    pairs: list[tuple[str, str]] = []
    for index in range(len(argv) - 1):
        flag = argv[index]
        if flag == "-c" or flag.startswith("-c:"):
            pairs.append((flag, argv[index + 1]))
    return pairs


def _is_subtitle_flag(flag: str) -> bool:
    """Return True for a subtitle-stream codec flag (``-c:s`` / ``-c:s:N``)."""
    return flag == "-c:s" or flag.startswith("-c:s:")


def _av_transcodes(argv: list[str]) -> list[tuple[str, str]]:
    """Return the codec pairs that would re-encode video or audio."""
    return [
        (flag, value)
        for flag, value in _codec_pairs(argv)
        if value != "copy" and not _is_subtitle_flag(flag)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# argv builders for each spec (one per spec/plan builder across six modules)
# ─────────────────────────────────────────────────────────────────────────────


def _cut_argv() -> list[str]:
    spec = CutSpec(
        in_path=Path("/in.mp4"),
        start=1.3,
        end=5.1,
        out_path=Path("/out.mp4"),
        keyframe_index=[0.0, 2.0, 4.0, 6.0, 8.0, 10.0],
        duration=12.0,
    )
    return spec.build_plan().argv


def _remux_argv() -> list[str]:
    spec = RemuxSpec(
        in_path=Path("/in.mp4"), out_path=Path("/out.mkv"), streams=H264_AAC
    )
    return spec.build_argv()


def _merge_argv() -> list[str]:
    return _build_argv(Path("/out.mp4"), H264_AAC, Path("/in0.mp4"))


def _extract_argv() -> list[str]:
    spec = ExtractSpec(
        in_path=Path("/in.mp4"),
        stream_index=0,
        out_path=Path("/out.m4a"),
        streams=H264_AAC,
    )
    return spec.build_argv()


def _strip_argv() -> list[str]:
    spec = StripSpec(
        in_path=Path("/in.mp4"),
        out_path=Path("/out.mp4"),
        keep_streams=[0, 1],
        streams=H264_AAC,
    )
    return spec.build_argv()


def _mux_mkv_argv() -> list[str]:
    spec = MuxSpec(
        in_path=Path("/in.mp4"),
        sub_path=Path("/subs.srt"),
        sub_fmt="srt",
        out_path=Path("/out.mkv"),
    )
    return spec.build_argv()


def _mux_mp4_mov_text_argv() -> list[str]:
    spec = MuxSpec(
        in_path=Path("/in.mp4"),
        sub_path=Path("/subs.srt"),
        sub_fmt="srt",
        out_path=Path("/out.mp4"),
    )
    return spec.build_argv()


def _detach_argv() -> list[str]:
    spec = DetachSpec(in_path=Path("/in.mkv"), out_path=Path("/out.srt"))
    return spec.build_argv("subrip")


def _metadata_edit_argv() -> list[str]:
    spec = MetadataEditSpec(
        in_path=Path("/in.mp4"), out_path=Path("/out.mp4"), title="Movie"
    )
    return spec.build_argv()


def _chapters_argv() -> list[str]:
    spec = ChaptersSpec(
        in_path=Path("/in.mp4"),
        out_path=Path("/out.mp4"),
        chapters=[ChapterArg(start_time=0.0, title="Intro", end_time=1.0)],
    )
    try:
        return spec.build_argv()
    finally:
        spec.cleanup()


def _rotate_argv() -> list[str]:
    spec = RotateSpec(in_path=Path("/in.mp4"), out_path=Path("/out.mp4"), degrees=90)
    return spec.build_argv()


# Every spec builder that must be zero-encoder for video/audio. CoverSpec and
# ReplaceSpec are exercised in their own tests (the former is the whitelisted
# exception, the latter needs a fake ffmpeg for the muxer probe).
_SPEC_BUILDERS = [
    ("cut", _cut_argv),
    ("remux", _remux_argv),
    ("merge", _merge_argv),
    ("tracks.extract", _extract_argv),
    ("tracks.strip", _strip_argv),
    ("subtitles.mux.mkv", _mux_mkv_argv),
    ("subtitles.mux.mp4.mov_text", _mux_mp4_mov_text_argv),
    ("subtitles.detach", _detach_argv),
    ("meta.metadata_edit", _metadata_edit_argv),
    ("meta.chapters", _chapters_argv),
    ("meta.rotate", _rotate_argv),
]


# ─────────────────────────────────────────────────────────────────────────────
# The core guard: no video/audio encoder, anywhere
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "build"),
    _SPEC_BUILDERS,
    ids=[name for name, _ in _SPEC_BUILDERS],
)
def test_spec_never_reencodes_video_or_audio(
    name: str, build: Callable[[], list[str]]
) -> None:
    """Scan each builder: a non-copy video/audio codec is a test failure."""
    argv = build()
    assert _codec_pairs(argv), f"{name}: argv has no codec flag (vacuous scan)"
    transcodes = _av_transcodes(argv)
    assert not transcodes, (
        f"{name}: video/audio encoder outside whitelist: {transcodes}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Whitelist 1: mov_text subtitle transcode, only when transcode_warning is set
# ─────────────────────────────────────────────────────────────────────────────


def test_mov_text_transcode_only_when_warning() -> None:
    """srt -> mp4 emits ``-c:s mov_text`` (subtitle, not video/audio) + warning."""
    spec = MuxSpec(
        in_path=Path("/in.mp4"),
        sub_path=Path("/subs.srt"),
        sub_fmt="srt",
        out_path=Path("/out.mp4"),
    )
    argv = spec.build_argv()
    assert spec.transcode_warning is True
    assert _av_transcodes(argv) == []
    assert "-c:s" in argv
    assert argv[argv.index("-c:s") + 1] == "mov_text"


def test_mkv_subtitle_is_copied_not_transcoded() -> None:
    """srt -> mkv copies the subtitle, emits no mov_text and no warning."""
    spec = MuxSpec(
        in_path=Path("/in.mp4"),
        sub_path=Path("/subs.srt"),
        sub_fmt="srt",
        out_path=Path("/out.mkv"),
    )
    argv = spec.build_argv()
    assert spec.transcode_warning is False
    assert "-c:s" in argv
    assert argv[argv.index("-c:s") + 1] == "copy"
    assert "mov_text" not in argv


# ─────────────────────────────────────────────────────────────────────────────
# Whitelist 2: attached_pic one-shot PNG wrap, CoverSpec only
# ─────────────────────────────────────────────────────────────────────────────


def test_cover_spec_only_whitelisted_video_codec_is_png() -> None:
    """Cover art is the one sanctioned non-copy video codec: ``-c:v:1 png``."""
    spec = CoverSpec(
        in_path=Path("/in.mp4"),
        out_path=Path("/out.mp4"),
        image_path=Path("/cover.png"),
    )
    argv = spec.build_argv()
    assert _av_transcodes(argv) == [("-c:v:1", "png")]
    assert "-disposition:v:1" in argv
    assert "attached_pic" in argv


# ─────────────────────────────────────────────────────────────────────────────
# tracks.replace (needs a fake ffmpeg for the muxer capability probe)
# ─────────────────────────────────────────────────────────────────────────────

_HINTS_ONLY_MP4 = """\
Muxer mp4 [MP4 (MPEG-4 Part 14)]:
    Default video codec: h264.
    Default audio codec: aac.
"""


def _fake_ffmpeg(tmp_path: Path) -> Path:
    """Create an executable fake ffmpeg whose muxer help is inconclusive."""
    script = tmp_path / "fake_ffmpeg"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{_HINTS_ONLY_MP4}\nEOF\n")
    script.chmod(0o755)
    return script


def test_replace_spec_never_reencodes(tmp_path: Path) -> None:
    """tracks.replace maps 0:v and 1:a:0 with ``-c copy``, no encoder."""
    fake = _fake_ffmpeg(tmp_path)
    spec = ReplaceSpec(
        in_path=Path("/in.mp4"),
        out_path=Path("/out.mp4"),
        new_audio_path=Path("/new.m4a"),
    )
    argv = spec.build_argv("aac", probe_bin=fake)
    assert _av_transcodes(argv) == []


# ─────────────────────────────────────────────────────────────────────────────
# The remux subtitle block: reject, never silently transcode
# ─────────────────────────────────────────────────────────────────────────────


def test_srt_to_mp4_remux_blocks_instead_of_transcoding() -> None:
    """srt/mkv -> mp4 must raise SubtitleIncompatibleError, never emit argv."""
    spec = RemuxSpec(
        in_path=Path("/in.mkv"),
        out_path=Path("/out.mp4"),
        streams=H264_AAC_SRT,
    )
    with pytest.raises(SubtitleIncompatibleError) as exc:
        spec.build_argv()
    assert "mov_text" in str(exc.value)


def test_mov_text_to_mp4_remux_is_allowed() -> None:
    """mov_text already matches MP4, so the remux proceeds losslessly."""
    streams = [*H264_AAC, _stream(2, "subtitle", "mov_text")]
    spec = RemuxSpec(
        in_path=Path("/in.mkv"),
        out_path=Path("/out.mp4"),
        streams=streams,
    )
    argv = spec.build_argv()
    assert _av_transcodes(argv) == []
