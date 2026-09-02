"""Soft-subtitle mux and detach operations.

The subtitle matrix is the one place in the lossless toolbox where a ``copy``
operation can silently become a text-level transcode (plan todo 9 / R1 §1.5):
MP4's only native text format is ``mov_text``, so an SRT subtitle muxed into
MP4 is re-encoded as mov_text — content-preserving but not a stream copy. Every
spec therefore carries ``transcode_warning`` so the UI can surface the red-bar
warning instead of silently labelling the result "lossless".

Mux matrix (by target container and subtitle format):

    mkv  + srt/ass/webvtt   -> ``-c:s copy``        (no warning)
    mp4  + srt              -> ``-c:s mov_text``    (warning)
    mp4  + ass/webvtt/dvb   -> SubtitleUnsupportedError (construction time)
    webm + webvtt           -> ``-c:s copy``        (no warning)
    webm + srt/ass          -> SubtitleUnsupportedError

Detach matrix (always outputs SRT):

    source srt/subrip       -> ``-c:s copy``        (no warning)
    source mov_text/ass/webvtt -> ``-c:s text``     (warning, text conversion)
    source bitmap (dvb/pgs) -> SubtitleUnsupportedError
"""

from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ops/common.py (todo 5) is built in a parallel wave. Prefer its shared base
# argv; fall back to an inline stand-in until it lands so this module stays
# importable on its own.
try:
    from .common import build_base_args  # type: ignore[reportMissingImports]
except ImportError:
    def build_base_args() -> list[str]:
        """Inline stand-in for ops.common.build_base_args (todo 5)."""
        return ["-hide_banner", "-nostdin", "-y"]

_MOV_FAMILY = frozenset({"mp4", "m4v", "mov"})
_TEXT_SUBS = frozenset({"mov_text", "ass", "ssa", "webvtt"})
_SRT_CODECS = frozenset({"srt", "subrip"})


class SubtitleUnsupportedError(RuntimeError):
    """Raised when a subtitle codec cannot be carried by the target losslessly.

    Carries the offending ``codec`` and ``container`` plus an ``alternative``
    hint the UI can surface as the "keep MKV / accept mov_text style loss"
    choice, defaulting to rejection.

    Subclasses :class:`RuntimeError` (not :class:`ValueError`) deliberately:
    pydantic converts ``ValueError``/``AssertionError`` raised from a validator
    into a wrapped ``ValidationError``, but the UI must catch THIS type to
    offer the alternative.
    """

    def __init__(self, container: str, codec: str, *, alternative: str) -> None:
        """Record the offending pair and a user-facing alternative."""
        self.container = container
        self.codec = codec
        self.alternative = alternative
        super().__init__(
            f"subtitle codec {codec!r} cannot be written into "
            f"{container!r}: {alternative}"
        )


def _container_kind(suffix: str) -> str:
    """Normalize a filename suffix to a container kind token."""
    return suffix.lstrip(".").lower()


def _mux_codec(container: str, codec: str) -> str:
    """Return the output subtitle codec (or ``"copy"``) for muxing.

    ``"copy"`` is a lossless stream copy; ``"mov_text"`` marks the one
    text-level transcode. Raises :class:`SubtitleUnsupportedError` when the
    codec/container pair cannot be carried.
    """
    if container == "mkv":
        return "copy"
    if container in _MOV_FAMILY:
        if codec in _SRT_CODECS:
            return "mov_text"
        if codec == "dvb_subtitle":
            raise SubtitleUnsupportedError(
                container,
                codec,
                alternative="dvb_subtitle is a bitmap format with no MP4 "
                "text representation",
            )
        raise SubtitleUnsupportedError(
            container,
            codec,
            alternative="keep the file in MKV to preserve the subtitle "
            "byte-for-byte, or accept a text-level conversion to mov_text "
            "(styling is lost); the UI offers this choice and rejects by "
            "default",
        )
    if container == "webm":
        if codec == "webvtt":
            return "copy"
        raise SubtitleUnsupportedError(
            container,
            codec,
            alternative="WebM carries only WebVTT subtitles; keep the "
            "subtitle in MKV instead",
        )
    return "copy"


def _detach_codec(source_codec: str) -> str:
    """Return the output subtitle codec for extracting ``source_codec`` to SRT.

    ``"copy"`` is lossless; ``"text"`` is the subrip encoder (a text-level
    conversion from mov_text/ass/webvtt). Bitmap subtitles cannot become SRT
    and raise :class:`SubtitleUnsupportedError`.
    """
    if source_codec in _SRT_CODECS:
        return "copy"
    if source_codec in _TEXT_SUBS:
        return "text"
    raise SubtitleUnsupportedError(
        container="srt",
        codec=source_codec,
        alternative="bitmap subtitles (dvb_subtitle, PGS, VobSub) cannot be "
        "converted to a text SRT",
    )


class MuxSpec(BaseModel):
    """Mux an external subtitle file into a video container.

    The target container is derived from ``out_path``; unsupported
    codec/container pairs are rejected at construction so the UI can offer the
    "keep MKV or accept style loss" choice before any ffmpeg call.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["mux"] = "mux"
    in_path: Path
    sub_path: Path
    sub_fmt: Literal["srt", "ass", "webvtt"]
    out_path: Path
    transcode_warning: bool = False

    @model_validator(mode="after")
    def _reject_unsupported_target(self) -> "MuxSpec":
        """Reject unsupported codec/container pairs at construction.

        Raises :class:`SubtitleUnsupportedError` (a ``RuntimeError``, so
        pydantic does not wrap it into a ``ValidationError``), giving the UI a
        chance to offer the "keep MKV or accept mov_text style loss" choice
        before any ffmpeg call.
        """
        _mux_codec(_container_kind(self.out_path.suffix), self.sub_fmt)
        return self

    def build_argv(self) -> list[str]:
        """Build the ffmpeg argv and set ``transcode_warning`` accordingly."""
        codec = _mux_codec(_container_kind(self.out_path.suffix), self.sub_fmt)
        self.transcode_warning = codec != "copy"
        return [
            *build_base_args(),
            "-i", str(self.in_path),
            "-i", str(self.sub_path),
            "-map", "0",
            "-map", "1",
            "-c", "copy",
            "-c:s", codec,
            str(self.out_path),
        ]


class DetachSpec(BaseModel):
    """Extract a subtitle stream to a standalone SRT file.

    The source codec is not part of the spec (it is probed separately), so the
    copy-vs-convert decision happens in :meth:`build_argv`.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["detach"] = "detach"
    in_path: Path
    out_path: Path
    stream_index: int = Field(default=0, ge=0)
    transcode_warning: bool = False

    def build_argv(self, source_codec: str) -> list[str]:
        """Build the ffmpeg argv and set ``transcode_warning`` accordingly.

        Args:
            source_codec: The probed subtitle codec name (e.g. ``"subrip"`` or
                ``"mov_text"``).
        """
        codec = _detach_codec(source_codec)
        self.transcode_warning = codec != "copy"
        return [
            *build_base_args(),
            "-i", str(self.in_path),
            "-map", f"0:s:{self.stream_index}",
            "-c:s", codec,
            "-f", "srt",
            str(self.out_path),
        ]


SubtitleSpec: TypeAlias = MuxSpec | DetachSpec
