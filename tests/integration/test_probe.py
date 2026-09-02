"""Integration tests: real ffprobe against the synthetic media corpus.

These tests depend on the media factory fixtures defined in
``tests/conftest.py`` (todo 2), consumed as function parameters. Each fixture
is a ``MediaSample`` exposing ``.path``, ``.codec`` and ``.duration``.
"""

from pathlib import Path
from typing import Protocol

import pytest

from lossless_toolbox.probe import ProbeError, keyframes, probe

pytestmark = pytest.mark.integration


class _MediaSample(Protocol):
    """Shape of the conftest media fixture objects (todo 2)."""

    path: Path
    codec: str
    duration: float


def test_probe_h264_aac_mp4(h264_aac_mp4: _MediaSample) -> None:
    """Given an H.264+AAC MP4; then 2 streams and ~12s duration are reported."""
    media = probe(h264_aac_mp4.path)
    assert len(media.streams) == 2
    assert media.duration == pytest.approx(12.0, abs=0.5)
    codecs = {s.codec_name for s in media.streams}
    assert "aac" in codecs
    assert "h264" in codecs or "mpeg4" in codecs


def test_keyframes_h264_aac_mp4(h264_aac_mp4: _MediaSample) -> None:
    """Given an H.264+AAC MP4 with 2s GOP; then keyframes start at 0 and step 2."""
    index = keyframes(h264_aac_mp4.path)
    assert index.times
    assert index.times[0] == pytest.approx(0.0, abs=0.1)
    diffs = [b - a for a, b in zip(index.times, index.times[1:], strict=False)]
    assert all(d == pytest.approx(2.0, abs=0.1) for d in diffs)


def test_probe_hevc_aac_mkv(hevc_aac_mkv: _MediaSample) -> None:
    """Given an HEVC+AAC MKV; then 2 streams and HEVC video are reported."""
    media = probe(hevc_aac_mkv.path)
    assert len(media.streams) == 2
    assert media.duration == pytest.approx(12.0, abs=0.5)
    assert any(s.codec_name in {"hevc", "mpeg4"} for s in media.streams)


def test_probe_srt_mkvm(srt_mkvm: _MediaSample) -> None:
    """Given an MKV with SRT subs; then subtitle streams are surfaced."""
    media = probe(srt_mkvm.path)
    assert media.duration == pytest.approx(12.0, abs=0.5)
    assert any(s.codec_type == "video" for s in media.streams)
    assert any(s.codec_type == "audio" for s in media.streams)
    sub_streams = [s for s in media.streams if s.codec_type == "subtitle"]
    assert sub_streams
    assert all(s.codec_name in {"subrip", "srt"} for s in sub_streams)


def test_probe_annexb_ts(annexb_ts: _MediaSample) -> None:
    """Given an annexb TS; then mpegts format and 2 streams are reported."""
    media = probe(annexb_ts.path)
    assert len(media.streams) == 2
    assert media.duration == pytest.approx(12.0, abs=0.5)
    assert "mpegts" in media.format_name


def test_probe_nonzero_start_ts(nonzero_start_ts: _MediaSample) -> None:
    """Given a TS with nonzero start time; then duration stays ~12s."""
    media = probe(nonzero_start_ts.path)
    assert len(media.streams) == 2
    assert media.duration == pytest.approx(12.0, abs=0.5)
    assert "mpegts" in media.format_name


def test_probe_non_media_raises_probe_error(tmp_path: Path) -> None:
    """Given a non-media file; then probe raises ProbeError, not a half model."""
    text_file = tmp_path / "empty.txt"
    text_file.write_text("this is not a media file\n")
    with pytest.raises(ProbeError):
        probe(text_file)
