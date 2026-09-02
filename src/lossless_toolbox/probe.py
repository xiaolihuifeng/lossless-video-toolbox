"""ffprobe wrapper: probe media files and index video keyframes.

This layer only READS media via ffprobe — no ffmpeg write operation lives
here. Untrusted ffprobe JSON is parsed once at this boundary into the typed
models in :mod:`lossless_toolbox.models`.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from .ffmpeg_locator import resolve
from .models import Chapter, KeyframeIndex, MediaFile, StreamInfo

if TYPE_CHECKING:
    from pathlib import Path

_SHOW_ALL = ["-show_streams", "-show_format", "-show_chapters", "-of", "json"]
_ROTATION_MATRIX_SIZE = 9


class ProbeError(RuntimeError):
    """Raised when ffprobe cannot produce a usable result for a path."""

    def __init__(self, path: Path, stderr: str) -> None:
        """Create a ProbeError carrying the offending path and ffprobe stderr."""
        self.path = path
        self.stderr = stderr
        super().__init__(f"ffprobe failed on {path}: {stderr}")


def _resolve_ffprobe() -> str:
    """Return the ffprobe binary path via the ffmpeg_locator (todo 3)."""
    return str(resolve("ffprobe").path)


def _run_ffprobe(argv: list[str], path: Path) -> str:
    """Run ffprobe and return stdout, raising ProbeError on any failure."""
    proc = subprocess.run(  # noqa: S603 - argv list built from fixed flags, no shell
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise ProbeError(path, proc.stderr.strip())
    return proc.stdout


def _parse_json(stdout: str, path: Path) -> dict[str, Any]:
    """Parse ffprobe JSON, raising ProbeError on malformed output."""
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(path, f"ffprobe returned invalid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ProbeError(path, "ffprobe returned non-object JSON")
    return cast("dict[str, Any]", raw)


def _rotation_from_field(value: object) -> int | None:
    """Normalize a numeric rotation value to [0, 360), or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value) % 360
    except ValueError:
        return None


def _matrix_values_from_displaymatrix(text: str) -> list[int]:
    """Extract int matrix values from ffprobe's displaymatrix string.

    The string may be a hexdump (``ADDRESS: a b c`` lines) or plain
    space-separated numbers; address prefixes are stripped before the
    remaining signed integers are collected.
    """
    body = "\n".join(
        line.partition(":")[2] if ":" in line else line for line in text.splitlines()
    )
    return [int(token) for token in re.findall(r"-?\d+", body)]


def _rotation_from_matrix_values(values: list[int]) -> int | None:
    """Derive rotation degrees from a 3x3 display matrix.

    Implements ffmpeg's ``av_display_rotation_get`` convention on the 16.16
    fixed-point matrix: normalize each basis vector, then
    ``-atan2(m[1], m[0])`` in degrees, folded into [0, 360).
    """
    if len(values) < _ROTATION_MATRIX_SIZE:
        return None
    m = values[:_ROTATION_MATRIX_SIZE]
    scale0 = math.hypot(m[0], m[3])
    scale1 = math.hypot(m[1], m[4])
    if scale0 == 0.0 or scale1 == 0.0:
        return None
    degrees = math.degrees(math.atan2(m[1] / scale1, m[0] / scale0))
    return round(-degrees) % 360


def _rotation_from_side_data(side_data_list: list[dict[str, Any]]) -> int | None:
    """Extract rotation from a stream's side_data_list, if present."""
    for entry in side_data_list:
        if entry.get("side_data_type") != "Display Matrix":
            continue
        rotation = _rotation_from_field(entry.get("rotation"))
        if rotation is not None:
            return rotation
        matrix = entry.get("displaymatrix")
        if isinstance(matrix, str):
            rotation = _rotation_from_matrix_values(
                _matrix_values_from_displaymatrix(matrix),
            )
            if rotation is not None:
                return rotation
    return None


def _build_stream(raw: dict[str, Any]) -> StreamInfo:
    """Parse one ffprobe stream dict into a typed StreamInfo."""
    tags = cast("dict[str, Any] | None", raw.get("tags"))
    language = tags.get("language") if tags else None
    side_data = cast("list[dict[str, Any]]", raw.get("side_data_list") or [])
    disposition = cast("dict[str, Any]", raw.get("disposition") or {})
    return StreamInfo(
        index=raw["index"],
        codec_type=raw["codec_type"],
        codec_name=raw["codec_name"],
        width=raw.get("width"),
        height=raw.get("height"),
        sample_rate=raw.get("sample_rate"),
        channels=raw.get("channels"),
        language=language,
        disposition=disposition,
        rotation=_rotation_from_side_data(side_data),
    )


def _build_chapter(raw: dict[str, Any]) -> Chapter:
    """Parse one ffprobe chapter dict into a typed Chapter."""
    tags = cast("dict[str, Any] | None", raw.get("tags"))
    title = tags.get("title") if tags else None
    return Chapter(
        id=raw["id"],
        start_time=float(raw["start_time"]),
        end_time=float(raw["end_time"]),
        title=title,
    )


def _build_media_file(path: Path, raw: dict[str, Any]) -> MediaFile:
    """Assemble a typed MediaFile from parsed ffprobe JSON."""
    fmt = cast("dict[str, Any]", raw.get("format") or {})
    stream_dicts = cast("list[dict[str, Any]]", raw.get("streams") or [])
    chapter_dicts = cast("list[dict[str, Any]]", raw.get("chapters") or [])
    streams = [_build_stream(s) for s in stream_dicts]
    chapters = [_build_chapter(c) for c in chapter_dicts]
    return MediaFile(
        path=path,
        format_name=fmt.get("format_name", ""),
        duration=float(fmt.get("duration", 0.0)),
        streams=streams,
        chapters=chapters or None,
    )


def probe(path: Path) -> MediaFile:
    """Probe a media file and return its typed metadata.

    One ffprobe invocation fetches streams, format and chapters together.
    Raises :class:`ProbeError` for non-media or unreadable inputs.
    """
    argv = [
        _resolve_ffprobe(),
        "-hide_banner",
        "-loglevel",
        "error",
        *_SHOW_ALL,
        str(path),
    ]
    stdout = _run_ffprobe(argv, path)
    raw = _parse_json(stdout, path)
    try:
        return _build_media_file(path, raw)
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        raise ProbeError(path, f"unexpected ffprobe schema: {exc}") from exc


def _keyframe_times(packets: list[dict[str, Any]]) -> KeyframeIndex:
    """Filter packets to keyframes (flags containing ``K``) and collect times."""
    times: list[float] = []
    for packet in packets:
        flags = packet.get("flags") or ""
        if isinstance(flags, str) and "K" in flags:
            times.append(float(packet.get("pts_time", 0.0)))
    return KeyframeIndex(times=times)


def keyframes(path: Path, stream_index: int = 0) -> KeyframeIndex:
    """Return the keyframe index (pts_time list) for a video stream."""
    argv = [
        _resolve_ffprobe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-select_streams",
        f"v:{stream_index}",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,duration_time,flags",
        "-of",
        "json",
        str(path),
    ]
    stdout = _run_ffprobe(argv, path)
    raw = _parse_json(stdout, path)
    packets = cast("list[dict[str, Any]]", raw.get("packets") or [])
    return _keyframe_times(packets)
