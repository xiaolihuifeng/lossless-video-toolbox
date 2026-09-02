# pyright: reportPrivateUsage=false
# e2e tests drive widget internals directly, so private-member access is allowed.
"""End-to-end QA: real UI -> queue -> ffmpeg chains (plan todo 16).

Every test drives the real :class:`MainWindow` (no fake runner), submits a
batch through the queue, and lets the real ``ffmpeg``/``ffprobe`` binaries do
the work. Assertions therefore lock the whole stack at once: probe -> panel
spec -> argv -> runner -> ffmpeg -> observable output. Losslessness is the
load-bearing invariant, checked via per-stream ``-c copy -f md5`` digests
(video and audio) and re-probed codec names.

The offscreen Qt platform is forced before any Qt import so the suite runs
headless. All waits use ``qtbot.waitUntil`` with generous ceilings (real
ffmpeg jobs are queued sequentially). No manual clicks: file/panel/run actions
go through the model API and widget ``click()``.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_API", "pyside6")

import json
import re
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QThread

from lossless_toolbox.probe import probe
from lossless_toolbox.ui.main_window import MainWindow

pytestmark = pytest.mark.gui

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Protocol

    from pytestqt.qtbot import QtBot

    class _MediaSample(Protocol):
        path: Path
        codec: str
        duration: float


_PROBE_TIMEOUT_MS = 30_000
_BATCH_TIMEOUT_MS = 60_000
_MD5_RE = re.compile(r"MD5=([0-9a-f]{32})")


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    assert binary is not None, "ffmpeg not found on PATH"
    return binary


def _wait_probed(qtbot: QtBot, window: MainWindow) -> None:
    """Wait until every list entry has a probe outcome (media or error)."""
    qtbot.waitUntil(
        lambda: all(
            entry.media is not None or entry.probe_error is not None
            for entry in window.file_panel.model().entries()
        ),
        timeout=_PROBE_TIMEOUT_MS,
    )


def _select_row(window: MainWindow, row: int) -> None:
    """Select a file-list row, mirroring the user's list selection."""
    window.file_panel.list_view().setCurrentIndex(window.file_panel.model().index(row))


def _wait_panel_workers(window: MainWindow) -> None:
    """Wait for async panel probe threads (remux compat / cut keyframes)."""
    for panel in window.panels.values():
        for thread in panel.findChildren(QThread):
            thread.wait(10_000)


