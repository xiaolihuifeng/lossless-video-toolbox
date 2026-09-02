# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
# ruff: noqa: SLF001 - assembly module wiring the window's private widget
# state; the thin bound methods stay on MainWindow.
# pyright: reportPrivateUsage=false
"""Run-flow orchestration moved out of the main window (todo 14 split).

Everything here drives one window's spec collection and queue-worker
lifecycle: :func:`start_run` reads the active panel into validated ops
specs, :func:`confirm_overwrite` gates existing outputs, and the
``connect_worker``/callback group replays queue events into the status bar
and summary label. The window keeps thin bound methods (``_on_run_clicked``,
``_confirm_overwrite``) so its surface stays unchanged.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PySide6.QtWidgets import QMessageBox

from .specs import build_job_argv, default_output_path
from .widgets.base import OperationPanel, PanelError
from .widgets.merge import MergePanel
from .workers import QueueWorker

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .main_window import MainWindow

__all__ = ["confirm_overwrite", "start_run"]


def start_run(win: MainWindow) -> None:
    """Collect specs from the active panel and start the queue worker."""
    panel = win.active_panel()
    reason = panel.validation_error(win._current_entry)
    if reason is not None:
        win.statusBar().showMessage(reason, 8000)
        return
    output_dir = _output_dir(win)
    if isinstance(panel, MergePanel):
        specs, outputs = _merge_specs(win, panel, output_dir)
    else:
        specs, outputs = _per_file_specs(win, panel, output_dir)
    if specs is None:
        return
    existing = [out for out in outputs if out.exists()]
    if existing and not confirm_overwrite(win, len(existing)):
        win.statusBar().showMessage("已取消运行", 5000)
        return
    win._summary_label.setText("运行中…")
    worker = QueueWorker(
        specs,
        runner_factory=win._runner_factory,
        argv_builder=build_job_argv,
    )
    connect_worker(win, worker)
    win._queue_worker = worker
    worker.start()
    win._update_buttons()


def _merge_specs(
    win: MainWindow, panel: MergePanel, output_dir: Path | None
) -> tuple[list[object] | None, list[Path]]:
    """Build the single MergeSpec from the panel's ordered list."""
    paths = panel.paths()
    try:
        out = default_output_path(paths[0], "merge", output_dir)
        spec = panel.build_spec(None, out)
    except PanelError as exc:
        win.statusBar().showMessage(str(exc), 8000)
        return None, []
    return [spec], [out]


def _per_file_specs(
    win: MainWindow, panel: OperationPanel, output_dir: Path | None
) -> tuple[list[object] | None, list[Path]]:
    """Build one spec per probed file, using the panel's extension rule."""
    specs: list[object] = []
    outputs: list[Path] = []
    for entry in win.file_panel.probed_entries():
        extension = panel.output_extension(entry)
        out = default_output_path(
            entry.path, panel.operation, output_dir, extension=extension
        )
        try:
            specs.append(panel.build_spec(entry, out))
        except PanelError as exc:
            win.statusBar().showMessage(str(exc), 8000)
            return None, []
        outputs.append(out)
    return specs, outputs


def _output_dir(win: MainWindow) -> Path | None:
    """Return the parsed output directory, or None when blank."""
    text = win._output_dir_edit.text().strip()
    return Path(text) if text else None


def confirm_overwrite(win: MainWindow, count: int) -> bool:
    """Ask the user to confirm overwriting ``count`` existing outputs."""
    answer = QMessageBox.question(
        win,
        "覆盖确认",
        f"{count} 个输出文件已存在，是否覆盖？",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def connect_worker(win: MainWindow, worker: QueueWorker) -> None:
    """Wire the queue worker's signals to status/summary updates."""
    worker.job_started.connect(partial(_on_job_started, win))
    worker.job_finished.connect(partial(_on_job_finished, win))
    worker.all_done.connect(partial(_on_all_done, win))
    worker.finished.connect(partial(_on_queue_worker_done, win, worker))


def _on_job_started(win: MainWindow, job: object) -> None:
    """Show the started job's input name in the status bar."""
    spec = getattr(job, "spec", None)
    name = getattr(spec, "in_path", None)
    display = Path(str(name)).name if name is not None else "任务"
    win.statusBar().showMessage(f"正在处理：{display}", 0)


def _on_job_finished(win: MainWindow, job: object) -> None:
    """Surface a finished job's terminal status."""
    if getattr(job, "status", None) == "failed":
        win.statusBar().showMessage(f"任务失败：{getattr(job, 'error', None)}", 8000)


def _on_all_done(win: MainWindow, jobs: object) -> None:
    """Summarize the batch, clean up specs and re-enable the controls."""
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
        cleanup = getattr(getattr(record, "spec", None), "cleanup", None)
        if cleanup is not None:
            cleanup()
    win._summary_label.setText(f"完成：{done} 成功 / {failed} 失败 / {cancelled} 取消")
    win.statusBar().showMessage("批处理结束", 5000)


def _on_queue_worker_done(win: MainWindow, worker: QueueWorker) -> None:
    """Release the finished queue worker and reset the buttons."""
    if win._queue_worker is worker:
        win._queue_worker = None
    win._update_buttons()
    worker.deleteLater()
