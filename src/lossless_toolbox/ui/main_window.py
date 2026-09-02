# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""Main window shell: drag-drop file list, probe info panel and op bar.

Left: a :class:`QListView` over :class:`FileListModel` (file name / duration /
resolution per row), fed by drag-and-drop of local files only; dropped paths
whose extension is not a known media container are skipped and counted. Each
accepted file is probed asynchronously by a :class:`ProbeWorker` — no ffmpeg/
ffprobe process ever runs on the UI thread.

Right: the selected file's probed stream table (codec type / codec name /
language / resolution) plus a duration line.

Bottom: the six-operation selector (转封装/剪切/合并/音轨/字幕/元数据), an
output-directory row and 运行/取消. Running builds one ops spec per probed
file (panel data arrives in todo 14; panel-gated specs fail with an actionable
:class:`JobSpecError` recorded on the job) and hands them to a
:class:`QueueWorker`. Existing default outputs are confirmed for overwrite
via :class:`QMessageBox` first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lossless_toolbox.models import MediaFile
from lossless_toolbox.ops.cut import CutSpec
from lossless_toolbox.ops.merge import MergeSpec
from lossless_toolbox.ops.meta import MetadataEditSpec
from lossless_toolbox.ops.remux import RemuxSpec
from lossless_toolbox.ops.subtitles import DetachSpec
from lossless_toolbox.ops.tracks import ExtractSpec
from lossless_toolbox.runner import Runner

from .workers import ProbeWorker, QueueWorker

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from PySide6.QtCore import QPersistentModelIndex
    from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent

    from lossless_toolbox.models import StreamInfo

    from .workers import ProgressRunner

logger = logging.getLogger(__name__)

_MEDIA_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".mov",
        ".m4v",
        ".ts",
        ".mts",
        ".m2ts",
        ".webm",
        ".avi",
        ".mpg",
        ".mpeg",
        ".m4a",
        ".aac",
        ".mp3",
        ".flac",
        ".wav",
        ".ogg",
    }
)

_OP_ITEMS: Final[tuple[tuple[str, str], ...]] = (
    ("转封装", "remux"),
    ("剪切", "cut"),
    ("合并", "merge"),
    ("音轨", "tracks"),
    ("字幕", "subtitles"),
    ("元数据", "meta"),
)

_DEFAULT_EXT: Final[dict[str, str]] = {
    "remux": ".mkv",
    "cut": ".mkv",
    "merge": ".mp4",
    "tracks": ".m4a",
    "subtitles": ".mkv",
    "meta": "",
}

_CODEC_TYPE_ZH: Final[dict[str, str]] = {
    "video": "视频",
    "audio": "音频",
    "subtitle": "字幕",
    "data": "数据",
    "attachment": "附件",
}

_NO_BUILD_ARGV_MSG = (
    "%s 缺少 build_argv() 接口：该操作的参数面板由下一阶段（todo 14）接入，"
    "当前 shell 版本暂不可运行"
)
_BUILD_ARGV_FAILED_MSG = "%s.build_argv() 调用失败：%s"
_UNKNOWN_OP_MSG = "未知操作：%s"
_NOT_PROBED_MSG = "%s 尚未探测完成"


class JobSpecError(RuntimeError):
    """Raised when a submitted spec cannot be turned into an argv."""


@dataclass(slots=True)
class FileEntry:
    """One row of the file list: path plus async probe outcome."""

    path: Path
    media: MediaFile | None = None
    probe_error: str | None = None


def default_output_path(
    source: Path, op_key: str, output_dir: Path | None = None
) -> Path:
    """Return the default output path ``<stem>.<ext>`` for one job.

    The extension follows the operation rules (remux/cut/subtitles -> mkv,
    merge -> mp4, tracks -> m4a, meta -> same container). When the computed
    path would overwrite the source itself, the operation key is inserted
    (``<stem>.<op>.<ext>``) as a collision guard.
    """
    ext = _DEFAULT_EXT.get(op_key)
    if ext is None:
        raise JobSpecError(_UNKNOWN_OP_MSG % op_key)
    if not ext:
        ext = source.suffix.lower()
    directory = output_dir if output_dir is not None else source.parent
    candidate = directory / f"{source.stem}{ext}"
    if candidate == source:
        candidate = directory / f"{source.stem}.{op_key}{ext}"
    return candidate


