# Full Code Review — Betfair Results Downloader

**Date:** 2026-08-23
**Scope:** Entire repository at `883edb1` — all source modules, scheduler package, installers, reporting, tests, CI, scripts, notebook, and documentation.
**Baseline:** `ruff check .` clean · `pytest -q` 160/160 passing.

> **Implementation status (2026-08-23):** all five phases of the remediation
> plan below were implemented on this branch in the same PR. The two flagged
> decisions were resolved as recommended: the inert
> `include_horses`/`include_greyhounds` flags were removed (Azure scope is
> the explicit `DEFAULT_AZURE_EVENT_TYPE_IDS` constant), and `audit.py` was
> kept and exposed as the `betfair-results audit` subcommand. The suite grew
> from 160 to 168 tests; `CHANGELOG.md` carries the user-facing summary.
> The MIT license choice in D3 is a default — swap it if you prefer another.

---

## Overall assessment

The project is in good shape for its purpose: the headless scheduled path
(`__main__` → `scheduler/runner` → `downloader_core` → `csv_utils` /
`azure_publish`) is coherent, idempotent by design, well-logged, and the
four-gate Azure safety model is enforced consistently. Documentation is
unusually thorough (cert enrollment guide, config reference, troubleshooting).
The test suite is meaningful — it covers dedupe ordering subtleties, DDL batch
splitting, installers on all four platforms, and gap-detection cascades.

The main structural problem is **a legacy "GUI branch" layer that no longer
has any callers** but still carries real bugs, dead config, and doc drift.
The findings below are ordered by severity; the remediation plan at the end
sequences them into small, independently shippable pieces.

---

## A. Correctness findings

### A1. Failed Azure publish is recorded as published (HIGH)
`azure_publish.publish_to_azure_sql()` catches every exception and returns
`AzurePublishResult(attempted=True, message="Azure publish failed: …")`.
`scheduler/runner.py:191` then does `azure_published = az.attempted`, so a
**failed** publish is recorded in `RunResult`, `run_history.jsonl`, and
`ScheduleState` as a successful, published run. The `except` block in the
runner only catches exceptions that *escape* the publisher — which by design
none do.

*Impact:* run history and ScheduleState lie about publish outcomes; a
persistent Azure failure is invisible unless someone reads log text.
*(Mitigating: the sync is idempotent, so the next successful run repairs the
data — the defect is in reporting/observability, not data loss.)*

*Fix:* add an `ok: bool` field to `AzurePublishResult` (`attempted` = we tried,
`ok` = it worked); set `azure_published = az.ok`; return `status="partial"`
when `attempted and not ok`.

### A2. Unhandled pipeline exceptions bypass run-history (HIGH)
`runner._run_pipeline()` guards only the auth step. If
`fetch_cleared_orders_df_range`, enrichment, or CSV writing raises (e.g.
`APIError` after retries exhaust, disk error), the exception propagates out of
`run_scheduled()` — the process dies with a traceback, **no
`run_history.jsonl` record is appended**, and no `RunResult` is produced. The
history file therefore under-reports failures, which matters because
`schedule logs` is the primary operational view.

*Fix:* wrap the body of `_run_pipeline` in `try/except`, return
`RunResult(ok=False, status="failed", message=…)`; `run_scheduled` already
appends history for failed results.

### A3. Sub-second gap at chunk boundaries (MEDIUM)
`_build_datetime_chunks` ends each chunk at `23:59:59` and starts the next at
`00:00:00`. Betfair settled timestamps carry millisecond precision, so an
order settled in `(23:59:59.000, 00:00:00.000)` falls between chunks. The same
1-second blind spot exists at range end via `_to_utc_datetime(…,
end_of_day=True)`. Scheduled runs are protected by the 2-hour overlap on the
*next* run, but **`backfill --from --to` has no overlap** — its final second
of each chunk and of the whole range is genuinely uncovered.

*Fix:* use half-open windows — chunk N's `to` equal to chunk N+1's `from`
(midnight), and range end = `to_date + 1 day 00:00:00`. Verify against
Betfair's documented `settledDateRange` inclusivity to avoid double-counting
(harmless anyway given `betId` dedupe).

### A4. `run_state.json` persistence has silently failed since 0.5.0 (MEDIUM — dead path)
`DownloadResult.from_utc`/`to_utc` are annotated `Optional[str]` but
`fetch_cleared_orders_df_range` assigns `datetime` objects
(`downloader_core.py:373-374`). `pipeline.run_pipeline` puts them into
`run_state`, and `state.save_run_state` uses `json.dumps` without
`default=` — raising `TypeError` on every run, caught and downgraded to a
warning (`persist_success=False`). Confirmed by execution.

