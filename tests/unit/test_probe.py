"""Unit tests for the ffprobe probe layer's pure parsing logic.

These tests exercise the parsing boundary against synthetic ffprobe JSON
dicts and synthetic display-matrix strings. No ffprobe process is spawned
here (that is the integration test's job), so the suite stays fast and
hermetic while locking the typed-model contract.
"""

from pathlib import Path

import pytest

from lossless_toolbox.models import KeyframeIndex, MediaFile, StreamInfo
from lossless_toolbox.probe import (
    ProbeError,
    _build_media_file,
    _build_stream,
    _keyframe_times,
    _matrix_values_from_displaymatrix,
    _rotation_from_matrix_values,
    _rotation_from_side_data,
)

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# Rotation derivation
# ─────────────────────────────────────────────────────────────────────────────


def test_rotation_from_matrix_identity_is_zero() -> None:
    """Given an identity display matrix; when parsed; then rotation is 0."""
    m = [65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824]
    assert _rotation_from_matrix_values(m) == 0


def test_rotation_from_matrix_90_degrees() -> None:
    """Given a 90-degree CCW matrix; when parsed; then rotation is 90."""
    m = [0, -65536, 0, 65536, 0, 0, 0, 0, 1073741824]
    assert _rotation_from_matrix_values(m) == 90


def test_rotation_from_matrix_180_degrees() -> None:
    """Given a 180-degree matrix; when parsed; then rotation is 180."""
    m = [-65536, 0, 0, 0, -65536, 0, 0, 0, 1073741824]
    assert _rotation_from_matrix_values(m) == 180


def test_rotation_from_matrix_270_degrees() -> None:
    """Given a 270-degree CCW matrix; when parsed; then rotation is 270."""
    m = [0, 65536, 0, -65536, 0, 0, 0, 0, 1073741824]
    assert _rotation_from_matrix_values(m) == 270


def test_rotation_from_matrix_too_short_is_none() -> None:
    """Given fewer than 9 matrix values; when parsed; then rotation is None."""
    assert _rotation_from_matrix_values([1, 0, 0, 0, 1, 0]) is None


def test_matrix_values_from_hexdump_strips_addresses() -> None:
    """Given ffprobe's hexdump displaymatrix; then the 9 matrix ints are found.

    The hexdump interleaves ``ADDRESS:`` prefixes that must NOT be mistaken
    for matrix elements.
    """
    hexdump = (
        "\n00000000:            0      -65536           0\n"
        "00000001:        65536           0           0\n"
        "00000002:            0           0  1073741824\n"
    )
    assert _matrix_values_from_displaymatrix(hexdump) == [
        0, -65536, 0, 65536, 0, 0, 0, 0, 1073741824,
    ]


def test_rotation_from_side_data_prefers_rotation_field() -> None:
    """Given side data with a numeric rotation field; then that value is used."""
    side_data = [
        {"side_data_type": "Display Matrix", "rotation": -90, "displaymatrix": "..."},
    ]
    assert _rotation_from_side_data(side_data) == 270


def test_rotation_from_side_data_falls_back_to_matrix() -> None:
    """Given side data without rotation but with displaymatrix; then parse it."""
    side_data = [
        {
            "side_data_type": "Display Matrix",
            "displaymatrix": (
                "\n00000000:            0      -65536           0\n"
                "00000001:        65536           0           0\n"
                "00000002:            0           0  1073741824\n"
            ),
        },
    ]
    assert _rotation_from_side_data(side_data) == 90


def test_rotation_from_side_data_absent_is_none() -> None:
    """Given no display-matrix side data; then rotation is None."""
    assert _rotation_from_side_data([]) is None
    assert _rotation_from_side_data([{"side_data_type": "Other"}]) is None


# ─────────────────────────────────────────────────────────────────────────────
# Stream parsing
# ─────────────────────────────────────────────────────────────────────────────

_VIDEO_STREAM = {
    "index": 0,
    "codec_name": "h264",
    "codec_type": "video",
    "width": 320,
    "height": 240,
    "disposition": {"default": 1, "dub": 0, "forced": 0},
    "tags": {"language": "und", "handler_name": "VideoHandler"},
}

