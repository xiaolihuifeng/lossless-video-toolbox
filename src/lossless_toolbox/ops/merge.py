"""Lossless merge via the concat demuxer (todo 7).

Two or more segment files are concatenated without re-encoding by feeding the
concat demuxer a file list over stdin. Streams are stream-copied from the
virtual concat input (``-i -``); container metadata and chapters are taken
from the first real input file (``-map_metadata 1 -map_chapters 1``), and each
stream's disposition flags are replayed explicitly because the concat demuxer
does not carry them. A preflight compatibility check blocks merges whose
segments do not share identical stream parameters.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - pydantic resolves Path fields at runtime
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import BaseModel, ConfigDict

from lossless_toolbox.ffmpeg_locator import resolve
from lossless_toolbox.probe import probe

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lossless_toolbox.models import StreamInfo

# NOTE(todo 5): ops/common.py is built by a parallel Wave-2 worker. Once it
# lands, the base/copy/movflags pieces below should converge onto it at the
# Wave-2 gate. Until then they are inlined so this module stays self-contained.

_MP4_SUFFIXES: Final[frozenset[str]] = frozenset({".mp4", ".mov", ".m4v"})

# The shell-style escape for a single quote inside a single-quoted concat-list
# entry: close the quote, emit an escaped quote, then reopen the quote.
_SINGLE_QUOTE_ESCAPE: Final[str] = "'\\''"

# Fields compared, in order, by the concat-compatibility preflight.
_CONCAT_COMPAT_FIELDS: Final[tuple[str, ...]] = (
    "codec_name",
    "profile",
    "width",
    "height",
    "pix_fmt",
    "sample_rate",
    "channels",
)

_COMPAT_SHOW_ENTRIES: Final[str] = (
    "stream=index,codec_name,profile,width,height,pix_fmt,sample_rate,channels"
)

_MIN_MERGE_INPUTS: Final[int] = 2


class MergeError(RuntimeError):
    """Raised when a merge cannot be planned (too few or incompatible inputs)."""


class MergeSpec(BaseModel):
    """Inputs to a lossless merge: an ordered list of segments and an output."""

    model_config = ConfigDict(frozen=True)

    paths: list[Path]
    out_path: Path

    def build_argv(self) -> list[str]:
        """Build the concat-demuxer argv (flags-only, no binary prefix)."""
        return build_plan(self).argv

    def build_stdin_data(self) -> bytes:
        """Return the concat file list as UTF-8 bytes for the stdin channel.

        The concat demuxer reads its file list from standard input (``-i -``);
        the queue feeds this payload to the runner so the list never touches a
        temp file.
        """
        return build_plan(self).concat_list.encode("utf-8")


class MergePlan(BaseModel):
    """A built merge: the concat list (fed via stdin) and the ffmpeg argv."""

    model_config = ConfigDict(frozen=True)

    concat_list: str
    argv: list[str]


class CompatReport(BaseModel):
    """Result of a concat-compatibility preflight over segment files."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    differences: list[str]


@dataclass(frozen=True, slots=True)
class _CompatStream:
    """The concat-relevant fields of one stream, as read by ffprobe."""

    codec_name: str
    profile: str | None
    width: int | None
    height: int | None
    pix_fmt: str | None
    sample_rate: int | None
    channels: int | None

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> _CompatStream:
        """Build from one ffprobe ``stream`` JSON object."""

        def coerce_int(key: str) -> int | None:
            value = entry.get(key)
            if value is None:
                return None
            try:
                return int(str(value))
            except (ValueError, TypeError):
                return None

        def coerce_str(key: str) -> str | None:
            value = entry.get(key)
            return None if value is None else str(value)

        return cls(
            codec_name=coerce_str("codec_name") or "",
            profile=coerce_str("profile"),
            width=coerce_int("width"),
            height=coerce_int("height"),
            pix_fmt=coerce_str("pix_fmt"),
            sample_rate=coerce_int("sample_rate"),
            channels=coerce_int("channels"),
        )


def _escape_concat_path(path: Path) -> str:
    """Return the escaped ``file:<abs>`` URI for one concat-list entry."""
    return f"file:{path.resolve()}".replace("'", _SINGLE_QUOTE_ESCAPE)


def _build_concat_list(paths: Sequence[Path]) -> str:
    """Build the concat-demuxer file list (one ``file '...'`` line each)."""
    return "\n".join(f"file '{_escape_concat_path(p)}'" for p in paths)


def _copy_map_args(stream_count: int) -> list[str]:
    """Map and stream-copy every stream of the concat input (input 0)."""
    args: list[str] = []
    for index in range(stream_count):
        args.extend(["-map", f"0:{index}", f"-c:{index}", "copy"])
    return args


