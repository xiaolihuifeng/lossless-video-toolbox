"""Unit tests for the meta ops argv builders (metadata/chapters/rotate/cover).

These tests lock the exact ``ffmpeg`` argv each spec produces and the
construction-time rejection branches (MKV+rotate, invalid degrees, unknown
container cover, unknown image format). No ffmpeg process is spawned here —
that is the integration test's job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lossless_toolbox.ops.meta import (
    ChapterArg,
    ChaptersSpec,
    CoverSpec,
    MetadataEditSpec,
    RotateSpec,
    UnsupportedCoverError,
    UnsupportedRotateError,
    build_ffmetadata,
    to_ffmetadata,
)

pytestmark = pytest.mark.unit

_IN = Path("in.mp4")
_OUT = Path("out.mp4")
_BASE = ["-hide_banner", "-nostdin", "-y"]


def _assert_binary(argv: list[str]) -> None:
    assert argv[0].endswith("ffmpeg")


# ─────────────────────────────────────────────────────────────────────────────
# MetadataEditSpec
# ─────────────────────────────────────────────────────────────────────────────


def test_metadata_edit_full_argv() -> None:
    """Given title+language_map+creation_time; then all -metadata forms appear."""
    spec = MetadataEditSpec(
        in_path=_IN,
        out_path=_OUT,
        title="My Movie",
        language_map={1: "eng"},
        creation_time="2024-01-01T00:00:00Z",
    )
    argv = spec.build_argv()
    _assert_binary(argv)
    assert argv[1:] == [
        *_BASE,
        "-i", "in.mp4",
        "-metadata", "title=My Movie",
        "-metadata:s:1", "language=eng",
        "-metadata", "creation_time=2024-01-01T00:00:00Z",
        "-c", "copy",
        "out.mp4",
    ]


def test_metadata_edit_minimal_argv() -> None:
    """Given no optional fields; then argv degrades to a plain stream copy."""
    spec = MetadataEditSpec(in_path=_IN, out_path=_OUT)
    assert spec.build_argv()[1:] == [
        *_BASE, "-i", "in.mp4", "-c", "copy", "out.mp4",
    ]


def test_metadata_edit_language_map_sorted() -> None:
    """Given an unsorted language_map; then languages are emitted by index."""
    spec = MetadataEditSpec(
        in_path=_IN, out_path=_OUT, language_map={2: "fre", 0: "eng"},
    )
    argv = spec.build_argv()
    assert "-metadata:s:0" in argv
    assert "-metadata:s:2" in argv
    assert argv.index("-metadata:s:0") < argv.index("-metadata:s:2")


# ─────────────────────────────────────────────────────────────────────────────
# ChaptersSpec + ffmetadata text
# ─────────────────────────────────────────────────────────────────────────────


def testbuild_ffmetadata_derives_end_from_next_start() -> None:
    """Given contiguous chapters; then each END is the next chapter's START."""
    text = build_ffmetadata([
        ChapterArg(start_time=0.0, title="Intro"),
        ChapterArg(start_time=3.0, title="Middle"),
        ChapterArg(start_time=6.0, title="Outro", end_time=9.0),
    ])
    assert text.startswith(";FFMETADATA1\n")
    assert "START=0\nEND=3000\ntitle=Intro" in text
    assert "START=3000\nEND=6000\ntitle=Middle" in text
    assert "START=6000\nEND=9000\ntitle=Outro" in text


def testbuild_ffmetadata_escapes_title() -> None:
    """Given reserved chars in a title; then they are backslash-escaped."""
    text = build_ffmetadata([ChapterArg(start_time=0.0, title="A=B;C", end_time=1.0)])
    assert "title=A\\=B\\;C" in text


def testbuild_ffmetadata_rejects_empty() -> None:
    """Given no chapters; then ValueError is raised."""
    with pytest.raises(ValueError, match="empty"):
        build_ffmetadata([])


def testbuild_ffmetadata_last_chapter_requires_end() -> None:
    """Given a trailing chapter without end_time; then ValueError is raised."""
    with pytest.raises(ValueError, match="end_time"):
        build_ffmetadata([ChapterArg(start_time=0.0, title="Only")])


def test_chapters_spec_writes_temp_file_and_builds_argv() -> None:
    """Given chapters; then a temp ffmetadata file becomes the second input."""
    spec = ChaptersSpec(
        in_path=_IN,
        out_path=_OUT,
        chapters=[
            ChapterArg(start_time=0.0, title="Intro"),
            ChapterArg(start_time=3.0, title="End", end_time=6.0),
        ],
    )
    ffmeta_path: Path | None = None
    try:
        argv = spec.build_argv()
        _assert_binary(argv)
        inputs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-i"]
        assert inputs[0] == "in.mp4"
        ffmeta_path = Path(inputs[1])
        assert ffmeta_path.suffix == ".ffmetadata"
        assert ffmeta_path.is_file()
        assert argv[argv.index("-map_metadata") + 1] == "0"
        assert argv[argv.index("-map_chapters") + 1] == "1"
        assert argv[-1] == "out.mp4"
    finally:
        spec.cleanup()
    assert ffmeta_path is not None
    assert not ffmeta_path.exists()