*Impact today:* none in production — `pipeline.py` has **no remaining
callers** since the GUI was removed (see C1). But it's a live bug in code the
tests still exercise around, and the type annotation is wrong either way.

*Fix:* store `.isoformat()` strings (matches the annotation), or delete the
legacy layer (preferred, C1).

### A5. Empty download confirms an unobserved checkpoint (LOW)
On a zero-row download, `runner._run_pipeline` returns
`last_confirmed_settled_at_utc = to_dt_utc` (i.e. "now"), which
`upsert_schedule_state` persists as the *confirmed settled* checkpoint. Nothing
was actually observed at that instant; the name and semantics ("latest
confirmed settled timestamp") no longer hold. The 2-hour overlap makes the
practical risk small, but if Betfair ever settles/void-reverses with
visibility latency beyond `min_overlap_hours`, empty runs would have walked
the checkpoint past real data.

*Fix:* on zero rows, carry the previous checkpoint forward (or
`max(previous_checkpoint, csv_max_settled)`) instead of `to_dt_utc`.

### A6. `dm-report` heading breaks on Windows (LOW)
`reporting/daily_dm_report.py:42` uses `%-d` and `%-I` strftime codes —
glibc-only extensions. On Windows (`%#d`/`%#I` there), `strftime` raises
`ValueError` or emits literal `-d`. The README claims full Windows support.

*Fix:* format portably, e.g. build the string from `dt.day` and
`dt.strftime("%B")` / manual 12-hour conversion, with a unit test.

### A7. Headless CLI never validates credentials (MEDIUM)
`validate_credentials()` — including the schedule-section validation built
specifically for scheduled mode (cert files present, timezone valid, HH:MM
formats, numeric bounds) — is only called from `run.run_downloader()`, which is
GUI-era dead code. `_load_creds_and_schedule()` in `__main__.py` parses but
never validates, so `run`/`backfill` fail later with rawer errors (or worse,
run with a mis-typed timezone falling back at a different layer).

*Fix:* call `validate_credentials` in `_load_creds_and_schedule`; print errors
and exit 2, print warnings and continue.

### A8. `include_horses` / `include_greyhounds` config is inert (MEDIUM)
`DownloaderConfig.selected_event_type_ids()` exists, the README documents the
flags as gating downloads, and the template ships them — but every call to
`prepare_azure_dataset` hardcodes `allowed_event_type_ids={7, 4339}`, and the
download itself is never filtered by sport anywhere. Setting either flag to
`false` changes nothing.

*Fix (choose one):* wire `selected_event_type_ids()` through the runner and
pipeline into `prepare_azure_dataset`, or remove the flags and document that
downloads are unfiltered and Azure publish is fixed to horses+greyhounds.
Given the scheduler ignores `DownloaderConfig` entirely, removal + doc fix is
the smaller, honest change.

---

## B. Robustness and edge cases

- **B1. Enrichment has no retry and loses work on failure.**
  `_call_list_cleared_orders` has timeout retry/backoff;
  `list_market_catalogue` calls have none, and the cache is only written after
  *all* batches complete. One transient APIError aborts the whole scheduled
  run *after* a successful download, discarding every fetched catalogue row.
  Fix: share the retry helper, write the cache once at the end even on partial
  failure (or per batch), and consider making enrichment failure non-fatal —
  CSV output should still be written (names can backfill from cache next run).
- **B2. `pyodbc = None` fallback produces cryptic errors.** In
  `azure_publish`, if the native import failed, `pyodbc.connect` raises
  `AttributeError` which the broad `except` renders as
  `Azure publish failed: 'NoneType' object has no attribute 'connect'`. Check
  explicitly and emit the same actionable message `azure_remediation` uses.
- **B3. Canonical column order churns.** `update_csv_with_new_data` reindexes
  to the *sorted* union of columns, so the canonical CSV's column order is
  alphabetical and changes whenever a new column appears; reads also lack
  `low_memory=False`/dtype control (DtypeWarning on wide files). Minor, but
  worth pinning: preserve existing order, append new columns at the end.
- **B4. Marker-era remnants.** `RunResult.skipped`/`skip_reason` are never
  set; `check_today_success_marker` has no callers; `_cmd_run`'s docstring
  still says "Checks today's success marker." Remove/update together.
- **B5. `secrets.repo_root()` assumes an editable install.** A wheel install
  would resolve `secrets/` under site-packages. At minimum document the
  editable-install requirement next to the function; better, allow an
  environment-variable override (e.g. `BETFAIR_RESULTS_CREDENTIALS`).
- **B6. ScheduleState MERGE race.** Two machines upserting concurrently can
  hit the classic MERGE race (PK violation). It's caught and logged — fine —
  but add `WITH (HOLDLOCK)` to make the documented two-machine story fully
  true.
- **B7. cron installer discards retry minutes.** All retry times share the
  primary time's minute (documented in the docstring, but launchd/systemd/
  Task Scheduler honor exact minutes). Emit one cron line per distinct
  minute instead.
