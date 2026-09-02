# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""File list panel: model, drag-drop intake and async probe lifecycle.

The panel owns everything about the left-hand file list: the
:class:`FileListModel` over :class:`FileEntry` rows, drag-and-drop intake of
local files (non-media extensions skipped and counted), and one
:class:`~lossless_toolbox.ui.workers.ProbeWorker` per accepted file — no
ffprobe process ever runs on the UI thread. Probe outcomes are applied to
the model and surfaced as signals for the main window to react to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListView,
    QVBoxLayout,
    QWidget,
)

from lossless_toolbox.models import MediaFile

from .texts import MEDIA_SUFFIXES
from .workers import ProbeWorker

if TYPE_CHECKING:
    from collections.abc import Sequence

    from PySide6.QtCore import QPersistentModelIndex
    from PySide6.QtGui import QDragEnterEvent, QDropEvent

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FileEntry:
    """One row of the file list: path plus async probe outcome."""

    path: Path
    media: MediaFile | None = None
    probe_error: str | None = None


class FileListModel(QAbstractListModel):
    """List model over :class:`FileEntry` rows (name / duration / resolution)."""

    EntryRole = Qt.ItemDataRole.UserRole

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create an empty model."""
        super().__init__(parent)
        self._entries: list[FileEntry] = []

    def rowCount(  # noqa: N802
        self,
        parent: QModelIndex | QPersistentModelIndex | None = None,
    ) -> int:
        """Return the number of file rows."""
        return 0 if parent is not None and parent.isValid() else len(self._entries)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        """Return display text, the entry, or a tooltip for ``index``."""
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return _display_text(entry)
        if role == self.EntryRole:
            return entry
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(entry.path)
        return None

    def append_entry(self, entry: FileEntry) -> int:
        """Append one entry and return its row index."""
        row = len(self._entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self._entries.append(entry)
        self.endInsertRows()
        return row

    def update_media(
        self, row: int, media: MediaFile | None, error: str | None
    ) -> None:
        """Apply an async probe outcome and notify the view."""
        entry = self._entries[row]
        entry.media = media
        entry.probe_error = error
        index = self.index(row)
        self.dataChanged.emit(index, index)

    def row_of(self, path: Path) -> int | None:
        """Return the row whose entry path matches, or None."""
        for row, entry in enumerate(self._entries):
            if entry.path == path:
                return row
        return None

    def entry_at(self, row: int) -> FileEntry | None:
        """Return the entry at ``row`` or None when out of range."""
        if not 0 <= row < len(self._entries):
            return None
        return self._entries[row]

    def entries(self) -> list[FileEntry]:
        """Return a snapshot of all entries in list order."""
        return list(self._entries)


def _display_text(entry: FileEntry) -> str:
    """Format one list row: name, duration and resolution (or probe state)."""
    if entry.media is not None:
        return (
            f"{entry.path.name}  {entry.media.duration:.1f}s  "
            f"{_resolution_summary(entry.media)}"
        )
    if entry.probe_error is not None:
        return f"{entry.path.name}  （探测失败）"
    return f"{entry.path.name}  （探测中…）"


def _resolution_summary(media: MediaFile) -> str:
    """Return first video WxH, else first audio rate, else a dash."""
    video = next((s for s in media.streams if s.codec_type == "video"), None)
    if video is not None and video.width is not None and video.height is not None:
        return f"{video.width}×{video.height}"
    audio = next((s for s in media.streams if s.codec_type == "audio"), None)
    if audio is not None:
        if audio.sample_rate is not None:
            return f"{audio.sample_rate} Hz"
        return audio.codec_name
    return "—"


class FilePanel(QWidget):
    """File list widget: drag-drop intake plus per-file async probing."""

    file_selected = Signal(object)  # FileEntry | None
    file_probed = Signal(object)  # FileEntry
    files_changed = Signal()  # row count changed
    skipped = Signal(int)  # non-media paths dropped

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the list view and connect selection tracking."""
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._model = FileListModel(self)
        self._probe_workers: list[ProbeWorker] = []

        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._list_view.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("文件列表"))
        layout.addWidget(self._list_view)

        self._list_view.selectionModel().currentRowChanged.connect(self._on_row_changed)

    def model(self) -> FileListModel:
        """Return the underlying list model."""
        return self._model

    def list_view(self) -> QListView:
        """Return the list view (for tests and embedding)."""
        return self._list_view

    def current_entry(self) -> FileEntry | None:
        """Return the entry of the currently selected row, or None."""
        return self._model.entry_at(self._list_view.currentIndex().row())

    def probed_entries(self) -> list[FileEntry]:
        """Return entries whose probe succeeded."""
        return [entry for entry in self._model.entries() if entry.media is not None]

    def add_files(self, paths: Sequence[str]) -> tuple[int, int]:
        """Accept local file paths, probing media ones; return (added, skipped)."""
        added = 0
        skipped = 0
        existing = {entry.path for entry in self._model.entries()}
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                skipped += 1
                continue
            if path.suffix.lower() not in MEDIA_SUFFIXES:
                skipped += 1
                continue
            if path in existing:
                continue
            entry = FileEntry(path=path)
            self._model.append_entry(entry)
            self._start_probe(entry)
            existing.add(path)
            added += 1
        if skipped:
            self.skipped.emit(skipped)
        logger.info("added %d file(s), skipped %d", added, skipped)
        if added:
            self.files_changed.emit()
        return added, skipped

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """Accept drags that carry file URLs."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """Accept dropped local files; non-media paths are skipped and counted."""
        urls = event.mimeData().urls()
        self.add_files([url.toLocalFile() for url in urls if url.isLocalFile()])
        event.acceptProposedAction()

    def wait_for_workers(self, timeout_ms: int) -> None:
        """Wait for in-flight probe workers (window close path)."""
        for worker in self._probe_workers:
            worker.wait(timeout_ms)

    def _on_row_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        """Emit ``file_selected`` for the newly selected row."""
        self.file_selected.emit(self._model.entry_at(current.row()))

    def _start_probe(self, entry: FileEntry) -> None:
        """Probe ``entry`` on a worker thread (never on the UI thread)."""
        worker = ProbeWorker(entry.path)
        worker.probe_finished.connect(self._on_probe_finished)
        worker.finished.connect(partial(self._on_probe_worker_done, worker))
        self._probe_workers.append(worker)
        worker.start()

    def _on_probe_finished(self, path: Path, result: object) -> None:
        """Apply a finished probe to the model and emit ``file_probed``."""
        row = self._model.row_of(path)
        if row is None:
            return
        entry = self._model.entry_at(row)
        if entry is None:
            return
        if isinstance(result, MediaFile):
            self._model.update_media(row, result, None)
        else:
            self._model.update_media(row, None, str(result))
        self.file_probed.emit(entry)

    def _on_probe_worker_done(self, worker: ProbeWorker) -> None:
        """Forget a finished probe worker so its QThread can be collected."""
        if worker in self._probe_workers:
            self._probe_workers.remove(worker)
        worker.deleteLater()
