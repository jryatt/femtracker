#!/bin/bash
# Start FEM Tracker on Linux or macOS, the dev way or the deployed way. See README.md.
#   ./run.sh dev    [args...]   editable source checkout: .venv + pip install -e, then femtracker start
#                               extra args are passed to 'femtracker start', e.g. --no-browser
#   ./run.sh deploy [--no-tests]   standalone build (packaging/build.sh), then run the built binary
#                               --no-tests skips the test suite before building, same as build.sh
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  echo "Usage: $0 dev [args for 'femtracker start']" >&2
  echo "       $0 deploy [--no-tests]" >&2
  exit 1
}

mode="${1:-}"
[ -n "$mode" ] && shift || true
case "$mode" in
  dev|deploy) ;;
  *) usage ;;
esac

PY="${PYTHON:-python3}"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "Python 3.10 or newer is needed. On a Mac: brew install python@3.12, then PYTHON=python3.12 $0 $mode" >&2
  exit 1
fi

if [ "$mode" = "dev" ]; then
  echo "=== Preparing the dev environment (.venv)"
  [ -x .venv/bin/python ] || "$PY" -m venv .venv
  .venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
  .venv/bin/python -m pip install --disable-pip-version-check -q -e ".[test]"
  echo "=== Starting FEM Tracker from source"
  exec .venv/bin/femtracker start "$@"
else
  echo "=== Building the standalone binary (packaging/build.sh)"
  packaging/build.sh "$@"
  echo "=== Starting the built FEM Tracker"
  exec build/dist/FEMTracker/FEMTracker start
fi
