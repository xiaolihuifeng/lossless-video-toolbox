"""Unit tests for the soft-subtitle mux/detach matrix.

Locks the full subtitle container matrix (plan todo 9 / R1 §1.5): the one
place in the lossless toolbox where a ``copy`` operation silently becomes a
text-level transcode. MP4's only native text format is ``mov_text``, so SRT
muxed into MP4 is re-encoded (content-preserving, not stream-copy) and must
carry ``transcode_warning``. No ffmpeg is spawned here — only argv
construction, the ``transcode_warning`` flag, and the construction-time
rejections are asserted.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from lossless_toolbox.ops.subtitles import (
    DetachSpec,
    MuxSpec,
    SubtitleUnsupportedError,
    _detach_codec,
    _mux_codec,
)

pytestmark = pytest.mark.unit

SubFmt = Literal["srt", "ass", "webvtt"]

_BASE = ["-hide_banner", "-nostdin", "-y"]


def _mux(sub_fmt: SubFmt, out: str) -> MuxSpec:
    """Build a MuxSpec against an MP4 source and the given target container."""
    return MuxSpec(
        in_path=Path("in.mp4"),
        sub_path=Path(f"subs.{sub_fmt}"),
        sub_fmt=sub_fmt,
        out_path=Path(out),
    )


def _detach(source_codec: str) -> DetachSpec:
    """Build a DetachSpec for the default subtitle stream."""
    return DetachSpec(in_path=Path("in.mkv"), out_path=Path("out.srt"))


# ─────────────────────────────────────────────────────────────────────────────
# Mux matrix — copy paths (no transcode warning)
# ─────────────────────────────────────────────────────────────────────────────


def test_mux_srt_to_mkv_copies_without_warning() -> None:
    """Given SRT into an MKV target; then `-c:s copy` and no warning."""
    spec = _mux("srt", "out.mkv")
    argv = spec.build_argv()
    assert spec.transcode_warning is False
    assert argv == [
        *_BASE,
        "-i", "in.mp4",
        "-i", "subs.srt",
        "-map", "0",
        "-map", "1",
        "-c", "copy",
        "-c:s", "copy",
        "out.mkv",
    ]


def test_mux_ass_to_mkv_copies_without_warning() -> None:
    """Given ASS into an MKV target; then `-c:s copy` and no warning."""
    spec = _mux("ass", "out.mkv")
    argv = spec.build_argv()
    assert spec.transcode_warning is False
    assert argv[-2] == "copy"
    assert "-c:s" in argv
    assert "mov_text" not in argv


def test_mux_webvtt_to_webm_copies_without_warning() -> None:
    """Given WebVTT into a WebM target; then `-c:s copy` and no warning."""
    spec = _mux("webvtt", "out.webm")
    argv = spec.build_argv()
    assert spec.transcode_warning is False
    assert argv[-2] == "copy"


# ─────────────────────────────────────────────────────────────────────────────
# Mux matrix — transcode path (MP4 + srt → mov_text)
# ─────────────────────────────────────────────────────────────────────────────


def test_mux_srt_to_mp4_transcodes_to_mov_text_with_warning() -> None:
    """Given SRT into an MP4 target; then `-c:s mov_text` and a warning."""
    spec = _mux("srt", "out.mp4")
    argv = spec.build_argv()
    assert spec.transcode_warning is True
    assert argv[-2] == "mov_text"
    assert "mov_text" in argv


def test_mux_srt_to_mov_transcodes_to_mov_text() -> None:
    """Given SRT into a MOV target (mov family); then mov_text + warning."""
    spec = _mux("srt", "out.mov")
    argv = spec.build_argv()
    assert spec.transcode_warning is True
    assert "mov_text" in argv


# ─────────────────────────────────────────────────────────────────────────────
# Mux matrix — construction-time rejections
# ─────────────────────────────────────────────────────────────────────────────


def test_mux_ass_to_mp4_rejected_at_construction(tmp_path: Path) -> None:
    """Given ASS into an MP4 target; then construction raises with guidance."""
    ass_file = tmp_path / "subs.ass"
    ass_file.write_text("[Script Info]\n", encoding="utf-8")
    with pytest.raises(SubtitleUnsupportedError) as excinfo:
        MuxSpec(
            in_path=Path("in.mp4"),
            sub_path=ass_file,
            sub_fmt="ass",
            out_path=Path("out.mp4"),
        )
    message = str(excinfo.value)
    assert "ass" in message
    assert "MKV" in message
    assert "mov_text" in message


def test_mux_webvtt_to_mp4_rejected_at_construction() -> None:
    """Given WebVTT into an MP4 target; then construction raises."""
    with pytest.raises(SubtitleUnsupportedError):
        _mux("webvtt", "out.mp4")


def test_mux_srt_to_webm_rejected_at_construction() -> None:
    """Given SRT into a WebM target (WebM carries only WebVTT); then raise."""
    with pytest.raises(SubtitleUnsupportedError):
        _mux("srt", "out.webm")


def test_mux_ass_to_webm_rejected_at_construction() -> None:
    """Given ASS into a WebM target; then raise."""
    with pytest.raises(SubtitleUnsupportedError):
        _mux("ass", "out.webm")


def test_dvb_subtitle_to_mp4_rejected() -> None:
    """Given a dvb_subtitle source into MP4; then the matrix rejects it."""
    with pytest.raises(SubtitleUnsupportedError):
        _mux_codec("mp4", "dvb_subtitle")


# ─────────────────────────────────────────────────────────────────────────────
# Detach matrix — copy vs text-conversion
# ─────────────────────────────────────────────────────────────────────────────


def test_detach_subrip_copies_without_warning() -> None:
    """Given a SubRip source; then `-c:s copy -f srt` and no warning."""
    spec = _detach("subrip")
    argv = spec.build_argv("subrip")
    assert spec.transcode_warning is False
    assert argv == [
        *_BASE,
        "-i", "in.mkv",
        "-map", "0:s:0",
        "-c:s", "copy",
        "-f", "srt",
        "out.srt",
    ]


def test_detach_srt_copies_without_warning() -> None:
    """Given an `srt` codec alias; then copy (no warning)."""
    spec = _detach("srt")
    spec.build_argv("srt")
    assert spec.transcode_warning is False


def test_detach_mov_text_converts_with_warning() -> None:
    """Given a mov_text source; then `-c:s text` conversion with a warning."""
    spec = _detach("mov_text")
    argv = spec.build_argv("mov_text")
    assert spec.transcode_warning is True
    assert argv[-4] == "text"
    assert argv[-3:] == ["-f", "srt", "out.srt"]


def test_detach_ass_converts_with_warning() -> None:
    """Given an ASS source; then text conversion with a warning."""
    spec = _detach("ass")
    argv = spec.build_argv("ass")
    assert spec.transcode_warning is True
    assert "-c:s" in argv
    assert "text" in argv


def test_detach_dvb_subtitle_rejected() -> None:
    """Given a bitmap source (dvb_subtitle); then detach to SRT is rejected."""
    with pytest.raises(SubtitleUnsupportedError):
        _detach_codec("dvb_subtitle")


def test_detach_stream_index_maps_requested_stream() -> None:
    """Given stream_index=2; then `-map 0:s:2` is emitted."""
    spec = DetachSpec(
        in_path=Path("in.mkv"),
        out_path=Path("out.srt"),
        stream_index=2,
    )
    argv = spec.build_argv("subrip")
    assert "-map" in argv
    assert "0:s:2" in argv


def test_detach_stream_index_negative_rejected() -> None:
    """Given a negative stream index; then validation rejects it."""
    with pytest.raises(ValidationError):
        DetachSpec(in_path=Path("in.mkv"), out_path=Path("o.srt"), stream_index=-1)


# ─────────────────────────────────────────────────────────────────────────────
# Spec identity / union shape
# ─────────────────────────────────────────────────────────────────────────────


def test_specs_expose_kind_discriminator() -> None:
    """Given each spec type; then its `kind` discriminator is fixed."""
    assert _mux("srt", "out.mkv").kind == "mux"
    assert _detach("subrip").kind == "detach"


def test_mux_spec_defaults_transcode_warning_false() -> None:
    """Given a fresh MuxSpec; then transcode_warning defaults to False."""
    spec = MuxSpec(
        in_path=Path("in.mp4"),
        sub_path=Path("s.srt"),
        sub_fmt="srt",
        out_path=Path("out.mkv"),
    )
    assert spec.transcode_warning is False
