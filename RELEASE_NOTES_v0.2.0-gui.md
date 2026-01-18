# v0.2.0-gui - GUI-first downloader with smart lookback and publish-only Azure

This release delivers a GUI-first workflow for Windows users, with smarter defaults and a new publish-only Azure option sourced from local canonical CSV data.
It targets tag v0.2.0-gui on main.

## Highlights
- Smart lookback recommendation based on the latest settledDate in the canonical CSV (UTC gap + 1 overlap, capped at 90).
- Publish-only Azure button that publishes from the canonical CSV without downloading.
- Cleared orders now capture Betfair itemDescription metadata with smoke checks for validation.

## Safety
- Dry Run blocks Azure writes by default.
- Explicit unlock plus a final confirmation dialog are required for non-dry-run publishing.

## How to run
```
python -m betfair_results_downloader.gui_app
```

## What changed (summary)
- GUI-first runner remains the recommended workflow.
- itemDescription fields are preserved in canonical and snapshot CSV outputs.
- Publish-only flow inserts missing markets only and reports when Azure is up to date.
- Publish-only summaries distinguish publish_requested vs publish_attempted.
- Precision-safe marketId comparisons reduce Azure mismatch risks.