def _disposition_args(streams: Sequence[StreamInfo]) -> list[str]:
    """Replay each stream's enabled disposition flags as explicit args."""
    args: list[str] = []
    for index, stream in enumerate(streams):
        for flag, enabled in stream.disposition.items():
            if enabled:
                args.extend([f"-disposition:{index}", flag])
    return args


def _build_argv(
    out_path: Path,
    streams: Sequence[StreamInfo],
    first_input: Path,
) -> list[str]:
    """Assemble the full ffmpeg argv for a concat-demuxer merge."""
    argv = [
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
        str(first_input),
    ]
    argv.extend(_copy_map_args(len(streams)))
    argv.extend(["-map_metadata", "1", "-map_chapters", "1"])
    argv.extend(_disposition_args(streams))
    if out_path.suffix.lower() in _MP4_SUFFIXES:
        argv.extend(["-movflags", "+faststart"])
    argv.extend(["-ignore_unknown", str(out_path)])
    return argv


def _compare_stream_sets(
    reference: Sequence[_CompatStream],
    candidate: Sequence[_CompatStream],
) -> list[str]:
    """Return field-by-field differences between two stream sets."""
    if len(reference) != len(candidate):
        return [f"stream count: {len(reference)} != {len(candidate)}"]
    differences: list[str] = []
    for index, (ref_stream, cand_stream) in enumerate(
        zip(reference, candidate, strict=True)
    ):
        for field in _CONCAT_COMPAT_FIELDS:
            ref_value = getattr(ref_stream, field)
            cand_value = getattr(cand_stream, field)
            if ref_value != cand_value:
                differences.append(
                    f"stream {index} {field}: {ref_value!r} != {cand_value!r}"
                )
    return differences


def _probe_compat_streams(path: Path) -> list[_CompatStream]:
    """Read each stream's concat-relevant fields directly from ffprobe.

    ``profile`` and ``pix_fmt`` are deliberately absent from the typed
    :mod:`~lossless_toolbox.models` probe boundary (they are only needed here,
    for concat compatibility), so they are read with a targeted ffprobe call
    rather than widening the shared model.
    """
    argv = [
        str(resolve("ffprobe").path),
        "-v",
        "error",
        "-show_entries",
        _COMPAT_SHOW_ENTRIES,
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(  # noqa: S603 - argv list built from fixed flags, no shell
        argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        message = f"ffprobe failed on {path}: {proc.stderr.strip()}"
        raise MergeError(message)
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        message = f"ffprobe returned invalid JSON for {path}: {exc.msg}"
        raise MergeError(message) from exc
    if not isinstance(parsed, dict):
        message = f"ffprobe returned non-object JSON for {path}"
        raise MergeError(message)
    raw = cast("dict[str, Any]", parsed)
    entries = cast("list[dict[str, Any]]", raw.get("streams") or [])
    return [_CompatStream.from_entry(entry) for entry in entries]


def check_concat_compatibility(paths: Sequence[Path]) -> CompatReport:
    """Preflight: compare concat-relevant stream fields across all inputs.

    Returns a :class:`CompatReport` (never raises on a mere mismatch) so the
    UI can surface the exact field-by-field differences that block a merge.
    """
    if len(paths) < _MIN_MERGE_INPUTS:
        return CompatReport(
            ok=False,
            differences=[
                f"need at least {_MIN_MERGE_INPUTS} files, got {len(paths)}"
            ],
        )
    stream_sets = [_probe_compat_streams(path) for path in paths]
    reference = stream_sets[0]
    differences: list[str] = []
    for candidate in stream_sets[1:]:
        differences.extend(_compare_stream_sets(reference, candidate))
    return CompatReport(ok=not differences, differences=differences)


def build_plan(spec: MergeSpec) -> MergePlan:
    """Build the concat list and ffmpeg argv for a lossless merge.

    Raises:
        MergeError: For fewer than two inputs, or when the segments are not
            concat-compatible.
    """
    paths = spec.paths
    if len(paths) < _MIN_MERGE_INPUTS:
        message = (
            f"merge requires at least {_MIN_MERGE_INPUTS} inputs, got {len(paths)}"
        )
        raise MergeError(message)
    report = check_concat_compatibility(paths)
    if not report.ok:
        details = "\n".join(report.differences)
        message = f"inputs are not concat-compatible:\n{details}"
        raise MergeError(message)
    concat_list = _build_concat_list(paths)
    reference = probe(paths[0])
    argv = _build_argv(spec.out_path, reference.streams, paths[0])
    return MergePlan(concat_list=concat_list, argv=argv)
