"""ffmetadata chapter text generation and export (ffmpeg §21.32)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from .argv import ffmpeg_path

if TYPE_CHECKING:
    from pathlib import Path

_FFMETADATA_TIMEBASE: Final[str] = "1/1000"  # milliseconds, matching ffmpeg export
_MS_PER_SECOND: Final[int] = 1000


class ChapterArg(BaseModel):
    """A single chapter marker with an optional explicit end time.

    When ``end_time`` is omitted it is derived from the next chapter's start;
    the final chapter must therefore supply one explicitly.
    """

    model_config = ConfigDict(frozen=True)

    start_time: float
    title: str
    end_time: float | None = None


def _escape_ffmetadata(value: str) -> str:
    """Escape ffmetadata-reserved characters in a value (ffmpeg §21.32)."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("=", "\\=")
        .replace("\n", "\\\n")
    )


def build_ffmetadata(chapters: list[ChapterArg]) -> str:
    """Render chapters to ffmetadata text (``;FFMETADATA1`` + ``[CHAPTER]``).

    A chapter's ``END`` is its explicit ``end_time`` or, when omitted, the next
    chapter's ``start_time``; the final chapter must therefore carry one.
    """
    if not chapters:
        message = "chapters must not be empty"
        raise ValueError(message)
    ordered = sorted(chapters, key=lambda chapter: chapter.start_time)
    lines = [";FFMETADATA1"]
    for index, chapter in enumerate(ordered):
        if chapter.end_time is not None:
            end = chapter.end_time
        elif index + 1 < len(ordered):
            end = ordered[index + 1].start_time
        else:
            message = "the last chapter requires an explicit end_time"
            raise ValueError(message)
        if end <= chapter.start_time:
            message = "chapter end_time must be greater than start_time"
            raise ValueError(message)
        lines.extend([
            "[CHAPTER]",
            f"TIMEBASE={_FFMETADATA_TIMEBASE}",
            f"START={round(chapter.start_time * _MS_PER_SECOND)}",
            f"END={round(end * _MS_PER_SECOND)}",
            f"title={_escape_ffmetadata(chapter.title)}",
        ])
    return "\n".join(lines) + "\n"


def to_ffmetadata(path: Path) -> list[str]:
    """Build argv to export ``path``'s chapters as ffmetadata text on stdout."""
    return [
        ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map_chapters",
        "0",
        "-f",
        "ffmetadata",
        "-",
    ]
