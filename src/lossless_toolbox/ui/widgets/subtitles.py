# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""Subtitle panel: mux/detach with the mov_text transcode warning (todo 14e).

Mux keeps the source container (so an SRT into an MP4 source shows the
explicit mov_text text-transcode warning); detach extracts one subtitle
stream to SRT (the source codec is resolved by the argv dispatcher inside
the queue worker thread). Constructing a throwaway
:class:`~lossless_toolbox.ops.subtitles.MuxSpec` validates the
codec/container pair and surfaces :class:`SubtitleUnsupportedError` before
the run — all pure, no ffmpeg process on the UI thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from lossless_toolbox.ops.subtitles import (
    DetachSpec,
    MuxSpec,
    SubtitleUnsupportedError,
)

from .base import NOT_SELECTED_MSG, OperationPanel, PanelError

if TYPE_CHECKING:
    from lossless_toolbox.ui.file_panel import FileEntry

_MUX_PAGE = 0
_DETACH_PAGE = 1
_SUB_FORMATS: dict[str, Literal["srt", "ass", "webvtt"]] = {
    "srt": "srt",
    "ass": "ass",
    "webvtt": "webvtt",
}
_SUB_FILTER = "字幕文件 (*.srt *.ass *.webvtt)"
_NEED_SUB_FILE_MSG = "请选择字幕文件"
_BAD_SUB_EXT_MSG = "字幕文件须为 srt / ass / webvtt"
_NO_SUB_STREAM_MSG = "该文件没有字幕流"
_MOV_TEXT_WARNING_MSG = (
    "注意：字幕封装进 MP4 需要 mov_text 文本转码（非流拷贝），画质与音轨不受影响"
)


def _sub_format(suffix: str) -> Literal["srt", "ass", "webvtt"] | None:
    """Map a lower-cased extension to a MuxSpec literal format, or None."""
    return _SUB_FORMATS.get(suffix.lstrip("."))


