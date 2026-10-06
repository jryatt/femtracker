#!/bin/bash
# Build and test FEM Tracker on macOS or Linux, for trying it out before the Windows build.
#   packaging/build.sh              run the tests, build ./FEMTracker, smoke-test it, zip it
#   packaging/build.sh --no-tests   skip the test suite
# Output: build/dist/FEMTracker/FEMTracker and dist/FEMTracker-<version>-<os>-<arch>-portable.zip
# The Windows installer can only be built on Windows (packaging\windows\build.bat or GitHub Actions).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "Python 3.10 or newer is needed. On a Mac: brew install python@3.12, then PYTHON=python3.12 $0" >&2
  exit 1
fi

echo "=== Preparing the build environment (build/venv)"
[ -x build/venv/bin/python ] || "$PY" -m venv build/venv
build/venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
build/venv/bin/python -m pip install --disable-pip-version-check -q -r packaging/windows/requirements-build.txt pytest

if [ "${1:-}" != "--no-tests" ]; then
  echo "=== Running the tests (real server, fake HPC)"
  build/venv/bin/python -m pytest tests -q
fi

build/venv/bin/python packaging/windows/build.py --no-installer
echo
echo "Run it:  build/dist/FEMTracker/FEMTracker start"