# ─────────────────────────────────────────────────────────────────────────────
# RotateSpec
# ─────────────────────────────────────────────────────────────────────────────


def test_rotate_mp4_argv_90() -> None:
    """Given degrees=90 on MP4; then -display_rotation 270 precedes -i."""
    spec = RotateSpec(in_path=_IN, out_path=_OUT, degrees=90)
    argv = spec.build_argv()
    _assert_binary(argv)
    rotation_index = argv.index("-display_rotation:v:0")
    assert argv[rotation_index + 1] == "270"
    assert rotation_index < argv.index("-i")
    assert argv[1:] == [
        *_BASE,
        "-display_rotation:v:0", "270",
        "-i", "in.mp4",
        "-c", "copy",
        "out.mp4",
    ]


def test_rotate_mp4_argv_180() -> None:
    """Given degrees=180; then -display_rotation is 360-180=180."""
    spec = RotateSpec(in_path=_IN, out_path=Path("out.mov"), degrees=180)
    argv = spec.build_argv()
    assert argv[argv.index("-display_rotation:v:0") + 1] == "180"


def test_rotate_rejects_mkv() -> None:
    """Given an MKV target; then construction raises (Matroska has no element)."""
    with pytest.raises(ValidationError, match="Matroska"):
        RotateSpec(in_path=_IN, out_path=Path("out.mkv"), degrees=90)


def test_rotate_rejects_invalid_degrees() -> None:
    """Given degrees not in 0/90/180/270; then construction raises ValueError."""
    with pytest.raises(ValidationError, match="degrees"):
        RotateSpec(in_path=_IN, out_path=_OUT, degrees=45)


# ─────────────────────────────────────────────────────────────────────────────
# CoverSpec
# ─────────────────────────────────────────────────────────────────────────────


def test_cover_mp4_argv() -> None:
    """Given an MP4 target; then attached_pic disposition argv is produced."""
    spec = CoverSpec(in_path=_IN, out_path=_OUT, image_path=Path("cover.png"))
    argv = spec.build_argv()
    _assert_binary(argv)
    assert argv[1:] == [
        *_BASE,
        "-i", "in.mp4",
        "-i", "cover.png",
        "-map", "0",
        "-map", "1",
        "-c", "copy",
        "-c:v:1", "png",
        "-disposition:v:1", "attached_pic",
        "out.mp4",
    ]


def test_cover_mkv_argv_png() -> None:
    """Given an MKV target; then -attach + mimetype argv is produced."""
    spec = CoverSpec(
        in_path=_IN, out_path=Path("out.mkv"), image_path=Path("cover.png"),
    )
    argv = spec.build_argv()
    assert "-attach" in argv
    assert argv[argv.index("-attach") + 1] == "cover.png"
    assert "-metadata:s:t:0" in argv
    assert "mimetype=image/png" in argv


def test_cover_mkv_argv_jpeg_mimetype() -> None:
    """Given a .jpg cover; then the mimetype is image/jpeg."""
    spec = CoverSpec(
        in_path=_IN, out_path=Path("out.mkv"), image_path=Path("cover.jpg"),
    )
    assert "mimetype=image/jpeg" in spec.build_argv()


def test_cover_rejects_unknown_container() -> None:
    """Given an unknown container; then construction raises UnsupportedCover."""
    with pytest.raises(ValidationError, match="cover"):
        CoverSpec(in_path=_IN, out_path=Path("out.avi"), image_path=Path("cover.png"))


def test_cover_rejects_unknown_image_format() -> None:
    """Given a non-jpeg/png cover; then construction raises UnsupportedCover."""
    with pytest.raises(ValidationError, match="cover image"):
        CoverSpec(in_path=_IN, out_path=Path("out.mkv"), image_path=Path("cover.gif"))


# ─────────────────────────────────────────────────────────────────────────────
# to_ffmetadata + error taxonomy
# ─────────────────────────────────────────────────────────────────────────────


def test_to_ffmetadata_argv() -> None:
    """Given a path; then the ffmetadata export argv ends with -f ffmetadata -."""
    argv = to_ffmetadata(Path("in.mkv"))
    _assert_binary(argv)
    assert "-map_chapters" in argv
    assert "-f" in argv
    assert argv[argv.index("-f") + 1] == "ffmetadata"
    assert argv[-1] == "-"


def test_error_types_are_value_errors() -> None:
    """Given the custom errors; then they subclass ValueError."""
    assert issubclass(UnsupportedRotateError, ValueError)
    assert issubclass(UnsupportedCoverError, ValueError)
