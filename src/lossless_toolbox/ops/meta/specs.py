"""Container-level metadata, chapter, rotation and cover-art operation specs.

Every spec here is a Pydantic v2 value object whose :meth:`build_argv` returns
a complete ``ffmpeg`` command line. All four operations are container-level
edits — no pixel is decoded and no media stream is re-encoded. The one
deliberate exception is MP4 cover art, where the still image is muxed once into
a single PNG video stream (a container operation, not a media transcode); see
:class:`CoverSpec`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Final

from pydantic import (
    BaseModel,
    ConfigDict,
    PrivateAttr,
    field_validator,
    model_validator,
)
from typing_extensions import Self

# TODO(todo5): drop this inline fallback once ops/common.py is guaranteed merged.
try:
    from lossless_toolbox.ops.common import build_base_args
except ModuleNotFoundError:  # pragma: no cover - common.py lands with todo 5
    def build_base_args() -> list[str]:
        """Inline fallback mirroring ops/common.build_base_args."""
        return ["-hide_banner", "-nostdin", "-y"]

from .argv import cover_mimetype, is_mp4_like, suffix
from .errors import UnsupportedCoverError, UnsupportedRotateError
from .ffmetadata import ChapterArg, build_ffmetadata

_ROTATION_VALUES: Final[frozenset[int]] = frozenset({0, 90, 180, 270})


class MetadataEditSpec(BaseModel):
    """Edit container-level title, per-stream language and creation time.

    ``duration`` is the probed media duration used only for progress scaling
    (never in the argv).
    """

    model_config = ConfigDict(frozen=True)

    in_path: Path
    out_path: Path
    title: str | None = None
    language_map: dict[int, str] | None = None
    creation_time: str | None = None
    duration: float | None = None

    def build_argv(self) -> list[str]:
        """Build the stream-copy argv injecting the requested metadata."""
        argv = [*build_base_args(), "-i", str(self.in_path)]
        if self.title is not None:
            argv += ["-metadata", f"title={self.title}"]
        if self.language_map:
            for index in sorted(self.language_map):
                argv += [
                    f"-metadata:s:{index}",
                    f"language={self.language_map[index]}",
                ]
        if self.creation_time is not None:
            argv += ["-metadata", f"creation_time={self.creation_time}"]
        argv += ["-c", "copy", str(self.out_path)]
        return argv


class ChaptersSpec(BaseModel):
    """Write chapters via an ffmetadata sidecar file used as a second input.

    :meth:`build_argv` writes the ffmetadata text to a ``delete=False`` temp
    file and returns argv referencing it; call :meth:`cleanup` after the run to
    remove that temp file (idempotent, safe to call repeatedly). ``duration``
    is the probed media duration used only for progress scaling.
    """

    model_config = ConfigDict(frozen=True)

    in_path: Path
    out_path: Path
    chapters: list[ChapterArg]
    duration: float | None = None

    _ffmeta_tmp: Path | None = PrivateAttr(default=None)

    def build_argv(self) -> list[str]:
        """Write the ffmetadata sidecar and build the two-input argv."""
        self.cleanup()
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ffmetadata",
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp.write(build_ffmetadata(self.chapters))
            tmp.flush()
        self._ffmeta_tmp = Path(tmp.name)
        return [
            *build_base_args(),
            "-i",
            str(self.in_path),
            "-i",
            str(self._ffmeta_tmp),
            "-map_metadata",
            "0",
            "-map_chapters",
            "1",
            "-c",
            "copy",
            str(self.out_path),
        ]

    def cleanup(self) -> None:
        """Delete the ffmetadata temp file, if any (idempotent)."""
        if self._ffmeta_tmp is not None:
            self._ffmeta_tmp.unlink(missing_ok=True)
            self._ffmeta_tmp = None


class RotateSpec(BaseModel):
    """Write display-rotation metadata for MP4/MOV outputs (no pixel rotation).

    ``degrees`` is a clockwise user-facing rotation; ffmpeg's
    ``-display_rotation`` is counter-clockwise, so the emitted value is
    ``360 - degrees``. Rotation is only persisted by the MP4/MOV ``tkhd``
    matrix — Matroska has no standard rotation element, so an MKV target is
    rejected at construction. ``duration`` is the probed media duration used
    only for progress scaling (never in the argv).
    """

    model_config = ConfigDict(frozen=True)

    in_path: Path
    out_path: Path
    degrees: int
    duration: float | None = None

    @field_validator("degrees")
    @classmethod
    def _valid_degrees(cls, value: int) -> int:
        if value not in _ROTATION_VALUES:
            message = f"degrees must be one of 0/90/180/270, got {value}"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _check_target(self) -> Self:
        if not is_mp4_like(self.out_path):
            raise UnsupportedRotateError(suffix(self.out_path))
        return self

    def build_argv(self) -> list[str]:
        """Build the argv with ``-display_rotation`` before the input."""
        return [
            *build_base_args(),
            "-display_rotation:v:0",
            str(360 - self.degrees),
            "-i",
            str(self.in_path),
            "-c",
            "copy",
            str(self.out_path),
        ]


class CoverSpec(BaseModel):
    """Embed cover art (MP4/MOV ``attached_pic`` or MKV ``-attach``).

    ``duration`` is the probed media duration used only for progress scaling
    (never in the argv).
    """

    model_config = ConfigDict(frozen=True)

    in_path: Path
    out_path: Path
    image_path: Path
    duration: float | None = None

    @model_validator(mode="after")
    def _check_target(self) -> Self:
        cover_mimetype(self.image_path)
        if is_mp4_like(self.out_path) or suffix(self.out_path) == ".mkv":
            return self
        raise UnsupportedCoverError.for_container(suffix(self.out_path))

    def build_argv(self) -> list[str]:
        """Build the argv for the container's cover-art mechanism."""
        if suffix(self.out_path) == ".mkv":
            return self._mkv_argv()
        return self._mp4_argv()

    def _mp4_argv(self) -> list[str]:
        # One-shot PNG wrapping: the still image is muxed into a single PNG video
        # stream with the attached_pic disposition. This is a container operation
        # on the image, NOT a re-encode of the source A/V streams.
        return [
            *build_base_args(),
            "-i",
            str(self.in_path),
            "-i",
            str(self.image_path),
            "-map",
            "0",
            "-map",
            "1",
            "-c",
            "copy",
            "-c:v:1",
            "png",
            "-disposition:v:1",
            "attached_pic",
            str(self.out_path),
        ]

    def _mkv_argv(self) -> list[str]:
        return [
            *build_base_args(),
            "-i",
            str(self.in_path),
            "-map",
            "0",
            "-c",
            "copy",
            "-attach",
            str(self.image_path),
            "-metadata:s:t:0",
            f"mimetype={cover_mimetype(self.image_path)}",
            str(self.out_path),
        ]