class SubtitlePanel(OperationPanel):
    """Mux-or-detach subtitle operation with a live transcode warning bar."""

    operation = "subtitles"

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the mode combo, sub-file row and warning label."""
        super().__init__(parent)
        self._entry: FileEntry | None = None
        self._sub_path: Path | None = None

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("封装字幕", "mux")
        self._mode_combo.addItem("抽取字幕", "detach")

        self._sub_edit = QLineEdit()
        self._sub_edit.setPlaceholderText("选择 .srt / .ass / .webvtt 字幕文件")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse_clicked)
        sub_row = QHBoxLayout()
        sub_row.addWidget(self._sub_edit)
        sub_row.addWidget(browse)
        mux_page = QWidget()
        mux_layout = QVBoxLayout(mux_page)
        mux_layout.addLayout(sub_row)

        self._stream_combo = QComboBox()
        detach_page = QWidget()
        detach_form = QFormLayout(detach_page)
        detach_form.addRow("字幕流", self._stream_combo)

        self._stack = QStackedWidget()
        self._stack.addWidget(mux_page)
        self._stack.addWidget(detach_page)

        self._warning_label = QLabel("")
        self._warning_label.setWordWrap(True)
        self._warning_label.setStyleSheet("color: #b00020;")
        self._warning_label.setVisible(False)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("模式", self._mode_combo)
        layout.addLayout(form)
        layout.addWidget(self._stack)
        layout.addWidget(self._warning_label)

        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._sub_edit.textChanged.connect(self._on_sub_edit_changed)
        self._stream_combo.currentIndexChanged.connect(self._on_edit)

    def set_context(self, entry: FileEntry | None) -> None:
        """Rebuild the detach stream combo and refresh the warning bar."""
        self._entry = entry
        media = entry.media if entry is not None else None
        self._stream_combo.clear()
        if media is not None:
            for ordinal, stream in enumerate(
                s for s in media.streams if s.codec_type == "subtitle"
            ):
                self._stream_combo.addItem(
                    f"字幕 {ordinal}（{stream.codec_name}）", ordinal
                )
        self._refresh_warning()

    def set_sub_path(self, path: Path | None) -> None:
        """Set the mux subtitle path programmatically (test seam)."""
        self._sub_path = path
        self._sub_edit.setText(str(path) if path is not None else "")
        self._refresh_warning()
        self.changed.emit()

    def validation_error(self, entry: FileEntry | None = None) -> str | None:
        """Block on unprobed media, a missing sub file or an unsupported pair."""
        reason = super().validation_error(entry)
        if reason is not None:
            return reason
        if self._mode_combo.currentData() == "detach":
            if self._stream_combo.count() == 0:
                return _NO_SUB_STREAM_MSG
            return None
        if self._sub_path is None:
            return _NEED_SUB_FILE_MSG
        if _sub_format(self._sub_path.suffix.lower()) is None:
            return _BAD_SUB_EXT_MSG
        error = self._mux_construction_error(entry)
        return None if error is None else error

    def output_extension(self, entry: FileEntry | None = None) -> str | None:
        """Mux keeps the source container; detach writes SRT."""
        if self._mode_combo.currentData() == "detach":
            return ".srt"
        if entry is not None:
            return entry.path.suffix.lower()
        return None

    def build_spec(self, entry: FileEntry | None, out_path: Path) -> object:
        """Build MuxSpec or DetachSpec from the widget state."""
        media = self._require_media(entry)
        if self._mode_combo.currentData() == "detach":
            ordinal = self._stream_combo.currentIndex()
            if ordinal < 0:
                raise PanelError(_NO_SUB_STREAM_MSG)
            return DetachSpec(
                in_path=media.path, stream_index=ordinal, out_path=out_path
            )
        if self._sub_path is None:
            raise PanelError(_NEED_SUB_FILE_MSG)
        fmt = _sub_format(self._sub_path.suffix.lower())
        if fmt is None:
            raise PanelError(_BAD_SUB_EXT_MSG)
        try:
            return MuxSpec(
                in_path=media.path,
                sub_path=self._sub_path,
                sub_fmt=fmt,
                out_path=out_path,
            )
        except SubtitleUnsupportedError as exc:
            raise PanelError(str(exc)) from exc

    def _mux_construction_error(self, entry: FileEntry | None) -> str | None:
        """Return the MuxSpec construction error for the current state."""
        if entry is None:
            return NOT_SELECTED_MSG
        if self._sub_path is None:
            return _NEED_SUB_FILE_MSG
        fmt = _sub_format(self._sub_path.suffix.lower())
        if fmt is None:
            return _BAD_SUB_EXT_MSG
        try:
            MuxSpec(
                in_path=entry.path,
                sub_path=self._sub_path,
                sub_fmt=fmt,
                out_path=entry.path.with_suffix(entry.path.suffix),
            )
        except SubtitleUnsupportedError as exc:
            return str(exc)
        return None

    def _refresh_warning(self) -> None:
        """Show the red mov_text warning when the mux pair transcodes text."""
        self._warning_label.setVisible(False)
        entry = self._entry
        if (
            entry is None
            or entry.media is None
            or self._mode_combo.currentData() != "mux"
            or self._sub_path is None
        ):
            return
        fmt = _sub_format(self._sub_path.suffix.lower())
        if fmt is None:
            return
        try:
            spec = MuxSpec(
                in_path=entry.path,
                sub_path=self._sub_path,
                sub_fmt=fmt,
                out_path=entry.path.with_suffix(entry.path.suffix),
            )
        except SubtitleUnsupportedError as exc:
            self._warning_label.setText(str(exc))
            self._warning_label.setVisible(True)
            return
        spec.build_argv()  # pure argv build; sets transcode_warning
        if spec.transcode_warning:
            self._warning_label.setText(_MOV_TEXT_WARNING_MSG)
            self._warning_label.setVisible(True)

    def _on_mode_changed(self, _index: int) -> None:
        """Switch the stacked page and refresh the warning."""
        self._stack.setCurrentIndex(self._mode_combo.currentIndex())
        self._refresh_warning()
        self.changed.emit()

    def _on_sub_edit_changed(self, text: str) -> None:
        """Re-parse the sub path from the edit box."""
        stripped = text.strip()
        self._sub_path = Path(stripped) if stripped else None
        self._refresh_warning()
        self.changed.emit()

    def _on_edit(self, *_args: object) -> None:
        """Notify the window on detach-side widget edits."""
        self.changed.emit()

    def _on_browse_clicked(self) -> None:
        """Open a subtitle file picker."""
        chosen, _ = QFileDialog.getOpenFileName(self, "选择字幕文件", "", _SUB_FILTER)
        if chosen:
            self.set_sub_path(Path(chosen))
