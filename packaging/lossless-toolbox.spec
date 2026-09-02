# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Lossless Toolbox (onedir, windowed).

Build with::

    pyinstaller packaging/lossless-toolbox.spec

Produced artifact: ``dist/lossless-toolbox/`` (onedir) with the launcher
``dist/lossless-toolbox/lossless-toolbox`` and the bundled ffmpeg/ffprobe at
``<dist>/lossless-toolbox/_internal/resources/bin/{ffmpeg,ffprobe}`` (resolved
by ``ffmpeg_locator`` via ``sys._MEIPASS``).

The ``excludes`` list dodges PyInstaller issue #6447 (PySide6 hooks dragging
in unused Qt modules such as QtNetwork/QtMultimedia/QtWebEngine; this app
uses only QtCore/QtGui/QtWidgets). Analysis entry is
``src/lossless_toolbox/__main__.py`` whose ``main()`` is the console entry.
"""

from pathlib import Path

import sys

# SPECPATH is the directory containing this spec file (packaging/).
ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - injected by PyInstaller

_ENTRY = ROOT / "src" / "lossless_toolbox" / "__main__.py"
_BIN_DIR = ROOT / "resources" / "bin"

# ── exclude unused Qt modules (pyinstaller#6447) ─────────────────────
excludes = [
    "PySide6.QtQml",
    "PySide6.QtWebEngineCore",
    "PySide6.QtQuick",
    "PySide6.QtNetwork",
    # Qt3D* family (six modules)
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
]

# ── add-binary: bundled ffmpeg/ffprobe (win32 uses .exe suffix) ──────
# The bundled binaries are copied to ``resources/bin`` (resolved under
# ``sys._MEIPASS`` at runtime by ``ffmpeg_locator._bundled_bin_dir``).
binaries = []
if sys.platform == "win32":
    for _name in ("ffmpeg", "ffprobe"):
        _src = _BIN_DIR / f"{_name}.exe"
        if _src.is_file():
            binaries.append((str(_src), "resources/bin"))
else:
    for _name in ("ffmpeg", "ffprobe"):
        _src = _BIN_DIR / _name
        if _src.is_file():
            binaries.append((str(_src), "resources/bin"))

# ── add-data: icon and desktop metadata ──────────────────────────────
datas = [
    (str(ROOT / "resources" / "icon.png"), "resources"),
    (str(ROOT / "packaging" / "lossless-toolbox.desktop"), "resources"),
]

# ── hiddenimports: fallback so the GUI/ops chain is always bundled ────
# All imports in the package are static, so PyInstaller's module graph finds
# them on its own; these are an explicit safety net for the spec→argv
# dispatcher (ui/specs.py) and the worker/queue graph.
hiddenimports = [
    "lossless_toolbox",
    "lossless_toolbox.ffmpeg_locator",
    "lossless_toolbox.models",
    "lossless_toolbox.probe",
    "lossless_toolbox.queue",
    "lossless_toolbox.runner",
    "lossless_toolbox.ops",
    "lossless_toolbox.ops.common",
    "lossless_toolbox.ops.cut",
    "lossless_toolbox.ops.merge",
    "lossless_toolbox.ops.remux",
    "lossless_toolbox.ops.subtitles",
    "lossless_toolbox.ops.tracks",
    "lossless_toolbox.ops.meta",
    "lossless_toolbox.ops.meta.argv",
    "lossless_toolbox.ops.meta.errors",
    "lossless_toolbox.ops.meta.ffmetadata",
    "lossless_toolbox.ops.meta.specs",
    "lossless_toolbox.ui",
    "lossless_toolbox.ui.file_panel",
    "lossless_toolbox.ui.info_panel",
    "lossless_toolbox.ui.main_window",
    "lossless_toolbox.ui.progress_panel",
    "lossless_toolbox.ui.run_flow",
    "lossless_toolbox.ui.specs",
    "lossless_toolbox.ui.texts",
    "lossless_toolbox.ui.workers",
    "lossless_toolbox.ui.widgets",
    "lossless_toolbox.ui.widgets.base",
    "lossless_toolbox.ui.widgets.cut",
    "lossless_toolbox.ui.widgets.merge",
    "lossless_toolbox.ui.widgets.meta",
    "lossless_toolbox.ui.widgets.meta_helpers",
    "lossless_toolbox.ui.widgets.remux",
    "lossless_toolbox.ui.widgets.subtitles",
    "lossless_toolbox.ui.widgets.tracks",
]

a = Analysis(
    [str(_ENTRY)],
    pathex=[str(ROOT / "src")],  # equivalent to ``pathex=['src']`` from repo root
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lossless-toolbox",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed (no console on Windows)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="lossless-toolbox",
)