def build_job_argv(spec: object) -> list[str]:
    """Dispatch one ops spec to its ``build_argv()`` and validate the argv.

    This generic dispatcher is the shell-stage stand-in for the todo-14
    panels: specs without a parameter-free ``build_argv`` (cut/merge, or the
    parameterised track/subtitle builders) raise :class:`JobSpecError`, which
    the queue records on ``JobRecord.error`` without touching ffmpeg.
    """
    build = cast("Callable[[], object]", getattr(spec, "build_argv", None))
    if build is None:
        raise JobSpecError(_NO_BUILD_ARGV_MSG % type(spec).__name__)
    try:
        argv = build()
    except TypeError as exc:
        raise JobSpecError(_BUILD_ARGV_FAILED_MSG % (type(spec).__name__, exc)) from exc
    argv_list = cast("list[object]", argv)
    if not isinstance(argv, list) or any(
        not isinstance(token, str) for token in argv_list
    ):
        raise JobSpecError(
            _BUILD_ARGV_FAILED_MSG % (type(spec).__name__, "返回了非法的 argv")
        )
    return cast("list[str]", argv)


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


def _stream_detail(stream: StreamInfo) -> str:
    """Return the resolution/rate cell text for one stream."""
    if stream.width is not None and stream.height is not None:
        return f"{stream.width}×{stream.height}"
    if stream.sample_rate is not None:
        channels = f" / {stream.channels} 声道" if stream.channels else ""
        return f"{stream.sample_rate} Hz{channels}"
    return "—"


