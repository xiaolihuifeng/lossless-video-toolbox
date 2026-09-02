"""Unit tests for audio track extract/strip/replace argv construction.

Locks the three TrackSpec argv shapes (plan todo 8): the ADTS trap (bare
``.aac`` output needs ``-f adts``), per-stream copy for strip, and the
``-map 0:v`` / ``-map 1:a:0`` ordering for replace. No ffmpeg/ffprobe is
spawned for the extract/strip paths; the replace container-capability check is
exercised against a fake ffmpeg muxer help, so the whole module stays I/O-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lossless_toolbox.models import StreamInfo
from lossless_toolbox.ops.tracks import ExtractSpec, ReplaceSpec, StripSpec, TrackError

pytestmark = pytest.mark.unit

_BASE = ["-hide_banner", "-nostdin", "-y"]


def _stream(index: int, codec_type: str, codec_name: str) -> StreamInfo:
    """Build a minimal typed stream for argv construction (no ffprobe)."""
    return StreamInfo(
        index=index,
        codec_type=codec_type,
        codec_name=codec_name,
        disposition={},
    )


H264_AAC = [_stream(0, "video", "h264"), _stream(1, "audio", "aac")]


def _extract(out: str, stream_index: int = 0) -> ExtractSpec:
    """Build an ExtractSpec against the H.264+AAC fixture streams."""
    return ExtractSpec(
        in_path=Path("/in.mp4"),
        stream_index=stream_index,
        out_path=Path(out),
        streams=H264_AAC,
    )


def _strip(keep_streams: list[int], out: str = "/out.mp4") -> StripSpec:
    """Build a StripSpec against the H.264+AAC fixture streams."""
    return StripSpec(
        in_path=Path("/in.mp4"),
        out_path=Path(out),
        keep_streams=keep_streams,
        streams=H264_AAC,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Extract — -map 0:a:N -c copy, ADTS trap for bare .aac
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_m4a_exact_argv() -> None:
    """Given audio stream 0; when extracted to .m4a; then exact copy argv."""
    assert _extract("/out.m4a").build_argv() == [
        *_BASE,
        "-i", "/in.mp4",
        "-map", "0:a:0",
        "-c", "copy",
        "/out.m4a",
    ]


def test_extract_aac_adds_adts() -> None:
    """Given a bare .aac target; then -f adts is emitted (ADTS trap)."""
    assert _extract("/out.aac").build_argv() == [
        *_BASE,
        "-i", "/in.mp4",
        "-map", "0:a:0",
        "-c", "copy",
        "-f", "adts",
        "/out.aac",
    ]


def test_extract_stream_index_out_of_range_raises() -> None:
    """Given stream_index=9 beyond the audio count; then TrackError, no ffmpeg."""
    with pytest.raises(TrackError):
        _extract("/out.m4a", stream_index=9).build_argv()


def test_extract_negative_stream_index_rejected() -> None:
    """Given a negative stream index; then pydantic validation rejects it."""
    with pytest.raises(ValidationError):
        _extract("/out.m4a", stream_index=-1)


# ─────────────────────────────────────────────────────────────────────────────
# Strip — -map 0:<i> per keep, per-output-position copy
# ─────────────────────────────────────────────────────────────────────────────


def test_strip_keep_single_exact_argv() -> None:
    """Given keep=[0]; then per-stream copy of the video stream only."""
    assert _strip([0]).build_argv() == [
        *_BASE,
        "-i", "/in.mp4",
        "-map", "0:0",
        "-c:0", "copy",
        "-map_metadata", "0", "-ignore_unknown",
        "/out.mp4",
    ]


def test_strip_keep_all_per_stream_copy() -> None:
    """Given keep=[0,1]; then both streams mapped and copied per output position."""
    assert _strip([0, 1], "/out.mkv").build_argv() == [
        *_BASE,
        "-i", "/in.mp4",
        "-map", "0:0", "-map", "0:1",
        "-c:0", "copy", "-c:1", "copy",
        "-map_metadata", "0", "-ignore_unknown",
        "/out.mkv",
    ]


def test_strip_keep_out_of_range_raises() -> None:
    """Given keep=[0,5] beyond the stream count; then TrackError."""
    with pytest.raises(TrackError):
        _strip([0, 5]).build_argv()


def test_strip_keep_empty_raises() -> None:
    """Given keep=[]; then TrackError (stripping everything is meaningless)."""
    with pytest.raises(TrackError):
        _strip([]).build_argv()


# ─────────────────────────────────────────────────────────────────────────────
# Replace — -map 0:v -map 1:a:0 -c copy, container-capability gate
# ─────────────────────────────────────────────────────────────────────────────


def _fake_ffmpeg(tmp_path: Path, stdout: str) -> Path:
    """Create an executable fake ffmpeg that prints ``stdout`` and exits 0."""
    script = tmp_path / "fake_ffmpeg"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\n")
    script.chmod(0o755)
    return script


_HINTS_ONLY_MP4 = """\
Muxer mp4 [MP4 (MPEG-4 Part 14)]:
    Default video codec: h264.
    Default audio codec: aac.
"""


def test_replace_map_order_exact_argv(tmp_path: Path) -> None:
    """Given a compatible new audio; then -map 0:v precedes -map 1:a:0."""
    fake = _fake_ffmpeg(tmp_path, _HINTS_ONLY_MP4)
    spec = ReplaceSpec(
        in_path=Path("/in.mp4"),
        out_path=Path("/out.mp4"),
        new_audio_path=Path("/new.m4a"),
    )
    assert spec.build_argv("aac", probe_bin=fake) == [
        *_BASE,
        "-i", "/in.mp4",
        "-i", "/new.m4a",
        "-map", "0:v",
        "-map", "1:a:0",
        "-c", "copy",
        "/out.mp4",
    ]


def test_replace_incompatible_codec_raises(tmp_path: Path) -> None:
    """Given a muxer enumeration omitting the codec; then TrackError guidance."""
    enumerated = (
        "Muxer mp4 [MP4]:\n"
        "    Supported audio codecs:\n"
        "        aac\n"
        "        mp3\n"
    )
    fake = _fake_ffmpeg(tmp_path, enumerated)
    spec = ReplaceSpec(
        in_path=Path("/in.mp4"),
        out_path=Path("/out.mp4"),
        new_audio_path=Path("/new.mkv"),
    )
    with pytest.raises(TrackError) as exc:
        spec.build_argv("flac", probe_bin=fake)
    assert "flac" in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────────
# Spec identity / union shape
# ─────────────────────────────────────────────────────────────────────────────


def test_specs_expose_kind_discriminator() -> None:
    """Given each spec type; then its `kind` discriminator is fixed."""
    assert _extract("/out.m4a").kind == "extract"
    assert _strip([0]).kind == "strip"
    assert (
        ReplaceSpec(
            in_path=Path("/in.mp4"),
            out_path=Path("/out.mp4"),
            new_audio_path=Path("/new.m4a"),
        ).kind
        == "replace"
    )
