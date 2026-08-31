# Betfair Results Downloader

A professional Python application for downloading settled Betfair orders, enriching them with market metadata, writing reliable CSV outputs, and optionally publishing aggregated market-level results to Azure SQL — safe by default.

---

## Features

- **Headless CLI downloader** — `betfair-results run / backfill / audit` with structured logs, idempotent re-runs, and settled-date gap auditing
- **CSV outputs** — canonical file plus dated gzip snapshots, idempotent updates, safe to re-run
- **Data lifecycle management** — automatic snapshot retention, snapshot compression, and yearly archival of old rows keep the results folder small *(new in 0.6.0)*
- **Market metadata enrichment** — cached market catalogue lookups (avoids repeat API calls)
- **Azure SQL publishing** — incremental, non-destructive, multi-gate safety model
- **DM reporting** — repo-native week-to-date and day-to-date summary generation, printed for an external messenger or posted straight to Slack with `--post-slack`
- **Non-interactive cert authentication** — `betfairlightweight` cert-based login for headless use *(new in 0.5.0)*
- **CLI entry point** — `python -m betfair_results_downloader` with `auth-test` subcommand *(new in 0.5.0)*
- **Chunked date-range download** — automatic splitting into safe Betfair settledDateRange windows *(new in 0.5.0)*
- **Scheduled automatic daily downloads** — gap detection, multi-window retry, macOS launchd installer

---

## Quick Start

```bash
git clone https://github.com/<your-org>/betfair-results-downloader.git
cd betfair-results-downloader
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -e .
```

Then:

