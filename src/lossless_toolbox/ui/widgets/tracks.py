# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""Tracks panel: extract or strip via stream checkboxes (todo 14d).

Extract pulls one audio stream (``-map 0:a:N``) into ``.m4a`` or ``.aac``
(ADTS handled by the ops layer); strip keeps the checked streams and copies
them into the source container. Replace is deliberately not offered yet:
its ``build_argv`` needs the replacement codec, which the generic argv
dispatcher does not yet supply (todo 15/16 territory).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from lossless_toolbox.ops.tracks import ExtractSpec, StripSpec
from lossless_toolbox.ui.texts import CODEC_TYPE_ZH

from .base import OperationPanel, PanelError

if TYPE_CHECKING:
    from pathlib import Path

    from lossless_toolbox.models import MediaFile, StreamInfo
    from lossless_toolbox.ui.file_panel import FileEntry

_EXTRACT_PAGE = 0
_STRIP_PAGE = 1
_NO_AUDIO_MSG = "该文件没有音频流"
_NOTHING_KEPT_MSG = "请至少勾选一条保留流"


class TracksPanel(OperationPanel):
    """Extract-or-strip track operation with per-stream checkboxes."""

    operation = "tracks"

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the mode combo and the extract/strip stacked pages."""
        super().__init__(parent)
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("提取音轨", "extract")
        self._mode_combo.addItem("剥离流", "strip")

        self._audio_combo = QComboBox()
        self._format_combo = QComboBox()
        self._format_combo.addItem("M4A", ".m4a")
        self._format_combo.addItem("AAC（裸流）", ".aac")

        extract_page = QWidget()
        extract_form = QFormLayout(extract_page)
        extract_form.addRow("音轨", self._audio_combo)
        extract_form.addRow("输出格式", self._format_combo)

        self._checks_widget = QWidget()
        self._checks_layout = QVBoxLayout(self._checks_widget)
        self._checks: list[QCheckBox] = []
        strip_page = QScrollArea()
        strip_page.setWidget(self._checks_widget)
        strip_page.setWidgetResizable(True)

        self._stack = QStackedWidget()
        self._stack.addWidget(extract_page)
        self._stack.addWidget(strip_page)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("模式", self._mode_combo)
        layout.addLayout(form)
        layout.addWidget(self._stack)

        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._audio_combo.currentIndexChanged.connect(self._on_edit)
        self._format_combo.currentIndexChanged.connect(self._on_edit)

    def set_context(self, entry: FileEntry | None) -> None:
        """Rebuild the audio selector and the strip checkboxes."""
        media = entry.media if entry is not None else None
        self._audio_combo.clear()
        if media is not None:
            for ordinal, stream in enumerate(_audio_streams(media)):
                self._audio_combo.addItem(
                    f"音轨 {ordinal}（{stream.codec_name}）", stream.index
                )
        self._rebuild_checks(media)

    def validation_error(self, entry: FileEntry | None = None) -> str | None:
        """Block on unprobed media or extract-without-audio."""
        reason = super().validation_error(entry)
        if reason is not None:
            return reason
        if self._mode_combo.currentData() == "extract":
            if self._audio_combo.count() == 0:
                return _NO_AUDIO_MSG
        elif not any(check.isChecked() for check in self._checks):
            return _NOTHING_KEPT_MSG
        return None

    def output_extension(self, entry: FileEntry | None = None) -> str | None:
        """Extract follows the chosen format; strip keeps the source container."""
        if self._mode_combo.currentData() == "extract":
            return str(self._format_combo.currentData())
        if entry is not None:
            return entry.path.suffix.lower()
        return None

    def build_spec(self, entry: FileEntry | None, out_path: Path) -> object:
        """Build ExtractSpec or StripSpec from the widget state."""
        media = self._require_media(entry)
        if self._mode_combo.currentData() == "extract":
            audio = _audio_streams(media)
            ordinal = self._audio_combo.currentIndex()
            if ordinal < 0 or ordinal >= len(audio):
                raise PanelError(_NO_AUDIO_MSG)
            return ExtractSpec(
                in_path=media.path,
                stream_index=ordinal,
                out_path=out_path,
                streams=media.streams,
                duration=media.duration,
            )
        keep = [
            stream.index
            for stream, check in zip(media.streams, self._checks, strict=True)
            if check.isChecked()
        ]
        if not keep:
            raise PanelError(_NOTHING_KEPT_MSG)
        return StripSpec(
            in_path=media.path,
            out_path=out_path,
            keep_streams=keep,
            streams=media.streams,
            duration=media.duration,
        )

    def _on_mode_changed(self, _index: int) -> None:
        """Switch the stacked page and notify the window."""
        self._stack.setCurrentIndex(self._mode_combo.currentIndex())
        self.changed.emit()

    def _on_edit(self, *_args: object) -> None:
        """Notify the window on any extract-side widget edit."""
        self.changed.emit()

    def _rebuild_checks(self, media: MediaFile | None) -> None:
        """Replace the strip checkboxes with one row per stream."""
        for check in self._checks:
            self._checks_layout.removeWidget(check)
            check.deleteLater()
        self._checks = []
        if media is None:
            return
        for stream in media.streams:
            label = (
                f"{stream.index} "
                f"{CODEC_TYPE_ZH.get(stream.codec_type, stream.codec_type)}"
                f"（{stream.codec_name}）"
            )
            check = QCheckBox(label)
            check.setChecked(True)
            check.toggled.connect(self._on_check_toggled)
            self._checks_layout.addWidget(check)
            self._checks.append(check)

    def _on_check_toggled(self, _checked: bool) -> None:
        """Notify the window whenever a keep checkbox flips."""
        self.changed.emit()


def _audio_streams(media: MediaFile) -> list[StreamInfo]:
    """Return the media's audio streams in stream order."""
    return [stream for stream in media.streams if stream.codec_type == "audio"]
