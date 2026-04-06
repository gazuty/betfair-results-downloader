## [Unreleased]

### Added (Phase 3.1 — 2026-04-06)

- **`scheduler/installers/__init__.py`** — `get_installer()` platform dispatch: macOS → `LaunchdInstaller`; Windows → `TaskSchedulerInstaller`; Linux+systemd → `SystemdUserInstaller`; Linux → `CronInstaller`.
- **`scheduler/installers/launchd.py`** — `LaunchdInstaller` with `install()`, `uninstall()`, `status()`, `logs()`. Generates XML plist with `StartCalendarInterval` array from `primary_time` + `retry_times`. Defaults to `sys.executable`. `dry_run=True` skips launchctl for testing.
- **`schedule` CLI subcommand** (`__main__.py`) — fully implemented: `install` (with `--time`/`--retries` overrides), `uninstall`, `status`, `logs --tail N`.

### Added (Phase 2.2 — 2026-04-06)

- **`scheduler/gap_detector.py`** — `compute_backfill_window(creds, schedule_cfg)` with three-level cascade: Azure `ScheduleState.LastCoveredDateUtc` → CSV max `settledDate` → cold-start fallback. Caps at `max_backfill_days`, applies `min_coverage_overlap_days` re-pull.
- **`scheduler/runner.py`** — `run_scheduled()` (skip-marker check, gap detect, pipeline, state upsert, marker write, history append) and `run_backfill()` (explicit range, no state update). Four-gate Azure publish model. `client.logout()` in `finally`.
- **`run` CLI subcommand** — calls `run_scheduled`; exit `0` on success/skip, `1` on failure.
- **`backfill` CLI subcommand** — `--from YYYY-MM-DD --to YYYY-MM-DD`; calls `run_backfill`; exit `0`/`1`/`2`.

### Added (Phase 2.1 — 2026-04-06)

- **`scheduler/state.py`** — `read_schedule_state`, `upsert_schedule_state` (MERGE), `append_run_history` (JSONL), `check_today_success_marker`, `write_today_success_marker`. Azure failures caught and returned as `None`/`False` — callers fall back to CSV state without crashing.
- **`scripts/azure_create_schedulestate.py`** — idempotent DDL for `dbo.ScheduleState` with `IF OBJECT_ID IS NULL` guard.

### Added (Phase 1.2 — 2026-04-06)

- **`ScheduleConfig` dataclass** (`config.py`) — frozen dataclass with all scheduled-download settings: `enabled`, `timezone`, `primary_time`, `retry_times`, `publish_to_azure`, `allow_azure_publish`, `max_backfill_days`, `chunk_days`, `min_coverage_overlap_days`, `log_dir`, `history_file`.
- **`parse_schedule_config(creds)`** (`config.py`) — parses the `schedule` block from a credentials dict with all defaults applied; absent or empty block returns `ScheduleConfig(enabled=False)`.
- **Schedule validation in `validate_credentials`** (`secrets.py`) — when `schedule.enabled=true`, validates: `betfair.certs_dir` exists and contains the cert pair; `timezone` is valid IANA; `primary_time`/`retry_times` match `HH:MM`; `max_backfill_days ≤ 365`; `chunk_days ≤ 90`; warns (does not error) if `allow_azure_publish=true` but `enable_azure_sql=false` or `dry_run=true`. All errors are collected together (no fail-fast). Skipped entirely when `schedule.enabled=false`.
- **`CredentialValidation.warnings`** field (`secrets.py`) — new `warnings: list[str]` field (default `[]`); backward-compatible.
- **`credentials.template.json`** updated with full `schedule` block (all safe defaults, `enabled: false`).

---

## [0.4.0] - 2026-04-06

Foundations for scheduled automatic downloads. Additive and backward-compatible — the GUI pipeline and all existing workflows are unchanged.

### Added

- **Cert-based non-interactive Betfair authentication** (`scheduler/auth.py`). Enables fully headless `client.login()` using a client certificate pair stored in a user-configured directory, via the new `betfair.certs_dir` credentials field.
- **Chunked date-range download** (`downloader_core.fetch_cleared_orders_df_range`). Accepts explicit `date`/`datetime` endpoints, splits into safe Betfair `settledDateRange` windows (default 30 days per chunk), and supports reusing a pre-authenticated `APIClient` to eliminate double-login during enrichment.
- **Package CLI entry point** — `python -m betfair_results_downloader` (and the `betfair-results` console script after `pip install -e .`). Implemented subcommand:
  - `auth-test` — verifies cert login end-to-end, masks all secrets, reports actionable errors
- **Stable CLI surface for upcoming phases** — `run`, `backfill`, and `schedule` subcommands are declared and visible in `--help`; they currently print a clear "not yet implemented" message and exit `2`.
- **Unit tests** for date-range chunking covering single-day, exact-fit, exact-multiple, remainder, inverted, and invalid-input edge cases (`tests/test_date_windows.py`, 11 tests).
- **`pyproject.toml` runtime dependencies** — added `betfairlightweight`, `pandas`, `numpy`, `pyodbc` (previously only in `requirements.txt`) and a `[project.scripts]` entry for the CLI.
- **Comprehensive documentation** — README restructured around current capabilities with detailed cert enrollment guide, CLI reference, configuration reference table, and scheduled-downloads roadmap.

### Changed

- `fetch_cleared_orders_df` is now a thin delegator over `fetch_cleared_orders_df_range`. Its public signature and output format are unchanged; chunking is a transparent robustness improvement for large lookbacks.
- `enrich_with_market_catalogue` accepts an optional `api_client` parameter (default `None`) so headless callers can reuse a single authenticated session. The GUI pipeline does not pass it, preserving existing behaviour.
- `credentials.template.json` now includes `betfair.certs_dir`, `user.db_user_id`, and `paths.results_csv_dir` fields (previously inconsistent with README).

### Fixed

- Enrichment cache folder (`outputs/`) now resolves relative to the package location rather than `Path.cwd()`, so the cache is consistent regardless of where the GUI or CLI is launched from.

---

## [0.3.0] - 2026-01-19

- Added Azure Tools GUI (health check, backup, normalize, scoped index, cleanup wizard)
- Hardened Azure publishing invariants with scoped uniqueness enforcement
- Added remediation module and script wrappers for safe recovery
- No changes to core downloader behavior
