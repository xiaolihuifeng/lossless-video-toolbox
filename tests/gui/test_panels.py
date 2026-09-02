# pyright: reportPrivateUsage=false
# Panel tests drive widget internals directly, so private-member access is allowed.
# ruff: noqa: RUF001 - zh-CN UI assertions use fullwidth punctuation deliberately
"""GUI panel tests: widget state → validated ops Spec (todo 14).

Each of the six operation panels is driven purely through widget state and
its ``build_spec`` output is compared for equality against the expected ops
model (:mod:`lossless_toolbox.ops`). Synthetic probed media keeps most
panels deterministic; the cut panel reuses the real session fixture because
its keyframe index must come from a genuine ffprobe scan (async via
KeyframeWorker, polled with ``qtbot.waitUntil``).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyside6")

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QThread

from lossless_toolbox.models import MediaFile, StreamInfo
from lossless_toolbox.ops.cut import CutSpec
from lossless_toolbox.ops.merge import MergeSpec
from lossless_toolbox.ops.meta import MetadataEditSpec, RotateSpec
from lossless_toolbox.ops.remux import CompatResult, RemuxSpec
from lossless_toolbox.ops.subtitles import DetachSpec, MuxSpec
from lossless_toolbox.ops.tracks import ExtractSpec, StripSpec
from lossless_toolbox.probe import probe
from lossless_toolbox.ui.file_panel import FileEntry
from lossless_toolbox.ui.widgets import (
    CutPanel,
    MergePanel,
    MetaPanel,
    RemuxPanel,
    SubtitlePanel,
    TracksPanel,
)
from lossless_toolbox.ui.widgets.base import PanelError

pytestmark = pytest.mark.gui

if TYPE_CHECKING:
    from typing import Protocol

    from PySide6.QtWidgets import QWidget
    from pytestqt.qtbot import QtBot

    class _MediaSample(Protocol):
        path: Path
        codec: str
        duration: float


_ROTATE_UNSUPPORTED_MSG = "仅 MP4/MOV 输出支持旋转（Matroska 无标准旋转元素）"


def _synthetic_media(suffix: str, *, subtitle: bool) -> MediaFile:
    """Build a probed MediaFile: video + audio, plus an optional srt sub."""
    streams = [
        StreamInfo(
            index=0,
            codec_type="video",
            codec_name="h264",
            width=320,
            height=240,
            disposition={},
        ),
        StreamInfo(
            index=1,
            codec_type="audio",
            codec_name="aac",
            sample_rate=44100,
            channels=2,
            disposition={},
        ),
    ]
    if subtitle:
        streams.append(
            StreamInfo(index=2, codec_type="subtitle", codec_name="srt", disposition={})
        )
    return MediaFile(
        path=Path(f"/media/clip{suffix}"),
        format_name="matroska,webm",
        duration=12.0,
        streams=streams,
    )


def _entry(suffix: str, *, subtitle: bool = False) -> FileEntry:
    """Build a probed FileEntry from synthetic media."""
    return FileEntry(
        path=Path(f"/media/clip{suffix}"),
        media=_synthetic_media(suffix, subtitle=subtitle),
    )


def _wait_panel_workers(panel: QWidget) -> None:
    """Wait for every async worker thread spawned by the panel to end.

    Blocks (test context only) so no QThread is destroyed while running
    when the panel is garbage-collected at test teardown.
    """
    for thread in panel.findChildren(QThread):
        thread.wait(15_000)


def test_remux_panel_builds_spec_and_blocks_subtitle_incompat(qtbot: QtBot) -> None:
    """Container combo → RemuxSpec; srt subs into mp4 block the build."""
    panel = RemuxPanel(compat_check=lambda _c, _k, _p: CompatResult(ok=True))
    qtbot.addWidget(panel)
    entry = _entry(".mkv")
    panel.set_context(entry)
    panel._container_combo.setCurrentText("mp4")
    out = Path("/out/clip.mp4")

    spec = panel.build_spec(entry, out)
    assert isinstance(spec, RemuxSpec)
    assert spec == RemuxSpec(
        in_path=entry.path,
        out_path=out,
        streams=entry.media.streams,
        duration=12.0,
    )

    sub_entry = _entry(".mkv", subtitle=True)
    panel.set_context(sub_entry)  # container still mp4
    assert not panel._warning_label.isHidden()
    assert "mov_text" in panel._warning_label.text()
    assert panel.validation_error(sub_entry) is not None
    with pytest.raises(PanelError):
        panel.build_spec(sub_entry, out)

    panel._container_combo.setCurrentText("mkv")
    assert panel.validation_error(sub_entry) is None
    assert isinstance(panel.build_spec(sub_entry, out), RemuxSpec)
    _wait_panel_workers(panel)


def test_remux_panel_async_muxer_warning(qtbot: QtBot) -> None:
    """An async muxer-probe failure surfaces in the warning bar."""
    panel = RemuxPanel(
        compat_check=lambda _c, codec, _p: CompatResult(
            ok=codec != "aac", reason="muxer 不支持 aac"
        )
    )
    qtbot.addWidget(panel)
    panel.set_context(_entry(".mkv"))

    qtbot.waitUntil(lambda: not panel._warning_label.isHidden(), timeout=15_000)
    assert "aac" in panel._warning_label.text()
    assert "mkv" in panel._warning_label.text()
    _wait_panel_workers(panel)


def test_cut_panel_builds_spec_with_keyframes_and_snap_preview(
    qtbot: QtBot, h264_aac_mp4: _MediaSample
) -> None:
    """Start/end values → CutSpec carrying the keyframe index; label snaps."""
    media = probe(h264_aac_mp4.path)
    entry = FileEntry(path=h264_aac_mp4.path, media=media)
    panel = CutPanel()
    qtbot.addWidget(panel)
    panel.set_context(entry)
    qtbot.waitUntil(lambda: panel._keyframes is not None, timeout=30_000)

    times = panel._keyframes.times
    label_full_range = panel._preview_label.text()
    panel._start_spin.setValue(2.5)
    panel._end_spin.setValue(8.0)
    assert panel._preview_label.text() != label_full_range
    first_after = next(t for t in times if t >= 2.5)
    assert "实际切点" in panel._preview_label.text()
    assert f"{first_after:.3f}" in panel._preview_label.text()

    out = Path("/out/clip.mkv")
    spec = panel.build_spec(entry, out)
    assert isinstance(spec, CutSpec)
    assert spec == CutSpec(
        in_path=entry.path,
        start=2.5,
        end=8.0,
        out_path=out,
        keyframe_index=times,
        duration=media.duration,
        has_attached_pic=any(s.disposition.get("attached_pic") for s in media.streams),
    )
    assert spec.build_plan().actual_start == pytest.approx(first_after)

    panel._start_spin.setValue(9.0)
    assert panel.validation_error(entry) == "开始时间必须小于结束时间"


def test_merge_panel_move_buttons_reorder_merge_spec(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """Move up/down reorders MergeSpec.paths in the panel's list order."""
    panel = MergePanel()
    qtbot.addWidget(panel)
    a, b, c = (tmp_path / f"{name}.mp4" for name in ("a", "b", "c"))
    for segment in (a, b, c):
        segment.write_bytes(b"x")
    assert panel.add_paths([str(a), str(b), str(c)]) == 3
    assert panel.validation_error() is None

    panel._list.setCurrentRow(0)
    panel._down_button.click()
    assert panel.paths() == [Path(b), Path(a), Path(c)]
    out = Path("/out/merged.mp4")
    spec = panel.build_spec(None, out)
    assert isinstance(spec, MergeSpec)
    assert spec == MergeSpec(paths=[Path(b), Path(a), Path(c)], out_path=out)

    panel._list.setCurrentRow(1)
    panel._up_button.click()
    assert panel.paths() == [Path(a), Path(b), Path(c)]
    assert panel.build_spec(None, out) == MergeSpec(
        paths=[Path(a), Path(b), Path(c)], out_path=out
    )

    panel._list.setCurrentRow(0)
    panel._remove_button.click()
    panel._list.setCurrentRow(0)
    panel._remove_button.click()
    assert panel.validation_error() == "请至少添加两个文件"
    with pytest.raises(PanelError):
        panel.build_spec(None, out)