class MainWindow(QMainWindow):
    """The shell window: list + probe panel + operation bar + run/cancel."""

    def __init__(
        self,
        *,
        runner_factory: Callable[[], ProgressRunner] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Assemble the shell UI and wire drag-drop, probe and run flows."""
        super().__init__(parent)
        self.setWindowTitle("无损视频工具箱")
        self.setAcceptDrops(True)

        self._runner_factory = runner_factory or partial(Runner)
        self._model = FileListModel(self)
        self._probe_workers: list[ProbeWorker] = []
        self._queue_worker: QueueWorker | None = None

        self._file_list = QListView()
        self._file_list.setModel(self._model)
        self._file_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )

        self._duration_label = QLabel("时长：—")
        self._stream_table = self._build_stream_table()

        self._op_combo = QComboBox()
        for label, key in _OP_ITEMS:
            self._op_combo.addItem(label, key)

        self._output_dir_edit = QLineEdit()
        self._output_dir_edit.setPlaceholderText("留空则输出到源文件目录")

        self._run_button = QPushButton("运行")
        self._run_button.clicked.connect(self._on_run_clicked)
        self._cancel_button = QPushButton("取消")
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        self._summary_label = QLabel("就绪")

        self._build_layout()
        self._connect_signals()
        self._update_buttons()

    @staticmethod
    def _build_stream_table() -> QTableWidget:
        """Create the empty stream-info table with zh-CN column headers."""
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["#", "类型", "编码", "语言", "分辨率/采样率"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        return table

    def _build_layout(self) -> None:
        """Compose the splitter (list | info) and the bottom operation bar."""
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("文件列表"))
        left_layout.addWidget(self._file_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("流信息"))
        right_layout.addWidget(self._duration_label)
        right_layout.addWidget(self._stream_table)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("操作"))
        bar.addWidget(self._op_combo)
        bar.addWidget(QLabel("输出目录"))
        bar.addWidget(self._output_dir_edit)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._on_browse_clicked)
        bar.addWidget(browse)
        add_button = QPushButton("选择文件…")
        add_button.clicked.connect(self._on_add_files_clicked)
        bar.addWidget(add_button)
        bar.addStretch(1)
        bar.addWidget(self._summary_label)
        bar.addWidget(self._run_button)
        bar.addWidget(self._cancel_button)

        central = QWidget()
        root = QVBoxLayout(central)
        root.addWidget(splitter)
        root.addLayout(bar)
        self.setCentralWidget(central)

    def _connect_signals(self) -> None:
        """Wire selection, model-growth and info-panel refresh signals."""
        self._file_list.selectionModel().currentRowChanged.connect(
            self._on_row_selected
        )
        self._model.rowsInserted.connect(self._on_model_rows_changed)

    def _on_model_rows_changed(self, *_args: object) -> None:
        """Refresh button states after the file list grows."""
        self._update_buttons()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """Accept drags that carry file URLs."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """Accept dropped local files; non-media paths are skipped and counted."""
        urls = event.mimeData().urls()
        self.add_files([url.toLocalFile() for url in urls if url.isLocalFile()])
        event.acceptProposedAction()

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
            if path.suffix.lower() not in _MEDIA_SUFFIXES:
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
            self.statusBar().showMessage(f"已跳过 {skipped} 个非媒体文件", 5000)
        logger.info("added %d file(s), skipped %d", added, skipped)
        self._update_buttons()
        return added, skipped

    def _start_probe(self, entry: FileEntry) -> None:
        """Probe ``entry`` on a worker thread (never on the UI thread)."""
        worker = ProbeWorker(entry.path)
        worker.probe_finished.connect(self._on_probe_finished)
        worker.finished.connect(partial(self._on_probe_worker_done, worker))
        self._probe_workers.append(worker)
        worker.start()

    def _on_probe_finished(self, path: Path, result: object) -> None:
        """Apply a finished probe to the model and refresh the info panel."""
        row = self._model.row_of(path)
        if row is None:
            return
        if isinstance(result, MediaFile):
            self._model.update_media(row, result, None)
        else:
            self._model.update_media(row, None, str(result))
        if self._file_list.currentIndex().row() == row:
            self._refresh_info(row)

    def _on_probe_worker_done(self, worker: ProbeWorker) -> None:
        """Forget a finished probe worker so its QThread can be collected."""
        if worker in self._probe_workers:
            self._probe_workers.remove(worker)
        worker.deleteLater()

    def _on_row_selected(self, current: QModelIndex, _previous: QModelIndex) -> None:
        """Refresh the info panel for the newly selected row."""
        self._refresh_info(current.row())

    def _refresh_info(self, row: int) -> None:
        """Render the probed streams of row ``row`` into the info panel."""
        entry = self._model.entry_at(row)
        self._stream_table.setRowCount(0)
        if entry is None:
            self._duration_label.setText("时长：—")
            return
        if entry.media is None:
            state = f"（{entry.probe_error}）" if entry.probe_error else "（探测中…）"
            self._duration_label.setText(f"时长：{state}")
            return
        media = entry.media
        self._duration_label.setText(
            f"时长：{media.duration:.2f} 秒　容器：{media.format_name}"
        )
        self._stream_table.setRowCount(len(media.streams))
        for row_index, stream in enumerate(media.streams):
            self._fill_stream_row(row_index, stream)

    def _fill_stream_row(self, row: int, stream: StreamInfo) -> None:
        """Fill one stream-table row from a typed StreamInfo."""
        cells = (
            str(stream.index),
            _CODEC_TYPE_ZH.get(stream.codec_type, stream.codec_type),
            stream.codec_name,
            stream.language or "—",
            _stream_detail(stream),
        )
        for column, text in enumerate(cells):
            self._stream_table.setItem(row, column, QTableWidgetItem(text))

    def _on_browse_clicked(self) -> None:
        """Open a directory picker and store the chosen output directory."""
        chosen = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self._output_dir_edit.text()
        )
        if chosen:
            self._output_dir_edit.setText(chosen)

    def _on_add_files_clicked(self) -> None:
        """Open a file picker and add the chosen files to the list."""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择文件",
            "",
            "媒体文件 (*.mp4 *.mkv *.mov *.m4v *.ts *.webm *.avi *.m4a *.aac *.mp3)",
        )
        if files:
            self.add_files(files)

    def _on_run_clicked(self) -> None:
        """Build specs for every probed file and start the queue worker."""
        entries = self._probed_entries()
        if not entries:
            return
        op_key = str(self._op_combo.currentData())
        output_dir = self._output_dir()
        try:
            specs = [self._build_spec(entry, op_key, output_dir) for entry in entries]
        except JobSpecError as exc:
            self.statusBar().showMessage(str(exc), 8000)
            return
        outputs = [
            default_output_path(entry.path, op_key, output_dir) for entry in entries
        ]
        existing = [out for out in outputs if out.exists()]
        if existing and not self._confirm_overwrite(len(existing)):
            self.statusBar().showMessage("已取消运行", 5000)
            return
        self._summary_label.setText("运行中…")
        worker = QueueWorker(
            specs,
            runner_factory=self._runner_factory,
            argv_builder=build_job_argv,
        )
        self._connect_worker(worker)
        self._queue_worker = worker
        worker.start()
        self._update_buttons()

    def _on_cancel_clicked(self) -> None:
        """Cancel the running batch (running job + remaining queue)."""
        if self._queue_worker is not None:
            self._queue_worker.cancel_all()
            self.statusBar().showMessage("正在取消…", 5000)

    def _probed_entries(self) -> list[FileEntry]:
        """Return entries whose probe succeeded (skipping unprobed rows)."""
        return [entry for entry in self._model.entries() if entry.media is not None]

    def _output_dir(self) -> Path | None:
        """Return the parsed output directory, or None when blank."""
        text = self._output_dir_edit.text().strip()
        return Path(text) if text else None

    def _build_spec(
        self, entry: FileEntry, op_key: str, output_dir: Path | None
    ) -> object:
        """Build the shell-stage spec for one probed file and operation.

        Remux/tracks/meta specs are complete (their argv builders run for
        real); cut/merge/subtitles carry shell defaults and fail at argv
        dispatch with an actionable JobSpecError until todo 14 panels land.
        """
        media = entry.media
        if media is None:
            raise JobSpecError(_NOT_PROBED_MSG % entry.path.name)
        out = default_output_path(entry.path, op_key, output_dir)
        if op_key == "remux":
            return RemuxSpec(in_path=entry.path, out_path=out, streams=media.streams)
        if op_key == "tracks":
            return ExtractSpec(
                in_path=entry.path, stream_index=0, out_path=out, streams=media.streams
            )
        if op_key == "meta":
            return MetadataEditSpec(in_path=entry.path, out_path=out)
        if op_key == "cut":
            return CutSpec(
                in_path=entry.path,
                start=0.0,
                end=media.duration,
                out_path=out,
                keyframe_index=[],
                duration=media.duration,
            )
        if op_key == "merge":
            return MergeSpec(paths=[entry.path], out_path=out)
        if op_key == "subtitles":
            return DetachSpec(in_path=entry.path, stream_index=0, out_path=out)
        raise JobSpecError(_UNKNOWN_OP_MSG % op_key)

    def _confirm_overwrite(self, count: int) -> bool:
        """Ask the user to confirm overwriting ``count`` existing outputs."""
        answer = QMessageBox.question(
            self,
            "覆盖确认",
            f"{count} 个输出文件已存在，是否覆盖？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _connect_worker(self, worker: QueueWorker) -> None:
        """Wire the queue worker's signals to status/summary updates."""
        worker.job_started.connect(self._on_job_started)
        worker.job_finished.connect(self._on_job_finished)
        worker.all_done.connect(self._on_all_done)
        worker.finished.connect(partial(self._on_queue_worker_done, worker))

    def _on_job_started(self, job: object) -> None:
        """Show the started job's input name in the status bar."""
        spec = getattr(job, "spec", None)
        name = getattr(spec, "in_path", None)
        display = Path(str(name)).name if name is not None else "任务"
        self.statusBar().showMessage(f"正在处理：{display}", 0)

    def _on_job_finished(self, job: object) -> None:
        """Surface a finished job's terminal status."""
        if getattr(job, "status", None) == "failed":
            self.statusBar().showMessage(
                f"任务失败：{getattr(job, 'error', None)}", 8000
            )

    def _on_all_done(self, jobs: object) -> None:
        """Summarize the batch outcome and re-enable the run controls."""
        records = cast("Sequence[object]", jobs)
        done = failed = cancelled = 0
        for record in records:
            status = getattr(record, "status", "?")
            if status == "done":
                done += 1
            elif status == "failed":
                failed += 1
            elif status == "cancelled":
                cancelled += 1
        self._summary_label.setText(
            f"完成：{done} 成功 / {failed} 失败 / {cancelled} 取消"
        )
        self.statusBar().showMessage("批处理结束", 5000)

    def _on_queue_worker_done(self, worker: QueueWorker) -> None:
        """Release the finished queue worker and reset the buttons."""
        if self._queue_worker is worker:
            self._queue_worker = None
        self._update_buttons()
        worker.deleteLater()

    def _update_buttons(self) -> None:
        """Refresh run/cancel enablement from list and worker state."""
        running = self._queue_worker is not None and self._queue_worker.isRunning()
        has_files = self._model.rowCount() > 0
        self._run_button.setEnabled(has_files and not running)
        self._cancel_button.setEnabled(running)
        self._op_combo.setEnabled(not running)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Wait briefly for in-flight worker threads before closing."""
        if self._queue_worker is not None:
            self._queue_worker.cancel_all()
            self._queue_worker.wait(5000)
        for worker in self._probe_workers:
            worker.wait(5000)
        super().closeEvent(event)
