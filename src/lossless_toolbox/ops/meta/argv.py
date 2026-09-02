"""Shared argv-building helpers for the meta operations.

These helpers are package-internal (the public surface lives in
:mod:`lossless_toolbox.ops.meta`); they are non-underscored so sibling modules
can import them without tripping ``reportPrivateUsage``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Final

from lossless_toolbox.ffmpeg_locator import resolve

from .errors import UnsupportedCoverError

if TYPE_CHECKING:
    from pathlib import Path

_MP4_LIKE_SUFFIXES: Final[frozenset[str]] = frozenset({".mp4", ".mov"})
_COVER_MIMETYPES: Final[dict[str, str]] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    """Return the resolved ffmpeg binary path (cached across the process)."""
    return str(resolve("ffmpeg").path)


def suffix(path: Path) -> str:
    """Return the lower-cased file extension, e.g. ``".mkv"``."""
    return path.suffix.lower()


def is_mp4_like(path: Path) -> bool:
    """Return whether the path is an MP4/MOV-family container."""
    return suffix(path) in _MP4_LIKE_SUFFIXES


def cover_mimetype(image_path: Path) -> str:
    """Map a cover image extension to its MIME type, rejecting unknown ones."""
    mimetype = _COVER_MIMETYPES.get(suffix(image_path))
    if mimetype is None:
        raise UnsupportedCoverError.for_image(suffix(image_path))
    return mimetype
