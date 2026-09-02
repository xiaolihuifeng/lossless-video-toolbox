# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""Meta panel: metadata / chapters / rotation / cover (todo 14f).

One mode combo picks which of the four todo-10 specs the panel produces —
each ops model stays 1:1 (no invented combined spec). Rotation is enabled
only for MP4/MOV sources (Matroska has no standard rotation element) and
the disabled state explains why. Cover/rotate pre-checks keep construction
errors out of the run path; everything here is pure widget-state reading.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lossless_toolbox.ops.meta import (
    ChaptersSpec,
    CoverSpec,
    MetadataEditSpec,
    RotateSpec,
)
from lossless_toolbox.ui.texts import CODEC_TYPE_ZH

from .base import OperationPanel, PanelError
from .meta_helpers import chapters_error, language_map, parse_chapters

if TYPE_CHECKING:
    from lossless_toolbox.ui.file_panel import FileEntry

_META_PAGE = 0
_CHAPTERS_PAGE = 1
_ROTATE_PAGE = 2
_COVER_PAGE = 3
_MP4_LIKE_SUFFIXES: frozenset[str] = frozenset({".mp4", ".mov"})
_COVER_CONTAINERS: frozenset[str] = frozenset({".mp4", ".mov", ".mkv"})
_COVER_IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png"})
_COVER_FILTER = "图片 (*.jpg *.jpeg *.png)"
_ROTATE_UNSUPPORTED_MSG = "仅 MP4/MOV 输出支持旋转（Matroska 无标准旋转元素）"
_NEED_COVER_MSG = "请选择封面图片"
_BAD_COVER_MSG = "封面仅支持 jpg/png 且输出须为 mp4/mov/mkv"