_AUDIO_STREAM = {
    "index": 1,
    "codec_name": "aac",
    "codec_type": "audio",
    "sample_rate": "44100",
    "channels": 1,
    "disposition": {"default": 1},
    "tags": {"language": "eng"},
}


def test_build_stream_extracts_video_fields() -> None:
    """Given a video stream dict; when parsed; then width/height are typed ints."""
    info = _build_stream(_VIDEO_STREAM)
    assert isinstance(info, StreamInfo)
    assert info.index == 0
    assert info.codec_type == "video"
    assert info.codec_name == "h264"
    assert info.width == 320
    assert info.height == 240
    assert info.sample_rate is None
    assert info.channels is None


def test_build_stream_extracts_audio_fields() -> None:
    """Given an audio stream dict; then sample_rate/channels coerce to int."""
    info = _build_stream(_AUDIO_STREAM)
    assert info.sample_rate == 44100
    assert info.channels == 1
    assert info.width is None


def test_build_stream_language_comes_from_tags() -> None:
    """Given tags.language; then it is surfaced on the typed model."""
    assert _build_stream(_AUDIO_STREAM).language == "eng"


def test_build_stream_disposition_coerces_to_bool() -> None:
    """Given disposition ints (0/1); then they coerce to bool."""
    info = _build_stream(_VIDEO_STREAM)
    assert info.disposition == {"default": True, "dub": False, "forced": False}
    assert info.disposition["default"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Media file assembly
# ─────────────────────────────────────────────────────────────────────────────


def _raw_media() -> dict:
    return {
        "streams": [_VIDEO_STREAM, _AUDIO_STREAM],
        "chapters": [],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "12.000000",
            "nb_streams": 2,
        },
    }


def test_build_media_file_typed_fields() -> None:
    """Given raw ffprobe JSON; then a MediaFile is built with typed fields."""
    sample = Path("sample.mp4")
    media = _build_media_file(sample, _raw_media())
    assert isinstance(media, MediaFile)
    assert media.path == sample
    assert media.format_name == "mov,mp4,m4a,3gp,3g2,mj2"
    assert media.duration == pytest.approx(12.0)
    assert len(media.streams) == 2
    assert media.chapters is None


def test_build_media_file_with_chapters() -> None:
    """Given chapters; then title is extracted from tags."""
    raw = _raw_media()
    raw["chapters"] = [
        {
            "id": 0,
            "start_time": "0.000000",
            "end_time": "5.000000",
            "tags": {"title": "Intro"},
        },
    ]
    media = _build_media_file(Path("sample.mp4"), raw)
    assert media.chapters is not None
    assert len(media.chapters) == 1
    assert media.chapters[0].title == "Intro"
    assert media.chapters[0].start_time == pytest.approx(0.0)
    assert media.chapters[0].end_time == pytest.approx(5.0)


# ─────────────────────────────────────────────────────────────────────────────
# Keyframe filtering
# ─────────────────────────────────────────────────────────────────────────────


def test_keyframe_times_filters_on_k_flag() -> None:
    """Given packets with mixed flags; then only K-flagged pts are kept."""
    packets = [
        {"pts_time": "0.000000", "flags": "K__"},
        {"pts_time": "0.033333", "flags": "__"},
        {"pts_time": "2.000000", "flags": "K__"},
        {"pts_time": "4.000000", "flags": "K__"},
    ]
    index = _keyframe_times(packets)
    assert isinstance(index, KeyframeIndex)
    assert index.times == pytest.approx([0.0, 2.0, 4.0])


def test_keyframe_times_empty_packets() -> None:
    """Given no packets; then an empty KeyframeIndex is returned."""
    assert _keyframe_times([]).times == []


# ─────────────────────────────────────────────────────────────────────────────
# ProbeError
# ─────────────────────────────────────────────────────────────────────────────


def test_probe_error_carries_path_and_stderr() -> None:
    """Given a probe failure; then ProbeError exposes path and stderr."""
    bad = Path("not_a_media_file.txt")
    err = ProbeError(bad, "Invalid data found when processing input")
    assert err.path == bad
    assert "Invalid data" in err.stderr
    assert str(bad) in str(err)
