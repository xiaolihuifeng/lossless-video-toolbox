# pyright: reportPrivateUsage=false
# Panel tests drive widget internals directly, so private-member access is allowed.
"""GUI tests for the queue progress panel (todo 15).

The panel is driven through a real :class:`QueueWorker` with scripted fake
runners, so every assertion observes the actual signal path (worker thread
-> queued emission -> panel slots on the main thread). No ffmpeg process is
ever spawned.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyside6")

import threading
import time
from typing import TYPE_CHECKING, cast

import pytest
from PySide6.QtCore import QProcess, Qt, QThread
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QDialog, QPlainTextEdit

from lossless_toolbox.runner import ProgressEvent, RunResult
from lossless_toolbox.ui.main_window import MainWindow
from lossless_toolbox.ui.progress_panel import ProgressPanel
from lossless_toolbox.ui.workers import QueueWorker

pytestmark = pytest.mark.gui

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path
    from typing import Protocol

    from pytestqt.qtbot import QtBot

    class _MediaSample(Protocol):
        path: Path
        codec: str
        duration: float


_PROBE_TIMEOUT_MS = 30_000


class _FakeRunner:
    """Scriptable fake matching the ProgressRunner protocol."""

    def __init__(
        self,
        *,
        events: Sequence[ProgressEvent] = (),
        exit_code: int = 0,
        stderr_tail: str = "",
        delay: float = 0.02,
        block: threading.Event | None = None,
    ) -> None:
        self._events = list(events)
        self._exit_code = exit_code
        self._stderr_tail = stderr_tail
        self._delay = delay
        self._block = block
        self.argv: list[str] = []
        self.cancel_called = False

    def run(
        self,
        argv: Sequence[str],
        *,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> RunResult:
        self.argv = list(argv)
        if self._block is not None:
            self._block.wait(15.0)
        for event in self._events:
            if on_progress is not None:
                on_progress(event)
            time.sleep(self._delay)
        return RunResult(
            exit_code=self._exit_code,
            cancelled=False,
            progress_events=tuple(self._events),
            last_out_time=None,
            duration=0.0,
            stderr_tail=self._stderr_tail,
        )

    def cancel(self) -> None:
        self.cancel_called = True
        if self._block is not None:
            self._block.set()


class _DummySpec:
    """A spec with a name and a parameter-free build_argv."""

    def __init__(self, name: str) -> None:
        self.in_path = name

    def build_argv(self) -> list[str]:
        return ["-fake"]


def _fake_argv_builder(spec: object) -> list[str]:
    return cast("_DummySpec", spec).build_argv()


def _events() -> list[ProgressEvent]:
    return [
        ProgressEvent(out_time=1.0, progress=0.0),
        ProgressEvent(out_time=2.0, progress=0.5),
        ProgressEvent(out_time=3.0, progress=1.0),
    ]


def _wait_probed(qtbot: QtBot, window: MainWindow) -> None:
    qtbot.waitUntil(
        lambda: all(
            entry.media is not None or entry.probe_error is not None
            for entry in window.file_panel.model().entries()
        ),
        timeout=_PROBE_TIMEOUT_MS,
    )


def _select_row(window: MainWindow, row: int) -> None:
    window.file_panel.list_view().setCurrentIndex(window.file_panel.model().index(row))


def _wait_panel_workers(window: MainWindow) -> None:
    for panel in window.panels.values():
        for thread in panel.findChildren(QThread):
            thread.wait(10_000)


def test_progress_advances_and_rows_flip_to_done(qtbot: QtBot) -> None:
    """(A) 0/50/100 progress drives the bars; rows land on the done state."""
    fakes = [
        _FakeRunner(events=_events(), delay=0.03),
        _FakeRunner(events=_events(), delay=0.03),
    ]
    panel = ProgressPanel()
    qtbot.addWidget(panel)
    worker = QueueWorker(
        [_DummySpec("a.mp4"), _DummySpec("b.mp4")],
        runner_factory=lambda: fakes.pop(0),
        argv_builder=_fake_argv_builder,
    )
    panel.attach(worker)
    done_spy = QSignalSpy(worker.all_done)
    main_values: list[int] = []
    panel.progress_bar.valueChanged.connect(main_values.append)

    worker.start()
    qtbot.waitUntil(lambda: panel.table.rowCount() == 1, timeout=10_000)
    assert panel.row_status(0) == "▶ 运行中"
    assert panel.row_name(0) == "a.mp4"
    row_bar = panel.row_bar(0)
    assert row_bar is not None
    row_values: list[int] = []
    row_bar.valueChanged.connect(row_values.append)

    qtbot.waitUntil(lambda: done_spy.count() == 1, timeout=15_000)
    worker.wait(5_000)

    assert 50 in main_values
    assert 100 in main_values
    assert 100 in row_values
    assert panel.row_bar(0) is not None
    assert panel.row_bar(0).text() == "100%"
    assert panel.row_status(0) == "✓ 成功"
    assert panel.row_status(1) == "✓ 成功"
    assert panel.row_name(1) == "b.mp4"
    assert panel.progress_bar.value() == 0
    assert "2 成功" in panel.summary_label.text()


def test_failed_job_shows_error_row_and_non_modal_details(
    qtbot: QtBot,
) -> None:
    """(B) A failing job lands ✗; the next job still runs; details dialog opens."""
    stderr = "ENCODER_BOOM: invalid stream 0x1f\nframe decode failed"
    fakes = [
        _FakeRunner(exit_code=1, stderr_tail=stderr),
        _FakeRunner(exit_code=0),
    ]
    panel = ProgressPanel()
    qtbot.addWidget(panel)
    worker = QueueWorker(
        [_DummySpec("bad.mp4"), _DummySpec("good.mp4")],
        runner_factory=lambda: fakes.pop(0),
        argv_builder=_fake_argv_builder,
    )
    panel.attach(worker)
    done_spy = QSignalSpy(worker.all_done)
    worker.start()
    qtbot.waitUntil(lambda: done_spy.count() == 1, timeout=15_000)
    worker.wait(5_000)

    assert panel.row_status(0) == "✗ 失败"
    assert panel.row_status(1) == "✓ 成功"
    summary = panel.summary_label.text()
    assert "1 成功" in summary
    assert "1 失败" in summary

    panel.show()
    panel.resize(640, 480)
    panel.activateWindow()
    panel.setFocus()
    qtbot.waitUntil(
        lambda: panel.table.visualItemRect(panel.table.item(0, 0)).width() > 0,
        timeout=5_000,
    )
    rect = panel.table.visualItemRect(panel.table.item(0, 0))
    qtbot.mouseClick(
        panel.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )
    qtbot.mouseDClick(
        panel.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )
    dialogs = panel.findChildren(QDialog)
    assert dialogs
    dialog = dialogs[-1]
    assert dialog.windowModality() == Qt.WindowModality.NonModal
    view = dialog.findChild(QPlainTextEdit)
    assert view is not None
    assert stderr in view.toPlainText()
    dialog.close()


def test_cancelled_job_marks_row_and_counts_in_summary(qtbot: QtBot) -> None:
    """(C) cancel_current mid-run lands ⊘ on row 0 while row 1 still succeeds."""
    gate = threading.Event()
    first_fake = _FakeRunner(block=gate, exit_code=1)
    fakes = [
        first_fake,
        _FakeRunner(exit_code=0),
    ]
    panel = ProgressPanel()
    qtbot.addWidget(panel)
    worker = QueueWorker(
        [_DummySpec("first.mp4"), _DummySpec("second.mp4")],
        runner_factory=lambda: fakes.pop(0),
        argv_builder=_fake_argv_builder,
    )
    panel.attach(worker)
    started_spy = QSignalSpy(worker.job_started)
    done_spy = QSignalSpy(worker.all_done)
    worker.start()

    qtbot.waitUntil(lambda: started_spy.count() == 1, timeout=10_000)
    worker.cancel_current()
    qtbot.waitUntil(lambda: done_spy.count() == 1, timeout=15_000)
    worker.wait(5_000)

    assert first_fake.cancel_called
    assert panel.row_status(0) == "⊘ 已取消"
    assert panel.row_status(1) == "✓ 成功"
    summary = panel.summary_label.text()
    assert "1 取消" in summary
    assert "1 成功" in summary


def test_open_output_dir_button_launches_xdg_open(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """(D) The open button spawns ``xdg-open <dir>``; failure emits a signal."""
    calls: list[list[str]] = []

    def fake_start_detached(program: str, arguments: list[str]) -> tuple[bool, int]:
        calls.append([program, *arguments])
        return True, 1234

    monkeypatch.setattr(QProcess, "startDetached", staticmethod(fake_start_detached))

    panel = ProgressPanel()
    qtbot.addWidget(panel)
    assert not panel.open_dir_button.isEnabled()
    panel.set_output_dir(tmp_path)
    assert panel.open_dir_button.isEnabled()
    qtbot.mouseClick(panel.open_dir_button, Qt.MouseButton.LeftButton)
    assert calls == [["xdg-open", str(tmp_path)]]

    def failing_start_detached(program: str, arguments: list[str]) -> tuple[bool, int]:
        calls.append([program, *arguments])
        return False, -1

    monkeypatch.setattr(
        QProcess, "startDetached", staticmethod(failing_start_detached)
    )
    spy = QSignalSpy(panel.open_failed)
    qtbot.mouseClick(panel.open_dir_button, Qt.MouseButton.LeftButton)
    assert spy.count() == 1
    assert spy.at(0)[0] == str(tmp_path)


def test_main_window_wires_progress_panel_into_run_flow(
    qtbot: QtBot, h264_aac_mp4: _MediaSample, tmp_path: Path
) -> None:
    """(E) A full run through the window lands the panel summary and open dir."""
    fakes: list[_FakeRunner] = []

    def factory() -> _FakeRunner:
        fake = _FakeRunner()
        fakes.append(fake)
        return fake

    window = MainWindow(runner_factory=factory)
    qtbot.addWidget(window)
    panel = window.progress_panel
    window.file_panel.add_files([str(h264_aac_mp4.path)])
    _wait_probed(qtbot, window)
    _select_row(window, 0)
    window._output_dir_edit.setText(str(tmp_path))
    window._run_button.click()

    qtbot.waitUntil(lambda: "1 成功" in panel.summary_label.text(), timeout=15_000)
    qtbot.waitUntil(lambda: window._queue_worker is None, timeout=5_000)
    assert panel.row_status(0) == "✓ 成功"
    assert panel.open_dir_button.isEnabled()
    assert panel._output_dir == tmp_path
    assert "1 成功" in window._summary_label.text()
    _wait_panel_workers(window)
