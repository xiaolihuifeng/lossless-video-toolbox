# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""Cut panel: start/end inputs with live keyframe-snap preview (todo 14b).

Keyframes are loaded asynchronously by
:class:`~lossless_toolbox.ui.workers.KeyframeWorker` when a file is
selected — the UI thread never runs ffprobe. The snap preview constructs a
:class:`~lossless_toolbox.ops.cut.CutSpec` and calls its pure
``build_plan()`` (no process, no argv execution) to surface the actual cut
points, mirroring exactly what the queue will run.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QDoubleSpinBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from lossless_toolbox.models import KeyframeIndex
from lossless_toolbox.ops.cut import CutRangeError, CutSpec, UnsupportedInputError
from lossless_toolbox.ui.workers import KeyframeWorker

from .base import OperationPanel, PanelError

if TYPE_CHECKING:
    from pathlib import Path

    from lossless_toolbox.models import MediaFile
    from lossless_toolbox.ui.file_panel import FileEntry

_KEYFRAMES_MISSING_MSG = "关键帧尚未加载，请等待异步加载完成"
_OTHER_FILE_KEYFRAMES_MSG = "该文件的关键帧尚未加载：请先在文件列表选中它"


class CutPanel(OperationPanel):
    """Start/end spinboxes plus a live ``actual_start → actual_end`` label."""

    operation = "cut"

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the range inputs and the snap-preview label."""
        super().__init__(parent)
        self._entry: FileEntry | None = None
        self._keyframes: KeyframeIndex | None = None
        self._keyframes_error: str | None = None
        self._worker: KeyframeWorker | None = None

        self._start_spin = QDoubleSpinBox()
        self._start_spin.setDecimals(3)
        self._start_spin.setMaximum(1_000_000.0)
        self._start_spin.setSuffix(" 秒")
        self._end_spin = QDoubleSpinBox()
        self._end_spin.setDecimals(3)
        self._end_spin.setMaximum(1_000_000.0)
        self._end_spin.setSuffix(" 秒")

        self._preview_label = QLabel("关键帧加载中…")
        self._preview_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("开始时间", self._start_spin)
        form.addRow("结束时间", self._end_spin)
        form.addRow("实际切点", self._preview_label)
        layout.addLayout(form)

        self._start_spin.valueChanged.connect(self._on_range_changed)
        self._end_spin.valueChanged.connect(self._on_range_changed)

    def set_context(self, entry: FileEntry | None) -> None:
        """Load keyframes for the new selection and reset the range bounds."""
        self._entry = entry
        media = entry.media if entry is not None else None
        if entry is not None and media is not None:
            with QSignalBlocker(self._start_spin), QSignalBlocker(self._end_spin):
                self._start_spin.setMaximum(media.duration)
                self._end_spin.setMaximum(media.duration)
                self._start_spin.setValue(0.0)
                self._end_spin.setValue(media.duration)
            self._load_keyframes(entry.path)
        else:
            self._keyframes = None
            self._keyframes_error = None
        self._refresh_preview()
        self.changed.emit()

    def validation_error(self, entry: FileEntry | None = None) -> str | None:
        """Block on unprobed media, missing keyframes or an invalid range."""
        reason = super().validation_error(entry)
        if reason is not None:
            return reason
        if self._keyframes is None:
            return _KEYFRAMES_MISSING_MSG
        if self._start_spin.value() >= self._end_spin.value():
            return "开始时间必须小于结束时间"
        return None

    def build_spec(self, entry: FileEntry | None, out_path: Path) -> object:
        """Build the CutSpec, validating the range via a pure build_plan."""
        media = self._require_media(entry)
        if entry is not self._entry or self._keyframes is None:
            raise PanelError(_OTHER_FILE_KEYFRAMES_MSG)
        spec = CutSpec(
            in_path=media.path,
            start=self._start_spin.value(),
            end=self._end_spin.value(),
            out_path=out_path,
            keyframe_index=self._keyframes.times,
            duration=media.duration,
            has_attached_pic=_has_attached_pic(media),
        )
        try:
            spec.build_plan()
        except CutRangeError as exc:
            raise PanelError(str(exc)) from exc
        except UnsupportedInputError as exc:
            raise PanelError(str(exc)) from exc
        return spec

    def _load_keyframes(self, path: Path) -> None:
        """Kick off the async keyframe scan for ``path``."""
        self._keyframes = None
        self._keyframes_error = None
        worker = KeyframeWorker(path)
        worker.keyframes_loaded.connect(self._on_keyframes_loaded)
        worker.finished.connect(partial(self._on_worker_done, worker))
        self._worker = worker
        worker.start()

    def _on_keyframes_loaded(self, path: Path, result: object) -> None:
        """Store a keyframe outcome for the still-current selection."""
        if self._entry is None or self._entry.path != path:
            return
        if isinstance(result, KeyframeIndex):
            self._keyframes = result
            self._keyframes_error = None
        else:
            self._keyframes = None
            self._keyframes_error = str(result)
        self._refresh_preview()
        self.changed.emit()

    def _on_worker_done(self, worker: KeyframeWorker) -> None:
        """Release the finished keyframe worker."""
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    def _on_range_changed(self, _value: float) -> None:
        """Refresh the preview and notify the window on any range edit."""
        self._refresh_preview()
        self.changed.emit()

    def _refresh_preview(self) -> None:
        """Recompute the snapped cut points (pure CutSpec.build_plan)."""
        entry = self._entry
        if entry is None or entry.media is None:
            self._preview_label.setText("—")
            return
        if self._keyframes is None:
            if self._keyframes_error is not None:
                self._preview_label.setText(f"关键帧加载失败：{self._keyframes_error}")
            else:
                self._preview_label.setText("关键帧加载中…")
            return
        spec = CutSpec(
            in_path=entry.path,
            start=self._start_spin.value(),
            end=self._end_spin.value(),
            out_path=entry.path,
            keyframe_index=self._keyframes.times,
            duration=entry.media.duration,
            has_attached_pic=_has_attached_pic(entry.media),
        )
        try:
            plan = spec.build_plan()
        except CutRangeError:
            self._preview_label.setText(
                "无效区间（开始时间必须小于结束时间且不超过时长）"
            )
            return
        except UnsupportedInputError as exc:
            self._preview_label.setText(str(exc))
            return
        self._preview_label.setText(
            f"实际切点：{plan.actual_start:.3f} → {plan.actual_end:.3f}"
        )


def _has_attached_pic(media: MediaFile) -> bool:
    """Return whether any stream carries the attached_pic disposition."""
    return any(stream.disposition.get("attached_pic") for stream in media.streams)