def test_tracks_panel_extract_and_strip_checkboxes(qtbot: QtBot) -> None:
    """Extract maps the audio combo; strip follows the keep checkboxes."""
    panel = TracksPanel()
    qtbot.addWidget(panel)
    entry = _entry(".mkv", subtitle=True)
    panel.set_context(entry)
    out = Path("/out/track.m4a")

    spec = panel.build_spec(entry, out)
    assert isinstance(spec, ExtractSpec)
    assert spec == ExtractSpec(
        in_path=entry.path,
        stream_index=0,
        out_path=out,
        streams=entry.media.streams,
        duration=12.0,
    )
    assert panel.output_extension(entry) == ".m4a"
    panel._format_combo.setCurrentIndex(1)
    assert panel.output_extension(entry) == ".aac"

    panel._mode_combo.setCurrentIndex(1)
    spec = panel.build_spec(entry, out)
    assert isinstance(spec, StripSpec)
    assert spec == StripSpec(
        in_path=entry.path,
        out_path=out,
        keep_streams=[0, 1, 2],
        streams=entry.media.streams,
        duration=12.0,
    )

    panel._checks[0].setChecked(False)
    assert panel.build_spec(entry, out) == StripSpec(
        in_path=entry.path,
        out_path=out,
        keep_streams=[1, 2],
        streams=entry.media.streams,
        duration=12.0,
    )

    for check in panel._checks:
        check.setChecked(False)
    assert panel.validation_error(entry) == "请至少勾选一条保留流"
    with pytest.raises(PanelError):
        panel.build_spec(entry, out)


