"""Unit tests for the lossless remux operation and common argv builders."""

from __future__ import annotations

from pathlib import Path

import pytest

from lossless_toolbox.models import StreamInfo
from lossless_toolbox.ops.remux import (
    CompatResult,
    RemuxSpec,
    SubtitleIncompatibleError,
    check_subtitle_compat,
    muxer_supports,
)

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
HEVC_AAC = [_stream(0, "video", "hevc"), _stream(1, "audio", "aac")]
H264_AAC_SRT = [*H264_AAC, _stream(2, "subtitle", "subrip")]


def test_mp4_to_mkv_exact_argv() -> None:
    """Given an H.264+AAC input; when remuxed to MKV; then argv is exact."""
    spec = RemuxSpec(
        in_path=Path("/in.mp4"), out_path=Path("/out.mkv"), streams=H264_AAC
    )
    assert spec.build_argv() == [
        "-hide_banner", "-nostdin", "-y",
        "-i", "/in.mp4",
        "-map", "0:0", "-map", "0:1",
        "-c:0", "copy", "-c:1", "copy",
        "-map_metadata", "0", "-ignore_unknown",
        "/out.mkv",
    ]


def test_mp4_target_adds_faststart() -> None:
    """Given an MKV input; when remuxed to MP4; then -movflags +faststart is emitted."""
    spec = RemuxSpec(
        in_path=Path("/in.mkv"), out_path=Path("/out.mp4"), streams=H264_AAC
    )
    argv = spec.build_argv()
    assert argv[-3:] == ["-movflags", "+faststart", "/out.mp4"]


def test_ts_target_h264_adds_annexb_bsf() -> None:
    """Given H.264 video; when remuxed to TS; then h264_mp4toannexb bsf is emitted."""
    spec = RemuxSpec(
        in_path=Path("/in.mp4"), out_path=Path("/out.ts"), streams=H264_AAC
    )
    argv = spec.build_argv()
    assert "-bsf:v" in argv
    assert "h264_mp4toannexb" in argv
    assert argv[-2:] == ["h264_mp4toannexb", "/out.ts"]


def test_ts_target_hevc_adds_annexb_bsf() -> None:
    """Given HEVC video; when remuxed to TS; then hevc_mp4toannexb bsf is emitted."""
    spec = RemuxSpec(
        in_path=Path("/in.mkv"), out_path=Path("/out.ts"), streams=HEVC_AAC
    )
    argv = spec.build_argv()
    assert argv[-2:] == ["hevc_mp4toannexb", "/out.ts"]


def test_aac_bare_output_adds_adts() -> None:
    """Given an audio stream; when remuxed to .aac; then -f adts is emitted."""
    audio_only = [_stream(1, "audio", "aac")]
    spec = RemuxSpec(
        in_path=Path("/in.mp4"), out_path=Path("/out.aac"), streams=audio_only
    )
    argv = spec.build_argv()
    assert argv[-3:] == ["-f", "adts", "/out.aac"]


def test_srt_subtitle_to_mp4_raises() -> None:
    """Given an SRT subtitle; when remuxed to MP4; then remux raises an error."""
    spec = RemuxSpec(
        in_path=Path("/in.mkv"), out_path=Path("/out.mp4"), streams=H264_AAC_SRT
    )
    with pytest.raises(SubtitleIncompatibleError) as exc:
        spec.build_argv()
    assert "mov_text" in str(exc.value)


def test_srt_subtitle_to_mkv_is_copied() -> None:
    """Given an SRT subtitle; when remuxed to MKV; then the subtitle is copied."""
    spec = RemuxSpec(
        in_path=Path("/in.mkv"), out_path=Path("/out.mkv"), streams=H264_AAC_SRT
    )
    argv = spec.build_argv()
    assert "-c:2" in argv
    assert "copy" in argv
    assert argv[-1] == "/out.mkv"


def test_check_subtitle_compat_returns_verdict() -> None:
    """Given srt->mp4; when checked; then a non-compatible verdict with a reason."""
    result = check_subtitle_compat("mp4", H264_AAC_SRT)
    assert result == CompatResult(
        ok=False,
        reason=(
            "subtitle stream 2 (subrip) cannot be copied into 'mp4': MP4-family "
            "containers carry only mov_text subtitles; this would require a "
            "mov_text text transcode. Use the subtitle operation or drop the "
            "subtitle stream instead."
        ),
    )


def test_check_subtitle_compat_mov_text_ok() -> None:
    """Given mov_text subtitles; when checked against mp4; then compatible."""
    streams = [*H264_AAC, _stream(2, "subtitle", "mov_text")]
    assert check_subtitle_compat("mp4", streams).ok


def _fake_ffmpeg(tmp_path: Path, stdout: str, exit_code: int = 0) -> Path:
    """Create an executable fake ffmpeg that prints ``stdout`` and exits."""
    script = tmp_path / "fake_ffmpeg"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {exit_code}\n")
    script.chmod(0o755)
    return script


_ENUMERATED_MP4 = """\
Muxer mp4 [MP4 (MPEG-4 Part 14)]:
    Common extensions: mp4.
    Mime type: video/mp4.
    Default video codec: h264.
    Default audio codec: aac.
    Supported video codecs:
        h264
        hevc
        mpeg4
    Supported audio codecs:
        aac
        mp3
    Supported subtitle codecs:
        mov_text
mov/mp4/... muxer AVOptions:
  -movflags <flags> E.......... MOV muxer flags
"""

_HINTS_ONLY_MP4 = """\
Muxer mp4 [MP4 (MPEG-4 Part 14)]:
    Common extensions: mp4.
    Mime type: video/mp4.
    Default video codec: h264.
    Default audio codec: aac.
mov/mp4/... muxer AVOptions:
  -movflags <flags> E.......... MOV muxer flags
"""


def test_muxer_supports_enumerated_codec_present(tmp_path: Path) -> None:
    """Given an enumerated muxer help; when codec is listed; then ok."""
    fake = _fake_ffmpeg(tmp_path, _ENUMERATED_MP4)
    assert muxer_supports("mp4", "hevc", fake) == CompatResult(ok=True)


def test_muxer_supports_enumerated_codec_absent(tmp_path: Path) -> None:
    """Given an enumerated muxer help; when codec is absent; then not ok."""
    fake = _fake_ffmpeg(tmp_path, _ENUMERATED_MP4)
    result = muxer_supports("mp4", "subrip", fake)
    assert result.ok is False
    assert result.reason is not None
    assert "subrip" in result.reason


def test_muxer_supports_hints_only_is_inconclusive(tmp_path: Path) -> None:
    """Given ffmpeg 6.x/7.x (default-codec hints only); when probed; then allow."""
    fake = _fake_ffmpeg(tmp_path, _HINTS_ONLY_MP4)
    assert muxer_supports("mp4", "hevc", fake).ok


def test_muxer_supports_probe_failure_allows(tmp_path: Path) -> None:
    """Given a failing probe; when queried; then allow (defer to ffmpeg)."""
    fake = _fake_ffmpeg(tmp_path, "", exit_code=1)
    assert muxer_supports("mp4", "h264", fake).ok


def test_muxer_supports_missing_binary_allows(tmp_path: Path) -> None:
    """Given a missing binary; when queried; then allow rather than raise."""
    assert muxer_supports("mp4", "h264", tmp_path / "absent").ok
