"""Unit tests for the lossless_toolbox package scaffold."""

import pytest

import lossless_toolbox

pytestmark = pytest.mark.unit


def test_import_exposes_version() -> None:
    """Given the package; when imported; then __version__ is a non-empty string."""
    assert lossless_toolbox.__version__