1. Create your credentials file (see [Credentials file](#credentials-file)) — copy `secrets/credentials.template.json` as a starting point.
2. Enroll a Betfair client certificate (see [Betfair Certificate Enrollment](#betfair-certificate-enrollment)) and verify with `betfair-results auth-test`.
3. Run a one-off download: `betfair-results run` — four phases: download → enrich → CSV → Azure (if enabled).
4. *(Optional)* Install the daily scheduled job: `betfair-results schedule install`.

### Lookback (auto)

Scheduled runs compute their own incremental window via gap detection (see [Gap Detection](#gap-detection-logic)). For manual catch-up over an explicit range use `betfair-results backfill --from YYYY-MM-DD --to YYYY-MM-DD`.

### Run logs

Every run (scheduled or backfill) appends a structured record to
`outputs/run_history.jsonl` (or `schedule.log_dir` when set) — including
failed runs, so the history is a complete operational record. Scheduled runs
also emit human-readable log lines to stdout, captured by the platform
scheduler (e.g. `outputs/launchd.out.log` / `launchd.err.log` on macOS).
Inspect recent activity with `betfair-results schedule logs --tail 50`.

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

The app reads credentials from `secrets/credentials.json` by default. You can store the real file anywhere (recommended: a local, non-cloud-synced directory such as `~/.betfair/`, mode 600 — cloud sync clients can evict the file to an online-only placeholder, which breaks non-interactive login, and their version history retains old copies of your secrets) and point at it via a tiny pointer file.

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

Cert-based login lets the app authenticate to Betfair **without any interactive step** — no browser, no prompts, no session timeouts mid-run. This is required for automated/scheduled use and is the approach Betfair officially supports for bot accounts. All download paths (`run`, `backfill`, scheduled mode) use cert-based auth.

#### What a client certificate is (and why Betfair needs one)

A client certificate is a cryptographic identity card. You generate a matching **public certificate** (`.crt`) and **private key** (`.key`) locally. You upload the `.crt` to your Betfair account — Betfair stores it and trusts any request signed by the matching private key. The `.key` file stays on your machine and must never leave it.

Betfair's documented non-interactive login endpoint (`identitysso-cert.betfair.com/api/certlogin`) requires a mutually-authenticated TLS handshake using this cert pair. The `betfairlightweight` library handles the TLS plumbing automatically once you tell it where your cert directory lives.

> **⚠️ Never share, commit, or upload your `.key` file.** Treat it like a password. Anyone with the `.key` can authenticate as you. If you ever suspect it has leaked, generate a new pair and replace the enrolled cert on your Betfair account immediately.

#### Prerequisites

- Betfair Exchange account with API access
- A registered **app key** (found in your Betfair Developer account)
- **Two-factor authentication enabled** on your Betfair account — Betfair will not accept a certificate upload without it

#### Step 1 — Generate the cert pair

Pick a safe location. Prefer a **local, non-cloud-synced** directory such as `~/.betfair/certs` — cloud sync clients (OneDrive, iCloud) can evict files to online-only placeholders, which breaks non-interactive login until the file is re-downloaded. If you run scheduled downloads on more than one machine, generate or copy the pair to the same local path on each.

```bash
CERTS_DIR="$HOME/.betfair/certs"
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

#### Safety gates

All four gates must be open before any row is written to `dbo.MarketResults` (see [Azure Publish Safety Gates — Scheduled Mode](#azure-publish-safety-gates-scheduled-mode)):

1. `user.enable_azure_sql: true` in credentials
2. `user.dry_run: false` in credentials
3. `schedule.publish_to_azure: true`
4. `schedule.allow_azure_publish: true`

If any gate is closed, the run completes as a dry run with no database writes.

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

#### Azure recovery tools

Removed. `azure_remediation.py` held user-scoped recovery helpers — duplicate
audit, scoped backup, UserID normalisation, dedupe and row deletion — but
nothing imported it, there was no CLI surface, and the wrapper scripts that
once called it had already been deleted.

This README previously described it as "fully covered by tests". It was
covered at roughly 3%: two tests, both on `get_scoped_user_id`, against a
module containing uncommitted `DELETE` statements aimed at production. That
sentence was exactly what you would have relied on before running one of
them.

The module is recoverable from git history if the tooling is ever wanted
again — preferably behind a CLI subcommand, and sharing `azure_publish`'s
UserID predicate rather than its own.

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

Runs one scheduled incremental download.

```bash
python -m betfair_results_downloader run
```

**What it does:**

1. Validates credentials up front (including the `schedule` section) and exits with the collected errors on bad configuration.
2. Computes the incremental download window via gap detection (see [Gap Detection](#gap-detection-logic)).
3. Uses the latest confirmed settled timestamp as the primary checkpoint, with canonical CSV fallback and a configurable safety overlap.
4. Downloads cleared orders using cert-based auth (chunked by `schedule.chunk_days`; chunk windows are half-open and contiguous so no instant falls between chunks).
5. Enriches with market catalogue (uses cache, timeout retry; enrichment failure is non-fatal — CSVs are still written).
6. Writes canonical + gzip snapshot CSVs, archives rows older than `user.canonical_archive_months`, and prunes snapshots beyond `user.snapshot_retention_days`.
7. Optionally publishes to Azure SQL (see [Azure Publish Safety Gates — Scheduled Mode](#azure-publish-safety-gates-scheduled-mode)). A failed publish records the run as `partial`, never as published.
8. On success: upserts `dbo.ScheduleState` with both UTC and scheduler-local coverage dates plus the latest confirmed settled timestamp (monotonic — an empty download keeps the previous checkpoint), writes audit markers, appends to `run_history.jsonl`. Failed runs are also recorded in `run_history.jsonl`.

Exit codes: `0` = success · `1` = failure · `2` = bad configuration.

Output: structured log lines to stdout (human-readable, one-per-event format).

### `backfill`

**Status:** ✅ Implemented

Downloads an explicit date range. No skip-marker check; does not advance scheduler coverage state fields.

```bash
python -m betfair_results_downloader backfill --from YYYY-MM-DD --to YYYY-MM-DD
```

Both `--from` and `--to` are required and inclusive (the final day is covered
through to midnight). Azure publish gates apply.

Exit codes: `0` = success · `1` = failure · `2` = bad arguments or configuration.

### `audit`

**Status:** ✅ Implemented

Reports missing settled-date gaps in the canonical CSV so you know exactly
what to backfill.

```bash
python -m betfair_results_downloader audit [--window DAYS]
```

Scans `cleared_orders_cleaned.csv` in the resolved results directory over the
last `--window` days (default 90 — Betfair's maximum backfill) and prints any
missing calendar-day ranges with ready-to-run backfill suggestions.

### `dm-report`

**Status:** ✅ Implemented

Renders the daily DM report from the local results CSV. By default it only prints the final message body to stdout, for an external messenger to deliver. Pass `--post-slack` to have it post the report to Slack itself, so a plain launchd job can deliver it with no agent involved.

```bash
python -m betfair_results_downloader dm-report
```

Optional overrides:

```bash
# Render for a specific Sydney-local or offset-aware timestamp
python -m betfair_results_downloader dm-report --at 2026-06-06T21:00:00+10:00

# Render from a specific CSV and print the source path
python -m betfair_results_downloader dm-report --csv /path/to/cleared_orders_cleaned.csv --show-source
```

Render and post the report to Slack (used by the `com.betfair.results.dmreport`
LaunchAgent):

```bash
python -m betfair_results_downloader dm-report --post-slack
```

`--post-slack` reads `~/.betfair/slack.json` (falling back to a `slack` section
in credentials.json) and posts failures as well as successes, so a broken
scheduled run is announced rather than failing silently. See
`docs/openclaw-dm-reporting.md` for configuration.

Behavior:

- computes **Week to date** from the most recent Sunday `12:00 AM` Australia/Sydney time
- computes **Today** from the current day `12:00 AM` Australia/Sydney time
- includes only **Horses** and **Greyhounds** in the summary
- prefers the exact canonical CSV `cleared_orders_cleaned.csv` when present
- prints the exact report body intended for user-facing delivery

See `docs/openclaw-dm-reporting.md` for the design rationale, the recommended split between launchd downloader cadence and OpenClaw report cadence, and the expected semantics of the 6:00 am versus 7:35 pm report.

### `schedule`

**Status:** ✅ Implemented

Manages the platform scheduled job that runs `betfair-results run` automatically.

For this deployment, the recommended production model is:

- use the OS scheduler (for example macOS launchd) for downloader cadence
- use a launchd agent running `dm-report --post-slack` for user-facing DM report cadence (OpenClaw remains supported for agent-delivered reports)

The built-in `schedule` command remains available, but it is not the preferred production control plane for the current OpenClaw reporting setup.

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

### Scheduler timezone and coverage semantics

Scheduled runs now track both:
- *UTC coverage date* for interoperability and auditability
- *scheduler-local coverage date* using `schedule.timezone`
- *latest confirmed settled timestamp* for intraday incremental checkpointing

For `Australia/Sydney`, this means all four scheduled runs can perform real download attempts on the same local day. The scheduler no longer suppresses later runs just because an earlier run succeeded.

State and logs therefore expose both day-level and timestamp-level perspectives:
- Azure ScheduleState stores `LastCoveredDateUtc`, `LastCoveredDateLocal`, `LastCoveredTimezone`, and `LastConfirmedSettledAtUtc`
- local marker files still store both `last_success_local_YYYY-MM-DD.marker` and `last_success_utc_YYYY-MM-DD.marker` for audit visibility
- `run_history.jsonl` records `today_local`, `today_utc`, `schedule_timezone`, `from_dt_utc`, `to_dt_utc`, and `last_confirmed_settled_at_utc`

For existing Azure deployments, run the schema upgrade before enabling this build against production:

```bash
python scripts/azure_upgrade_schedulestate.py
```

---

## Platform Notes

### Windows and Linux

Not supported. The `schedule` command works on macOS only.

Task Scheduler, systemd `--user` and cron backends existed but were never
run: this is a single-user macOS tool, and ~1,000 lines of untested
cross-platform code cost more than they bought. `schedule install` and its
siblings now raise a clear error on other platforms.

If you had installed a scheduled job on Windows or Linux with an earlier
version, remove it with the platform's own tooling — `schtasks /Delete /TN
BetfairResultsScheduler`, `systemctl --user disable --now
betfair-results.timer`, or by deleting the `# BETFAIR_RESULTS_SCHEDULER`
line from your crontab. The backends are recoverable from git history if
another platform is ever needed.

### macOS (launchd)

- **Plist location:** `~/Library/LaunchAgents/com.betfair.results.scheduler.plist`
- **Loaded agent:** `launchctl list | grep com.betfair.results`
- **Logs:** `outputs/launchd.out.log` and `outputs/launchd.err.log` (relative to repo root)
- **On sleep/wake:** launchd will run missed jobs when the machine wakes. Later runs are not suppressed by the local success marker, so each wake-triggered scheduled invocation can still perform an incremental catch-up download.
- **Re-install after credential change:** run `schedule uninstall && schedule install` to pick up updated credentials.

---

### Gap Detection Logic

The incremental window is computed in three steps:

1. **Azure `dbo.ScheduleState`** — reads `LastConfirmedSettledAtUtc` for this user from Azure SQL when available.
2. **Canonical CSV** — reads the maximum `settledDate` directly from `cleared_orders_cleaned.csv` in the resolved results directory.
3. **Cold-start fallback** — current scheduler time minus `max_backfill_days`.

In all cases the checkpoint is pulled back by `min_overlap_hours` for safety re-download, then capped at `max_backfill_days` before the current time. This overlap is intended to protect against late-arriving or boundary-timestamp records while remaining safe under CSV dedupe.

### Azure ScheduleState schema maintenance

For a fresh database:

```bash
python scripts/azure_create_schedulestate.py
```

For an existing database created before dual Sydney/UTC scheduler coverage support:

```bash
python scripts/azure_upgrade_schedulestate.py
```

The upgrade is idempotent. It adds:
- `LastCoveredDateLocal`
- `LastCoveredTimezone`
- `LastConfirmedSettledAtUtc`
- `LastSuccessfulDownloadStartedUtc`
- `LastSuccessfulDownloadFinishedUtc`

and backfills legacy rows by copying `LastCoveredDateUtc` into `LastCoveredDateLocal` where needed, with `LastCoveredTimezone` defaulted to `Australia/Sydney` for those backfilled rows.

> **Note:** versions before 0.6.0 shipped a broken upgrade script (the DDL and backfill ran in a single T-SQL batch, so it always failed with `Invalid column name`). If your scheduler logs show `Failed to upsert ScheduleState ... Invalid column name 'LastCoveredDateLocal'`, upgrade and re-run the script — the scheduler otherwise silently falls back to the CSV checkpoint on every run.

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
| `user_id` | string | `"YourUserName"` | ✅ | Display name used in logs |
| `db_user_id` | string | *(falls back to `user_id`)* | Only if publishing to Azure | Explicit UserID key for the `MarketResults` table |
| `enable_azure_sql` | bool | `false` | ✅ | Master toggle for Azure publishing |
| `dry_run` | bool | `true` | ✅ | Second safety gate — must be `false` to actually write to DB |
| `snapshot_retention_days` | integer | `14` | Optional | Number of dated snapshot files to keep; older snapshots are deleted after each run. Set to `0` to disable pruning |
| `compress_snapshots` | bool | `true` | Optional | Write dated snapshots as gzip (`.csv.gz`, ~18× smaller). The canonical CSV is always uncompressed |
| `canonical_archive_months` | integer | `12` | Optional | Rows settled longer ago than this move from the canonical CSV into yearly `cleared_orders_archive_YYYY.csv.gz` files after each run. Set to `0` to disable archival |

> **Note:** downloads always fetch *all* settled orders on the account; sport
> filtering happens only at Azure-publish time, which is fixed in code to
> Horse Racing (7) and Greyhound Racing (4339). The former
> `days` / `include_horses` / `include_greyhounds` fields were GUI-era
> settings with no remaining effect and have been removed (they are ignored
> if still present in an old credentials file).

### `paths` (required)

| Field | Type | Required | Notes |
|---|---|---|---|
| `results_csv_dir` | string | **Required** | Path to where canonical and snapshot CSVs are written (`~` is expanded). Use a **local, non-cloud-synced** directory such as `~/BetfairData` — cloud sync eviction corrupted reads of the canonical twice. When empty, the run fails loudly; the old OneDrive-guessing fallback was removed. |
| `backup_dir` | string | Optional | One-way disaster-recovery copy target (e.g. an OneDrive folder). After each successful CSV write, compressed snapshots and yearly archives are copied there and dated snapshots pruned to `user.snapshot_retention_days`. Backup failures warn (to Slack) but never fail the run. Nothing is ever read back from it. |

### `schedule` (optional — for scheduled automatic downloads)

When `schedule.enabled` is `false` (default), this entire block is ignored and all schedule validation is skipped.

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Master toggle — must be `true` to activate scheduled runs |
| `timezone` | string | `"Australia/Sydney"` | IANA timezone for scheduler-local day semantics and time interpretation (e.g. `"America/New_York"`) |
| `primary_time` | string | `"06:00"` | Primary daily run time, `HH:MM` format (local time in `timezone`) |
| `retry_times` | string[] | `["09:00", "19:00", "23:00"]` | Additional daily windows if primary run was missed or failed |
| `publish_to_azure` | bool | `true` | Whether the scheduler should attempt Azure SQL publishing |
| `allow_azure_publish` | bool | `false` | Explicit second gate for scheduler Azure writes (see [Safety Gates](#azure-publish-safety-gates-scheduled-mode)) |
| `max_backfill_days` | int | `90` | Maximum days to back-fill in a single run; must be ≤ 365 |
| `chunk_days` | int | `30` | Betfair API window size in days; must be ≤ 90 |
| `min_overlap_hours` | int | `2` | Hours to rewind before the last confirmed settled timestamp for safety overlap |
| `log_dir` | string | `""` | Directory for `run_history.jsonl`, `last_success_local_*.marker`, and `last_success_utc_*.marker` files (defaults to `outputs/` when empty) |
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
    "enable_azure_sql": false,
    "dry_run": true,
    "snapshot_retention_days": 14,
    "compress_snapshots": true,
    "canonical_archive_months": 12
  },
  "paths": {
    "results_csv_dir": "~/BetfairData",
    "backup_dir": "/Users/me/OneDrive/BF/Results Database"
  },
  "azure_sql": {
    "server": "myserver.database.windows.net",
    "database": "BettingResults",
    "username": "sqladmin",
    "password": "...",
    "driver": "ODBC Driver 18 for SQL Server"
  }
}
```

The tracked template lives at [`secrets/credentials.template.json`](secrets/credentials.template.json) — copy it to `secrets/credentials.json` (or an external path referenced by `credentials.location.json`) and fill it in.

---

## Outputs

### CSV outputs (`paths.results_csv_dir`)

`results_csv_dir` should be **local disk** (default layout: `~/BetfairData`). If `paths.backup_dir` is set, compressed snapshots and yearly archives are also copied there after each successful run as a one-way disaster-recovery backup.

- **Canonical CSV** — `cleared_orders_cleaned.csv`. Stable filename, always reflects the rolling dataset (last `user.canonical_archive_months` months, default 12). Idempotent updates via `betId` dedupe.
- **Snapshot CSVs** — `cleared_orders_cleaned_YYYY-MM-DD.csv.gz`. Dated gzip copies of the canonical for short-term rollback; only the newest `user.snapshot_retention_days` (default 14) are kept, older ones are deleted after each run.
- **Yearly archives** — `cleared_orders_archive_YYYY.csv.gz`. Rows settled more than `user.canonical_archive_months` ago are moved here from the canonical, deduplicated on `betId`. Read them with `pandas.read_csv` directly (gzip is transparent).

### Enrichment cache (`<results_csv_dir>/.cache/`)

- `market_catalogue_event_cache.csv` — accumulating cache of market catalogue lookups
- `market_catalogue_event_latest.csv` — latest snapshot

Both are git-ignored.

**Note on enrichment:** Betfair commonly returns zero market catalogues for already-settled markets. The app will report `"API returned 0 catalogues (common for settled markets). Enriched from cache only."` This is expected behaviour, not an error.

### Publishing outputs

The canonical CSV is the source of truth. The optional Azure SQL channel consumes it independently:

- **Azure SQL** (`dbo.MarketResults`) — aggregated market-level results for horse racing and greyhound racing only. Incremental sync via `(UserID, MarketID)` key. Gated by four safety switches.

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
| 3.2 | ⛔ removed | `41afba9` | Windows Task Scheduler, Linux systemd --user and cron backends — never run, removed as dead code |

Full design document (architecture, config schema, safety gates, state model, error handling, open questions) is captured in the project's planning conversation. Summary:

- **Source of truth for the incremental checkpoint:** `dbo.ScheduleState.LastConfirmedSettledAtUtc` + canonical CSV fallback
- **Retry pattern:** primary run at user-configured time (default `06:00`) with additional windows at `09:00`, `19:00`, `23:00`; every window performs a real incremental download from the timestamp checkpoint (day-level skip suppression was retired with the intraday checkpoint redesign)
- **Safety:** four-gate Azure publish model (`enable_azure_sql` + `dry_run=false` + `schedule.publish_to_azure` + `schedule.allow_azure_publish`)
- **Auth:** cert-based only — shipped in Phase 1.1, verified via `auth-test`
- **Concurrency:** two-machine concurrent runs are accepted as safe due to full idempotency (`betId` dedupe + `(UserID, MarketID)` incremental sync)
- **Backfill:** `python -m betfair_results_downloader backfill --from YYYY-MM-DD --to YYYY-MM-DD` for manual catch-up

---

## Repository Structure

```
src/betfair_results_downloader/
  downloader_core.py      # Betfair API calls, enrichment, chunked range download
  azure_publish.py        # Azure SQL incremental sync plan + apply
  azure_common.py         # Shared Azure ODBC connection-string builder
  csv_utils.py            # Canonical CSV dedupe + atomic write
  audit.py                # Settled-date gap analysis (backs the `audit` command)
  secrets.py              # Credentials resolver + validator
  config.py               # ScheduleConfig dataclass + event type constants
  paths.py                # Results/backup dir resolution (fail-loud, no guessing)
  backup.py               # One-way compressed backup to paths.backup_dir
  __main__.py             # CLI entry point (auth-test, run, backfill, audit, schedule, dm-report)
  scheduler/              # Scheduled-downloads package
    auth.py               # build_api_client() — cert-based login
    date_windows.py       # chunk_date_range() — safe API windowing
    gap_detector.py       # compute_backfill_window() — Azure/CSV/cold-start
    runner.py             # run_scheduled() / run_backfill() — headless pipeline
    state.py              # ScheduleState read/upsert, JSONL history, markers
    installers/           # Platform-specific scheduler installers
      launchd.py          # macOS LaunchAgent plist
  reporting/              # DM report generation (IO, schema, daily report)

secrets/
  credentials.template.json   # committed seed template
  credentials.json            # git-ignored; copy from the template
  credentials.location.json   # optional pointer to an external credentials file

tests/                    # Pytest suite
scripts/                  # ScheduleState DDL scripts + DM report renderer
outputs/                  # Enrichment cache + scheduler artifacts (git-ignored)
```

---

## Troubleshooting

### Downloads

- **Credentials not found** — check that `secrets/credentials.json` (or the path in `credentials.location.json`) exists and contains valid JSON.
- **"API returned 0 catalogues"** — expected for already-settled markets; enrichment falls back to the cache.
- **Azure publish silently skipped** — verify all four safety gates are open (see [Azure SQL Publishing](#azure-sql-publishing)).

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
