#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SRC="$(cd "$SCRIPT_DIR/../UnityMcpBridge/UnityMcpServer~/src" && pwd)"

PROJECT_PATH="${1:-$(pwd)}"
LOG_DIR="${2:-$PROJECT_PATH/logs}"

# Ensure required Python dependencies are available.
if ! python -c "import mcp" >/dev/null 2>&1; then
  echo "[try_pipeline] Missing required Python dependency 'mcp'. Please install UnityMcpBridge/UnityMcpServer~/src first." >&2
  exit 1
fi

PYTHONPATH="$SERVER_SRC${PYTHONPATH:+:$PYTHONPATH}" python -m tools.pipeline_runner --project "$PROJECT_PATH" --log-dir "$LOG_DIR"
