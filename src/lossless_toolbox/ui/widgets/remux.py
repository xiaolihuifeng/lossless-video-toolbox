# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""Remux panel: target container plus compatibility warning bar (todo 14a).

The warning bar has two sources: the pure subtitle-compatibility check
(:func:`lossless_toolbox.ops.remux.check_subtitle_compat`, e.g. the
srt/ass→MP4 blocking) computed synchronously, and async
:func:`lossless_toolbox.ops.remux.muxer_supports` results for the
video/audio codecs, probed off the UI thread via
:class:`~lossless_toolbox.ui.workers.CompatProbeWorker` so the panel itself
never spawns an ffmpeg process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from lossless_toolbox.ops.remux import (
    CompatResult,
    RemuxSpec,
    check_subtitle_compat,
    muxer_supports,
)
from lossless_toolbox.ui.workers import CompatProbeWorker

from .base import OperationPanel, PanelError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from lossless_toolbox.ui.file_panel import FileEntry

_CONTAINERS: tuple[str, ...] = ("mp4", "mkv", "mov", "ts")
_PROBEABLE_TYPES: frozenset[str] = frozenset({"video", "audio"})
_DEFAULT_CONTAINER = "mkv"


class RemuxPanel(OperationPanel):
    """Target container selection with live compatibility warnings."""

    operation = "remux"

    def __init__(
        self,
        compat_check: Callable[[str, str, Path], CompatResult] = muxer_supports,
        parent: QWidget | None = None,
    ) -> None:
        """Build the container combo and warning bar.

        ``compat_check`` is the injectable muxer-probe seam (async, never
        on the UI thread).
        """
        super().__init__(parent)
        self._compat_check = compat_check
        self._entry: FileEntry | None = None
        self._worker: CompatProbeWorker | None = None
        self._generation = 0

        self._container_combo = QComboBox()
        self._container_combo.addItems(list(_CONTAINERS))
        self._container_combo.setCurrentText(_DEFAULT_CONTAINER)

        self._warning_label = QLabel("")
        self._warning_label.setWordWrap(True)
        self._warning_label.setStyleSheet("color: #b00020;")
        self._warning_label.setVisible(False)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("目标容器", self._container_combo)
        layout.addLayout(form)
        layout.addWidget(self._warning_label)

        self._container_combo.currentIndexChanged.connect(self._on_container_changed)

    def set_context(self, entry: FileEntry | None) -> None:
        """Store the current file and recompute the compatibility warning."""
        self._entry = entry
        self._refresh_warning()

    def container(self) -> str:
        """Return the currently selected target container."""
        return str(self._container_combo.currentText())

    def validation_error(self, entry: FileEntry | None = None) -> str | None:
        """Block on unprobed media or an incompatible subtitle stream."""
        reason = super().validation_error(entry)
        if reason is not None:
            return reason
        media = self._require_media(entry)
        compat = check_subtitle_compat(self.container(), media.streams)
        return None if compat.ok else (compat.reason or "字幕流不兼容")

    def output_extension(
        self, entry: FileEntry | None = None  # noqa: ARG002
    ) -> str | None:
        """The chosen container drives the output extension."""
        return f".{self.container()}"

    def build_spec(self, entry: FileEntry | None, out_path: Path) -> object:
        """Build a RemuxSpec, rejecting subtitle-incompatible targets first."""
        media = self._require_media(entry)
        compat = check_subtitle_compat(self.container(), media.streams)
        if not compat.ok:
            raise PanelError(compat.reason or "字幕流不兼容")
        return RemuxSpec(
            in_path=media.path,
            out_path=out_path,
            streams=media.streams,
            duration=media.duration,
        )

    def _on_container_changed(self, _index: int) -> None:
        """Recompute warnings and notify the window when the container changes."""
        self._refresh_warning()
        self.changed.emit()

    def _on_compat_ready(
        self,
        generation: int,
        container: str,
        codec: str,
        result: CompatResult,
    ) -> None:
        """Apply one async muxer-probe result unless it is stale."""
        if generation != self._generation or container != self.container():
            return
        if not result.ok:
            self._warning_label.setText(
                f"muxer 探测：{codec} 不被容器 {container!r} 支持（{result.reason}）"
            )
            self._warning_label.setVisible(True)

    def _refresh_warning(self) -> None:
        """Recompute the subtitle check and kick off the async muxer probe."""
        entry = self._entry
        if entry is None or entry.media is None:
            self._warning_label.setVisible(False)
            return
        container = self.container()
        compat = check_subtitle_compat(container, entry.media.streams)
        if not compat.ok:
            self._warning_label.setText(compat.reason or "字幕流不兼容")
            self._warning_label.setVisible(True)
        else:
            self._warning_label.setVisible(False)
        codecs = sorted(
            {
                stream.codec_name
                for stream in entry.media.streams
                if stream.codec_type in _PROBEABLE_TYPES
            }
        )
        if not codecs:
            return
        self._generation += 1
        worker = CompatProbeWorker(
            self._generation, container, codecs, self._compat_check, self
        )
        worker.compat_ready.connect(self._on_compat_ready)
        self._worker = worker
        worker.start()
