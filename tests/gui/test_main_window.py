# pyright: reportPrivateUsage=false
# Widget tests drive window internals directly, so private-member access is allowed.
# ruff: noqa: RUF001 - zh-CN UI assertions use fullwidth punctuation deliberately
"""GUI shell tests: window, drag-drop model, naming, and worker signals.

The offscreen Qt platform is forced before any Qt import so the suite runs
headless (the acceptance commands also prefix QT_QPA_PLATFORM=offscreen).
Real ffprobe probing runs through ProbeWorker (async); the queue run path
uses an injected fake runner so no ffmpeg process is spawned by the GUI.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyside6")

from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QThread, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QMessageBox

from lossless_toolbox.__main__ import main as cli_main
from lossless_toolbox.runner import ProgressEvent, RunResult
from lossless_toolbox.ui.main_window import MainWindow, default_output_path
from lossless_toolbox.ui.workers import QueueWorker

pytestmark = pytest.mark.gui

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Protocol

    from pytestqt.qtbot import QtBot

    class _MediaSample(Protocol):
        path: Path
        codec: str
        duration: float


_PROBE_TIMEOUT_MS = 30_000


class _FakeProgressRunner:
    """Scriptable fake runner matching the ProgressRunner protocol."""

    def __init__(
        self,
        events: Sequence[ProgressEvent] = (),
        *,
        exit_code: int = 0,
        stderr_tail: str = "",
    ) -> None:
        self._events = list(events)
        self._exit_code = exit_code
        self._stderr_tail = stderr_tail
        self.argv: list[str] = []
        self.duration: float | None = None
        self.cancel_called = False

    def run(
        self,
        argv: Sequence[str],
        *,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        stdin_bytes: bytes | None = None,
        duration: float | None = None,
    ) -> RunResult:
        self.argv = list(argv)
        self.duration = duration
        for event in self._events:
            if on_progress is not None:
                on_progress(event)
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


class _DummySpec:
    """A spec with a parameter-free build_argv for the worker unit test."""

    def build_argv(self) -> list[str]:
        return ["-fake"]


def _fake_argv_builder(spec: object) -> list[str]:
    return cast("_DummySpec", spec).build_argv()


def _record_event(order: list[str], label: str, *_args: object) -> None:
    """Append one event label, ignoring the signal's positional payload."""
    order.append(label)


def _capture_status(statuses: list[str], job: object, *_args: object) -> None:
    """Capture a job record's status at signal-emission time."""
    statuses.append(str(getattr(job, "status", "?")))


def _wait_probed(qtbot: QtBot, window: MainWindow) -> None:
    """Wait until every list entry has a probe outcome (media or error)."""
    qtbot.waitUntil(
        lambda: all(
            entry.media is not None or entry.probe_error is not None
            for entry in window.file_panel.model().entries()
        ),
        timeout=_PROBE_TIMEOUT_MS,
    )


def _wait_panel_workers(window: MainWindow) -> None:
    """Wait for async panel probe threads (remux compat) to finish.

    Blocks (test context only) so no QThread is destroyed while running
    when the window is garbage-collected at test teardown.
    """
    for panel in window.panels.values():
        for thread in panel.findChildren(QThread):
            thread.wait(10_000)


def _select_row(window: MainWindow, row: int) -> None:
    """Select a file-list row, mirroring the user's list selection."""
    window.file_panel.list_view().setCurrentIndex(window.file_panel.model().index(row))


def test_window_constructs_and_shows_zh_cn_controls(qtbot: QtBot) -> None:
    """(a) A shown window has zh-CN title and the six-operation selector."""
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    assert window.isVisible()
    assert window.windowTitle() == "无损视频工具箱"
    assert window._run_button.text() == "运行"
    assert window._cancel_button.text() == "取消"
    assert window._op_combo.count() == 6
    assert window._op_combo.itemText(0) == "转封装"
    assert window._op_combo.itemText(5) == "元数据"


def test_drop_two_fixtures_populates_list_and_info_panel(
    qtbot: QtBot, h264_aac_mp4: _MediaSample, hevc_aac_mkv: _MediaSample
) -> None:
    """(b) Two dropped fixtures become 2 rows; the probe panel renders streams."""
    window = MainWindow()
    qtbot.addWidget(window)
    added, skipped = window.file_panel.add_files(
        [str(h264_aac_mp4.path), str(hevc_aac_mkv.path)]
    )
    assert (added, skipped) == (2, 0)
    model = window.file_panel.model()
    assert model.rowCount() == 2
    _wait_probed(qtbot, window)

    row0 = model.index(0)
    assert row0.isValid()
    assert "12.0s" in str(row0.data())

    _select_row(window, 0)
    table = window.info_panel.stream_table
    assert table.rowCount() >= 2
    type_texts: set[str] = set()
    for row in range(table.rowCount()):
        item = table.item(row, 1)
        if item is not None:
            type_texts.add(item.text())
    assert "视频" in type_texts
    assert "音频" in type_texts
    assert "时长：" in window.info_panel.duration_label.text()
    _wait_panel_workers(window)


