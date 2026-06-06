#!/bin/zsh
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/Users/markmcfarlane/Projects/betfair-results-downloader/.claude/worktrees/openclaw-scheduled-dm-reporting}"
PYTHON_BIN="${PYTHON_BIN:-/Users/markmcfarlane/Projects/betfair-results-downloader/.venv/bin/python}"
REPORT_AT="${REPORT_AT:-}"

cd "$REPO_ROOT"

if [[ -n "$REPORT_AT" ]]; then
  PYTHONPATH=src "$PYTHON_BIN" -m betfair_results_downloader dm-report --at "$REPORT_AT"
else
  PYTHONPATH=src "$PYTHON_BIN" -m betfair_results_downloader dm-report
fi
