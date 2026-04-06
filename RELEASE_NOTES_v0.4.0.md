v0.4.0 — Lookback v2, GUI Workflow Cleanup, and Run Logs

Highlights
- Lookback v2 recommends days based on missing settled-date gaps, run_state watermark, or CSV heuristics.
- GUI workflow clarified with step-by-step guidance and buttons placed by task area.
- Run logs are saved per run for reproducible debugging.
- Manual override is explicit via a checkbox, defaulting to auto lookback.

Detailed Changes
GUI/UX
- Workflow instruction appears at the top: "1) Choose Paths 2) Validate 3) Compute Lookback 4) Run Downloader 5) (Optional) Publish to Azure".
- Validate lives under Betfair credentials, Compute Lookback near lookback config, Run Downloader above Azure, Publish inside Azure section.
- Output area only contains output utilities (Clear/Copy/Open folders).
- Effective lookback is displayed read-only and persists until recomputed.
- Manual override checkbox enables manual Days input for that run.

Lookback
- Decision order:
  - Missing settled-date gaps within the audit window (<= 90 days) -> recommend from the earliest missing date in the most recent missing range.
  - Else use run_state.json last_success_utc.
  - Else fall back to canonical CSV latest settledDate heuristic.
  - Else first run (no CSV/state) defaults to 90 days (Betfair max).
- Audit computes missing settled dates only between observed dates inside the backfillable window.

Logging
- Run output is persisted to `results_csv_dir/run_logs/run_YYYYMMDD_HHMMSS.txt`.
- Log lines are ASCII-safe and UTF-8 encoded to avoid mojibake.

Azure
- Publish-only flow still reads from the canonical CSV only.
- Safety gates unchanged (enable_azure_sql + dry_run false + GUI unlock + typed confirmation + final confirm).

Upgrade Notes
- New run_state files are written under `results_csv_dir` when runs complete successfully.
- Lookback recommendations may change if missing gaps are detected within the last 90 days.
- If you want to force a specific Days value for a run, enable Manual override first.

Safety Guarantees (Azure)
- Safe-by-default: dry run blocks writes unless explicitly disabled.
- User-scoped operations with explicit unlock and confirmation steps.
- Incremental sync only; no destructive writes by default.

Known Limitations
- Betfair API restricts lookback to 90 days; gaps older than 90 days cannot be backfilled.
- Missing-date audit operates only within the backfillable window and between observed dates.

Quality: pytest + ruff clean.