def _stream_md5(path: Path, selector: str) -> str:
    """Return the md5 of one stream's elementary data (lossless truth).

    ``ffmpeg -map <selector> -c copy -f md5 -`` hashes the copied packets, so
    a remux that re-encodes would change the digest. The ``selector`` is an
    ffmpeg stream specifier (e.g. ``0:v:0`` / ``0:a:0``).
    """
    proc = subprocess.run(  # noqa: S603 - static argv list, no shell
        [
            _ffmpeg(),
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            selector,
            "-c",
            "copy",
            "-f",
            "md5",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    match = _MD5_RE.search(proc.stdout)
    assert match is not None, proc.stdout
    return match.group(1)


def _codec(path: Path, codec_type: str) -> str:
    """Return the first ``codec_type`` stream's codec name (via ffprobe)."""
    return next(
        stream.codec_name
        for stream in probe(path).streams
        if stream.codec_type == codec_type
    )


def _first_video_packet(path: Path) -> tuple[float, str]:
    """Return the first video packet's ``(pts_time, flags)`` via ffprobe."""
    proc = subprocess.run(  # noqa: S603 - static argv list, no shell
        [
            shutil.which("ffprobe") or "",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=pts_time,flags",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    packet = json.loads(proc.stdout)["packets"][0]
    return float(packet["pts_time"]), str(packet.get("flags", ""))


def _summary_count(summary: str, key: str) -> int:
    """Parse one ``N key`` count out of the zh-CN summary label."""
    match = re.search(rf"(\d+) {key}", summary)
    return int(match.group(1)) if match else 0


def test_batch_remux_two_files_lossless(
    qtbot: QtBot,
    h264_aac_mp4: _MediaSample,
    hevc_aac_mkv: _MediaSample,
    tmp_path: Path,
) -> None:
    """Two-file batch remux -> both outputs exist, codec + md5 unchanged."""
    window = MainWindow()
    qtbot.addWidget(window)
    window.file_panel.add_files([str(h264_aac_mp4.path), str(hevc_aac_mkv.path)])
    _wait_probed(qtbot, window)
    _select_row(window, 0)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    window._output_dir_edit.setText(str(out_dir))
    window._run_button.click()

    qtbot.waitUntil(
        lambda: "2 成功" in window.progress_panel.summary_label.text(),
        timeout=_BATCH_TIMEOUT_MS,
    )
    qtbot.waitUntil(lambda: window._queue_worker is None, timeout=10_000)
    assert "成功" in window._summary_label.text()

    # The remux panel's default target container is mkv, so both inputs land
    # on ``<stem>.mkv`` (the mp4 input changes container; the mkv input is a
    # same-container remux into the fresh output directory).
    out_mp4 = out_dir / f"{h264_aac_mp4.path.stem}.mkv"
    out_mkv = out_dir / f"{hevc_aac_mkv.path.stem}.mkv"
    assert out_mp4.is_file()
    assert out_mkv.is_file()

    # Zero-re-encode guard: codec names and per-stream md5 are byte-identical.
    for out, source in ((out_mp4, h264_aac_mp4.path), (out_mkv, hevc_aac_mkv.path)):
        assert _codec(out, "video") == _codec(source, "video")
        assert _codec(out, "audio") == _codec(source, "audio")
        assert _stream_md5(out, "0:v:0") == _stream_md5(source, "0:v:0")
        assert _stream_md5(out, "0:a:0") == _stream_md5(source, "0:a:0")
    _wait_panel_workers(window)


def test_remux_to_mp4_blocked_with_subtitles(
    qtbot: QtBot, srt_mkvm: _MediaSample, tmp_path: Path
) -> None:
    """srt subtitle -> mp4 remux is blocked at validation, before any job."""
    window = MainWindow()
    qtbot.addWidget(window)
    window.file_panel.add_files([str(srt_mkvm.path)])
    _wait_probed(qtbot, window)
    _select_row(window, 0)
    window._output_dir_edit.setText(str(tmp_path))

    remux_panel = window.panels["remux"]
    remux_panel._container_combo.setCurrentText("mp4")

    # Blocked up front: the run button is disabled and no worker is created.
    assert not window._run_button.isEnabled()
    reason = remux_panel.validation_error(window.file_panel.current_entry())
    assert reason is not None
    assert "mov_text" in reason

    window._run_button.click()
    assert window._queue_worker is None
    assert not any(tmp_path.iterdir())  # no output produced
    _wait_panel_workers(window)


def test_cut_snaps_to_keyframes_e2e(
    qtbot: QtBot, h264_aac_mp4: _MediaSample, tmp_path: Path
) -> None:
    """Cut(1.2, 6.0) snaps to keyframes; the copy starts on a keyframe, lossless."""
    window = MainWindow()
    qtbot.addWidget(window)
    window.file_panel.add_files([str(h264_aac_mp4.path)])
    _wait_probed(qtbot, window)
    _select_row(window, 0)

    window._op_combo.setCurrentIndex(window._op_combo.findData("cut"))
    cut_panel = window.panels["cut"]
    qtbot.waitUntil(lambda: cut_panel._keyframes is not None, timeout=30_000)

    times = cut_panel._keyframes.times
    start, end = 1.2, 6.0
    expected_start = next(t for t in times if t >= start)
    expected_end = next(t for t in times if t >= end)

    cut_panel._start_spin.setValue(start)
    cut_panel._end_spin.setValue(end)
    label = cut_panel._preview_label.text()
    assert f"{expected_start:.3f}" in label
    assert f"{expected_end:.3f}" in label

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    window._output_dir_edit.setText(str(out_dir))
    window._run_button.click()

    qtbot.waitUntil(
        lambda: "1 成功" in window.progress_panel.summary_label.text(),
        timeout=_BATCH_TIMEOUT_MS,
    )
    qtbot.waitUntil(lambda: window._queue_worker is None, timeout=10_000)

    out = out_dir / f"{h264_aac_mp4.path.stem}.mkv"
    assert out.is_file()

    out_media = probe(out)
    assert out_media.duration == pytest.approx(expected_end - expected_start, abs=0.6)
    assert _codec(out, "video") == _codec(h264_aac_mp4.path, "video")
    _, flags = _first_video_packet(out)
    assert "K" in flags
    _wait_panel_workers(window)


def test_merge_two_compatible_files(
    qtbot: QtBot, h264_aac_mp4: _MediaSample, tmp_path: Path
) -> None:
    """Merge a file with a same-codec 6s copy of itself -> lossless concat."""
    dup = tmp_path / "dup.mp4"
    proc = subprocess.run(  # noqa: S603 - controlled invocation, no shell
        [
            _ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(h264_aac_mp4.path),
            "-c",
            "copy",
            "-t",
            "6",
            str(dup),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    window = MainWindow()
    qtbot.addWidget(window)
    window._op_combo.setCurrentIndex(window._op_combo.findData("merge"))
    merge_panel = window.panels["merge"]
    assert merge_panel.add_paths([str(h264_aac_mp4.path), str(dup)]) == 2

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    window._output_dir_edit.setText(str(out_dir))
    assert window._run_button.isEnabled()
    window._run_button.click()

    qtbot.waitUntil(
        lambda: "1 成功" in window.progress_panel.summary_label.text(),
        timeout=_BATCH_TIMEOUT_MS,
    )
    qtbot.waitUntil(lambda: window._queue_worker is None, timeout=10_000)

    out = out_dir / f"{h264_aac_mp4.path.stem}.mp4"
    assert out.is_file()

    merged = probe(out)
    expected_duration = probe(h264_aac_mp4.path).duration + probe(dup).duration
    assert merged.duration == pytest.approx(expected_duration, abs=1.5)
    assert _codec(out, "video") == _codec(h264_aac_mp4.path, "video")
    _wait_panel_workers(window)


def test_cancel_mid_batch_stops_remaining(
    qtbot: QtBot,
    h264_aac_mp4: _MediaSample,
    hevc_aac_mkv: _MediaSample,
    tmp_path: Path,
) -> None:
    """Cancel after the first job starts -> the second job never runs."""
    window = MainWindow()
    qtbot.addWidget(window)
    window.file_panel.add_files([str(h264_aac_mp4.path), str(hevc_aac_mkv.path)])
    _wait_probed(qtbot, window)
    _select_row(window, 0)

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    window._output_dir_edit.setText(str(out_dir))
    window._run_button.click()

    # The first job's row appears once job 0 is running; cancelling then marks
    # the still-queued second job cancelled before it ever spawns ffmpeg.
    qtbot.waitUntil(
        lambda: window.progress_panel.table.rowCount() >= 1,
        timeout=_BATCH_TIMEOUT_MS,
    )
    window._cancel_button.click()
    qtbot.waitUntil(lambda: window._queue_worker is None, timeout=_BATCH_TIMEOUT_MS)

    summary = window._summary_label.text()
    assert "取消" in summary
    done = _summary_count(summary, "成功")
    cancelled = _summary_count(summary, "取消")
    failed = _summary_count(summary, "失败")
    assert failed == 0
    assert done + cancelled == 2

    # The second (hevc) job was cancelled before running: its output is absent.
    assert not (out_dir / f"{hevc_aac_mkv.path.stem}.mkv").exists()
    _wait_panel_workers(window)
