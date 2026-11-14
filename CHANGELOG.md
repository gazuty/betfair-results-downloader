# Changelog

## [Unreleased]

### Added

- **A results feed for the dashboard, replacing the manual website download.** Every run now ends by writing `market_results.csv` and `market_results.meta.json` to `<results_csv_dir>/` (new `market_results.py`). The feed has one row per finished market, with website-style name, settled time (UTC and Sydney), Betfair's whole-market gross, commission and net. A comparison on 14 Nov 2026 covered 39,519 markets from 12 Dec 2025 to 8 Nov 2026. It showed the website download over-counting outrights that settle in stages and markets Betfair renamed, and missing about 50 non-racing markets, while the API figures agreed with it everywhere else to the cent. The feed uses the report's settlement gating and commission rules:
  - Partially settled markets and commission-unknown markets are held back.
  - A closed outright appears once, at its close.
  - A zero commission on a small win is judged by the same three-band rule as the DM report (see below), so the feed and the report never disagree.

  Gross and bet count come from the commission store, because 4 of 40,206 markets differed in the canonical's per-bet sum and the store matched the website each time. A run that cannot rebuild the feed keeps the old one and carries a ⚠️ warning, as does a market waiting more than 48 hours for commission. A new `export-market-results` subcommand rebuilds the feed from disk.
- **A zero commission on a small win is judged in three bands.** Betfair rounds commission to the nearest cent, so a charge under half a cent reads 0.00. Rates on this account run from 3.31% to 10.4% (median 5.45%; live store, 14 Nov 2026), so whether a small win's charge rounds to zero depends on its rate, which isn't stored:
  - **Up to 10 cents:** zero is genuine, as before.
  - **10 to 15 cents:** zero is genuine only at the lowest rates, so it counts once read at least 24 hours after the market's last leg settled. Until then it is re-read like any placeholder. Before this change, the $0.11 win on market 1.264531699 stayed commission unknown indefinitely.
  - **Above 15 cents:** zero is always the pre-close placeholder.

  `commission.is_placeholder_commission` applies these bands for the DM report, the recent-market re-read and the results feed.
- **Racecard capture for full racing names.** `listMarketCatalogue` returns nothing for a settled racing market, even 2–6 hours later (0 of 288 in a live check), and `itemDescription` names it only "18:01 Morphettville" / "Win". So a new `racecard.py` captures every racing market starting in the next 30 hours on every run, and from a new hourly `racecard` subcommand (`schedule install-racecard` installs `com.betfair.results.racecard` at :30 past each hour).
  - Full pages are halved until they fit under the 1,000-market cap.
  - The store keeps markets that were bet on, and drops the rest 72 hours after their start.
  - Names come from the racecard, then the catalogue cache, then a fallback from `itemDescription` with the track learned for that venue. An ambiguous or unseen venue gets country `Unknown`, because a name without brackets would count as UK on the dashboard. Sports keep their `itemDescription` names, which already match the website.
- **Per-market commission tracking.** The per-bet rows the downloader stores carry no commission — Betfair charges it per market, on the net winnings of the whole market — so a new `commission.py` reads `listClearedOrders` with `groupBy=MARKET`, which returns one row per market with the gross profit (equal to the sum of the per-bet profits) and the whole-market commission charged (0.0 on a losing or still-pending market). A new pipeline step (after the settlement-status step and before Azure publishing, in both `run` and `backfill`, on every run including an empty download) reads the window's grouped rows plus an explicit re-query of every market ever seen pending whose figure is not yet final, and persists them to `<results_csv_dir>/.cache/market_commission.csv`, upserting by market with the latest read winning. A new `backfill-commission --from YYYY-MM-DD --to YYYY-MM-DD` CLI subcommand (also `python -m betfair_results_downloader backfill-commission`) reads grouped rows for an explicit range with no bet download and no canonical rewrite, for backfilling the commission store after upgrading. On every run the step also re-reads, by explicit id, every canonical market settled in the last fortnight that the store has no usable row for (capped at 2,000 per run), so a window missed by a failed step heals on later runs. The step never fails a run; on error the run carries a ⚠️ warning and the report shows the affected markets as commission unknown until a later run re-reads them. CLOSED rows in `market_settlement_status.csv` are now kept for 400 days instead of 90, because the Year to date section needs a closed outright's pending/close timestamps for that long.

### Changed

