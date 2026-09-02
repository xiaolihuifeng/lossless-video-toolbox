"""Integration tests: real ffmpeg/ffprobe for the meta operations.

These tests run the argv produced by each spec against the synthetic media
corpus (``tests/conftest.py``) and assert the observable outcome via ffprobe
(rotation side data, attached_pic streams, chapter count/timestamps, and
visible title/language metadata).
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Protocol

import pytest

from lossless_toolbox.ffmpeg_locator import resolve
from lossless_toolbox.ops.meta import (
    ChapterArg,
    ChaptersSpec,
    CoverSpec,
    MetadataEditSpec,
    RotateSpec,
)
from lossless_toolbox.probe import probe

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


class _MediaSample(Protocol):
    """Shape of the conftest media fixture objects (todo 2)."""

    path: Path
    codec: str
    duration: float


_FFMPEG = str(resolve("ffmpeg").path)
_FFPROBE = str(resolve("ffprobe").path)


def _run(argv: list[str]) -> None:
    proc = subprocess.run(  # noqa: S603 - argv built by specs, no shell
        [_FFMPEG, *argv], capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr


def _make_cover_png(path: Path) -> Path:
    _run([
        "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=red:size=64x64",
        "-frames:v", "1", str(path),
    ])
    return path


def _format_title(path: Path) -> str | None:
    proc = subprocess.run(  # noqa: S603 - fixed ffprobe flags, no shell
        [
            _FFPROBE, "-v", "error", "-show_entries", "format_tags",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    tags = json.loads(proc.stdout).get("format", {}).get("tags", {})
    return tags.get("title")


def _has_attached_png(path: Path) -> bool:
    return any(
        stream.codec_name == "png" and stream.disposition.get("attached_pic")
        for stream in probe(path).streams
    )


def test_rotate_90_sets_display_rotation(
    tmp_path: Path, h264_aac_mp4: _MediaSample,
) -> None:
    """Given degrees=90 on MP4; then the probe reports rotation normalized to 270.

    ``degrees`` is a clockwise user rotation; ffmpeg's ``-display_rotation`` is
    counter-clockwise, so the emitted value is ``360 - 90 = 270`` and ffprobe
    reports it (raw ``-90``, normalized to ``270`` by the probe layer).
    """
    out = tmp_path / "rotated.mp4"
    spec = RotateSpec(in_path=h264_aac_mp4.path, out_path=out, degrees=90)
    _run(spec.build_argv())
    assert probe(out).streams[0].rotation == 270


def test_cover_mp4_embeds_attached_pic(
    tmp_path: Path, h264_aac_mp4: _MediaSample,
) -> None:
    """Given an MP4 target; then an attached_pic PNG stream is present."""
    cover = _make_cover_png(tmp_path / "cover.png")
    out = tmp_path / "covered.mp4"
    spec = CoverSpec(in_path=h264_aac_mp4.path, out_path=out, image_path=cover)
    _run(spec.build_argv())
    assert _has_attached_png(out)


def test_cover_mkv_embeds_attachment(
    tmp_path: Path, hevc_aac_mkv: _MediaSample,
) -> None:
    """Given an MKV target; then -attach embeds the cover as a PNG stream.

    Verified against the pinned ffmpeg 6.1.1 where ``-attach`` is reliable; no
    FeatureUnavailable degradation is needed.
    """
    cover = _make_cover_png(tmp_path / "cover.png")
    out = tmp_path / "covered.mkv"
    spec = CoverSpec(in_path=hevc_aac_mkv.path, out_path=out, image_path=cover)
    _run(spec.build_argv())
    assert _has_attached_png(out)


def test_chapters_written_with_correct_timestamps(
    tmp_path: Path, h264_aac_mp4: _MediaSample,
) -> None:
    """Given three chapters; then count and start times survive the remux."""
    out = tmp_path / "chapters.mp4"
    spec = ChaptersSpec(
        in_path=h264_aac_mp4.path,
        out_path=out,
        chapters=[
            ChapterArg(start_time=0.0, title="Intro"),
            ChapterArg(start_time=3.0, title="Middle"),
            ChapterArg(start_time=6.0, title="Outro", end_time=9.0),
        ],
    )
    try:
        _run(spec.build_argv())
    finally:
        spec.cleanup()
    media = probe(out)
    assert media.chapters is not None
    assert len(media.chapters) == 3
    assert [chapter.start_time for chapter in media.chapters] == pytest.approx(
        [0.0, 3.0, 6.0]
    )


def test_metadata_title_and_language_visible(
    tmp_path: Path, h264_aac_mp4: _MediaSample,
) -> None:
    """Given title + stream language; then both are visible via ffprobe."""
    out = tmp_path / "meta.mp4"
    spec = MetadataEditSpec(
        in_path=h264_aac_mp4.path,
        out_path=out,
        title="My Movie",
        language_map={1: "eng"},
    )
    _run(spec.build_argv())
    assert _format_title(out) == "My Movie"
    assert probe(out).streams[1].language == "eng"
