## [0.5.1] - 2026-04-19

### Removed

- **Google Sheets publishing** moved to the `feature/google-sheets` branch as an optional enhancement. `sheets_publish.py`, `market_approval.py`, `scripts/setup_sheets.py`, the `publish-sheet` CLI subcommand, the scheduler's Phase 5 auto-publish, the `google_sheets` config block, and the `gspread` / `google-auth` dependencies are all removed from `main`. Restore by merging `feature/google-sheets`.
- Duplicate `normalize_log_line` definition in `gui_app.py` (now imports from `run_logging.py`, which was the more complete implementation).

### Added

- **GitHub Actions CI** (`.github/workflows/ci.yml`) — runs `ruff check` and `pytest -q` on every push and PR to `main`, across Python 3.10 and 3.12.
- **`RELEASE_NOTES_v0.5.1.md`** — per the release process documented in `CONTRIBUTING.md`.

### Fixed

- `tests/test_run_logging.py` imports `normalize_log_line` from `run_logging` (matching its own name) rather than from `gui_app`, so the test suite no longer pulls in `tkinter` at collection time — required for headless CI.

---

## [0.5.0] - 2026-04-06

Scheduled automatic downloads with gap detection, multi-window retry, cross-platform installers, and Google Sheets publishing.

### Added

- **Cert-based non-interactive Betfair authentication** (`scheduler/auth.py`). Enables fully headless `client.login()` using a client certificate pair stored in a user-configured directory, via the new `betfair.certs_dir` credentials field.
- **Chunked date-range download** (`downloader_core.fetch_cleared_orders_df_range`). Accepts explicit `date`/`datetime` endpoints, splits into safe Betfair `settledDateRange` windows (default 30 days per chunk), and supports reusing a pre-authenticated `APIClient` to eliminate double-login during enrichment.
- **Package CLI entry point** — `python -m betfair_results_downloader` (and the `betfair-results` console script after `pip install -e .`). Subcommands: `auth-test`, `run`, `backfill`, `schedule`.
- **`ScheduleConfig` dataclass** (`config.py`) — frozen dataclass with all scheduled-download settings.
- **`parse_schedule_config(creds)`** (`config.py`) — parses the `schedule` block from a credentials dict with all defaults applied.
- **Schedule validation in `validate_credentials`** (`secrets.py`) — validates certs, timezone, times, backfill limits when `schedule.enabled=true`.
- **`scheduler/state.py`** — `read_schedule_state`, `upsert_schedule_state` (MERGE), `append_run_history` (JSONL), success markers. Azure failures caught gracefully.
- **`scheduler/gap_detector.py`** — three-level cascade: Azure state → CSV max settledDate → cold-start fallback. Caps at `max_backfill_days`, applies overlap re-pull.
- **`scheduler/runner.py`** — `run_scheduled()` and `run_backfill()`. Four-gate Azure publish model.
- **`schedule` CLI subcommand** — `install`, `uninstall`, `status`, `logs --tail N` with `--time`/`--retries` overrides.
- **Platform installers** — macOS launchd, Windows Task Scheduler, Linux systemd --user, Linux cron fallback.
- **`scripts/azure_create_schedulestate.py`** — idempotent DDL for `dbo.ScheduleState`.
- **`credentials.template.json`** updated with full `schedule` block.
- **Cross-platform path resolver** (`paths.py`) — OneDrive-aware `get_results_database_dir()`.
- **Comprehensive documentation** — README with cert enrollment guide, CLI reference, configuration reference, platform notes, troubleshooting.
- **Google Sheets publishing** (`sheets_publish.py`, `market_approval.py`) — market-level results uploaded to a Google Sheet with sport-based approval workflow (racing + soccer auto-approved, others require manual review). Incremental sync by `marketId` — inserts new markets, updates changed profits, leaves unchanged markets alone.
- **`publish-sheet` CLI subcommand** — interactive or non-interactive market approval and upload. Supports `--tab` and `--no-interactive` flags.
- **Scheduled Sheets auto-publish** — `runner.py` Phase 5 auto-publishes approved markets after each download when `google_sheets` is configured.
- **`scripts/setup_sheets.py`** — one-command setup for Google Sheets credentials.

### Changed

- `fetch_cleared_orders_df` is now a thin delegator over `fetch_cleared_orders_df_range`. Chunking is a transparent robustness improvement.
- `enrich_with_market_catalogue` accepts an optional `api_client` parameter so headless callers can reuse a single authenticated session.
- Enrichment cache moved from `<repo_root>/outputs/` to `<results_csv_dir>/.cache/` (from remote 0.4.0 merge).

### Fixed

- Gap detector reads the canonical CSV directly instead of delegating to `recommend_lookback_days()`, which was polluted by stale GUI `run_state.json` — prevented unnecessary multi-day backfills.
- Gap detector and runner share a single `resolve_results_dir()` function in `paths.py` — eliminates divergence when `paths.results_csv_dir` is empty.
- Reconciled scheduler with remote 0.4.0 API changes (`enrich_with_market_catalogue` parameter rename `repo_root` → `cache_dir`).

---

## [0.4.0] - 2026-02-15

- Lookback v2: recommends days based on missing settled-date gaps, run_state watermark, or CSV heuristics
- GUI workflow clarified with step-by-step guidance and buttons placed by task area
- Run logs saved per execution for reproducible debugging
- Manual override is explicit via checkbox, defaulting to auto lookback
- Reporting Dashboard added (Streamlit): overview, daily, and weekly pages

## [0.3.0] - 2026-01-19

- Added Azure Tools GUI (health check, backup, normalize, scoped index, cleanup wizard)
- Hardened Azure publishing invariants with scoped uniqueness enforcement
- Added remediation module and script wrappers for safe recovery
- No changes to core downloader behavior

## [0.2.0-gui] - 2026-01-05

- GUI-first downloader with smart lookback and publish-only Azure
- Smart lookback recommendation based on latest settledDate in canonical CSV
- Publish-only Azure button allows publishing from canonical CSV without downloading
- Cleared orders now capture Betfair itemDescription metadata
