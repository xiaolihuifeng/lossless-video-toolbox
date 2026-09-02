"""Merge panel: ordered input list with move-up/down controls (todo 14c).

The panel keeps its own ordered path list (independent of the window's file
list); ``build_spec`` produces one :class:`~lossless_toolbox.ops.merge.MergeSpec`
whose concat preflight runs later, in the queue worker thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lossless_toolbox.ops.merge import MergeSpec

from .base import OperationPanel, PanelError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lossless_toolbox.ui.file_panel import FileEntry

_NEED_TWO_MSG = "请至少添加两个文件"
_MIN_MERGE_FILES = 2
_MEDIA_FILTER = "媒体文件 (*.mp4 *.mkv *.mov *.m4v *.ts *.webm *.avi)"


class MergePanel(OperationPanel):
    """Ordered segment list producing one MergeSpec for the whole batch."""

    operation = "merge"
    needs_files = False

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the segment list and the add/remove/up/down buttons."""
        super().__init__(parent)
        self._paths: list[Path] = []

        self._list = QListWidget()

        self._add_button = QPushButton("添加文件…")
        self._add_button.clicked.connect(self._on_add_clicked)
        self._remove_button = QPushButton("移除")
        self._remove_button.clicked.connect(self._on_remove_clicked)
        self._up_button = QPushButton("上移")
        self._up_button.clicked.connect(self._on_move_up)
        self._down_button = QPushButton("下移")
        self._down_button.clicked.connect(self._on_move_down)

        buttons = QHBoxLayout()
        buttons.addWidget(self._add_button)
        buttons.addWidget(self._remove_button)
        buttons.addWidget(self._up_button)
        buttons.addWidget(self._down_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._list)
        layout.addLayout(buttons)

    def paths(self) -> list[Path]:
        """Return the ordered segment paths."""
        return list(self._paths)

    def add_paths(self, paths: Sequence[str]) -> int:
        """Append existing files not already listed; return the added count."""
        added = 0
        for raw in paths:
            path = Path(raw)
            if not path.is_file() or path in self._paths:
                continue
            self._paths.append(path)
            self._list.addItem(path.name)
            added += 1
        if added:
            self.changed.emit()
        return added

    def validation_error(
        self, entry: FileEntry | None = None  # noqa: ARG002
    ) -> str | None:
        """The merge list is entry-independent; only the segment count gates."""
        return None if len(self._paths) >= _MIN_MERGE_FILES else _NEED_TWO_MSG

    def build_spec(
        self, entry: FileEntry | None, out_path: Path  # noqa: ARG002
    ) -> object:
        """Build the MergeSpec from the ordered list."""
        if len(self._paths) < _MIN_MERGE_FILES:
            raise PanelError(_NEED_TWO_MSG)
        return MergeSpec(paths=list(self._paths), out_path=out_path)

    def _on_add_clicked(self) -> None:
        """Open a file picker and append the chosen segments."""
        files, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", _MEDIA_FILTER)
        if files:
            self.add_paths(files)

    def _on_remove_clicked(self) -> None:
        """Remove the selected row."""
        row = self._list.currentRow()
        if 0 <= row < len(self._paths):
            self._paths.pop(row)
            self._list.takeItem(row)
            self.changed.emit()

    def _on_move_up(self) -> None:
        """Move the selected row one position up."""
        row = self._list.currentRow()
        if row <= 0:
            return
        self._paths.insert(row - 1, self._paths.pop(row))
        self._list.insertItem(row - 1, self._list.takeItem(row))
        self._list.setCurrentRow(row - 1)
        self.changed.emit()

    def _on_move_down(self) -> None:
        """Move the selected row one position down."""
        row = self._list.currentRow()
        if row < 0 or row >= len(self._paths) - 1:
            return
        self._paths.insert(row + 1, self._paths.pop(row))
        self._list.insertItem(row + 1, self._list.takeItem(row))
        self._list.setCurrentRow(row + 1)
        self.changed.emit()
