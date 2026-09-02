"""Audio track extract/strip/replace via per-stream copy (todo 8).

Three lossless track operations, all stream-copy (never re-encoded):

* :class:`ExtractSpec` pulls one audio stream (``-map 0:a:N -c copy``); a bare
  ``.aac`` target adds ``-f adts`` so a raw ADTS elementary stream is written
  instead of an invalid MP4-flavoured one.
* :class:`StripSpec` keeps only the requested streams (``-map 0:<i>`` per keep)
  and copies each retained stream per output position.
* :class:`ReplaceSpec` swaps the audio track: ``in_path``'s video is kept, the
  first audio stream of ``new_audio_path`` becomes the new track
  (``-map 0:v -map 1:a:0 -c copy``). The new audio's codec is probed and
  checked against the target muxer before a command is built; an incompatible
  codec raises :class:`TrackError` with remux-first guidance.

Volume/delay/normalization and audio transcoding are deliberately out of scope
(re-encode domain); a replace whose codec the target muxer cannot carry is
rejected, never silently transcoded.
"""

from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from lossless_toolbox.ffmpeg_locator import resolve
from lossless_toolbox.models import StreamInfo
from lossless_toolbox.probe import probe

from .remux import muxer_supports

# ops/common.py (todo 5) is built by a parallel Wave-2 worker; prefer its
# shared argv fragments and fall back to inline stand-ins until it lands so
# this module stays importable on its own.
try:
    from .common import (  # type: ignore[reportMissingImports]
        build_base_args,
        copy_args,
        map_args,
    )
except ImportError:  # pragma: no cover - common lands in the same wave

    def build_base_args() -> list[str]:
        """Inline stand-in for ops.common.build_base_args."""
        return ["-hide_banner", "-nostdin", "-y"]

    def map_args(streams: list[int]) -> list[str]:
        """Inline stand-in for ops.common.map_args."""
        return [t for i in streams for t in ("-map", f"0:{i}")]

    def copy_args(streams: list[int]) -> list[str]:
        """Inline stand-in for ops.common.copy_args."""
        args = [t for p in range(len(streams)) for t in (f"-c:{p}", "copy")]
        args += ["-map_metadata", "0", "-ignore_unknown"]
        return args


class TrackError(RuntimeError):
    """Raised when a track operation cannot be planned.

    Subclasses :class:`RuntimeError` (not :class:`ValueError`) so pydantic does
    not wrap construction-time validation into a :class:`ValidationError`; the
    UI must catch THIS type to surface the actionable guidance.
    """


class ExtractSpec(BaseModel):
    """Extract a single audio stream to a standalone file (lossless copy).

    ``stream_index`` indexes the AUDIO streams of ``in_path`` (the ``N`` in
    ``-map 0:a:N``), not the global stream table. ``duration`` is the probed
    media duration used only for progress scaling (never in the argv).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["extract"] = "extract"
    in_path: Path
    stream_index: int = Field(default=0, ge=0)
    out_path: Path
    streams: list[StreamInfo]
    duration: float | None = None

    def build_argv(self) -> list[str]:
        """Build the extract argv, rejecting an out-of-range audio index.

        Raises:
            TrackError: When ``stream_index`` exceeds the audio stream count.
        """
        audio_streams = [s for s in self.streams if s.codec_type == "audio"]
        if self.stream_index >= len(audio_streams):
            message = (
                f"audio stream index {self.stream_index} out of range: "
                f"input has {len(audio_streams)} audio stream(s)"
            )
            raise TrackError(message)
        args = build_base_args()
        args += ["-i", str(self.in_path)]
        args += ["-map", f"0:a:{self.stream_index}"]
        args += ["-c", "copy"]
        if self.out_path.suffix.lower() == ".aac":
            args += ["-f", "adts"]
        args += [str(self.out_path)]
        return args


class StripSpec(BaseModel):
    """Keep only the requested streams, copying each one losslessly.

    ``duration`` is the probed media duration used only for progress scaling
    (never in the argv).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["strip"] = "strip"
    in_path: Path
    out_path: Path
    keep_streams: list[int]
    streams: list[StreamInfo]
    duration: float | None = None

    def build_argv(self) -> list[str]:
        """Build the strip argv, rejecting empty or out-of-range keeps.

        Raises:
            TrackError: When ``keep_streams`` is empty or references a stream
                index beyond the input's stream count.
        """
        if not self.keep_streams:
            message = "keep_streams must not be empty"
            raise TrackError(message)
        total = len(self.streams)
        for index in self.keep_streams:
            if not 0 <= index < total:
                message = (
                    f"keep stream index {index} out of range: "
                    f"input has {total} stream(s)"
                )
                raise TrackError(message)
        args = build_base_args()
        args += ["-i", str(self.in_path)]
        args += map_args(self.keep_streams)
        args += copy_args(self.keep_streams)
        args += [str(self.out_path)]
        return args


class ReplaceSpec(BaseModel):
    """Replace ``in_path``'s audio with ``new_audio_path``'s first audio stream.

    Video streams are kept (``-map 0:v``); the new file's first audio stream
    becomes the replacement track (``-map 1:a:0``), stream-copied. The new
    audio's codec is checked against the target muxer; an incompatible codec
    raises :class:`TrackError` (remux-first guidance) instead of silently
    transcoding.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["replace"] = "replace"
    in_path: Path
    out_path: Path
    new_audio_path: Path

    @property
    def out_container(self) -> str:
        """Return the target container name derived from the output suffix."""
        return self.out_path.suffix.lower().lstrip(".")

    def build_argv(
        self,
        new_audio_codec: str,
        *,
        probe_bin: Path | None = None,
    ) -> list[str]:
        """Build the replace argv for an already-probed new-audio codec.

        Args:
            new_audio_codec: The probed codec name of the new audio file's
                audio stream (e.g. ``"aac"``), from :func:`probe_audio_codec`.
            probe_bin: ffmpeg binary used for the ``-h muxer=`` capability
                probe; defaults to the located ffmpeg.

        Raises:
            TrackError: When the target muxer cannot carry ``new_audio_codec``.
        """
        ffmpeg = probe_bin if probe_bin is not None else resolve("ffmpeg").path
        compat = muxer_supports(self.out_container, new_audio_codec, ffmpeg)
        if not compat.ok:
            message = (
                f"new audio codec {new_audio_codec!r} cannot be written into "
                f"container {self.out_container!r}: {compat.reason}; remux the "
                f"new audio to a compatible container first"
            )
            raise TrackError(message)
        args = build_base_args()
        args += ["-i", str(self.in_path)]
        args += ["-i", str(self.new_audio_path)]
        args += ["-map", "0:v", "-map", "1:a:0"]
        args += ["-c", "copy"]
        args += [str(self.out_path)]
        return args


def probe_audio_codec(path: Path) -> str:
    """Return the first audio stream's codec name of ``path``.

    Raises:
        TrackError: When ``path`` has no audio stream.
    """
    media = probe(path)
    for stream in media.streams:
        if stream.codec_type == "audio":
            return stream.codec_name
    message = f"new audio file {path} has no audio stream"
    raise TrackError(message)


TrackSpec: TypeAlias = ExtractSpec | StripSpec | ReplaceSpec