- **B8. Personal paths hardcoded.**
  `paths.py:27` — `C:/Users/Mark/OneDrive` as the first Windows candidate;
  `scripts/render_dm_report.sh` — absolute `/Users/markmcfarlane/...` defaults
  including a `.claude/worktrees/...` path. Replace with `Path.home()`-based
  and repo-relative defaults.

---

## C. Code health

- **C1. Dead GUI-era layer (largest cleanup).** With the Tkinter GUI and
  Streamlit dashboard removed, nothing calls: `run.py` (`run_downloader`),
  `pipeline.py` (`run_pipeline`), `recommend.py` (v1 *and* v2),
  `reporting/io.py:build_cached_csv_loader` / `file_info`, or
  `state.load_run_state` (only used by `recommend`). The notebook uses its own
  inline cells plus `csv_utils` only. This layer carries bugs A4/A8 and ~900
  lines of maintenance surface. Recommend deleting it (with its tests) in one
  PR, per the 0.6.0 "headless-only" direction — `git` history preserves it.
  Keep `audit.py` (used by nothing after the cut — decide: it's genuinely
  useful; consider exposing it as an `audit` CLI subcommand instead of
  deleting).
- **C2. `DownloaderConfig.validate`** ends with a dead
  `if self.enable_azure_sql and self.dry_run is False: pass` branch.
- **C3. Mutable default argument** `allowed_event_type_ids: set[int] = {7, 4339}`
  in `prepare_azure_dataset` (never mutated, but a lint-magnet; use a frozen
  default or `None`).
- **C4. `Optional[callable]` annotations** in `downloader_core`
  (`enrich_with_market_catalogue`, `prune_snapshot_files`,
  `archive_old_canonical_rows`, `write_csv_outputs`) vs the correct
  `Optional[Callable[[str], None]]` used elsewhere.
- **C5. `_build_conn_str` duplicated three times** (`azure_publish`,
  `azure_remediation`, `scheduler/state`) — extract to one module (e.g.
  `azure_common.py`) so options like `Connection Timeout` can't drift.
- **C6. `min_coverage_overlap_days`** is parsed, documented as "legacy
  retained for compatibility", and used nowhere. Remove from config, template,
  and README (still accept-and-ignore unknown keys, so old files stay valid).
- **C7. Notebook.** Cell 2 duplicates the downloader with interactive login;
  cell 13 is a destructive DELETE+INSERT rebuild. As the package is now the
  source of truth, either retire the notebook or trim it to exploration cells
  with a prominent warning on cell 13.
- **C8. `requirements.txt` lists SQLAlchemy** which nothing imports. Trim, or
  add it properly if you want to silence pandas' "only SQLAlchemy
  connectable" warning in `read_existing_marketresults` (the warning itself is
  worth addressing either way).

---

## D. Documentation

- **D1. CHANGELOG contradicts the code.** 0.6.0 says *"`itemDescription` is no
  longer downloaded (`include_item_description=False`)"* — but
  `downloader_core.py:115` passes `True` and `_extract_item_description_fields`
  flattens it into `evt_*/mkt_*/runner_name` columns. The Unreleased section
  has no entry for the re-enablement. Add one (and note the flattening
  behavior).
- **D2. README Roadmap contradicts README body.** The roadmap summary still
  says each retry window *"checks whether the day has already been covered and
  skips silently"* — the timestamp-checkpoint redesign (documented correctly
  in *Scheduler timezone and coverage semantics*) removed exactly that. Also
  applies to `__main__._cmd_run`'s docstring (B4).
- **D3. No LICENSE file** and `pyproject.toml` has no `license`, `authors`, or
  `classifiers`. Even for a personal project, an explicit license (or a
  "personal use, all rights reserved" note) removes ambiguity; the README's
  `<your-org>` clone URLs suggest an audience beyond one machine.
- **D4. README config reference vs A8** — `include_horses`/`include_greyhounds`
  documented as gating downloads; they don't (resolve with A8).
- **D5. CONTRIBUTING** lists `ruff format .` as a quality check but CI doesn't
  enforce it (see E1); instructions are PowerShell-only while the primary
  deployment is macOS/launchd — add the bash equivalents.