- **The DM report is formatted for Slack.** The title and timestamp share the first line (`*Betfair results* · Wednesday 4 November, 6:10 AM`), so the push-notification preview says which report it is. Section headings and each Total line are bold. Every line now leads with net, the figure read first when Slack wraps the line on a phone: `• Horses: net $65.74 (gross $71.82, commission $6.08 · 8.5%)`. The commission rate still reads `n/a` when gross is zero or a loss, or when any market in the line is commission unknown. Plain `dm-report` stdout carries the same text, with Slack's `*` markers.
- **On Sundays the report shows Last week instead of Week to date.** The week starts at Sunday 12:00 AM, so on a Sunday Week to date only repeated Today. The finished Sunday-to-Saturday week takes its place, before Month to date and Year to date (which PR #37 limited to Sundays). `DailyDmReport` gains `last_week_start` and `last_week`.
- **Docs describe the Sunday gating and the new section order** (README and `docs/openclaw-dm-reporting.md`, flagged in Codex's review of PR #37), with weekday and Sunday sample output. They also correct the claim that month and year percentages settle to the effective commission rate: the percentage divides by gross net of losing markets, so on 2026-11-01 Year to date for Soccer read 1860.1% while the rate charged on its winning markets was about 5.4%.
- **`dm-report` shows commission and net alongside gross, and gains Month to date and Year to date sections.** Every breakdown line now reads `Total: gross $1,234.56, commission $98.76 (8.0%), net $1,135.80`, with the percentage being commission divided by gross profit for that sport and period, shown as `n/a` when gross is zero or a loss (Betfair charges on winning markets only, so the ratio has no meaning there) or when any market in the line is commission unknown. Commission is attributed to the window containing the market's latest leg, matching where Betfair places the grouped row, while gross stays per bet row as before. A window with markets that have no trustworthy commission figure — no row in the store, or a row read before the market closed — adds a line saying how many markets and that they are counted as $0.00. Two new sections, Month to date (since the 1st of the current month) and Year to date (since 1 January), sit after Today and before Pending, both Sydney time. The Pending section and Azure publishing (gross only, horses + greyhounds) are unchanged.

### Fixed

- **A market caught mid-settlement keeps its real settled time in the feed.** Any market a run had once seen open was dated at the run that later observed it closed, up to ten hours after the fact. That is right for an outright whose last leg is weeks older than its close, but of the 36 such markets on the live store on 15 Nov 2026 about 20 were ordinary races and correct-score markets, 11 of them pushed onto the next day. The feed now uses the last leg's own time when it settled within a day before the observed close. `dm-report` is unchanged. Totals are unaffected.
- **`export-market-results` no longer makes a stopped downloader look fresh.** The meta gains `lastDownloadUtc`: a run sets it, a rebuild from disk carries the previous value forward. `generatedUtc` still moves on every write. The dashboard judges the feed's age by the new field.
- **The feed is pinned as a contract with the dashboard.** `tests/test_feed_contract.py` builds the feed from fixed, invented inputs and requires it to equal `tests/fixtures/feed_contract/` byte for byte; the dashboard tests against a copy of the same files.
- **`schedule install` replaces a loaded job instead of failing on it.** A reinstall with new times wrote the plist and then failed on "service already loaded", so launchd kept the old times while `schedule status`, which reads the file, showed the new ones. The installer now boots out any loaded copy first, as `install-racecard` already did, and tries the bootstrap once more if launchd refuses it straight after. If the old job was unloaded and the new one still did not load, both installers say that no scheduled job is loaded, since a job that never runs can never alert.
- **An unreadable profit is no longer a silent $0.00 in the DM report.** A profit cell that is blank or not a number still counts as zero so the sums work, but the report now carries `⚠️ N bet rows have an unreadable profit (counted as $0.00); oldest settled <date>` under the title. None of the 1,296,734 rows in the live canonical were affected on 15 Nov 2026; this is for the day one is.
- **Docs and small corrections.** The README's `run` steps now include the racecard capture and the results feed; the CLI docstrings list every subcommand and schedule action; a status-step callback failure is logged instead of swallowed; `schedule install` no longer reads a `--python` option that never existed.
- **Two downloader commands can no longer overwrite each other's files.** `run`, `backfill`, `backfill-commission` and `export-market-results` each read a file, change it and replace it, with nothing to stop two of them overlapping: a manual backfill still running when the 06:00 job fired would have had one side's rows dropped by the other's write, and still printed OK. They now share one lock per results directory (`run_lock.py`, an advisory `flock` on `<results_csv_dir>/.pipeline.lock`, or `msvcrt.locking` on Windows, released when the process exits or is killed). A `backfill`, `backfill-commission` or `export-market-results` that finds it held fails at once and names the holder; `run`, which is the unattended scheduled job, waits up to ten minutes first, then fails, alerts and leaves the window to the next run. The hourly `racecard` job keeps its own store lock and never queues behind a backfill.
- **Slack alerts no longer lose text in angle brackets.** Slack reads `<...>` as link or mention syntax, so an exception such as `<urlopen error [Errno 8] ...>` vanished from the failure alert that reported it. Everything the CLI posts is now escaped for `&`, `<` and `>` (`slack_notify.escape_mrkdwn`).
- **Tiny wins no longer read as commission unknown.** The placeholder rule added in PR #35 treated any winning market stored with zero commission as a pre-close reading, but a win of 9 cents or less is charged 0.00 once Betfair rounds, so 212 genuine zeros were reported as unknown and the Year to date percentages rendered `n/a` for horses and greyhounds. The rule (now `commission.is_placeholder_commission`, shared by the report and the recent-market seed) applies only to wins above 10 cents.

## [0.8.0] - 2026-09-06

Per-market settlement status and an all-sports daily report (PRs #31, #32, #33).

### Added

- **Per-market settlement status.** Betfair settles the losing runners of an outright/tournament market as they are eliminated, so `listClearedOrders` delivers "settled" bets for a market whose outcome is still open (the Men's US Open 2026 Winner market settled 14 lay bets over six timestamps while 22 runners were still active), and cleared orders carry no market status to tell the two cases apart. A new pipeline step (`market_status.py`, after the CSV write and before Azure publishing, in both `run` and `backfill`) asks `listMarketBook` about every market the download window touched, every market still recorded as pending, and every canonical market settled in the last 14 days with no record yet, and persists the observations to `<results_csv_dir>/.cache/market_settlement_status.csv`. Absent markets are recorded as CLOSED (open markets are always returned; closed ones age out of the book after a variable period), provisionally: they are re-checked for 48 hours so one dropped response row cannot permanently mark a live outright final. The first-run seed is capped at 2,000 markets per run. The step never fails a run; on error the run carries a ⚠️ warning and the report treats that window's markets as final until the next run re-checks.
- **`dm-report` covers every sport and holds back partially settled markets.** Horses and Greyhounds are always shown, then one line per other sport with a settlement in the window, ordered by absolute profit (sport names from an expanded Betfair event-type map in `reporting/schema.py`). Any market not yet CLOSED is held out of every total and summarised in a new "Pending (partially settled, not counted above)" section. A market seen pending and later observed CLOSED counts all of its bets on the day the pipeline first saw it CLOSED (Sydney time), so early-settled legs are not lost in weeks already reported; a market CLOSED at first sight (racing, match odds) keeps its original settled date, so racing reporting is unchanged. Markets with no status record count as final, exactly as before. Azure publishing remains horses + greyhounds only.
- **`dm-report` has a Yesterday section.** The full previous Sydney calendar day, with the same per-sport breakdown as the other sections, placed between Week to date and Today. It is a closed day, so the 6:00 AM and 7:35 PM reports agree on it, and it ignores the week boundary (on a Sunday it is last week's Saturday). Partially settled markets are held out of it like everywhere else.

### Changed

- **Documentation refresh and repo health.** README repository structure now matches the tree (`betfair_net.py`, `slack_notify.py`, `scheduler/time_semantics.py`, the `reporting/` modules, `docs/`, `notebooks/`), the feature list covers settlement gating and the all-sports report, the completed scheduled-downloads roadmap is retitled as rollout history, and Troubleshooting gains a Reports section. CONTRIBUTING documents the pull-request workflow (branch, tests, local gates, CI, Codex review loop, merge). Python 3.14 — the interpreter the production launchd job runs — joins the CI matrix. The one stale merged remote branch was deleted.
- **`decimal_key` moved from `downloader_core._decimal_key` to `csv_utils.decimal_key`**, so `market_status.py` can share the same numeric-key matching (float-damaged marketId spellings, e.g. `1.2515001` for `1.251500100`) without importing `downloader_core`. `downloader_core._decimal_key` remains as an alias.

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
