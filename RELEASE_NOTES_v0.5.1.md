# Release Notes — v0.5.1

**Date:** 2026-04-19

## Summary

Google Sheets publishing has been moved off `main` and preserved on the
`feature/google-sheets` branch as an optional enhancement. `main` now focuses
on the CSV + Azure SQL outputs that are actively used.

This release also adds a minimal GitHub Actions CI pipeline (ruff + pytest on
Python 3.10 and 3.12) and fixes a test that was dragging `tkinter` into
collection unnecessarily.

## Removed

- `sheets_publish.py`, `market_approval.py`, `scripts/setup_sheets.py`
- `publish-sheet` CLI subcommand
- Scheduler runner Phase 5 (Sheets auto-publish)
- `google_sheets` config block from the credentials template and default
  structure in `secrets.py`
- `gspread` and `google-auth` dependencies from `pyproject.toml`

To restore Google Sheets publishing, merge the `feature/google-sheets` branch.

## Changed

- `gui_app.py` no longer defines its own `normalize_log_line` — it imports
  the canonical implementation from `run_logging.py`, removing a silent
  divergence between the two (the `run_logging` version also handles the
  mojibake form `â€¦`).
- `tests/test_run_logging.py` imports from `run_logging` rather than
  `gui_app`, so the test suite no longer requires `tkinter` to collect.

## Added

- `.github/workflows/ci.yml` — runs `ruff check` and `pytest -q` on push
  and PR against `main`, across Python 3.10 and 3.12.

## Upgrade notes

- Existing `credentials.json` files with a `google_sheets` block are
  harmless on `main` — the code that reads them has been removed, so the
  block is simply ignored. You can delete it at your leisure.
- No Betfair, Azure, or scheduler behaviour has changed.