def test_drop_non_media_file_is_skipped_with_hint(
    qtbot: QtBot, h264_aac_mp4: _MediaSample, tmp_path: Path
) -> None:
    """(c) A dropped .txt is rejected, counted and reported in the status bar."""
    window = MainWindow()
    qtbot.addWidget(window)
    txt = tmp_path / "notes.txt"
    txt.write_text("not media", encoding="utf-8")
    added, skipped = window.file_panel.add_files([str(h264_aac_mp4.path), str(txt)])
    assert (added, skipped) == (1, 1)
    assert window.file_panel.model().rowCount() == 1
    assert "已跳过 1 个非媒体文件" in window.statusBar().currentMessage()


def test_drop_event_accepts_local_urls_and_filters_foreign(
    qtbot: QtBot, h264_aac_mp4: _MediaSample
) -> None:
    """Drag-drop wiring: local file URLs land in the model, foreign URLs do not."""
    window = MainWindow()
    qtbot.addWidget(window)
    mime = QMimeData()
    mime.setUrls(
        [
            QUrl.fromLocalFile(str(h264_aac_mp4.path)),
            QUrl("http://example.com/video.mp4"),
        ]
    )

    enter_event = QDragEnterEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dragEnterEvent(enter_event)
    assert enter_event.isAccepted()

    drop_event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(drop_event)
    assert window.file_panel.model().rowCount() == 1


def test_run_button_enabled_only_with_files(
    qtbot: QtBot, tmp_path: Path, h264_aac_mp4: _MediaSample
) -> None:
    """(d) The run button tracks the file list; cancel tracks the worker."""
    window = MainWindow()
    qtbot.addWidget(window)
    assert not window._run_button.isEnabled()
    assert not window._cancel_button.isEnabled()

    txt = tmp_path / "a.txt"
    txt.write_text("x", encoding="utf-8")
    window.file_panel.add_files([str(txt)])
    assert not window._run_button.isEnabled()

    window.file_panel.add_files([str(h264_aac_mp4.path)])
    _select_row(window, 0)
    _wait_probed(qtbot, window)
    assert window._run_button.isEnabled()
    _wait_panel_workers(window)


def test_default_output_path_naming_rules() -> None:
    """(e) Per-op default extensions and the source-collision guard."""
    source = Path("/media/clip.mp4")
    assert default_output_path(source, "remux") == Path("/media/clip.mkv")
    assert default_output_path(source, "cut") == Path("/media/clip.mkv")
    assert default_output_path(Path("/media/clip.mkv"), "merge") == Path(
        "/media/clip.mp4"
    )
    assert default_output_path(source, "tracks") == Path("/media/clip.m4a")
    assert default_output_path(source, "subtitles") == Path("/media/clip.mkv")
    assert default_output_path(source, "meta") == Path("/media/clip.meta.mp4")
    assert default_output_path(Path("/media/clip.mkv"), "remux") == Path(
        "/media/clip.remux.mkv"
    )
    out_dir = Path("/out")
    assert default_output_path(source, "meta", out_dir) == Path("/out/clip.mp4")
    assert default_output_path(source, "remux", out_dir) == Path("/out/clip.mkv")


def test_overwrite_confirm_dialog_gates_the_run(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    h264_aac_mp4: _MediaSample,
    tmp_path: Path,
) -> None:
    """(e) An existing default output asks for overwrite confirmation."""
    window = MainWindow()
    qtbot.addWidget(window)
    window.file_panel.add_files([str(h264_aac_mp4.path)])
    _wait_probed(qtbot, window)
    _select_row(window, 0)
    window._output_dir_edit.setText(str(tmp_path))
    (tmp_path / f"{h264_aac_mp4.path.stem}.mkv").write_bytes(b"existing")

    calls: list[tuple[object, ...]] = []

    def fake_question(*args: object) -> QMessageBox.StandardButton:
        calls.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", fake_question)
    window._run_button.click()

    assert len(calls) == 1
    assert calls[0][1] == "覆盖确认"
    assert "已存在" in str(calls[0][2])
    assert window._queue_worker is None
    assert "已取消运行" in window.statusBar().currentMessage()
    _wait_panel_workers(window)


