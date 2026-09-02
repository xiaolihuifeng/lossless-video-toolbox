"""Metadata, chapters, rotation and cover-art operations (public API).

Four stream-copy operation specs — :class:`MetadataEditSpec`,
:class:`ChaptersSpec`, :class:`RotateSpec` and :class:`CoverSpec` — each build a
complete ``ffmpeg`` argv. Chapter input/output is expressed through
:class:`ChapterArg` plus :func:`build_ffmetadata` (text) and
:func:`to_ffmetadata` (export argv).
"""

from .errors import UnsupportedCoverError, UnsupportedRotateError
from .ffmetadata import ChapterArg, build_ffmetadata, to_ffmetadata
from .specs import ChaptersSpec, CoverSpec, MetadataEditSpec, RotateSpec

__all__ = [
    "ChapterArg",
    "ChaptersSpec",
    "CoverSpec",
    "MetadataEditSpec",
    "RotateSpec",
    "UnsupportedCoverError",
    "UnsupportedRotateError",
    "build_ffmetadata",
    "to_ffmetadata",
]
