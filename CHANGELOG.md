# Changelog

## [Unreleased]

_Nothing yet._

## [0.7.0] - 2026-08-23

Full-repository code review and remediation (PR #13). The review document
with findings and rationale lives at `docs/reviews/2026-08-23-full-code-review.md`.

### Fixed

- **Failed Azure publishes were recorded as published.** `publish_to_azure_sql` swallows its own exceptions into the result, and the scheduler read `attempted` as success — so a persistently failing publish still produced `azure=published` in `run_history.jsonl` and advanced `ScheduleState`. `AzurePublishResult` now carries `ok` (attempted = writes tried, ok = no error) and a failed publish records the run as `partial`.
- **Failed scheduled runs never reached `run_history.jsonl`.** Only the auth step was guarded; a download/enrich/CSV exception crashed the process before history was appended. `_run_pipeline` now converts any unhandled error into a failed `RunResult`, so every attempt — success or failure — is recorded.
- **Sub-second blind spot at download-chunk boundaries.** Chunks ended at `23:59:59` and the next began at `00:00:00`; orders settled inside that final second could be skipped. Windows are now half-open and contiguous (each chunk's exclusive end is the next chunk's start), and date ranges cover the final day through to midnight. Scheduled runs were protected by the 2-hour overlap; `backfill` was not.
- **Empty downloads advanced the settled-timestamp checkpoint to "now".** An empty run confirmed nothing but still asserted coverage. The runner now passes no checkpoint, and the `ScheduleState` MERGE keeps the stored value (`LastConfirmedSettledAtUtc` is now monotonic non-decreasing; the MERGE also runs `WITH (HOLDLOCK)` to close the classic two-machine upsert race).
- **`dm-report` headings broke on Windows** — `%-d`/`%-I` are glibc-only strftime codes; the heading is now formatted portably with identical output.
- **`run`/`backfill` never validated credentials.** The schedule-section validation (cert files present, timezone, HH:MM formats, bounds) only ran from GUI-era dead code. Both commands now validate up front and exit 2 with the collected errors.
- **Missing `pyodbc` produced a cryptic publish error** (`'NoneType' object has no attribute 'connect'`); the publisher now reports an actionable install message.
- **cron installer ignored retry-time minutes** — all retries were forced onto the primary time's minute. Times are now grouped by minute (one cron line per distinct minute), with backward-compatible replacement of old-format crontab entries.

### Added

- **`audit` CLI subcommand** — `betfair-results audit [--window DAYS]` reports missing settled-date gaps in the canonical CSV with ready-to-run backfill suggestions (exposes the existing `audit.py` analysis).
- **Enrichment retry + salvage** — `listMarketCatalogue` calls get the same TIMEOUT_ERROR retry/backoff as cleared orders; a mid-fetch failure caches and merges the rows fetched so far and is reported in the result instead of aborting the run. Enrichment failure is non-fatal to scheduled runs (CSVs are still written).
- **`LICENSE` (MIT)** and project metadata (license, authors, classifiers) in `pyproject.toml`.
- **Pinned ruff lint rule selection** in `pyproject.toml` so CI lint results no longer change when a newer ruff ships different defaults (ruff 0.16 broke previously-green runs with 216 new-rule findings).
- **CI hardening** — `ruff format --check`, Python 3.13 in the test matrix.

### Removed

- **The caller-less GUI-era layer**: `run.py`, `pipeline.py`, `recommend.py`, `state.py` (run_state.json persistence), `run_logging.py`, and the unused `reporting/io.py` helpers, plus their tests. The headless scheduler path (`__main__` → `scheduler/runner`) is the single pipeline. This also removes a latent bug: `run_state.json` persistence had silently failed on every run since 0.5.0 (datetimes handed to `json.dumps`).
- **`DownloaderConfig` and the inert `user.days` / `include_horses` / `include_greyhounds` settings.** Downloads always fetch all settled orders; Azure publishing is fixed in code to horses + greyhounds (`DEFAULT_AZURE_EVENT_TYPE_IDS`). Old credentials files with these keys remain valid — the keys are ignored.
- **`schedule.min_coverage_overlap_days`** — parsed but never used since the timestamp-checkpoint redesign; `min_overlap_hours` is the real overlap control.
- **Marker-era remnants** — `RunResult.skipped` / `skip_reason` and `check_today_success_marker` (markers are write-only audit artifacts; they stopped gating runs in 0.5.x).
- **Hardcoded personal paths** — the `C:/Users/Mark/OneDrive` Windows candidate in `paths.py` and the absolute developer paths in `scripts/render_dm_report.sh` (now repo-relative with env overrides).
- **`SQLAlchemy` from `requirements.txt`** — nothing imports it.

### Changed

- The three duplicated Azure ODBC connection-string builders are consolidated into `azure_common.build_conn_str`.
- `DownloadResult.from_utc`/`to_utc` are typed (and populated) as `datetime`, matching reality.

### Clarified

- The 0.6.0 note "`itemDescription` is no longer downloaded" was superseded shortly after by PR #10, which re-enabled `include_item_description=True` and flattens the blob into `evt_*`/`mkt_*`/`runner_name`/`market_type` columns instead of storing raw JSON. Current behaviour: itemDescription **is** downloaded and flattened; catalogue enrichment coalesces in as a fallback.

### Removed (pre-review cleanup)

- **Tkinter GUI** (`gui_app.py`) and **Streamlit reporting dashboard** (`reporting_app.py`, `reporting/ui.py`, `reporting/filters.py`, `reporting/exports.py`, `reporting/transforms.py`, `reporting/pages/`, plus the never-imported `reporting/derive.py` and `reporting/metrics.py`). The project is headless-only: CLI subcommands plus scheduled jobs. The `dm-report` path (`reporting/daily_dm_report.py`, `io.py`, `schema.py`) is unchanged.
- **`streamlit`, `plotly`, and `pytz` dependencies** — the GUI/dashboard consumers are gone and the single `pytz` use is replaced with stdlib `zoneinfo`.
- **Completed one-off scripts** — the itemDescription backfill/enrichment set and the Azure remediation wrapper scripts (logic lives on in `azure_remediation.py` and git history). The tested `ScheduleState` DDL scripts and `render_dm_report.sh` remain.
- **Per-version `RELEASE_NOTES_*.md` files** — release notes are now sections in this changelog.

### Fixed (pre-review cleanup)

- **Unused imports in `scripts/backfill_item_descriptions.py`** that broke CI lint on `main` (ruff F401).
- **Timestamp parsing warning** — the archival and dedupe sort-key paths now parse round-tripped CSV timestamps with `format="ISO8601"`, handling both historical renderings of the same instant (`2026-07-13 04:58:46+00:00` and `2026-07-13T04:58:46Z`) without the per-element dateutil fallback.

---

## [0.6.0] - 2026-07-09

Data lifecycle management. Unbounded full-copy snapshots (~25 GB across 94 files) had pushed the OneDrive-hosted results folder into dataless placeholder eviction, taking down both scheduled downloads (cert reads) and DM reports (CSV reads). This release keeps the results folder permanently small.

### Added

- **Snapshot retention** — dated snapshots beyond `user.snapshot_retention_days` (default 14) are deleted after each run (`prune_snapshot_files`). `0` disables.
- **Snapshot compression** — snapshots are written as `cleared_orders_cleaned_YYYY-MM-DD.csv.gz` (~18× smaller). Opt out with `user.compress_snapshots: false`.
- **Yearly canonical archival** — rows settled more than `user.canonical_archive_months` ago (default 12) move from the canonical CSV into `cleared_orders_archive_YYYY.csv.gz`, deduplicated on `betId` and safe to re-run after a partial failure (`archive_old_canonical_rows`). `0` disables.

### Changed

- **`itemDescription` is no longer downloaded** (`include_item_description=False`). The JSON blob accounted for ~45% of canonical file size on disk and nothing downstream reads it — event/market names come from the enrichment columns. The presence smoke-check was removed with it. Existing canonical files keep the column until manually stripped; new rows simply won't have it.
- README recommends storing Betfair certs in a local non-cloud-synced directory (e.g. `~/.betfair/certs`) — cloud sync clients can evict cert files to online-only placeholders, breaking non-interactive login with `OSError: [Errno 11] Resource deadlock avoided`.

### Fixed

- **`scripts/azure_upgrade_schedulestate.py` could never succeed** — it sent the `ALTER TABLE`s and the backfill `UPDATE`s as a single T-SQL batch, which SQL Server rejects at compile time (`Invalid column name`) because the `UPDATE` references columns added earlier in the same batch. The DDL and backfill now run as separate batches. Anyone running the intraday scheduler (0.5.x June builds) should re-run this script: until it succeeds, every scheduled run logs `Failed to upsert ScheduleState` and the Azure checkpoint silently never advances.

### Upgrade notes

- Existing uncompressed snapshots are pruned by the retention policy as new runs happen; no manual cleanup is required (deleting old ones by hand reclaims space immediately).
- If the results folder syncs via OneDrive/iCloud: keep the sync client running at login, consider pinning `cleared_orders_cleaned.csv` and `run_state.json` ("Always Keep on This Device"), and store certs in a local non-synced directory.

---

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
