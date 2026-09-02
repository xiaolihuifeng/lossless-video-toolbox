"""Main window: file/info panels, operation panel stack and run orchestration.

Assembly only: the file list lives in :mod:`~lossless_toolbox.ui.file_panel`,
the stream table in :mod:`~lossless_toolbox.ui.info_panel`, naming and argv
dispatch in :mod:`~lossless_toolbox.ui.specs`, zh-CN copy in
:mod:`~lossless_toolbox.ui.texts`, the six parameter panels in
:mod:`~lossless_toolbox.ui.widgets`, and the run/queue lifecycle in
:mod:`~lossless_toolbox.ui.run_flow`. The window wires selection → panel
context, panel state → run-button validation, and run → QueueWorker
(ffmpeg only ever runs in worker threads).
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from lossless_toolbox.runner import Runner

from .file_panel import FileEntry, FilePanel
from .info_panel import InfoPanel
from .run_flow import confirm_overwrite, start_run
from .specs import build_job_argv, default_output_path
from .texts import OP_ITEMS
from .widgets import (
    CutPanel,
    MergePanel,
    MetaPanel,
    RemuxPanel,
    SubtitlePanel,
    TracksPanel,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent

    from .widgets.base import OperationPanel
    from .workers import ProgressRunner, QueueWorker

logger = logging.getLogger(__name__)

__all__ = ["MainWindow", "build_job_argv", "default_output_path"]

_PANEL_TYPES: tuple[type[OperationPanel], ...] = (
    RemuxPanel,
    CutPanel,
    MergePanel,
    TracksPanel,
    SubtitlePanel,
    MetaPanel,
)


class MainWindow(QMainWindow):
    """The toolbox window: list + info panels, operation stack, run/cancel."""

    def __init__(
        self,
        *,
        runner_factory: Callable[[], ProgressRunner] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Assemble the UI and wire selection, panels and the run flow."""
        super().__init__(parent)
        self.setWindowTitle("无损视频工具箱")
        self.setAcceptDrops(True)
        self._runner_factory = runner_factory or partial(Runner)
        self._queue_worker: QueueWorker | None = None
        self._current_entry: FileEntry | None = None

        self.file_panel = FilePanel()
        self.info_panel = InfoPanel()
        self.panels = {
            panel_type.operation: panel_type() for panel_type in _PANEL_TYPES
        }
        self.panel_stack = QStackedWidget()
        for _label, key in OP_ITEMS:
            self.panel_stack.addWidget(self.panels[key])

        self._op_combo = QComboBox()
        for label, key in OP_ITEMS:
            self._op_combo.addItem(label, key)
        self._output_dir_edit = QLineEdit()
        self._output_dir_edit.setPlaceholderText("留空则输出到源文件目录")
        self._run_button = QPushButton("运行")
        self._run_button.clicked.connect(self._on_run_clicked)
        self._cancel_button = QPushButton("取消")
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        self._summary_label = QLabel("就绪")
        self._hint_label = QLabel("")
        self._hint_label.setStyleSheet("color: #b00020;")

        self._build_layout()
        self._connect_signals()
        self._refresh_hint()
        self._update_buttons()

    def active_panel(self) -> OperationPanel:
        """Return the panel matching the selected operation."""
        return self.panels[str(self._op_combo.currentData())]

    def _build_layout(self) -> None:
        """Compose splitter, panel stack and the bottom operation bar."""
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.file_panel)
        splitter.addWidget(self.info_panel)

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
        root.addWidget(self.panel_stack)
        root.addWidget(self._hint_label)
        root.addLayout(bar)
        self.setCentralWidget(central)

    def _connect_signals(self) -> None:
        """Wire file-panel, panel-stack and op-combo signals."""
        self.file_panel.file_selected.connect(self._on_file_selected)
        self.file_panel.file_probed.connect(self._on_file_probed)
        self.file_panel.files_changed.connect(self._update_buttons)
        self.file_panel.skipped.connect(self._on_files_skipped)
        self._op_combo.currentIndexChanged.connect(self._on_op_changed)
        for panel in self.panels.values():
            panel.changed.connect(self._on_panel_changed)

    def _on_file_selected(self, entry: object) -> None:
        """Refresh the info panel and the active panel for the selection."""
        self._current_entry = cast("FileEntry | None", entry)
        self.info_panel.refresh(self._current_entry)
        self.active_panel().set_context(self._current_entry)
        self._refresh_hint()
        self._update_buttons()

    def _on_file_probed(self, entry: object) -> None:
        """Re-apply the probe outcome when it belongs to the selection."""
        probed = cast("FileEntry", entry)
        if probed is self._current_entry:
            self.info_panel.refresh(probed)
            self.active_panel().set_context(probed)
            self._refresh_hint()
            self._update_buttons()

    def _on_files_skipped(self, count: int) -> None:
        """Surface the non-media drop count in the status bar."""
        self.statusBar().showMessage(f"已跳过 {count} 个非媒体文件", 5000)

    def _on_op_changed(self, index: int) -> None:
        """Switch the panel stack and re-validate against the selection."""
        self.panel_stack.setCurrentIndex(index)
        self.active_panel().set_context(self._current_entry)
        self._refresh_hint()
        self._update_buttons()

    def _on_panel_changed(self) -> None:
        """Re-validate after any panel widget edit."""
        self._refresh_hint()
        self._update_buttons()

    def _refresh_hint(self) -> None:
        """Show the active panel's validation reason under the stack."""
        reason = self.active_panel().validation_error(self._current_entry)
        self._hint_label.setText(reason or "")

    def _update_buttons(self) -> None:
        """Refresh run/cancel enablement from list, panel and worker state."""
        running = self._queue_worker is not None and self._queue_worker.isRunning()
        panel = self.active_panel()
        has_input = self.file_panel.model().rowCount() > 0 or not panel.needs_files
        ready = panel.validation_error(self._current_entry) is None
        self._run_button.setEnabled(has_input and ready and not running)
        self._cancel_button.setEnabled(running)
        self._op_combo.setEnabled(not running)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """Accept drags that carry file URLs."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """Accept dropped local files; non-media paths are skipped and counted."""
        urls = event.mimeData().urls()
        self.file_panel.add_files(
            [url.toLocalFile() for url in urls if url.isLocalFile()]
        )
        event.acceptProposedAction()

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
            self.file_panel.add_files(files)

    def _on_run_clicked(self) -> None:
        """Collect specs from the active panel and start the queue worker."""
        start_run(self)

    def _on_cancel_clicked(self) -> None:
        """Cancel the running batch (running job + remaining queue)."""
        if self._queue_worker is not None:
            self._queue_worker.cancel_all()
            self.statusBar().showMessage("正在取消…", 5000)

    def _confirm_overwrite(self, count: int) -> bool:
        """Ask the user to confirm overwriting ``count`` existing outputs."""
        return confirm_overwrite(self, count)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Wait briefly for in-flight worker threads before closing."""
        if self._queue_worker is not None:
            self._queue_worker.cancel_all()
            self._queue_worker.wait(5000)
        self.file_panel.wait_for_workers(5000)
        super().closeEvent(event)