def test_subtitle_panel_mux_detach_and_mov_text_warning(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """Mux/detach modes → MuxSpec/DetachSpec; mp4+srt raises the warning bar."""
    panel = SubtitlePanel()
    qtbot.addWidget(panel)
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")

    mp4_entry = _entry(".mp4")
    panel.set_context(mp4_entry)
    panel.set_sub_path(srt)
    out = Path("/out/clip.mp4")
    spec = panel.build_spec(mp4_entry, out)
    assert isinstance(spec, MuxSpec)
    assert spec == MuxSpec(
        in_path=mp4_entry.path,
        sub_path=srt,
        sub_fmt="srt",
        out_path=out,
        duration=12.0,
    )
    assert not panel._warning_label.isHidden()
    assert "mov_text" in panel._warning_label.text()

    mkv_entry = _entry(".mkv")
    panel.set_context(mkv_entry)
    panel.set_sub_path(srt)
    assert panel._warning_label.isHidden()
    assert isinstance(panel.build_spec(mkv_entry, out), MuxSpec)

    sub_entry = _entry(".mkv", subtitle=True)
    panel.set_context(sub_entry)
    panel._mode_combo.setCurrentIndex(1)
    sub_out = Path("/out/clip.srt")
    spec = panel.build_spec(sub_entry, sub_out)
    assert isinstance(spec, DetachSpec)
    assert spec == DetachSpec(
        in_path=sub_entry.path, out_path=sub_out, stream_index=0, duration=12.0
    )

    panel.set_context(mkv_entry)
    assert panel.validation_error(mkv_entry) == "该文件没有字幕流"
    with pytest.raises(PanelError):
        panel.build_spec(mkv_entry, sub_out)


def test_meta_panel_rotation_gated_by_mp4_like_container(qtbot: QtBot) -> None:
    """MKV disables rotation with an explanatory note; MP4 builds RotateSpec."""
    panel = MetaPanel()
    qtbot.addWidget(panel)
    mkv_entry = _entry(".mkv")
    panel.set_context(mkv_entry)
    assert not panel._rotate_combo.isEnabled()
    assert panel._rotate_note.text() == _ROTATE_UNSUPPORTED_MSG
    panel._mode_combo.setCurrentIndex(2)
    assert panel.validation_error(mkv_entry) == _ROTATE_UNSUPPORTED_MSG
    with pytest.raises(PanelError):
        panel.build_spec(mkv_entry, Path("/out/clip.mkv"))

    mp4_entry = _entry(".mp4")
    panel.set_context(mp4_entry)
    assert panel._rotate_combo.isEnabled()
    panel._rotate_combo.setCurrentIndex(1)
    out = Path("/out/clip.mp4")
    spec = panel.build_spec(mp4_entry, out)
    assert isinstance(spec, RotateSpec)
    assert spec == RotateSpec(
        in_path=mp4_entry.path, out_path=out, degrees=90, duration=12.0
    )

    panel._mode_combo.setCurrentIndex(0)
    panel._title_edit.setText("标题")
    panel._lang_table.item(0, 1).setText("chi")
    spec = panel.build_spec(mp4_entry, out)
    assert isinstance(spec, MetadataEditSpec)
    assert spec == MetadataEditSpec(
        in_path=mp4_entry.path,
        out_path=out,
        title="标题",
        language_map={0: "chi"},
        duration=12.0,
    )
