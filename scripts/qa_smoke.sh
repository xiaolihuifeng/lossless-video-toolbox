#!/usr/bin/env bash
# Full test-suite smoke gate for the lossless-toolbox (todo 16).
#
# Runs the whole unit + integration + gui suite against the project venv and
# propagates pytest's exit code unchanged. The offscreen Qt platform is forced
# so the GUI tests run headless.
set -euo pipefail

cd "$(dirname "$0")/.."

QT_QPA_PLATFORM=offscreen .venv/bin/pytest -m "unit or integration or gui" -q
