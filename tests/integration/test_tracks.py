"""Integration tests: real ffmpeg track operations against the media corpus.

Extract/strip/replace run through the built argv against the real ffmpeg; the
outputs are re-probed to prove each operation was lossless (codec preserved,
duration > 0, and the video elementary-stream md5 unchanged across a replace).
"""

import re
import subprocess
from pathlib import Path
from typing import Protocol

import pytest

from lossless_toolbox.ffmpeg_locator import resolve
from lossless_toolbox.ops.tracks import (
    ExtractSpec,
    ReplaceSpec,
    StripSpec,
    probe_audio_codec,
)
from lossless_toolbox.probe import probe

pytestmark = pytest.mark.integration

FFMPEG = str(resolve("ffmpeg").path)
_MD5_RE = re.compile(r"MD5=([0-9a-f]{32})")


class _MediaSample(Protocol):
    """Shape of the conftest media fixture objects (todo 2)."""

    path: Path
    codec: str
    duration: float


def _run(argv: list[str]) -> None:
    """Run an ops argv (flags only) through ffmpeg, failing on nonzero exit."""
    proc = subprocess.run(  # noqa: S603 - argv list built by ops, no shell
        [FFMPEG, *argv], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def _video_stream_md5(path: Path) -> str:
    """Return the MD5 of the first video elementary stream (lossless truth)."""
    proc = subprocess.run(  # noqa: S603 - static argv, no shell
        [FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy",
         "-f", "md5", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    match = _MD5_RE.search(proc.stdout)
    assert match is not None, proc.stdout
    return match.group(1)


def test_extract_audio_stream(h264_aac_mp4: _MediaSample, tmp_path: Path) -> None:
    """Given stream 1 (aac); when extracted; then output probes aac, duration > 0."""
    out = tmp_path / "extracted.m4a"
    media = probe(h264_aac_mp4.path)
    spec = ExtractSpec(
        in_path=h264_aac_mp4.path,
        stream_index=0,
        out_path=out,
        streams=media.streams,
    )
    _run(spec.build_argv())
    result = probe(out)
    audio = [s for s in result.streams if s.codec_type == "audio"]
    assert len(audio) == 1
    assert audio[0].codec_name == "aac"
    assert result.duration > 0


def test_strip_audio_leaves_no_audio_stream(
    h264_aac_mp4: _MediaSample, tmp_path: Path
) -> None:
    """Given keep=[0]; when stripped; then output has video but no audio stream."""
    out = tmp_path / "video_only.mp4"
    media = probe(h264_aac_mp4.path)
    spec = StripSpec(
        in_path=h264_aac_mp4.path,
        out_path=out,
        keep_streams=[0],
        streams=media.streams,
    )
    _run(spec.build_argv())
    result = probe(out)
    assert any(s.codec_type == "video" for s in result.streams)
    assert not any(s.codec_type == "audio" for s in result.streams)


def test_replace_audio_preserves_video_stream(
    h264_aac_mp4: _MediaSample, tmp_path: Path
) -> None:
    """Given a replacement track; when replaced; audio count and video md5 correct."""
    new_audio = tmp_path / "new_audio.m4a"
    media = probe(h264_aac_mp4.path)
    _run(
        ExtractSpec(
            in_path=h264_aac_mp4.path,
            stream_index=0,
            out_path=new_audio,
            streams=media.streams,
        ).build_argv()
    )

    out = tmp_path / "replaced.mp4"
    codec = probe_audio_codec(new_audio)
    _run(
        ReplaceSpec(
            in_path=h264_aac_mp4.path,
            out_path=out,
            new_audio_path=new_audio,
        ).build_argv(codec)
    )

    result = probe(out)
    audio = [s for s in result.streams if s.codec_type == "audio"]
    assert len(audio) == 1
    assert audio[0].codec_name == "aac"
    assert _video_stream_md5(out) == _video_stream_md5(h264_aac_mp4.path)
