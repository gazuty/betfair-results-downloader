#!/bin/bash
# Run backfill month-by-month using the existing CLI command.
# Each month is a fresh process with its own API session.
# Output logged to /tmp/backfill_full.log

set -euo pipefail

cd "$(dirname "$0")/.."
source .venv/bin/activate

LOG="/tmp/backfill_full.log"
echo "=== Full backfill started at $(date) ===" > "$LOG"

# Define monthly chunks (Dec 2025 already done via custom script)
declare -a RANGES=(
    "2025-12-11 2025-12-31"
    "2026-01-01 2026-01-31"
    "2026-02-01 2026-02-28"
    "2026-03-01 2026-03-31"
    "2026-04-01 2026-04-30"
    "2026-05-01 2026-05-31"
    "2026-06-01 2026-06-30"
    "2026-07-01 2026-07-12"
)

TOTAL=${#RANGES[@]}
SUCCESS=0
FAILED=0

for i in "${!RANGES[@]}"; do
    RANGE=(${RANGES[$i]})
    FROM="${RANGE[0]}"
    TO="${RANGE[1]}"
    CHUNK=$((i + 1))

    echo "" >> "$LOG"
    echo "=====================================================================" >> "$LOG"
    echo "CHUNK $CHUNK/$TOTAL: $FROM -> $TO  (started $(date '+%H:%M:%S'))" >> "$LOG"
    echo "=====================================================================" >> "$LOG"

    if betfair-results backfill --from "$FROM" --to "$TO" >> "$LOG" 2>&1; then
        echo "CHUNK $CHUNK RESULT: OK" >> "$LOG"
        SUCCESS=$((SUCCESS + 1))
    else
        echo "CHUNK $CHUNK RESULT: FAILED (exit=$?)" >> "$LOG"
        FAILED=$((FAILED + 1))
    fi

    # Brief pause between chunks
    if [ $CHUNK -lt $TOTAL ]; then
        echo "Pausing 3s before next chunk..." >> "$LOG"
        sleep 3
    fi
done

echo "" >> "$LOG"
echo "=====================================================================" >> "$LOG"
echo "BACKFILL COMPLETE at $(date)" >> "$LOG"
echo "  Success: $SUCCESS / $TOTAL" >> "$LOG"
echo "  Failed:  $FAILED / $TOTAL" >> "$LOG"
echo "=====================================================================" >> "$LOG"
