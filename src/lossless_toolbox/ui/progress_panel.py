"""Queue progress panel: per-job rows, live bars and a batch summary.

One row per job in submission order: a status word (queued/running/done/
failed/cancelled), the input title, and an inline mini progress bar. The
main bar mirrors the currently running job (busy state while the progress
fraction is unknown). Failures never pop a modal — double-clicking a failed
row opens a non-modal, closable details dialog with the full
``JobRecord.error`` and ``result.stderr_tail`` text. Every slot runs on the
UI thread: :class:`~lossless_toolbox.ui.workers.QueueWorker` emits its
signals from the worker thread and Qt queues them to the main thread, so
the panel never spawns any subprocess of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QProcess, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .texts import (
    ERROR_DETAILS_CLOSE,
    ERROR_DETAILS_FMT,
    ERROR_DETAILS_NONE,
    ERROR_DETAILS_STDERR_HEADER,
    ERROR_DETAILS_TITLE,
    JOB_TITLE_FALLBACK,
    OPEN_DIR,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    SUMMARY_DONE_FMT,
    SUMMARY_READY,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lossless_toolbox.queue import JobRecord
    from lossless_toolbox.runner import ProgressEvent

    from .workers import QueueWorker

_STATUS_WORDS: dict[str, str] = {
    "queued": STATUS_QUEUED,
    "running": STATUS_RUNNING,
    "done": STATUS_DONE,
    "failed": STATUS_FAILED,
    "cancelled": STATUS_CANCELLED,
}


class ProgressPanel(QWidget):
    """Live batch view: main bar, one row per job, summary and open-dir."""

    open_failed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the bar, the job table and the summary row."""
        super().__init__(parent)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["状态", "文件", "进度"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        self.summary_label = QLabel(SUMMARY_READY)
        self.open_dir_button = QPushButton(OPEN_DIR)
        self.open_dir_button.setEnabled(False)
        self.open_dir_button.clicked.connect(self._open_output_dir)

        summary_row = QHBoxLayout()
        summary_row.addWidget(self.summary_label)
        summary_row.addStretch(1)
        summary_row.addWidget(self.open_dir_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.table)
        layout.addLayout(summary_row)

        self._records: list[JobRecord[object]] = []
        self._row_by_id: dict[int, int] = {}
        self._output_dir: Path | None = None

    def attach(self, worker: QueueWorker) -> None:
        """Connect the worker's four job signals to the panel slots."""
        worker.job_started.connect(self._on_job_started)
        worker.job_progress.connect(self._on_job_progress)
        worker.job_finished.connect(self._on_job_finished)
        worker.all_done.connect(self._on_all_done)

    def reset(self, message: str = SUMMARY_READY) -> None:
        """Clear every row and bar value for a fresh batch."""
        self.table.setRowCount(0)
        self._records.clear()
        self._row_by_id.clear()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.summary_label.setText(message)
        self._output_dir = None
        self.open_dir_button.setEnabled(False)

    def set_output_dir(self, path: Path | None) -> None:
        """Store the batch output directory and arm the open button."""
        self._output_dir = path
        self.open_dir_button.setEnabled(path is not None)

    def row_status(self, row: int) -> str:
        """The status word of the job row (e.g. ``✓ 成功``)."""
        item = self.table.item(row, 0)
        return item.text() if item is not None else ""

    def row_name(self, row: int) -> str:
        """The displayed title of the job row."""
        item = self.table.item(row, 1)
        return item.text() if item is not None else ""

    def row_bar(self, row: int) -> QProgressBar | None:
        """The inline mini bar of the job row, or None when absent."""
        widget = self.table.cellWidget(row, 2)
        return widget if isinstance(widget, QProgressBar) else None

    def _on_job_started(self, record: JobRecord[object]) -> None:
        """Mark the record's row as running and idle the main bar."""
        self._set_row_status(record, STATUS_RUNNING)
        self.progress_bar.setRange(0, 0)

    def _on_job_progress(
        self, record: JobRecord[object], event: ProgressEvent
    ) -> None:
        """Forward one progress event to the main bar and the row's bar."""
        row = self._row_by_id.get(record.id)
        if row is None:
            return
        if event.progress is None:
            self.progress_bar.setRange(0, 0)
            row_bar = self.row_bar(row)
            if row_bar is not None:
                row_bar.setRange(0, 0)
            return
        value = round(event.progress * 100)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(value)
        row_bar = self.row_bar(row)
        if row_bar is not None:
            row_bar.setRange(0, 100)
            row_bar.setValue(value)

    def _on_job_finished(self, record: JobRecord[object]) -> None:
        """Land the row's terminal status and rewind the main bar."""
        self._set_row_status(record, _STATUS_WORDS.get(record.status, "?"))
        row_bar = self.row_bar(self._row_by_id.get(record.id, -1))
        if row_bar is not None:
            row_bar.setRange(0, 100)
            row_bar.setValue(100 if record.status == "done" else 0)
        if record.status == "failed":
            status_item = self.table.item(self._row_by_id.get(record.id, -1), 0)
            if status_item is not None:
                status_item.setToolTip(record.error or "")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    def _on_all_done(self, records: Sequence[JobRecord[object]]) -> None:
        """Settle every row (including never-started cancels) and summarize."""
        done = failed = cancelled = 0
        for record in records:
            self._set_row_status(record, _STATUS_WORDS.get(record.status, "?"))
            if record.status == "done":
                done += 1
            elif record.status == "failed":
                failed += 1
            elif record.status == "cancelled":
                cancelled += 1
        self.summary_label.setText(
            SUMMARY_DONE_FMT.format(done=done, failed=failed, cancelled=cancelled)
        )
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    def _ensure_row(self, record: JobRecord[object]) -> int:
        """Return the table row for ``record``, creating it on first sight."""
        row = self._row_by_id.get(record.id)
        if row is not None:
            return row
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(STATUS_QUEUED))
        self.table.setItem(row, 1, QTableWidgetItem(self._job_title(record.spec)))
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFormat("%v%")
        self.table.setCellWidget(row, 2, bar)
        self._row_by_id[record.id] = row
        self._records.append(record)
        return row

    def _set_row_status(self, record: JobRecord[object], word: str) -> None:
        """Write ``word`` into the record's status cell."""
        item = self.table.item(self._ensure_row(record), 0)
        if item is not None:
            item.setText(word)

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        """Open the non-modal error details for a double-clicked failed row."""
        if 0 <= row < len(self._records) and self._records[row].status == "failed":
            self._show_error_details(self._records[row])

    def _show_error_details(self, record: JobRecord[object]) -> None:
        """Show the full error and stderr tail in a non-modal dialog."""
        tail = record.result.stderr_tail if record.result is not None else ""
        text = ERROR_DETAILS_FMT.format(
            error=record.error or ERROR_DETAILS_NONE,
            stderr_header=ERROR_DETAILS_STDERR_HEADER,
            stderr_tail=tail,
        )
        dialog = QDialog(self)
        dialog.setWindowTitle(ERROR_DETAILS_TITLE)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(560, 380)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit(dialog)
        view.setReadOnly(True)
        view.setPlainText(text)
        close_button = QPushButton(ERROR_DETAILS_CLOSE, dialog)
        close_button.clicked.connect(dialog.close)
        layout.addWidget(view)
        layout.addWidget(close_button)
        dialog.show()

    def _open_output_dir(self) -> None:
        """Launch ``xdg-open`` on the stored output directory."""
        if self._output_dir is None:
            return
        started, _pid = QProcess.startDetached("xdg-open", [str(self._output_dir)])
        if not started:
            self.open_failed.emit(str(self._output_dir))

    @staticmethod
    def _job_title(spec: object) -> str:
        """The input file name for one spec (first input for merges)."""
        in_path = getattr(spec, "in_path", None)
        if in_path is not None:
            return Path(str(in_path)).name
        paths = getattr(spec, "paths", None)
        if paths:
            return Path(str(paths[0])).name
        return JOB_TITLE_FALLBACK
