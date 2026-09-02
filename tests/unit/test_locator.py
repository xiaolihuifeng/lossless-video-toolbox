"""Unit tests for the ffmpeg/ffprobe binary locator."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from lossless_toolbox.ffmpeg_locator import BinaryInfo, ToolchainError, resolve

pytestmark = pytest.mark.unit

_REAL_FFMPEG = Path("/usr/bin/ffmpeg")
_REAL_FFPROBE = Path("/usr/bin/ffprobe")

if not _REAL_FFMPEG.is_file() or not _REAL_FFPROBE.is_file():
    pytest.skip("system ffmpeg/ffprobe not available", allow_module_level=True)


@pytest.fixture(scope="module")
def real_binaries(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Copy the real ffmpeg/ffprobe once so tests never touch /usr/bin directly."""
    root = tmp_path_factory.mktemp("real-binaries")
    copied: dict[str, Path] = {}
    for name, real in (("ffmpeg", _REAL_FFMPEG), ("ffprobe", _REAL_FFPROBE)):
        dest = root / name
        shutil.copy2(real, dest)
        dest.chmod(0o755)
        copied[name] = dest
    return copied


def _install_binary(binary: Path, dest: Path) -> Path:
    """Copy ``binary`` to ``dest`` and make it runnable, returning ``dest``."""
    shutil.copy2(binary, dest)
    dest.chmod(0o755)
    return dest


def test_resolve_from_path_returns_path_and_version(
    real_binaries: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given only a PATH containing a real copy; resolve returns it with a version."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ffmpeg").symlink_to(real_binaries["ffmpeg"])
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("LOSSLESS_TOOLBOX_FFMPEG_PATH", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    info = resolve("ffmpeg")

    assert isinstance(info, BinaryInfo)
    assert info.path.is_file()
    assert info.version.startswith("ffmpeg version ")


def test_bundled_dir_takes_priority_over_env_and_path(
    real_binaries: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given bundled + env + PATH all present; the bundled binary must win."""
    bundled = tmp_path / "resources" / "bin" / "ffmpeg"
    bundled.parent.mkdir(parents=True, exist_ok=True)
    _install_binary(real_binaries["ffmpeg"], bundled)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    # A second valid copy reachable only through the env var.
    env_bin = _install_binary(real_binaries["ffmpeg"], tmp_path / "env-ffmpeg")
    monkeypatch.setenv("LOSSLESS_TOOLBOX_FFMPEG_PATH", str(env_bin))

    info = resolve("ffmpeg")

    assert info.path == bundled
    assert info.version.startswith("ffmpeg version ")


def test_resolve_from_env_var(
    real_binaries: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given only the env var set; resolve returns that path."""
    env_bin = _install_binary(real_binaries["ffmpeg"], tmp_path / "env-ffmpeg")
    monkeypatch.setenv("LOSSLESS_TOOLBOX_FFMPEG_PATH", str(env_bin))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    info = resolve("ffmpeg")

    assert info.path == env_bin
    assert info.version.startswith("ffmpeg version ")


def test_resolve_ffprobe_via_path(
    real_binaries: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a PATH-only ffprobe; resolve probes the ffprobe version string."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ffprobe").symlink_to(real_binaries["ffprobe"])
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("LOSSLESS_TOOLBOX_FFPROBE_PATH", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    info = resolve("ffprobe")

    assert info.version.startswith("ffprobe version ")


def test_bundled_win32_resolves_exe_suffix(
    real_binaries: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given win32 platform and a bundled ffmpeg.exe; resolve finds the .exe suffix."""
    exe = tmp_path / "resources" / "bin" / "ffmpeg.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    _install_binary(real_binaries["ffmpeg"], exe)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    info = resolve("ffmpeg")

    assert info.path == exe
    assert info.version.startswith("ffmpeg version ")


def test_strict_bundled_disables_path_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given strict_bundled and no bundle/env; resolve raises despite PATH ffmpeg."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delenv("LOSSLESS_TOOLBOX_FFMPEG_PATH", raising=False)

    with pytest.raises(ToolchainError):
        resolve("ffmpeg", strict_bundled=True)


def test_resolve_raises_toolchain_error_with_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given no bundle, no env, empty PATH; resolve raises with install guidance."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delenv("LOSSLESS_TOOLBOX_FFMPEG_PATH", raising=False)
    monkeypatch.delenv("LOSSLESS_TOOLBOX_FFPROBE_PATH", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(ToolchainError) as exc:
        resolve("ffmpeg")

    assert exc.value.name == "ffmpeg"
    assert exc.value.searches
    message = str(exc.value)
    assert "LOSSLESS_TOOLBOX_FFMPEG_PATH" in message
    assert "ffmpeg.org" in message


def test_version_probe_failure_raises_toolchain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a broken binary on PATH; resolve raises on version probe failure."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ffmpeg"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("LOSSLESS_TOOLBOX_FFMPEG_PATH", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    with pytest.raises(ToolchainError):
        resolve("ffmpeg")


def test_resolve_rejects_unknown_name() -> None:
    """Given an unsupported name; resolve raises ValueError before any search."""
    with pytest.raises(ValueError, match="unsupported binary"):
        resolve("vlc")
