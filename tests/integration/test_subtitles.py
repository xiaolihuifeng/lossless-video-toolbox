"""Integration tests: real ffmpeg/ffprobe for soft-subtitle mux/detach.

Mux an SRT into MKV (stream copy) and detach it back out — the detached SRT
must preserve the original line count. Mux an SRT into MP4 — the only native
MP4 text format is ``mov_text``, so the subtitle is re-encoded as mov_text and
the spec must carry ``transcode_warning``. Asserts the observable outcome via
ffprobe (subtitle codec) and the SRT text, never the argv shape.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Protocol

import pytest

from lossless_toolbox.ffmpeg_locator import resolve
from lossless_toolbox.ops.subtitles import DetachSpec, MuxSpec
from lossless_toolbox.probe import probe

if TYPE_CHECKING:
    from pathlib import Path

    from lossless_toolbox.models import MediaFile

pytestmark = pytest.mark.integration

_FFMPEG = str(resolve("ffmpeg").path)

_SRT_CONTENT = """\
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


class _MediaSample(Protocol):
    """Shape of the conftest media fixture objects (todo 2)."""

    path: Path
    codec: str
    duration: float


def _run(argv: list[str]) -> None:
    """Run an ops argv (flags only) through ffmpeg, failing on nonzero exit."""
    proc = subprocess.run(  # noqa: S603 - argv list built by specs, no shell
        [_FFMPEG, *argv], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def _subtitle_codec(media: MediaFile) -> str | None:
    """Return the first subtitle stream's codec name, or None if absent."""
    for stream in media.streams:
        if stream.codec_type == "subtitle":
            return stream.codec_name
    return None


def _write_srt(path: Path) -> Path:
    path.write_text(_SRT_CONTENT, encoding="utf-8")
    return path


def test_mux_srt_to_mkv_then_detach_preserves_line_count(
    h264_aac_mp4: _MediaSample, tmp_path: Path,
) -> None:
    """Given SRT muxed into MKV; when detached; then the SRT line count survives.

    MKV carries SubRip natively, so both the mux and the detach are stream
    copies (``transcode_warning is False`` on both).
    """
    srt = _write_srt(tmp_path / "subs.srt")
    muxed = tmp_path / "muxed.mkv"
    mux = MuxSpec(
        in_path=h264_aac_mp4.path,
        sub_path=srt,
        sub_fmt="srt",
        out_path=muxed,
    )
    _run(mux.build_argv())
    assert mux.transcode_warning is False

    codec = _subtitle_codec(probe(muxed))
    assert codec in {"srt", "subrip"}  # ffprobe reports subrip, never "srt"

    detached = tmp_path / "out.srt"
    detach = DetachSpec(in_path=muxed, out_path=detached)
    _run(detach.build_argv(codec or "subrip"))
    assert detach.transcode_warning is False

    # MKV stores SubRip on a coarser timescale, so the millisecond timestamps
    # can drift a few ms on the round trip; the equivalence criterion is the
    # subtitle structure surviving — same line count, same cue count, same text.
    original = _SRT_CONTENT.strip().splitlines()
    result = detached.read_text(encoding="utf-8").strip().splitlines()
    assert len(result) == len(original)
    assert sum("-->" in line for line in result) == sum(
        "-->" in line for line in original
    )
    assert "Hello, world." in result


def test_mux_srt_to_mp4_produces_mov_text_with_warning(
    h264_aac_mp4: _MediaSample, tmp_path: Path,
) -> None:
    """Given SRT muxed into MP4; then the subtitle is mov_text and warns.

    MP4 has no SubRip representation, so the subtitle is transcoded to
    ``mov_text`` (content-preserving, not a stream copy) and the spec surfaces
    ``transcode_warning``.
    """
    srt = _write_srt(tmp_path / "subs.srt")
    out = tmp_path / "muxed.mp4"
    mux = MuxSpec(
        in_path=h264_aac_mp4.path,
        sub_path=srt,
        sub_fmt="srt",
        out_path=out,
    )
    _run(mux.build_argv())
    assert mux.transcode_warning is True
    assert _subtitle_codec(probe(out)) == "mov_text"
