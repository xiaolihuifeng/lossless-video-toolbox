# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""Table-parsing helpers for the meta panel (todo 14 split).

Pure readers of the chapter and language tables, kept outside
:class:`~lossless_toolbox.ui.widgets.meta.MetaPanel` so the panel module
stays under its logic-line budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt

from lossless_toolbox.ops.meta.ffmetadata import ChapterArg

if TYPE_CHECKING:
    from PySide6.QtWidgets import QTableWidget

_NEED_CHAPTER_MSG = "请添加至少一个章节"
_BAD_CHAPTER_PARSE_MSG = "章节时间须为数字"
_BAD_CHAPTER_MSG = "章节第 %d 行的时间无效（结束须大于开始）"


def cell_text(table: QTableWidget, row: int, column: int) -> str:
    """Return a table cell's text, or an empty string for a missing item."""
    item = table.item(row, column)
    return "" if item is None else item.text()


def parse_chapters(table: QTableWidget) -> list[ChapterArg]:
    """Parse the chapter rows into ChapterArg values."""
    chapters: list[ChapterArg] = []
    for row in range(table.rowCount()):
        start_text = cell_text(table, row, 0).strip()
        end_text = cell_text(table, row, 1).strip()
        title = cell_text(table, row, 2).strip()
        chapters.append(
            ChapterArg(
                start_time=float(start_text),
                title=title,
                end_time=float(end_text) if end_text else None,
            )
        )
    return chapters


def chapters_error(table: QTableWidget) -> str | None:
    """Validate the chapter table; return the zh-CN reason when invalid."""
    if table.rowCount() == 0:
        return _NEED_CHAPTER_MSG
    try:
        chapters = parse_chapters(table)
    except ValueError:
        return _BAD_CHAPTER_PARSE_MSG
    for row, chapter in enumerate(chapters, start=1):
        if chapter.end_time is not None and chapter.end_time <= chapter.start_time:
            return _BAD_CHAPTER_MSG % row
    return None


def language_map(table: QTableWidget) -> dict[int, str]:
    """Collect the edited per-stream language entries."""
    result: dict[int, str] = {}
    for row in range(table.rowCount()):
        name_item = table.item(row, 0)
        value_item = table.item(row, 1)
        if name_item is None or value_item is None:
            continue
        value = value_item.text().strip()
        if not value:
            continue
        index = name_item.data(Qt.ItemDataRole.UserRole)
        if isinstance(index, int):
            result[index] = value
    return result
