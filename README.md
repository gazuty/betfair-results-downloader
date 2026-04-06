# Betfair Results Downloader

A professional Python application for downloading settled Betfair orders, enriching them with market metadata, writing reliable CSV outputs, and optionally publishing aggregated market-level results to Azure SQL and Google Sheets — safe by default.

---

## Features

- **GUI-first downloader** — Tkinter desktop app with First Run Wizard, live phase progress, and structured summaries
- **CSV outputs** — canonical file plus dated snapshots, idempotent updates, safe to re-run
- **Market metadata enrichment** — cached market catalogue lookups (avoids repeat API calls)
- **Azure SQL publishing** — incremental, non-destructive, multi-gate safety model
- **Google Sheets publishing** — market-level results to a shared Google Sheet with approval workflow and incremental sync
- **Azure Tools** — read-only health checks, scoped backups, emergency cleanup wizard
- **Reporting Dashboard** — local Streamlit UI for daily/weekly P&L analytics
- **Non-interactive cert authentication** — `betfairlightweight` cert-based login for headless use *(new in 0.5.0)*
- **CLI entry point** — `python -m betfair_results_downloader` with `auth-test` subcommand *(new in 0.5.0)*
- **Chunked date-range download** — automatic splitting into safe Betfair settledDateRange windows *(new in 0.5.0)*
- **Scheduled automatic daily downloads** — gap detection, multi-window retry, cross-platform installers (macOS launchd, Windows Task Scheduler, Linux systemd/cron)

---

## Quick Start (GUI mode)

The GUI is the supported interactive entry point.

```bash
git clone https://github.com/<your-org>/betfair-results-downloader.git
cd betfair-results-downloader
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -e .
python -m betfair_results_downloader.gui_app
```

On first launch, the **First Run Wizard** walks you through:

1. Choosing where to save `credentials.json`
2. Selecting a results output folder
3. Entering Betfair credentials
4. Setting run defaults (lookback days, sports)
5. *(Optional)* Entering Azure SQL credentials

Then click **Run Downloader** and watch the four-phase progress: download → enrich → CSV → Azure (if enabled).

### Recommended GUI Workflow

Follow the same order shown at the top of the GUI:

1) Choose Paths (Results folder) → 2) Validate → 3) Compute Lookback → 4) Run Downloader → (Optional) 5) Publish to Azure

### Lookback v2 (auto)

The downloader computes an **effective lookback** before a run. Decision order:

1. Missing settled-date gaps within the audit window (≤ 90 days) → recommend based on the earliest missing date in the most recent missing range.
2. Otherwise use `run_state.json` (`last_success_utc`).
3. Otherwise fall back to the canonical CSV latest settledDate heuristic.
4. If no CSV and no run_state exist, default to **90 days** (Betfair maximum backfill).

To force a manual value: tick **Manual override**, enter the Days value — it applies for one run only.

### Run logs

Each run persists a full log transcript for debugging:

`<results_csv_dir>/run_logs/run_YYYYMMDD_HHMMSS.txt`

These logs match the GUI output and are written in UTF-8 with ASCII-safe status lines.

---

## Detailed Setup Guide

### Prerequisites

