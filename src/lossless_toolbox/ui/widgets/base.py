"""Panel base contract shared by the six operation panels (todo 14).

Panels are pure spec factories: they read widget state into the existing ops
models (todo 5-10) and never touch ffmpeg/ffprobe processes — any probing
they need goes through the async workers in
:mod:`lossless_toolbox.ui.workers`. ``changed`` fires on every widget-state
edit so the main window can re-validate the run button live.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from pathlib import Path

    from lossless_toolbox.models import MediaFile
    from lossless_toolbox.ui.file_panel import FileEntry

NOT_SELECTED_MSG = "未选择文件"
NOT_PROBED_MSG = "%s 尚未探测完成"
PROBE_FAILED_MSG = "%s 探测失败"


class PanelError(RuntimeError):
    """Raised when a panel cannot build a valid spec (zh-CN reason)."""


class OperationPanel(QWidget):
    """Base for the six operation panels.

    Subclasses implement :meth:`build_spec` (one ops spec for ``entry``
    writing to ``out_path``), may override :meth:`validation_error` (the
    zh-CN reason the run button stays disabled) and
    :meth:`output_extension` (extension override for the default output
    naming). :meth:`set_context` receives the window's current file
    selection.
    """

    changed = Signal()
    operation: ClassVar[str] = ""
    needs_files: ClassVar[bool] = True

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the panel widget."""
        super().__init__(parent)

    def set_context(self, entry: FileEntry | None) -> None:
        """Refresh the panel for the newly selected file (default no-op)."""

    def validation_error(self, entry: FileEntry | None = None) -> str | None:
        """Return the zh-CN reason the run is blocked, or None when ready."""
        if entry is None:
            return NOT_SELECTED_MSG
        if entry.media is None:
            if entry.probe_error is not None:
                return PROBE_FAILED_MSG % entry.path.name
            return NOT_PROBED_MSG % entry.path.name
        return None

    def output_extension(self, entry: FileEntry | None = None) -> str | None:
        """Override the default output extension, or None for the rule default."""

    def build_spec(self, entry: FileEntry | None, out_path: Path) -> object:
        """Build one ops spec for ``entry`` writing to ``out_path``.

        Raises:
            PanelError: When the widget state is invalid for ``entry``.
        """
        raise NotImplementedError

    @staticmethod
    def _require_media(entry: FileEntry | None) -> MediaFile:
        """Return ``entry``'s probed media, raising PanelError otherwise."""
        if entry is None:
            raise PanelError(NOT_SELECTED_MSG)
        if entry.media is None:
            if entry.probe_error is not None:
                raise PanelError(PROBE_FAILED_MSG % entry.path.name)
            raise PanelError(NOT_PROBED_MSG % entry.path.name)
        return entry.media
