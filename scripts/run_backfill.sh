#!/bin/bash
# Run the backfill script with the given date range
# Usage: ./scripts/run_backfill.sh FROM_DATE TO_DATE
# Output goes to /tmp/backfill.log

cd "$(dirname "$0")/.."
source .venv/bin/activate

FROM="${1:-2025-12-11}"
TO="${2:-2026-07-12}"
LOG="/tmp/backfill.log"

echo "Starting backfill $FROM -> $TO at $(date)" > "$LOG"
python scripts/backfill_item_descriptions.py --from "$FROM" --to "$TO" >> "$LOG" 2>&1
echo "Exit code: $?" >> "$LOG"
echo "Finished at $(date)" >> "$LOG"
