"""Synthetic media fixtures generated with ffmpeg.

A session-scoped ``media`` fixture generates five probeable media files into
``tests/.media_cache/``, validating each with ffprobe before accepting it as
cached. A missing or corrupted cache entry is regenerated (one retry); a
persistent failure aborts the session with the ffmpeg stderr. Each named
fixture exposes a :class:`MediaSample` (path, codec, duration) so tests adapt
their assertions when the encoder degrades (e.g. libx264 → mpeg4).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

_CACHE_DIR: Final[Path] = Path(__file__).resolve().parent / ".media_cache"

EXPECTED_DURATION: Final[float] = 12.0

_SRT_CONTENT: Final[str] = """\
1
00:00:01,000 --> 00:00:03,000
Hello, world.

2
00:00:04,000 --> 00:00:07,000
Second subtitle line.

3
00:00:08,000 --> 00:00:11,000
Third subtitle line.
"""


@dataclass(frozen=True, slots=True)
class MediaSample:
    """A generated media file plus the video encoder it was produced with."""

    path: Path
    codec: str
    duration: float


@dataclass(frozen=True, slots=True)
class MediaSet:
    """The complete set of synthetic media fixtures."""

    h264_aac_mp4: MediaSample
    hevc_aac_mkv: MediaSample
    srt_mkvm: MediaSample
    annexb_ts: MediaSample
    nonzero_start_ts: MediaSample


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.fail("ffmpeg not found on PATH")
    return binary


def _ffprobe() -> str:
    binary = shutil.which("ffprobe")
    if binary is None:
        pytest.fail("ffprobe not found on PATH")
    return binary


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — controlled invocation, no shell
        args, capture_output=True, text=True, check=False
    )


def _has_encoder(name: str) -> bool:
    proc = _run([_ffmpeg(), "-hide_banner", "-encoders"])
    if proc.returncode != 0:
        return False
    return any(
        len(tokens) >= 2 and tokens[1] == name
        for tokens in (line.split() for line in proc.stdout.splitlines())
    )


# ruff format would explode these ffmpeg arg lists one-per-line, pushing the
# module past its 250-line budget; keep them compact.
# fmt: off
def _probe_duration(path: Path) -> float | None:
    """Return container duration in seconds, or None if unprobeable."""
    proc = _run(
        [_ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)]
    )
    if proc.returncode != 0:
        return None
    try:
        duration = float(json.loads(proc.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return None
    return duration if duration > 0 else None


def _is_cached(path: Path) -> bool:
    return path.is_file() and _probe_duration(path) is not None


def _generate_checked(args: list[str], target: Path) -> None:
    """Run ffmpeg, then accept ``target`` only if it probes as valid media."""
    stderr = ""
    for _ in range(2):
        target.unlink(missing_ok=True)
        proc = _run(args)
        if proc.returncode == 0 and _is_cached(target):
            return
        stderr = proc.stderr
    pytest.fail(f"ffmpeg failed to generate {target.name}:\n{stderr}")


def _video_args(codec: str) -> list[str]:
    """Encoder args; mpeg4 fallback lacks keyint_min/sc_threshold knobs."""
    if codec == "mpeg4":
        return ["-c:v", codec, "-g", "60"]
    return ["-c:v", codec, "-g", "60", "-keyint_min", "60", "-sc_threshold", "0"]


_SOURCE_ARGS: Final[tuple[str, ...]] = (
    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "12",
)

_COMMON_OPTS: Final[tuple[str, ...]] = (
    "-y", "-hide_banner", "-loglevel", "error",
)


def _ensure_video(codec: str, filename: str) -> Path:
    target = _CACHE_DIR / filename
    if _is_cached(target):
        return target
    _generate_checked(
        [_ffmpeg(), *_COMMON_OPTS, *_SOURCE_ARGS, *_video_args(codec),
         "-c:a", "aac", str(target)],
        target,
    )
    return target


def _ensure_srt(source: Path) -> Path:
    target = _CACHE_DIR / "subs_mkv.mkv"
    if _is_cached(target):
        return target
    srt_file = _CACHE_DIR / "subs.srt"
    srt_file.write_text(_SRT_CONTENT, encoding="utf-8")
    _generate_checked(
        [_ffmpeg(), *_COMMON_OPTS, "-i", str(source), "-i", str(srt_file),
         "-map", "0", "-map", "1", "-c", "copy", "-c:s", "srt", str(target)],
        target,
    )
    return target


def _ensure_annexb(source: Path) -> Path:
    target = _CACHE_DIR / "annexb.ts"
    if _is_cached(target):
        return target
    _generate_checked(
        [_ffmpeg(), *_COMMON_OPTS, "-i", str(source), "-c", "copy",
         "-bsf:v", "h264_mp4toannexb", "-f", "mpegts", str(target)],
        target,
    )
    return target


def _ensure_nonzero_start(source: Path) -> Path:
    target = _CACHE_DIR / "nonzero_start.ts"
    if _is_cached(target):
        return target
    _generate_checked(
        [_ffmpeg(), *_COMMON_OPTS, "-i", str(source), "-c", "copy",
         "-output_ts_offset", "30", "-f", "mpegts", str(target)],
        target,
    )
    return target


# fmt: on


def _generate_all() -> MediaSet:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    h264_codec = "libx264" if _has_encoder("libx264") else "mpeg4"
    hevc_codec = "libx265" if _has_encoder("libx265") else "mpeg4"

    h264_path = _ensure_video(h264_codec, "h264_aac.mp4")
    hevc_path = _ensure_video(hevc_codec, "hevc_aac.mkv")
    srt_path = _ensure_srt(h264_path)
    annexb_path = _ensure_annexb(h264_path)
    nonzero_path = _ensure_nonzero_start(h264_path)

    return MediaSet(
        h264_aac_mp4=MediaSample(h264_path, h264_codec, EXPECTED_DURATION),
        hevc_aac_mkv=MediaSample(hevc_path, hevc_codec, EXPECTED_DURATION),
        srt_mkvm=MediaSample(srt_path, h264_codec, EXPECTED_DURATION),
        annexb_ts=MediaSample(annexb_path, h264_codec, EXPECTED_DURATION),
        nonzero_start_ts=MediaSample(nonzero_path, h264_codec, EXPECTED_DURATION),
    )


@pytest.fixture(scope="session")
def media() -> MediaSet:
    """Generate (or reuse) the full set of synthetic media fixtures."""
    return _generate_all()


@pytest.fixture(scope="session")
def h264_aac_mp4(media: MediaSet) -> MediaSample:
    return media.h264_aac_mp4


@pytest.fixture(scope="session")
def hevc_aac_mkv(media: MediaSet) -> MediaSample:
    return media.hevc_aac_mkv


@pytest.fixture(scope="session")
def srt_mkvm(media: MediaSet) -> MediaSample:
    return media.srt_mkvm


@pytest.fixture(scope="session")
def annexb_ts(media: MediaSet) -> MediaSample:
    return media.annexb_ts


@pytest.fixture(scope="session")
def nonzero_start_ts(media: MediaSet) -> MediaSample:
    return media.nonzero_start_ts
