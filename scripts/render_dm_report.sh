#!/usr/bin/env bash
# Render the OpenClaw daily DM report from the repo this script lives in.
# Overrides: REPO_ROOT, PYTHON_BIN, REPORT_AT (ISO-8601 timestamp).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(dirname "$SCRIPT_DIR")}"
PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
REPORT_AT="${REPORT_AT:-}"

cd "$REPO_ROOT"

if [[ -n "$REPORT_AT" ]]; then
  "$PYTHON_BIN" -m betfair_results_downloader dm-report --at "$REPORT_AT"
else
  "$PYTHON_BIN" -m betfair_results_downloader dm-report
fi
