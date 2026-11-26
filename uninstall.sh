#!/usr/bin/env bash
set -euo pipefail

CUE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
  PYTHONPATH="${CUE_DIR}${PYTHONPATH:+:$PYTHONPATH}" python3 -m engine.cli uninstall "$@"
else
  echo "python3 is required to run uninstall." >&2
  exit 1
fi
