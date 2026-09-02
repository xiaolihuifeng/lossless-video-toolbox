"""README structure gate: the six required sections and lossless keywords.

Plan todo 19 acceptance: the zh-CN README must carry the six section headings
(安装 / 功能 / 无损边界 / 开发指南 / 打包 / 故障排查), the "不重编码" contract and
at least one "关键帧" explanation. These are string assertions over the rendered
README so a missing or renamed heading fails the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_README_PATH = Path(__file__).resolve().parents[2] / "README.md"

_REQUIRED_SECTIONS: tuple[str, ...] = (
    "## 安装",
    "## 功能",
    "## 无损边界",
    "## 开发指南",
    "## 打包",
    "## 故障排查",
)


def _readme() -> str:
    """Return the README text, failing the test if the file is absent."""
    if not _README_PATH.is_file():
        pytest.fail(f"README.md not found at {_README_PATH}")
    return _README_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("heading", _REQUIRED_SECTIONS)
def test_readme_has_required_section(heading: str) -> None:
    """Each of the six required section headings must be present verbatim."""
    assert heading in _readme()


def test_readme_states_no_reencode_contract() -> None:
    """The "绝不重编码" boundary must be stated verbatim in the README."""
    assert "不重编码" in _readme()


def test_readme_explains_keyframe_snapping() -> None:
    """The GOP / keyframe-snapping explanation must mention 关键帧 at least once."""
    assert "关键帧" in _readme()
