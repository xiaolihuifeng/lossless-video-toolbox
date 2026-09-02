"""Locate the bundled ffmpeg/ffprobe binaries and probe their versions.

Resolution order for :func:`resolve`:

1. the bundled ``resources/bin/<name>`` directory (located via ``sys._MEIPASS``
   under PyInstaller; on win32 the ``<name>.exe`` suffix is tried first),
2. the ``LOSSLESS_TOOLBOX_<NAME>_PATH`` environment variable,
3. a ``PATH`` lookup via :func:`shutil.which` (disabled when ``strict_bundled``
   is true).

A located binary is validated by running ``<binary> -version`` and reading the
first line (e.g. ``ffmpeg version 6.1.1-3ubuntu5+esm10 Copyright ...``). Any
failure to locate or probe raises :class:`ToolchainError`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_SUPPORTED_NAMES = frozenset({"ffmpeg", "ffprobe"})
_BUNDLED_RELATIVE = Path("resources") / "bin"
_PROBE_TIMEOUT = 30.0
_VERSION_RE = re.compile(r"\bversion\s+(\S+)")

_INSTALL_GUIDANCE = (
    "Install ffmpeg/ffprobe via your package manager (Debian/Ubuntu: "
    "`apt install ffmpeg`; macOS: `brew install ffmpeg`; Windows: download a "
    "static build from https://www.ffmpeg.org or https://www.gyan.dev), place "
    "them on PATH, or set LOSSLESS_TOOLBOX_FFMPEG_PATH / "
    "LOSSLESS_TOOLBOX_FFPROBE_PATH to their absolute locations."
)


@dataclass(frozen=True, slots=True)
class BinaryInfo:
    """A located external binary: its filesystem path and version string."""

    path: Path
    version: str


class ToolchainError(FileNotFoundError):
    """Raised when an ffmpeg/ffprobe binary cannot be located or probed.

    Carries the requested binary ``name`` and the ordered ``searches`` log so
    callers can surface actionable installation guidance to the user.
    """

    def __init__(
        self,
        name: str,
        searches: Sequence[str] = (),
        *,
        detail: str = "",
    ) -> None:
        """Record the binary name, search log and optional probe detail."""
        self.name = name
        self.searches = tuple(searches)
        self.detail = detail
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        lines = [f"could not locate a runnable '{self.name}' binary."]
        if self.searches:
            lines.append("search log:")
            lines.extend(
                f"  {index}. {entry}"
                for index, entry in enumerate(self.searches, start=1)
            )
        if self.detail:
            lines.append(f"detail: {self.detail}")
        lines.append(_INSTALL_GUIDANCE)
        return "\n".join(lines)


def resolve(name: str, strict_bundled: bool = False) -> BinaryInfo:
    """Resolve the ``name`` binary and return its path plus version string.

    Args:
        name: Either ``"ffmpeg"`` or ``"ffprobe"``.
        strict_bundled: When true, the ``PATH`` fallback is disabled so a
            bundled (or env-var) binary is required.

    Returns:
        The located binary's path and its ``-version`` first line.

    Raises:
        ValueError: If ``name`` is not a supported binary name.
        ToolchainError: If no runnable binary is found or version probing fails.
    """
    if name not in _SUPPORTED_NAMES:
        supported = ", ".join(sorted(_SUPPORTED_NAMES))
        message = f"unsupported binary {name!r}; expected one of: {supported}"
        raise ValueError(message)

    searches: list[str] = []
    path = _resolve_bundled(name, searches)
    if path is None:
        path = _resolve_env(name, searches)
    if path is None and not strict_bundled:
        path = _resolve_path(name, searches)

    if path is None:
        raise ToolchainError(name, searches)

    return _probe(path, name, searches)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _env_var_name(name: str) -> str:
    return f"LOSSLESS_TOOLBOX_{name.upper()}_PATH"


def _bundled_bin_dir() -> Path | None:
    meipass: str | None = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    return Path(meipass) / _BUNDLED_RELATIVE


def _bundled_candidates(name: str) -> tuple[str, ...]:
    if _is_windows():
        return (f"{name}.exe", name)
    return (name,)


def _is_runnable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _resolve_bundled(name: str, searches: list[str]) -> Path | None:
    bin_dir = _bundled_bin_dir()
    if bin_dir is None:
        searches.append("bundled: sys._MEIPASS not set (not a frozen bundle)")
        return None
    for candidate in _bundled_candidates(name):
        path = bin_dir / candidate
        if _is_runnable(path):
            return path
    searches.append(f"bundled: {bin_dir} not found")
    return None


def _resolve_env(name: str, searches: list[str]) -> Path | None:
    var = _env_var_name(name)
    value = os.environ.get(var)
    if not value:
        searches.append(f"env {var}: not set")
        return None
    path = Path(value).expanduser()
    if _is_runnable(path):
        return path
    searches.append(f"env {var}={value}: not a runnable file")
    return None


def _resolve_path(name: str, searches: list[str]) -> Path | None:
    found = shutil.which(name)
    if found is None:
        searches.append(f"PATH: {name!r} not found")
        return None
    return Path(found)


def _probe(path: Path, name: str, searches: Sequence[str]) -> BinaryInfo:
    try:
        result = subprocess.run(  # noqa: S603 - static args, list argv (no shell)
            [str(path), "-version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolchainError(
            name,
            searches,
            detail=f"`{path} -version` timed out after {_PROBE_TIMEOUT:g}s",
        ) from exc
    except OSError as exc:
        raise ToolchainError(
            name, searches, detail=f"could not execute `{path}`: {exc}"
        ) from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise ToolchainError(
            name,
            searches,
            detail=f"`{path} -version` exited with {result.returncode}: {stderr}",
        )

    first_line = result.stdout.splitlines()[0].strip() if result.stdout else ""
    if _VERSION_RE.search(first_line) is None:
        raise ToolchainError(
            name,
            searches,
            detail=f"`{path} -version` output unparseable: {first_line!r}",
        )

    return BinaryInfo(path=path, version=first_line)