def test_run_flow_submits_jobs_and_summarizes(
    qtbot: QtBot, h264_aac_mp4: _MediaSample, tmp_path: Path
) -> None:
    """Run click -> fake-runner job completes -> summary shows success."""
    fakes: list[_FakeProgressRunner] = []

    def factory() -> _FakeProgressRunner:
        fake = _FakeProgressRunner()
        fakes.append(fake)
        return fake

    window = MainWindow(runner_factory=factory)
    qtbot.addWidget(window)
    window.file_panel.add_files([str(h264_aac_mp4.path)])
    _wait_probed(qtbot, window)
    _select_row(window, 0)
    window._output_dir_edit.setText(str(tmp_path))
    window._run_button.click()

    qtbot.waitUntil(lambda: "成功" in window._summary_label.text(), timeout=15_000)
    assert fakes
    assert "-y" in fakes[0].argv
    assert str(tmp_path / f"{h264_aac_mp4.path.stem}.mkv") in fakes[0].argv
    assert "1 成功" in window._summary_label.text()
    _wait_panel_workers(window)


def test_queue_worker_signal_sequence_with_fake_runner(qtbot: QtBot) -> None:
    """(f) job_started/progress/finished/all_done fire in order on the worker."""
    events = [
        ProgressEvent(out_time=1.0, progress=0.5),
        ProgressEvent(out_time=2.0, progress=1.0, end=True),
    ]
    fake = _FakeProgressRunner(events)
    worker = QueueWorker(
        [_DummySpec()],
        runner_factory=lambda: fake,
        argv_builder=_fake_argv_builder,
    )
    started_spy = QSignalSpy(worker.job_started)
    progress_spy = QSignalSpy(worker.job_progress)
    finished_spy = QSignalSpy(worker.job_finished)
    done_spy = QSignalSpy(worker.all_done)

    order: list[str] = []
    statuses: list[str] = []
    direct = Qt.ConnectionType.DirectConnection
    worker.job_started.connect(partial(_record_event, order, "started"), direct)
    worker.job_progress.connect(partial(_record_event, order, "progress"), direct)
    worker.job_finished.connect(partial(_record_event, order, "finished"), direct)
    worker.all_done.connect(partial(_record_event, order, "all_done"), direct)
    worker.job_started.connect(partial(_capture_status, statuses), direct)

    worker.start()
    qtbot.waitUntil(lambda: done_spy.count() == 1, timeout=10_000)
    worker.wait(5_000)

    assert order == ["started", "progress", "progress", "finished", "all_done"]
    assert started_spy.count() == 1
    assert progress_spy.count() == 2
    assert finished_spy.count() == 1
    assert done_spy.count() == 1

    assert statuses == ["running"]
    progress_event = progress_spy.at(0)[1]
    assert isinstance(progress_event, ProgressEvent)
    assert progress_event.progress == pytest.approx(0.5)
    assert worker.results[0].status == "done"


def test_queue_worker_reports_failed_job(qtbot: QtBot) -> None:
    """A failing fake runner surfaces as job_finished(failed) with its error."""
    fake = _FakeProgressRunner(exit_code=1, stderr_tail="boom")
    worker = QueueWorker(
        [_DummySpec()],
        runner_factory=lambda: fake,
        argv_builder=_fake_argv_builder,
    )
    done_spy = QSignalSpy(worker.all_done)
    worker.start()
    qtbot.waitUntil(lambda: done_spy.count() == 1, timeout=10_000)
    worker.wait(5_000)

    records = done_spy.at(0)[0]
    assert records[0].status == "failed"
    assert "boom" in (records[0].error or "")


def test_cli_probe_self_reports_both_binaries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(g) --probe-self resolves both binaries and exits 0."""
    assert cli_main(["--probe-self"]) == 0
    out = capsys.readouterr().out
    assert "ffmpeg:" in out
    assert "ffprobe:" in out
    assert "version" in out


def test_cli_strict_bundled_fails_without_bundle(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(g) --strict-bundled without a bundle (no _MEIPASS) exits nonzero."""
    monkeypatch.delenv("LOSSLESS_TOOLBOX_FFMPEG_PATH", raising=False)
    monkeypatch.delenv("LOSSLESS_TOOLBOX_FFPROBE_PATH", raising=False)
    assert cli_main(["--strict-bundled"]) != 0
    assert "ffmpeg" in capsys.readouterr().err
