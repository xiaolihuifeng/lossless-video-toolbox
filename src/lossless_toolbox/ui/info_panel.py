# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""Stream-info panel: duration line plus the probed stream table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .texts import CODEC_TYPE_ZH

if TYPE_CHECKING:
    from lossless_toolbox.models import StreamInfo

    from .file_panel import FileEntry


class InfoPanel(QWidget):
    """Right-hand panel rendering the selected file's probe result."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the duration label and the empty stream table."""
        super().__init__(parent)
        self.duration_label = QLabel("时长：—")
        self.stream_table = QTableWidget(0, 5)
        self.stream_table.setHorizontalHeaderLabels(
            ["#", "类型", "编码", "语言", "分辨率/采样率"]
        )
        self.stream_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.stream_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("流信息"))
        layout.addWidget(self.duration_label)
        layout.addWidget(self.stream_table)

    def refresh(self, entry: FileEntry | None) -> None:
        """Render ``entry``'s probe outcome (streams or probe state)."""
        self.stream_table.setRowCount(0)
        if entry is None:
            self.duration_label.setText("时长：—")
            return
        if entry.media is None:
            state = f"（{entry.probe_error}）" if entry.probe_error else "（探测中…）"
            self.duration_label.setText(f"时长：{state}")
            return
        media = entry.media
        self.duration_label.setText(
            f"时长：{media.duration:.2f} 秒　容器：{media.format_name}"
        )
        self.stream_table.setRowCount(len(media.streams))
        for row, stream in enumerate(media.streams):
            self._fill_stream_row(row, stream)

    def _fill_stream_row(self, row: int, stream: StreamInfo) -> None:
        """Fill one stream-table row from a typed StreamInfo."""
        cells = (
            str(stream.index),
            CODEC_TYPE_ZH.get(stream.codec_type, stream.codec_type),
            stream.codec_name,
            stream.language or "—",
            _stream_detail(stream),
        )
        for column, text in enumerate(cells):
            self.stream_table.setItem(row, column, QTableWidgetItem(text))


def _stream_detail(stream: StreamInfo) -> str:
    """Return the resolution/rate cell text for one stream."""
    if stream.width is not None and stream.height is not None:
        return f"{stream.width}×{stream.height}"
    if stream.sample_rate is not None:
        channels = f" / {stream.channels} 声道" if stream.channels else ""
        return f"{stream.sample_rate} Hz{channels}"
    return "—"