- **Python 3.10+**
- **A Betfair account** with the Exchange API activated and an **app key** registered ([Betfair Developer Program](https://developer.betfair.com/))
- *(Optional)* **Azure SQL database** if you want to publish market-level results
- *(Optional, for Azure)* **ODBC Driver 18 for SQL Server** installed on the machine running the publish step
- *(Optional, for scheduled mode)* **Two-factor authentication enabled** on your Betfair account — required to enroll the client certificate used by non-interactive login

### Install

```bash
git clone https://github.com/<your-org>/betfair-results-downloader.git
cd betfair-results-downloader
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Editable install (`-e`) is recommended so you can pull updates without reinstalling.

### Credentials file

The app reads credentials from `secrets/credentials.json` by default. You can store the real file anywhere (recommended: a cloud-synced folder like OneDrive or iCloud Drive, so both your laptop and desktop share the same config) and point at it via a tiny pointer file.

**To store `credentials.json` outside the repo:**

Create `secrets/credentials.location.json` (git-ignored by default) with:

```json
{"path": "/absolute/path/to/your/credentials.json"}
```

The resolver handles `~` expansion and relative paths. See [`secrets.py`](src/betfair_results_downloader/secrets.py) for the full resolution rules.

**Minimum fields:**

```json
{
  "betfair": {
    "username": "your-betfair-username",
    "password": "your-betfair-password",
    "app_key": "your-betfair-app-key"
  },
  "user": {
    "user_id": "YourName",
    "days": 7,
    "include_horses": true,
    "include_greyhounds": true,
    "enable_azure_sql": false,
    "dry_run": true
  },
  "paths": {
    "results_csv_dir": "/absolute/path/to/output/folder"
  }
}
```

See [Configuration Reference](#configuration-reference) for the full schema including Azure SQL and the new `betfair.certs_dir` field for cert login.

---

### Betfair Certificate Enrollment

Cert-based login lets the app authenticate to Betfair **without any interactive step** — no browser, no prompts, no session timeouts mid-run. This is required for automated/scheduled use and is the approach Betfair officially supports for bot accounts.

The GUI doesn't need certs (it uses interactive login). You only need to enroll a certificate if you want to run `python -m betfair_results_downloader auth-test` or the scheduled downloads feature.

#### What a client certificate is (and why Betfair needs one)

A client certificate is a cryptographic identity card. You generate a matching **public certificate** (`.crt`) and **private key** (`.key`) locally. You upload the `.crt` to your Betfair account — Betfair stores it and trusts any request signed by the matching private key. The `.key` file stays on your machine and must never leave it.

Betfair's documented non-interactive login endpoint (`identitysso-cert.betfair.com/api/certlogin`) requires a mutually-authenticated TLS handshake using this cert pair. The `betfairlightweight` library handles the TLS plumbing automatically once you tell it where your cert directory lives.

> **⚠️ Never share, commit, or upload your `.key` file.** Treat it like a password. Anyone with the `.key` can authenticate as you. If you ever suspect it has leaked, generate a new pair and replace the enrolled cert on your Betfair account immediately.

#### Prerequisites

- Betfair Exchange account with API access
- A registered **app key** (found in your Betfair Developer account)
- **Two-factor authentication enabled** on your Betfair account — Betfair will not accept a certificate upload without it

#### Step 1 — Generate the cert pair

Pick a safe location. A cloud-synced `secrets/certs/` folder alongside your `credentials.json` keeps both machines in sync:

```bash
CERTS_DIR="$HOME/path/to/secrets/certs"
mkdir -p "$CERTS_DIR"

openssl req -x509 -newkey rsa:2048 \
  -keyout "$CERTS_DIR/client-2048.key" \
  -out    "$CERTS_DIR/client-2048.crt" \
  -days 3650 -nodes -subj "/CN=betfair"
```

**Flag-by-flag:**

| Flag | Meaning |
|---|---|
| `req -x509` | Create a self-signed X.509 certificate (Betfair doesn't need a CA signature) |
| `-newkey rsa:2048` | Generate a new 2048-bit RSA key pair |
| `-keyout` | Output path for the private key |
| `-out` | Output path for the public cert |
| `-days 3650` | Valid for 10 years — long enough that you won't have to rotate mid-project |
| `-nodes` | "No DES" — do not encrypt the private key with a passphrase (required, since headless runs can't prompt) |
| `-subj "/CN=betfair"` | Non-interactive subject line; skips the prompt tour |

**File naming matters.** `betfairlightweight` looks for files named exactly **`client-2048.crt`** and **`client-2048.key`** in the directory you point at. Don't rename them.

Verify the files and permissions:

```bash
ls -l "$CERTS_DIR"
# -rw-r--r--  client-2048.crt
# -rw-------  client-2048.key   <-- note mode 600, readable only by you
```

#### Step 2 — Upload the `.crt` to Betfair

1. Log in to [betfair.com](https://www.betfair.com) in a browser
2. Navigate to **My Account → My Security → Automated Login**
3. You'll be prompted to re-authenticate via 2FA
4. Click **Upload Certificate** and select **only** `client-2048.crt` (never upload the `.key`)
5. Betfair will display the SHA-256 fingerprint of the uploaded cert — you can cross-check it locally with:

   ```bash
   openssl x509 -in "$CERTS_DIR/client-2048.crt" -noout -fingerprint -sha256
   ```

6. Confirm the fingerprints match, then save

Enrollment is immediate — there is no waiting period.

#### Step 3 — Tell the app where the certs live

Add the `certs_dir` field to the `betfair` section of your `credentials.json`:

```json
{
  "betfair": {
    "username": "...",
    "password": "...",
    "app_key": "...",
    "certs_dir": "/absolute/path/to/secrets/certs"
  }
}
```

The directory must contain both `client-2048.crt` and `client-2048.key`. Tilde expansion (`~/...`) is supported.

#### Step 4 — Verify with `auth-test`

```bash
python -m betfair_results_downloader auth-test
```

Expected output on success:

```
Betfair auth-test (cert-based non-interactive login)
------------------------------------------------------------
Credentials source : /path/to/credentials.json
  username         : jo****ne
  app_key          : Ab************YZ
  certs_dir        : /path/to/secrets/certs
  client-2048.crt  : OK
  client-2048.key  : OK

Attempting login()...
OK: session_token obtained (length=44, masked=Ab****...****Cd)
OK: logout() clean
```

Exit code `0` means everything works. Any non-zero exit prints an actionable error.

#### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `betfair.certs_dir does not exist or is not a directory` | Path typo or wrong machine | Check the path resolves with `ls "$certs_dir"` |
| `Cert pair incomplete in <dir>. Missing: client-2048.key` | Key file got renamed, deleted, or never generated | Re-run the `openssl` command, or confirm the filename is exactly `client-2048.key` |
| `APIError: INVALID_USERNAME_OR_PASSWORD` | Wrong Betfair credentials, OR cert not yet enrolled on the account | Double-check username/password; re-verify the `.crt` upload in Betfair account settings |
| `APIError: CERT_AUTH_REQUIRED` | Account has cert login required but `auth-test` received only username/password | Confirm `certs_dir` is set in `credentials.json` and points at the directory containing both files |
| `FileNotFoundError: /certs` | `certs_dir` is unset and the library is falling back to its `/certs` default | Add `certs_dir` to the `betfair` block in `credentials.json` |
| 2FA prompt during upload step | Expected | Complete the 2FA challenge — Betfair requires it for every cert upload |

---

### Azure SQL Publishing

Azure publishing is **opt-in and safe by default**. It requires multiple explicit actions before any database writes can occur.

#### Safety gates (current GUI mode)

All of these must be true before any row is written to `dbo.MarketResults`:

1. `user.enable_azure_sql: true` in credentials
2. `user.dry_run: false` in credentials
3. In the GUI: tick the unlock checkbox
4. In the GUI: type `PUBLISH` exactly
5. In the GUI: confirm the final modal dialog after reviewing the prep summary

If any step is missing, the run completes as a dry run with no database writes.

#### Publish-only flow

The GUI has a **Publish to Azure** button that reads the existing canonical CSV and syncs it incrementally to Azure without re-downloading from Betfair. Same safety gates apply.

#### Scope restriction

Azure publishing is restricted in code to:

- **Horse Racing** (`eventTypeId = 7`)
- **Greyhound Racing** (`eventTypeId = 4339`)

Other sports are downloaded and written to CSV but excluded from Azure uploads.

#### Incremental sync model

- Sync key: `(UserID, MarketID)`
- Inserts new rows; updates changed rows; **never deletes**
- DB-only rows (present in Azure but not in the current dataset) are left untouched
- A filtered unique index is enforced per user to prevent duplicates

#### Azure Tools (recovery)

The GUI exposes a set of user-scoped recovery tools accessed via **Azure Tools**:

- Duplicate audit (read-only)
- Scoped backup export
- UserID normalization (padding fix)
- Scoped unique index creation/verification
- Emergency cleanup wizard (backup → wipe user rows → index → re-audit)

These exist for recovery, not routine use.

---

### Google Sheets Publishing

Market-level results can also be published to a Google Sheet for shared dashboards and reporting. This is a separate output channel from Azure SQL — both read from the same canonical CSV and aggregate independently.

#### Setup

1. Create a Google Cloud service account with Sheets API access
2. Share your Google Sheet with the service account email
3. Run the setup script:

   ```bash
   python scripts/setup_sheets.py
   ```

   This adds the `google_sheets` block to your `credentials.json` with `sheet_name` and `service_account_path`.

4. Test manually:

   ```bash
   python -m betfair_results_downloader publish-sheet
   ```

#### How it works

- The canonical CSV is aggregated to one row per market (sum of bet-level profits, bet count, last settled date)
- Markets are filtered through an **approval workflow**: racing + soccer are auto-approved; other sports require manual approval (because they may take weeks to fully settle)
- The upload is **incremental** — new markets are inserted, changed markets (profit updated as more bets settle) are updated, unchanged markets are left alone
- The sheet is always sorted by SettledDate descending

#### Scheduled auto-publish

When `google_sheets` is configured in `credentials.json`, every scheduled `run` automatically publishes approved markets after downloading and writing CSVs. Non-racing markets pending approval are logged but not uploaded.

---

### Reporting Dashboard

A local Streamlit app for analyzing the canonical CSV:

```bash
streamlit run src/betfair_results_downloader/reporting_app.py
```

Features:

- Reads **local CSVs only** — no Azure dependency
- UTC → Australia/Sydney timezone conversion
- Sunday–Saturday weekly aggregation
- Sport filtering (Horses, Greyhounds)
- Daily and weekly P&L views
- KPI summaries, CSV export, cached loading

---

## CLI Reference

As of **0.5.0**, the package exposes a CLI entry point:

```bash
python -m betfair_results_downloader <command> [options]
```

Or, after `pip install -e .`, the console script:

```bash
betfair-results <command> [options]
```

### `auth-test`

**Status:** ✅ Implemented

Verifies that cert-based non-interactive Betfair login works with your current `credentials.json`. Loads credentials via the standard resolver, attempts `client.login()` using the cert pair at `betfair.certs_dir`, and reports the outcome with all secrets masked.

```bash
python -m betfair_results_downloader auth-test
```

Exit codes: `0` success · `1` auth/runtime failure · `2` config/file-missing failure.

See [Betfair Certificate Enrollment — Step 4](#step-4--verify-with-auth-test) for example output and troubleshooting.

### `run`

**Status:** ✅ Implemented

Runs one scheduled download for the current day.

```bash
python -m betfair_results_downloader run
```

**What it does:**

1. Checks today's success marker (`outputs/last_success_YYYY-MM-DD.marker`) — skips silently if today's data has already been covered.
2. Computes the backfill window via gap detection (see [Gap Detection](#gap-detection-logic)).
3. Downloads cleared orders using cert-based auth (chunked by `schedule.chunk_days`).
4. Enriches with market catalogue (uses cache).
5. Writes canonical + snapshot CSVs.
6. Optionally publishes to Azure SQL (see [Azure Publish Safety Gates — Scheduled Mode](#azure-publish-safety-gates-scheduled-mode)).
7. On success: upserts `dbo.ScheduleState`, writes today's success marker, appends to `run_history.jsonl`.

Exit codes: `0` = success or already-skipped · `1` = failure.

Output: structured log lines to stdout (human-readable, one-per-event format).

### `backfill`

**Status:** ✅ Implemented

Downloads an explicit date range. No skip-marker check; does not advance `LastCoveredDateUtc`.

```bash
python -m betfair_results_downloader backfill --from YYYY-MM-DD --to YYYY-MM-DD
```

Both `--from` and `--to` are required and inclusive. Azure publish gates apply.

Exit codes: `0` = success · `1` = failure · `2` = bad arguments.

### `schedule`

**Status:** ✅ Implemented

Manages the platform scheduled job that runs `betfair-results run` automatically.

```bash
python -m betfair_results_downloader schedule install
python -m betfair_results_downloader schedule status
python -m betfair_results_downloader schedule uninstall
python -m betfair_results_downloader schedule logs [--tail N]
```

**Optional install overrides:**

```bash
# Override the primary run time
python -m betfair_results_downloader schedule install --time 07:00

# Override retry windows (comma-separated)
python -m betfair_results_downloader schedule install --time 07:00 --retries 10:00,20:00
```

The Python interpreter used is `sys.executable` (the one running the command), so run from your activated venv to ensure the correct interpreter is embedded in the scheduled job.

Exit codes: `0` = success · `1` = failure.

### `publish-sheet`

**Status:** ✅ Implemented

Aggregates the canonical CSV to market level and publishes approved markets to a Google Sheet. Uses an incremental sync — new markets are inserted, changed markets (profit updated as more bets settle) are updated, unchanged markets are left alone.

```bash
# Interactive mode — prompts to approve non-racing markets
python -m betfair_results_downloader publish-sheet

# Non-interactive — only uploads auto-approved markets (racing + soccer)
python -m betfair_results_downloader publish-sheet --no-interactive

# Custom tab name
python -m betfair_results_downloader publish-sheet --tab "My Tab"
```

**Approval workflow:**
- **Horse Racing**, **Greyhound Racing**, and **Soccer** markets are auto-approved (they settle quickly).
- Other sports (golf, cricket, tennis tournaments, etc.) require manual approval via the interactive prompt, because they may take days/weeks to fully settle and uploading early shows a false result.
- Approved market IDs are persisted in `approved_markets.json` so approvals survive across runs.

**Scheduled runs:** When `google_sheets` is configured in `credentials.json`, the scheduler's `run` command auto-publishes approved markets after each download (non-interactive mode). Pending markets are logged for manual review.

Exit codes: `0` = success · `1` = failure · `2` = config missing.

---

## Quick Start — Scheduled Mode

**Prerequisites:** Phase 1.1 cert auth must be working (`auth-test` returns 0) and `paths.results_csv_dir` must be set in `credentials.json`.

1. Enable scheduling in `credentials.json`:

   ```json
   "schedule": {
     "enabled": true,
     "timezone": "Australia/Sydney",
     "primary_time": "06:00",
     "retry_times": ["09:00", "19:00", "23:00"]
   }
   ```

2. Test a one-off run:

   ```bash
   python -m betfair_results_downloader run
   ```

3. Install the platform scheduler:

   ```bash
   python -m betfair_results_downloader schedule install
   python -m betfair_results_downloader schedule status
   ```

4. To remove:

   ```bash
   python -m betfair_results_downloader schedule uninstall
   ```

---

## Platform Notes

### Windows (Task Scheduler)

- **Task name:** `BetfairResultsScheduler`
- **Installed via:** `schtasks /Create /XML` — requires no admin rights for current-user tasks
- **Uses `pythonw.exe`** (not `python.exe`) to suppress the console window flash on each run
- **View/manage:** Task Scheduler GUI (`taskschd.msc`) or `schtasks /Query /TN BetfairResultsScheduler`
- **Logs:** `outputs/run_history.jsonl` relative to repo root

### Linux (systemd --user)

- **Unit files:** `~/.config/systemd/user/betfair-results.service` and `.timer`
- **Advantage over cron:** `Persistent=true` in the timer ensures missed runs (machine off) are retried on next login
- **View status:** `systemctl --user status betfair-results.timer`
- **View logs:** `journalctl --user -u betfair-results -n 50`

### Linux (cron fallback)

Used when systemd is not available (e.g. older distros, containers).

- **Identified by marker comment:** `# BETFAIR_RESULTS_SCHEDULER` in crontab
- **Idempotent install:** re-running `schedule install` replaces the existing entry
- **View crontab:** `crontab -l`

### macOS (launchd)

- **Plist location:** `~/Library/LaunchAgents/com.betfair.results.scheduler.plist`
- **Loaded agent:** `launchctl list | grep com.betfair.results`
- **Logs:** `outputs/launchd.out.log` and `outputs/launchd.err.log` (relative to repo root)
- **On sleep/wake:** launchd will run missed jobs when the machine wakes. If `today` is already marked via the success marker, the run is skipped automatically.
- **Re-install after credential change:** run `schedule uninstall && schedule install` to pick up updated credentials.

---

### Gap Detection Logic

The backfill window is computed in three steps:

1. **Azure `dbo.ScheduleState`** — reads `LastCoveredDateUtc` for this user from Azure SQL (requires `user.enable_azure_sql=true` and working `pyodbc`). Most authoritative source.
2. **Canonical CSV** — reads the maximum `settledDate` directly from `cleared_orders_cleaned.csv` in the resolved results directory (independent of the GUI's `run_state.json`).
3. **Cold-start fallback** — `today - max_backfill_days`.

In all cases the `from_date` is pulled back by `min_coverage_overlap_days` for safety re-pull, then capped at `max_backfill_days` before today.

### Azure Publish Safety Gates — Scheduled Mode

All four gates must be open for the scheduler to write to Azure SQL:

| Gate | Credential/Config key | Description |
|---|---|---|
| 1 | `user.enable_azure_sql = true` | Master Azure toggle |
| 2 | `user.dry_run = false` | Second safety gate |
| 3 | `schedule.publish_to_azure = true` | Scheduler-level toggle |
| 4 | `schedule.allow_azure_publish = true` | Explicit scheduler opt-in |

If any gate is closed, CSV outputs are written and state is advanced normally, but Azure publishing is skipped with a log message.

---

## Configuration Reference

Full annotated `credentials.json` schema. Fields marked **required** are mandatory for the feature they belong to; fields marked *(new in 0.5.0)* were added for cert login.

### `betfair` (required)

| Field | Type | Default | Required | Notes |
|---|---|---|---|---|
| `username` | string | — | ✅ | Betfair Exchange account username |
| `password` | string | — | ✅ | Betfair Exchange account password |
| `app_key` | string | — | ✅ | Registered Betfair app key |
| `certs_dir` | string | `""` | Only for `auth-test` / scheduled mode *(new in 0.5.0)* | Absolute path to a directory containing `client-2048.crt` and `client-2048.key` |

### `user` (required)

| Field | Type | Default | Required | Notes |
|---|---|---|---|---|
| `user_id` | string | `"YourUserName"` | ✅ | Display name used in logs and GUI |
| `db_user_id` | string | *(falls back to `user_id`)* | Only if publishing to Azure | Explicit UserID key for the `MarketResults` table |
| `days` | integer | `7` | ✅ | Default lookback window in days (GUI) |
| `include_horses` | bool | `true` | ✅ | Include `eventTypeId=7` in downloads |
| `include_greyhounds` | bool | `true` | ✅ | Include `eventTypeId=4339` in downloads |
| `enable_azure_sql` | bool | `false` | ✅ | Master toggle for Azure publishing |
| `dry_run` | bool | `true` | ✅ | Second safety gate — must be `false` to actually write to DB |

### `paths` (required)

| Field | Type | Required | Notes |
|---|---|---|---|
| `results_csv_dir` | string | Recommended | Absolute path to where canonical and snapshot CSVs should be written. When empty, the cross-platform OneDrive resolver (`paths.py`) is used as a fallback. |

### `schedule` (optional — for scheduled automatic downloads)

When `schedule.enabled` is `false` (default), this entire block is ignored and all schedule validation is skipped.

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Master toggle — must be `true` to activate scheduled runs |
| `timezone` | string | `"Australia/Sydney"` | IANA timezone for time interpretation (e.g. `"America/New_York"`) |
| `primary_time` | string | `"06:00"` | Primary daily run time, `HH:MM` format (local time in `timezone`) |
| `retry_times` | string[] | `["09:00", "19:00", "23:00"]` | Additional daily windows if primary run was missed or failed |
| `publish_to_azure` | bool | `true` | Whether the scheduler should attempt Azure SQL publishing |
| `allow_azure_publish` | bool | `false` | Explicit second gate for scheduler Azure writes (see [Safety Gates](#azure-publish-safety-gates-scheduled-mode)) |
| `max_backfill_days` | int | `90` | Maximum days to back-fill in a single run; must be ≤ 365 |
| `chunk_days` | int | `30` | Betfair API window size in days; must be ≤ 90 |
| `min_coverage_overlap_days` | int | `1` | Days of already-covered data to re-pull for safety overlap |
| `log_dir` | string | `""` | Directory for `run_history.jsonl` and success-marker files (defaults to `outputs/` when empty) |
| `history_file` | string | `""` | Override path for `run_history.jsonl` (derived from `log_dir` when empty) |

### `azure_sql` (required only if `user.enable_azure_sql = true`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `server` | string | — | Azure SQL server hostname |
| `database` | string | — | Database name |
| `username` | string | — | SQL auth username |
| `password` | string | — | SQL auth password |
| `driver` | string | `"ODBC Driver 18 for SQL Server"` | ODBC driver name as installed locally |
| `port` | integer | `1433` | Optional |

### `google_sheets` (optional — for Google Sheets publishing)

| Field | Type | Default | Notes |
|---|---|---|---|
| `sheet_name` | string | `""` | Name of the Google Sheet (e.g. `"Betfair Dashboard"`) |
| `service_account_path` | string | `""` | Absolute path to the Google service account JSON file |

When both fields are set, the `publish-sheet` CLI command and the scheduler's auto-publish are enabled. When empty, Sheets publishing is silently skipped.

### Full example

```json
{
  "betfair": {
    "username": "jo.bloggs",
    "password": "hunter2!",
    "app_key": "AbCdEfGhIjKlMnOp",
    "certs_dir": "/Users/me/OneDrive/secrets/certs"
  },
  "user": {
    "user_id": "JoBloggs",
    "db_user_id": "JoBloggs",
    "days": 7,
    "include_horses": true,
    "include_greyhounds": true,
    "enable_azure_sql": false,
    "dry_run": true
  },
  "paths": {
    "results_csv_dir": "/Users/me/OneDrive/BF/Results Database"
  },
  "azure_sql": {
    "server": "myserver.database.windows.net",
    "database": "BettingResults",
    "username": "sqladmin",
    "password": "...",
    "driver": "ODBC Driver 18 for SQL Server"
  },
  "google_sheets": {
    "sheet_name": "Betfair Dashboard",
    "service_account_path": "/path/to/service-account.json"
  }
}
```

The tracked template lives at [`secrets/credentials.template.json`](secrets/credentials.template.json) — the GUI First Run Wizard seeds from it automatically.

---

## Outputs

### CSV outputs (`paths.results_csv_dir`)

- **Canonical CSV** — `cleared_orders_cleaned.csv`. Stable filename, always reflects the latest full dataset. Idempotent updates via `betId` dedupe.
- **Snapshot CSVs** — `cleared_orders_cleaned_YYYY-MM-DD.csv`. Dated copies for historical tracking.

### Enrichment cache (`<results_csv_dir>/.cache/`)

- `market_catalogue_event_cache.csv` — accumulating cache of market catalogue lookups
- `market_catalogue_event_latest.csv` — latest snapshot

Both are git-ignored. Accessible via **Open Artifacts Folder** in the GUI.

**Note on enrichment:** Betfair commonly returns zero market catalogues for already-settled markets. The app will report `"API returned 0 catalogues (common for settled markets). Enriched from cache only."` This is expected behaviour, not an error.

### Publishing outputs

The canonical CSV is the source of truth. Two optional publishing channels consume it independently:

- **Azure SQL** (`dbo.MarketResults`) — aggregated market-level results for horse racing and greyhound racing only. Incremental sync via `(UserID, MarketID)` key. Gated by four safety switches.
- **Google Sheets** (`Market Results` tab) — aggregated market-level results for all approved sports. Incremental sync via `marketId`. Auto-approves racing + soccer; other sports require manual approval.

Both channels can be enabled simultaneously. They read from the same CSV but aggregate and publish independently — disabling one does not affect the other.

---

## Roadmap: Scheduled Automatic Downloads

Automated daily downloads with gap detection, multi-window retry, and cross-platform installers. Rolling out across phased PRs. Each phase ships independently and documents its own additions here.

| Phase | PR | Status | Delivers |
|---|---|---|---|
| 1.1 | ✅ shipped | `ae53e3e` | Cert-based non-interactive auth (`scheduler/auth.py`), chunked date-range download (`fetch_cleared_orders_df_range`), CLI entry point (`auth-test` implemented), `pyproject.toml` dependency fixes |
| 1.1b | ✅ shipped | `eb928ee` | Documentation overhaul for Phase 1.1 features |
| 1.2 | ✅ shipped | `b65e636` | `schedule` config block, `ScheduleConfig` dataclass, schedule validation in `secrets.py`, `credentials.template.json` updated |
| 2.1 | ✅ shipped | `e744cb5` | `dbo.ScheduleState` DDL script, `scheduler/state.py` (read, upsert, JSONL history, marker files) |
| 2.2 | ✅ shipped | `7741d3a` | Gap detection (`scheduler/gap_detector.py`), headless `runner.py`, `run` and `backfill` CLI subcommands |
| 3.1 | ✅ shipped | `7e1d368` | macOS launchd installer, `schedule install/uninstall/status/logs` subcommands, platform dispatch in `installers/__init__.py` |
| 3.2 | ✅ shipped | `41afba9` | Windows Task Scheduler (`schtasks`), Linux systemd --user, cron fallback |
| 4.1 | ⏳ planned | | Optional GUI Scheduling tab |

Full design document (architecture, config schema, safety gates, state model, error handling, open questions) is captured in the project's planning conversation. Summary:

- **Source of truth for "last covered date":** new `dbo.ScheduleState` Azure table + canonical CSV fallback
- **Retry pattern:** primary run at user-configured time (default `06:00`) with automatic retries at `09:00`, `19:00`, `23:00`; each window checks whether the day has already been covered and skips silently if so
- **Safety:** four-gate Azure publish model (`enable_azure_sql` + `dry_run=false` + `schedule.publish_to_azure` + `schedule.allow_azure_publish`)
- **Auth:** cert-based only — shipped in Phase 1.1, verified via `auth-test`
- **Concurrency:** two-machine concurrent runs are accepted as safe due to full idempotency (`betId` dedupe + `(UserID, MarketID)` incremental sync)
- **Backfill:** `python -m betfair_results_downloader backfill --from YYYY-MM-DD --to YYYY-MM-DD` for manual catch-up

---

## Repository Structure

```
src/betfair_results_downloader/
  gui_app.py              # Tkinter GUI (official interactive runner)
  run.py                  # Shared pipeline entry used by GUI + CLI
  pipeline.py             # 4-phase orchestration: download → enrich → CSV → Azure
  downloader_core.py      # Betfair API calls, enrichment, chunked range download
  azure_publish.py        # Azure SQL incremental sync plan + apply
  azure_remediation.py    # User-scoped Azure recovery tools
  sheets_publish.py       # Google Sheets incremental market-level publisher
  market_approval.py      # Sport-based approval workflow for Sheets publishing
  csv_utils.py            # Canonical CSV dedupe + atomic write
  recommend.py            # Lookback recommendation from existing CSV (GUI)
  audit.py                # Settled-date gap analysis
  state.py                # GUI run state persistence (run_state.json)
  run_logging.py          # Per-run log transcript writer
  secrets.py              # Credentials resolver + validator
  config.py               # DownloaderConfig + ScheduleConfig dataclasses
  paths.py                # Cross-platform OneDrive path resolver
  __main__.py             # CLI entry point (auth-test, run, backfill, schedule, publish-sheet)
  scheduler/              # Scheduled-downloads package
    auth.py               # build_api_client() — cert-based login
    date_windows.py       # chunk_date_range() — safe API windowing
    gap_detector.py       # compute_backfill_window() — Azure/CSV/cold-start
    runner.py             # run_scheduled() / run_backfill() — headless pipeline
    state.py              # ScheduleState read/upsert, JSONL history, markers
    installers/           # Platform-specific scheduler installers
      launchd.py          # macOS LaunchAgent plist
      taskscheduler.py    # Windows Task Scheduler XML
      systemd_user.py     # Linux systemd --user units
      cron.py             # Linux cron fallback
  reporting/              # Streamlit dashboard (IO, schema, filters, pages)
  reporting_app.py        # Streamlit entry point

secrets/
  credentials.template.json   # committed seed template
  credentials.json            # git-ignored; created by First Run Wizard
  credentials.location.json   # optional pointer to an external credentials file

tests/                    # Pytest suite
scripts/                  # Standalone Azure remediation scripts
outputs/                  # Enrichment cache + scheduler artifacts (git-ignored)
```

---

## Troubleshooting

### GUI

- **First Run Wizard keeps appearing** — check that `secrets/credentials.json` (or the path in `credentials.location.json`) exists and contains valid JSON.
- **"API returned 0 catalogues"** — expected for already-settled markets; enrichment falls back to the cache.
- **Azure publish button greyed out** — verify all four GUI safety gates are satisfied (see [Azure SQL Publishing](#azure-sql-publishing)).

### Cert authentication

See the [Cert Enrollment Troubleshooting table](#troubleshooting) above.

### Python environment

- **`pyodbc` import fails on macOS** — install the ODBC driver first: `brew install unixodbc` then `brew tap microsoft/mssql-release && brew install msodbcsql18`. Only required if using Azure publishing.
- **`ModuleNotFoundError: betfair_results_downloader`** — run `pip install -e .` from the repo root with your venv activated.

---

## Safety Notes

- Real credentials and outputs are **never committed**. `.gitignore` excludes `secrets/credentials.json`, `secrets/credentials.location.json`, `outputs/`, and all `*.csv` / `*.parquet` / `*.xlsx` files.
- Keep this behaviour intact when adding new files.
- **Never share, commit, or upload your `client-2048.key`** file. Treat it like a password.

---

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for developer setup, quality checks, and release steps.

---

## Disclaimer

This project is for **personal analytics and learning**. You are responsible for compliance with Betfair's terms of service and any applicable laws or regulations in your jurisdiction.