class MetaPanel(OperationPanel):
    """Four sub-forms sharing one mode combo: metadata, chapters, rotate, cover."""

    operation = "meta"

    def __init__(self, parent: QWidget | None = None) -> None:  # noqa: PLR0915
        """Build the mode combo and the four stacked sub-forms."""
        super().__init__(parent)
        self._entry: FileEntry | None = None

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("元数据", "metadata")
        self._mode_combo.addItem("章节", "chapters")
        self._mode_combo.addItem("旋转", "rotate")
        self._mode_combo.addItem("封面", "cover")

        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText("留空则不修改标题")
        self._lang_table = QTableWidget(0, 2)
        self._lang_table.setHorizontalHeaderLabels(["流", "语言（留空不修改）"])
        self._lang_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        meta_page = QWidget()
        meta_layout = QVBoxLayout(meta_page)
        meta_form = QFormLayout()
        meta_form.addRow("标题", self._title_edit)
        meta_layout.addLayout(meta_form)
        meta_layout.addWidget(self._lang_table)

        self._chapter_table = QTableWidget(0, 3)
        self._chapter_table.setHorizontalHeaderLabels(
            ["开始（秒）", "结束（秒）", "标题"]
        )
        self._chapter_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._add_chapter_button = QPushButton("添加章节")
        self._remove_chapter_button = QPushButton("删除所选")
        chapter_buttons = QHBoxLayout()
        chapter_buttons.addWidget(self._add_chapter_button)
        chapter_buttons.addWidget(self._remove_chapter_button)
        chapter_buttons.addStretch(1)
        chapters_page = QWidget()
        chapters_layout = QVBoxLayout(chapters_page)
        chapters_layout.addWidget(self._chapter_table)
        chapters_layout.addLayout(chapter_buttons)

        self._rotate_combo = QComboBox()
        for degrees in ("0", "90", "180", "270"):
            self._rotate_combo.addItem(f"{degrees}°", int(degrees))
        self._rotate_note = QLabel("")
        self._rotate_note.setWordWrap(True)
        rotate_page = QWidget()
        rotate_layout = QVBoxLayout(rotate_page)
        rotate_form = QFormLayout()
        rotate_form.addRow("旋转度数", self._rotate_combo)
        rotate_layout.addLayout(rotate_form)
        rotate_layout.addWidget(self._rotate_note)

        self._cover_edit = QLineEdit()
        self._cover_edit.setPlaceholderText("选择 jpg/png 封面图片")
        cover_browse = QPushButton("浏览…")
        cover_browse.clicked.connect(self._on_cover_browse)
        cover_row = QHBoxLayout()
        cover_row.addWidget(self._cover_edit)
        cover_row.addWidget(cover_browse)
        cover_page = QWidget()
        cover_layout = QVBoxLayout(cover_page)
        cover_layout.addLayout(cover_row)

        self._stack = QStackedWidget()
        for page in (meta_page, chapters_page, rotate_page, cover_page):
            self._stack.addWidget(page)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("模式", self._mode_combo)
        layout.addLayout(form)
        layout.addWidget(self._stack)

        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._title_edit.textChanged.connect(self._on_edit)
        self._lang_table.itemChanged.connect(self._on_item_changed)
        self._add_chapter_button.clicked.connect(self._on_add_chapter)
        self._remove_chapter_button.clicked.connect(self._on_remove_chapter)
        self._chapter_table.itemChanged.connect(self._on_item_changed)
        self._rotate_combo.currentIndexChanged.connect(self._on_edit)
        self._cover_edit.textChanged.connect(self._on_edit)

    def set_context(self, entry: FileEntry | None) -> None:
        """Rebuild the language table and update the rotation enablement."""
        self._entry = entry
        media = entry.media if entry is not None else None
        self._lang_table.setRowCount(0)
        if media is not None:
            for stream in media.streams:
                self._add_language_row(
                    stream.index,
                    stream.codec_type,
                    stream.codec_name,
                    stream.language,
                )
        self._refresh_rotation()

    def validation_error(self, entry: FileEntry | None = None) -> str | None:
        """Block on unprobed media or an invalid active sub-form."""
        reason = super().validation_error(entry)
        if reason is not None:
            return reason
        mode = str(self._mode_combo.currentData())
        if mode == "chapters":
            return chapters_error(self._chapter_table)
        if mode == "rotate" and not self._rotation_allowed():
            return _ROTATE_UNSUPPORTED_MSG
        if mode == "cover" and self._cover_error(entry) is not None:
            return self._cover_error(entry)
        return None

    def build_spec(self, entry: FileEntry | None, out_path: Path) -> object:
        """Build the spec for the active mode."""
        media = self._require_media(entry)
        mode = str(self._mode_combo.currentData())
        if mode == "metadata":
            return MetadataEditSpec(
                in_path=media.path,
                out_path=out_path,
                title=self._title_edit.text().strip() or None,
                language_map=language_map(self._lang_table) or None,
                duration=media.duration,
            )
        if mode == "chapters":
            error = chapters_error(self._chapter_table)
            if error is not None:
                raise PanelError(error)
            return ChaptersSpec(
                in_path=media.path,
                out_path=out_path,
                chapters=parse_chapters(self._chapter_table),
                duration=media.duration,
            )
        if mode == "rotate":
            if not self._rotation_allowed():
                raise PanelError(_ROTATE_UNSUPPORTED_MSG)
            return RotateSpec(
                in_path=media.path,
                out_path=out_path,
                degrees=int(self._rotate_combo.currentData()),
                duration=media.duration,
            )
        error = self._cover_error(entry)
        if error is not None:
            raise PanelError(error)
        return CoverSpec(
            in_path=media.path,
            out_path=out_path,
            image_path=Path(self._cover_edit.text().strip()),
            duration=media.duration,
        )

    def _add_language_row(
        self, index: int, codec_type: str, codec_name: str, language: str | None
    ) -> None:
        """Append one editable language row for a stream."""
        row = self._lang_table.rowCount()
        self._lang_table.insertRow(row)
        label = f"{index} {CODEC_TYPE_ZH.get(codec_type, codec_type)}（{codec_name}）"
        name_item = QTableWidgetItem(label)
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        name_item.setData(Qt.ItemDataRole.UserRole, index)
        self._lang_table.setItem(row, 0, name_item)
        self._lang_table.setItem(row, 1, QTableWidgetItem(language or ""))

    def _rotation_allowed(self) -> bool:
        """Rotation needs an MP4/MOV target (source suffix preserved)."""
        entry = self._entry
        return entry is not None and entry.path.suffix.lower() in _MP4_LIKE_SUFFIXES

    def _refresh_rotation(self) -> None:
        """Update the rotation combo enablement and the explanatory note."""
        allowed = self._rotation_allowed()
        self._rotate_combo.setEnabled(allowed)
        self._rotate_note.setText("" if allowed else _ROTATE_UNSUPPORTED_MSG)

    def _cover_error(self, entry: FileEntry | None) -> str | None:
        """Validate the cover sub-form against the ops CoverSpec rules."""
        text = self._cover_edit.text().strip()
        if not text:
            return _NEED_COVER_MSG
        image = Path(text)
        if (
            image.suffix.lower() not in _COVER_IMAGE_EXTS
            or entry is None
            or entry.path.suffix.lower() not in _COVER_CONTAINERS
        ):
            return _BAD_COVER_MSG
        return None

    def _on_mode_changed(self, _index: int) -> None:
        """Switch the stacked page and notify the window."""
        self._stack.setCurrentIndex(self._mode_combo.currentIndex())
        self.changed.emit()

    def _on_edit(self, *_args: object) -> None:
        """Notify the window on any single-widget edit."""
        self.changed.emit()

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        """Notify the window on any table cell edit."""
        self.changed.emit()

    def _on_add_chapter(self) -> None:
        """Append an empty chapter row."""
        row = self._chapter_table.rowCount()
        self._chapter_table.insertRow(row)
        self._chapter_table.setItem(row, 0, QTableWidgetItem(""))
        self._chapter_table.setItem(row, 1, QTableWidgetItem(""))
        self._chapter_table.setItem(row, 2, QTableWidgetItem(""))
        self.changed.emit()

    def _on_remove_chapter(self) -> None:
        """Remove the selected chapter row."""
        row = self._chapter_table.currentRow()
        if row >= 0:
            self._chapter_table.removeRow(row)
            self.changed.emit()

    def _on_cover_browse(self) -> None:
        """Open a cover image picker."""
        chosen, _ = QFileDialog.getOpenFileName(self, "选择封面图片", "", _COVER_FILTER)
        if chosen:
            self._cover_edit.setText(chosen)
