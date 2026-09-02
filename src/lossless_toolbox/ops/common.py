"""Shared ffmpeg argv fragments for stream-copy operations.

Every builder here is PURE: it appends tokens to a ``list[str]`` and never
spawns a process. The ops modules compose these fragments into full command
lines; the one place a process may run is :func:`remux.muxer_supports`.

The ``copy_args`` contract is subtle and load-bearing: ``map_args`` selects
streams by INPUT index (``-map 0:<i>``), while ``copy_args`` codes them by
OUTPUT position (``-c:<0..n-1> copy``). The two coincide for remux (which maps
every stream in order) but diverge for selective operations (tracks/subtitles),
so each takes the same ``streams`` sequence and derives its own index.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Containers whose MP4-family muxer benefits from a front-loaded moov atom.
_MOV_CONTAINERS = frozenset({"mp4", "mov", "m4v", "m4a", "m4b", "3gp", "3g2"})


def build_base_args() -> list[str]:
    """Return the common ffmpeg front flags: quiet banner, no stdin, overwrite."""
    return ["-hide_banner", "-nostdin", "-y"]


def map_args(streams: Sequence[int]) -> list[str]:
    """Emit ``-map 0:<i>`` for each input stream index in ``streams``."""
    args: list[str] = []
    for index in streams:
        args += ["-map", f"0:{index}"]
    return args


def copy_args(streams: Sequence[int]) -> list[str]:
    """Emit ``-c:<i> copy`` per output position, plus metadata-copy flags.

    Args:
        streams: The mapped input stream indices (their count drives the output
            position; their values are irrelevant here — :func:`map_args`
            already consumed them by input index).

    Returns:
        ``["-c:0", "copy", "-c:1", "copy", ..., "-map_metadata", "0",
        "-ignore_unknown"]``.
    """
    args: list[str] = []
    for position in range(len(streams)):
        args += [f"-c:{position}", "copy"]
    args += ["-map_metadata", "0", "-ignore_unknown"]
    return args


def movflags(container: str) -> list[str]:
    """Emit ``-movflags +faststart`` for MP4-family targets, else nothing."""
    if container in _MOV_CONTAINERS:
        return ["-movflags", "+faststart"]
    return []


def metadata_args(meta: Mapping[str, str]) -> list[str]:
    """Emit ``-metadata key=value`` for each entry in ``meta``."""
    args: list[str] = []
    for key, value in meta.items():
        args += ["-metadata", f"{key}={value}"]
    return args
