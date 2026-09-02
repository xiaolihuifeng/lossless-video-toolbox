# ruff: noqa: RUF001 - zh-CN UI copy uses fullwidth punctuation deliberately
"""zh-CN UI copy and shared constants for the Qt layer (todo 14 split)."""

from typing import Final

MEDIA_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".mov",
        ".m4v",
        ".ts",
        ".mts",
        ".m2ts",
        ".webm",
        ".avi",
        ".mpg",
        ".mpeg",
        ".m4a",
        ".aac",
        ".mp3",
        ".flac",
        ".wav",
        ".ogg",
    }
)

OP_ITEMS: Final[tuple[tuple[str, str], ...]] = (
    ("转封装", "remux"),
    ("剪切", "cut"),
    ("合并", "merge"),
    ("音轨", "tracks"),
    ("字幕", "subtitles"),
    ("元数据", "meta"),
)

DEFAULT_EXT: Final[dict[str, str]] = {
    "remux": ".mkv",
    "cut": ".mkv",
    "merge": ".mp4",
    "tracks": ".m4a",
    "subtitles": ".mkv",
    "meta": "",
}

CODEC_TYPE_ZH: Final[dict[str, str]] = {
    "video": "视频",
    "audio": "音频",
    "subtitle": "字幕",
    "data": "数据",
    "attachment": "附件",
}

NO_BUILD_ARGV_MSG = (
    "%s 缺少 build_argv() 接口：该操作无法由队列执行，请检查面板生成的 spec"
)
BUILD_ARGV_FAILED_MSG = "%s.build_argv() 调用失败：%s"
UNKNOWN_OP_MSG = "未知操作：%s"
DETACH_NO_SUB_MSG = "输入文件没有可抽取的字幕流：%s"
