"""Structure gate for the Windows build CI workflow (todo 18).

Plan todo 18 acceptance: ``.github/workflows/build-windows.yml`` must run on a
``windows-latest`` runner with Python 3.12, install ``.[dev]``, download the
SHA-256-pinned gyan.dev essentials ffmpeg/ffprobe into ``resources/bin`` (with
a real checksum verification command), drive the shared PyInstaller spec, and
upload a ``lossless-toolbox-windows-x64`` artifact plus an NSIS installer. The
unsigned-build/SmartScreen caveat must be documented in the workflow. These are
string/structure assertions over the rendered YAML so a missing or renamed
step fails the gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "build-windows.yml"
)
_SPEC_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "packaging" / "lossless-toolbox.spec"
)


def _workflow_text() -> str:
    """Return the workflow source, failing the test if the file is absent."""
    if not _WORKFLOW_PATH.is_file():
        pytest.fail(f"workflow not found at {_WORKFLOW_PATH}")
    return _WORKFLOW_PATH.read_text(encoding="utf-8")


def _workflow() -> dict[str, Any]:
    """Parse the workflow YAML with yaml.safe_load and return the root mapping."""
    data = yaml.safe_load(_workflow_text())
    if not isinstance(data, dict):
        pytest.fail("workflow YAML root is not a mapping")
    return data


def _build_job() -> dict[str, Any]:
    """Return the ``build-windows`` job mapping from the workflow."""
    jobs = _workflow()["jobs"]
    if not isinstance(jobs, dict) or "build-windows" not in jobs:
        pytest.fail("workflow must define a 'jobs.build-windows' job")
    job = jobs["build-windows"]
    if not isinstance(job, dict):
        pytest.fail("'jobs.build-windows' is not a mapping")
    return job


def _steps() -> list[dict[str, Any]]:
    """Return the step list of the ``build-windows`` job."""
    steps = _build_job().get("steps")
    if not isinstance(steps, list) or not steps:
        pytest.fail("job must define a non-empty steps list")
    return steps


def _step(*needles: str) -> dict[str, Any]:
    """Return the first step whose name contains every needle (case-insensitive)."""
    for step in _steps():
        name = step.get("name")
        if isinstance(name, str) and all(
            n.casefold() in name.casefold() for n in needles
        ):
            return step
    pytest.fail(f"no step named like {needles!r}")


def _upload_steps() -> list[dict[str, Any]]:
    """Return all steps that call the actions/upload-artifact action."""
    uploads = [
        s
        for s in _steps()
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    if not uploads:
        pytest.fail("no actions/upload-artifact step found")
    return uploads


def test_workflow_triggers_are_dispatch_and_tag_push() -> None:
    """``on`` must contain workflow_dispatch and a v* tag push."""
    triggers = _workflow()["on"]
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    assert "push" in triggers
    push = triggers["push"]
    assert isinstance(push, dict)
    assert any(str(tag) == "v*" for tag in push["tags"])


def test_job_runs_on_windows_latest() -> None:
    """The Windows build job must target a windows-latest hosted runner."""
    assert _build_job()["runs-on"] == "windows-latest"


def test_python_312_is_set_up() -> None:
    """setup-python must pin Python 3.12."""
    step = _step("python")
    assert str(step.get("uses", "")).startswith("actions/setup-python@")
    assert step["with"]["python-version"] == "3.12"


def test_dev_extras_are_installed() -> None:
    """The job installs the project with the dev extras (PyInstaller included)."""
    install = [s for s in _steps() if "pip install" in str(s.get("run", "")).lower()]
    assert any('-e ".[dev]"' in str(s.get("run", "")) for s in install)


def test_pyinstaller_step_builds_the_shared_spec() -> None:
    """PyInstaller must run against packaging/lossless-toolbox.spec (no copy)."""
    step = _step("build", "pyinstaller")
    run = str(step.get("run", ""))
    assert "pyinstaller" in run.casefold()
    assert "packaging/lossless-toolbox.spec" in run


def test_spec_is_confirmed_before_pyinstaller() -> None:
    """A preflight step must confirm the spec before the PyInstaller step runs."""
    names = [str(s.get("name", "")) for s in _steps()]
    preflight = next(
        (i for i, n in enumerate(names) if "spec is present" in n.casefold()),
        None,
    )
    build = next(
        (
            i
            for i, n in enumerate(names)
            if "pyinstaller" in n.casefold() and "build" in n.casefold()
        ),
        None,
    )
    assert preflight is not None
    assert build is not None
    assert preflight < build


def test_ffmpeg_download_step_pins_and_verifies_sha256() -> None:
    """The ffmpeg download must pin a digest and verify it with Get-FileHash."""
    step = _step("ffmpeg")
    run = str(step.get("run", ""))
    assert "Get-FileHash" in run
    assert "-Algorithm SHA256" in run
    env = _build_job().get("env", {})
    digest = env["FFMPEG_SHA256"]
    assert isinstance(digest, str)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    version = env["FFMPEG_VERSION"]
    assert isinstance(version, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert "releases/download/$version/$archive" in run
    assert run.count("essentials_build.zip") >= 1


def test_ffmpeg_binaries_land_in_resources_bin_as_exe() -> None:
    """ffmpeg.exe/ffprobe.exe must be copied into resources/bin."""
    step = _step("ffmpeg")
    run = str(step.get("run", ""))
    assert "'resources/bin'" in run
    assert "ffmpeg.exe" in run
    assert "ffprobe.exe" in run
    assert "Copy-Item" in run


def test_portable_zip_artifact_is_uploaded() -> None:
    """An upload-artifact step must publish the lossless-toolbox-windows-x64 zip."""
    uploads = _upload_steps()
    assert any(
        u["with"]["name"] == "lossless-toolbox-windows-x64" for u in uploads
    )
    assert any(
        "lossless-toolbox-windows-x64.zip" in str(u["with"].get("path", ""))
        for u in uploads
    )


def test_nsis_installer_is_built_via_chocolatey_makensis() -> None:
    """The NSIS compiler (makensis) is installed via choco and an installer is built.

    The choco package name is ``nsis`` (``makensis`` is only the installed
    compiler executable), so the workflow must install ``nsis`` and then invoke
    ``makensis.exe`` to compile the installer.
    """
    install = [s for s in _steps() if "choco install" in str(s.get("run", ""))]
    assert any("choco install nsis" in str(s.get("run", "")) for s in install)
    build = [s for s in _steps() if "makensisPath" in str(s.get("run", ""))]
    assert len(build) >= 1
    run = str(build[0].get("run", ""))
    assert "makensis" in run
    assert "lossless-toolbox-setup-0.1.0.exe" in run


def test_nsi_script_is_referenced_as_a_real_file_not_embedded_base64() -> None:
    """The NSI installer script must be a repo file, not base64 embedded inline.

    Earlier revisions embedded the NSI content as wrapped base64 whose multiline
    ``+ '...'`` continuations violated PowerShell line-continuation rules and
    crashed with an Int32 conversion error. The build step must reference the
    checked-in ``packaging/lossless-toolbox.nsi`` instead.
    """
    nsi_path = (
        Path(__file__).resolve().parents[2] / "packaging" / "lossless-toolbox.nsi"
    )
    assert nsi_path.is_file(), "packaging/lossless-toolbox.nsi must exist"
    build = [s for s in _steps() if "makensisPath" in str(s.get("run", ""))]
    run = str(build[0].get("run", ""))
    assert "packaging/lossless-toolbox.nsi" in run
    assert "FromBase64String" not in run
    assert "nsiB64" not in run
    nsi = nsi_path.read_text(encoding="utf-8")
    for required in (
        "!define APP_NAME",
        "!define APP_VERSION",
        'File /r "${SRCDIR}\\*"',
        "WriteUninstaller",
        'Section "Uninstall"',
    ):
        assert required in nsi, f"NSI missing required directive: {required}"


def test_unsigned_smartscreen_notice_is_documented() -> None:
    """The workflow must spell out that the unsigned build trips SmartScreen."""
    text = _workflow_text()
    assert "SmartScreen" in text
    assert "code-sign" in text.casefold() or "unsigned" in text.casefold()


def test_spec_win32_branch_supports_exe_and_windowed_when_present() -> None:
    """The shared spec branches to .exe add-binary and console=False on win32.

    ``packaging/lossless-toolbox.spec`` is owned by todo 17 (same wave); this
    gate only runs when the file is already on disk.
    """
    if not _SPEC_PATH.is_file():
        pytest.skip(
            "packaging/lossless-toolbox.spec not present yet (todo 17 in flight)"
        )
    text = _SPEC_PATH.read_text(encoding="utf-8")
    assert 'sys.platform == "win32"' in text
    assert ".exe" in text
    assert "console=False" in text
