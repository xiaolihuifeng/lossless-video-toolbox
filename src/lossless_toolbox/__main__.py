# ruff: noqa: RUF001 - zh-CN CLI copy uses fullwidth punctuation deliberately
"""Entry point: GUI launch plus ffmpeg/ffprobe self-check subcommands.

``python -m lossless_toolbox`` opens the main window. The two self-check
flags serve packaging QA (todo 17): ``--probe-self`` prints the resolved
ffmpeg/ffprobe paths and versions, and ``--strict-bundled`` demands the
bundled binaries (no PATH fallback) — in a dev checkout (no ``sys._MEIPASS``)
that reports an error and exits nonzero. The structured logging bootstrap
lives here too; log output stays English regardless of the zh-CN UI.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING

from lossless_toolbox.ffmpeg_locator import ToolchainError, resolve

if TYPE_CHECKING:
    from collections.abc import Sequence

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(*, level: int = logging.INFO) -> None:
    """Bootstrap the root logger with a structured line format."""
    logging.basicConfig(level=level, format=_LOG_FORMAT)


def build_parser() -> argparse.ArgumentParser:
    """Build the lossless-toolbox CLI parser."""
    parser = argparse.ArgumentParser(
        prog="lossless-toolbox",
        description="无损视频工具箱：不重编码的视频封装/剪切/合并工具",
    )
    parser.add_argument(
        "--probe-self",
        action="store_true",
        help="定位并打印 ffmpeg/ffprobe 路径与版本后退出（打包 QA 用）",
    )
    parser.add_argument(
        "--strict-bundled",
        action="store_true",
        help="仅接受捆绑的 ffmpeg/ffprobe（禁用 PATH 回退）",
    )
    return parser


def _print_probe_self(strict_bundled: bool) -> int:
    """Print both binary resolutions; return 0 on success, 1 otherwise."""
    failures: list[str] = []
    for name in ("ffmpeg", "ffprobe"):
        try:
            info = resolve(name, strict_bundled=strict_bundled)
        except (ToolchainError, ValueError) as exc:
            failures.append(f"{name}: {exc}")
            continue
        print(f"{name}: {info.path}")  # noqa: T201 - CLI self-check output
        print(f"{name} version: {info.version}")  # noqa: T201
    for failure in failures:
        print(f"ERROR: {failure}", file=sys.stderr)  # noqa: T201
    return 1 if failures else 0


def _run_gui() -> int:
    """Create the QApplication and run the main window event loop."""
    # PLC0415: keep the CLI self-checks import-light (no Qt loaded).
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from lossless_toolbox.ui.main_window import MainWindow  # noqa: PLC0415

    app = QApplication(sys.argv[:1])
    window = MainWindow()
    window.show()
    return app.exec()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI: self-checks when requested, otherwise launch the GUI."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    configure_logging()
    if args.probe_self or args.strict_bundled:
        return _print_probe_self(strict_bundled=args.strict_bundled)
    return _run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