---

## E. Tests & CI

Strong suite overall. Gaps worth closing, in value order:

- **E0. CI lint was version-drifting** *(found and fixed during this review)* —
  CI installs unpinned latest ruff, and ruff 0.16 expanded its default rule
  set, turning previously-green runs into 216 lint errors with no code change.
  Fixed by pinning `[tool.ruff.lint] select = ["E4","E7","E9","F"]` (the prior
  defaults) in `pyproject.toml`. The newly-flagged rules (BLE001 blind
  excepts, DTZ naive datetimes, PLW1510 `subprocess.run` without `check`)
  overlap heavily with findings A2/B2/B4 — adopting them incrementally is
  worthwhile Phase-5 work.
- **E1. CI doesn't enforce formatting** — add `ruff format --check .` to CI so
  the CONTRIBUTING contract is real.
- **E2. No tests for the download loop** — `fetch_cleared_orders_df_range`
  pagination, timeout-retry, chunk boundary behavior (would have caught A3),
  and `_normalize_cleared_orders_df` on empty/partial input, using a stubbed
  client.
- **E3. No tests for runner failure paths** — A1/A2 fixes should land with
  tests: publish failure ⇒ `status="partial"`, pipeline exception ⇒ history
  record with `status="failed"`.
- **E4. `publish_to_azure_sql` untested** (only `build_sync_plan`/
  `apply_sync_plan` are covered) — a fake-connection test of the
  dry-run/exception/commit paths would cover A1 and B2.
- **E5. dm-report formatting test pinned to glibc** — add the portable-format
  test with A6.
- **E6. Consider adding Python 3.13** to the CI matrix (cheap, matrix already
  exists) and a coverage report to keep the dead-code problem visible in
  future.

---

## F. Security posture (reviewed, acceptable)

- No secrets committed; `.gitignore` and template hygiene are solid; the
  `secrets/*` allowlist pattern is correct.
- Credentials in plaintext JSON is a documented tradeoff for a personal tool;
  cert-key handling guidance in the README is genuinely good.
- Log masking (`mask_value`, auth-test) leaks only 4 chars — fine for app
  keys; acceptable for session tokens given local-only logs.
- SQL goes through parameterized queries throughout (incl. remediation
  module); connection strings enforce `Encrypt=yes;TrustServerCertificate=no`.
- One nit: `auth-test` prints the session token length + masked form; drop the
  length to be strict.

---

## Remediation plan

Each phase is one small PR, independently shippable, tests included.

**Phase 1 — Correctness in the live scheduled path**
1. A1: `AzurePublishResult.ok`; runner uses it; `status="partial"` on failed
   publish (+ E3/E4 tests).
2. A2: try/except around `_run_pipeline` body so every failure lands in
   `run_history.jsonl` (+ test).
3. A3: half-open chunk/range windows in `_build_datetime_chunks` /
   `_to_utc_datetime` / `run_backfill` (+ boundary tests, E2).
4. A7: validate credentials in `_load_creds_and_schedule` (errors exit 2,
   warnings printed).

**Phase 2 — Edge cases & robustness**
5. A5: preserve prior checkpoint on empty downloads.
6. B1: enrichment retry + partial-cache persistence + non-fatal enrichment.
7. A6/E5: portable dm-report date formatting.
8. B2, B6, B7: pyodbc guard, MERGE HOLDLOCK, cron per-minute lines.

**Phase 3 — Dead code removal & config truth**
9. C1: delete `run.py`, `pipeline.py`, `recommend.py`, GUI-era `reporting/io`
   helpers + their tests; decide `audit.py`'s future (suggest: new `audit`
   CLI subcommand).
10. A8/C6/D4: remove inert `include_*` flags and `min_coverage_overlap_days`
    (or wire them through — decision needed), update README + template.
11. C2–C5: dead branch, mutable default, `callable` annotations, shared
    `_build_conn_str`.
12. B8: de-personalize `paths.py` and `render_dm_report.sh`.

**Phase 4 — Documentation & project metadata**
13. D1: CHANGELOG entry for itemDescription re-enable; D2/B4 doc drift.
14. D3: LICENSE + pyproject metadata.
15. D5: CONTRIBUTING bash parity; C7 notebook decision; C8 requirements trim.

**Phase 5 — CI hardening**
16. E1: `ruff format --check` in CI; E6: 3.13 matrix entry + coverage.

---

*Review artifacts: baseline commands were run in a clean Linux container
(Python 3.11): `ruff check .` → no findings; `pytest -q` → 160 passed. Bugs
A3 and A4 were confirmed by direct execution, not just inspection.*
