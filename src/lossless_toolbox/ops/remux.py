"""Lossless remux (container change) operation.

A remux rewrites the container around untouched elementary streams. The only
runtime process this module runs is the muxer capability probe inside
:func:`muxer_supports` — every argv is otherwise built purely.

Two design rules are load-bearing here (plan todo 5 / R1 §1.1):

* The target muxer is the authority on whether a codec can be carried. We probe
  ``ffmpeg -h muxer=<name>`` and never hardcode a container<->codec matrix;
  when the probe is inconclusive we allow and let ffmpeg report the real error.
* Subtitle streams are never silently transcoded. The MP4 family carries only
  ``mov_text`` subtitles, so remuxing an srt/ass/vobsub/pgs stream into MP4 is
  rejected up front (the UI offers "keep MKV / drop subtitle / use the subtitle
  operation") instead of emitting a command that would re-encode the text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path  # noqa: TC003 - pydantic resolves Path fields at runtime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

# StreamInfo is a Pydantic field type: it must stay importable at runtime.
from lossless_toolbox.models import StreamInfo  # noqa: TC001

from .common import build_base_args, copy_args, map_args, movflags

if TYPE_CHECKING:
    from collections.abc import Sequence

# MP4-family muxers carry only mov_text subtitles; everything else is a transcode.
_MOV_FAMILY = frozenset({"mp4", "mov", "m4v", "m4a", "m4b", "3gp", "3g2"})
_MOV_TEXT_CODECS = frozenset({"mov_text"})

# MPEG-TS stores H.264/HEVC in annex-B; MP4/MKV store it in length-prefixed form.
_TS_CONTAINERS = frozenset({"ts", "mts", "m2ts", "m2t"})
_ANNEXB_BSF = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb"}

_SUPPORTED_CODECS_RE = re.compile(
    r"Supported\s+(?:video|audio|subtitle)\s+codecs:"
)


class RemuxError(RuntimeError):
    """Base error for remux argv construction."""


class SubtitleIncompatibleError(RemuxError):
    """Raised when a subtitle stream cannot be losslessly copied into the target.

    Carries the :class:`CompatResult` reason so the UI can surface the
    "keep MKV / drop subtitle / mov_text transcode" choice.
    """


class CompatResult(BaseModel):
    """A muxer/codec compatibility verdict."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    reason: str | None = None


def _probe_muxer(container: str, probe_bin: Path) -> str | None:
    """Run ``ffmpeg -h muxer=<container>`` and return stdout, or None on failure."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv list, no shell
            [str(probe_bin), "-hide_banner", "-h", f"muxer={container}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_supported_codecs(stdout: str) -> frozenset[str] | None:
    """Parse a ``Supported ... codecs:`` enumeration out of muxer help.

    ffmpeg releases up to and including 7.x do NOT print this section (they only
    print ``Default ... codec`` hints); the parser exists so that a future
    ffmpeg which does enumerate codecs yields an authoritative answer. Returns
    ``None`` when no enumeration is present.
    """
    codecs: list[str] = []
    in_section = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if _SUPPORTED_CODECS_RE.search(stripped):
            in_section = True
            continue
        if not in_section:
            continue
        if not stripped:
            in_section = False
            continue
        # A new section (AVOptions / next header) ends the codec block.
        if stripped.endswith(("AVOptions:", ":")):
            in_section = False
            continue
        codecs.extend(stripped.split())
    return frozenset(codecs) if codecs else None


def muxer_supports(container: str, codec_name: str, probe_bin: Path) -> CompatResult:
    """Return whether ``container``'s muxer can carry ``codec_name``.

    Args:
        container: The target container name (e.g. ``"mp4"``, ``"mkv"``).
        codec_name: The probed codec name (e.g. ``"h264"``, ``"aac"``).
        probe_bin: Path to the ffmpeg binary used for ``-h muxer=``.

    Returns:
        ``ok=True`` when the codec is listed, or when the probe is inconclusive
        (the iron rule: never hardcode a matrix — let ffmpeg report the error).
        ``ok=False`` with a reason only when an authoritative enumeration
        explicitly omits the codec.
    """
    stdout = _probe_muxer(container, probe_bin)
    if stdout is None:
        return CompatResult(ok=True, reason="muxer probe unavailable")
    supported = _parse_supported_codecs(stdout)
    if supported is None:
        # No codec enumeration in this ffmpeg build — allow and defer to ffmpeg.
        return CompatResult(ok=True, reason=None)
    if codec_name in supported:
        return CompatResult(ok=True, reason=None)
    return CompatResult(
        ok=False,
        reason=f"muxer {container!r} does not list codec {codec_name!r}",
    )


def check_subtitle_compat(
    container: str, streams: Sequence[StreamInfo]
) -> CompatResult:
    """Return whether every subtitle stream can be copied into ``container``.

    The MP4 family carries only ``mov_text``; any other subtitle codec would
    require a text-level transcode and is therefore reported as incompatible
    rather than silently converted.
    """
    if container not in _MOV_FAMILY:
        return CompatResult(ok=True, reason=None)
    for stream in streams:
        if stream.codec_type != "subtitle":
            continue
        if stream.codec_name not in _MOV_TEXT_CODECS:
            return CompatResult(
                ok=False,
                reason=(
                    f"subtitle stream {stream.index} ({stream.codec_name}) "
                    f"cannot be copied into {container!r}: MP4-family "
                    f"containers carry only mov_text subtitles; this would "
                    f"require a mov_text text transcode. Use the subtitle "
                    f"operation or drop the subtitle stream instead."
                ),
            )
    return CompatResult(ok=True, reason=None)


class RemuxSpec(BaseModel):
    """A remux job: copy every stream of ``in_path`` into ``out_path``.

    ``streams`` is the ffprobe result for ``in_path``; it drives the TS
    annex-B bitstream filter and the subtitle compatibility guard, and is
    provided by the caller so :meth:`build_argv` stays pure.
    """

    model_config = ConfigDict(frozen=True)

    in_path: Path
    out_path: Path
    streams: list[StreamInfo]

    @property
    def out_container(self) -> str:
        """Return the target container name derived from the output suffix."""
        return self.out_path.suffix.lower().lstrip(".")

    def _bsf_args(self, container: str) -> list[str]:
        """Emit the annex-B bitstream filter when remuxing H.264/HEVC into TS."""
        if container not in _TS_CONTAINERS:
            return []
        for stream in self.streams:
            if stream.codec_type != "video":
                continue
            bsf = _ANNEXB_BSF.get(stream.codec_name)
            if bsf is not None:
                return ["-bsf:v", bsf]
        return []

    def build_argv(self) -> list[str]:
        """Build the full remux argv, rejecting incompatible subtitles first."""
        container = self.out_container
        compat = check_subtitle_compat(container, self.streams)
        if not compat.ok:
            raise SubtitleIncompatibleError(compat.reason or "subtitle incompatible")

        indices = [stream.index for stream in self.streams]
        args = build_base_args()
        args += ["-i", str(self.in_path)]
        args += map_args(indices)
        args += copy_args(indices)
        args += self._bsf_args(container)
        if container == "aac":
            args += ["-f", "adts"]
        args += movflags(container)
        args += [str(self.out_path)]
        return args
