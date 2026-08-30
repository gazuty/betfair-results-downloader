# OpenClaw DM Reporting

This feature adds a repo-native reporting path for rendering the daily DM
report. It was originally built for OpenClaw to deliver; `dm-report
--post-slack` now offers direct delivery as well. See
[Direct Slack delivery](#direct-slack-delivery) for why, and for the trade-off
that change makes against the design intent below.

## Design intent

The repository remains responsible for:

- reading the canonical cleared-orders CSV
- normalizing timestamps into Australia/Sydney time
- computing reporting windows
- formatting a stable human-readable report

OpenClaw remains responsible for:

- deciding when to run the report
- delivering the message to the user in the active chat channel

This keeps messaging transport outside the downloader runtime and avoids
storing chat-delivery logic in the core pipeline.

That separation still holds for the pipeline itself: `downloader_core` and
`scheduler/runner` contain no delivery logic, and plain `dm-report` still
only prints to stdout. Delivery is opt-in, confined to `slack_notify.py`,
and reached only via the `--post-slack` flag described below.

## Reporting windows

For a report run at a given Sydney-local timestamp:

- **Week to date** starts at the most recent Sunday at `12:00 AM`
- **Today** starts at the current day at `12:00 AM`

Both windows end at the report timestamp.

Only the following sports are included:

- Horses (`eventTypeId=7`)
- Greyhounds (`eventTypeId=4339`)

Other sports are excluded from the DM summary.

## CLI

Render the report body from the configured results directory:

```bash
python -m betfair_results_downloader dm-report
```

Render the report for a specific timestamp:

```bash
python -m betfair_results_downloader dm-report --at 2026-06-06T21:00:00+10:00
```

Render the report from a specific CSV and show the source path:

```bash
python -m betfair_results_downloader dm-report --csv /path/to/cleared_orders_cleaned.csv --show-source
```

If `--at` is provided without a timezone offset, it is interpreted as `Australia/Sydney` local time.

When `--csv` is not provided, `dm-report` prefers the exact canonical filename `cleared_orders_cleaned.csv` when present. If that file is absent, it falls back to the best discovered cleared-orders CSV in the results directory.

## Example output

```text
Betfair results update

Saturday 6 June, 6:00 AM

Week to date (since Sunday 12:00 AM)
• Total profit: $412.35
• Horses: $355.10
• Greyhounds: $57.25

Today (since 12:00 AM)
• Total profit: $48.90
• Horses: $36.40
• Greyhounds: $12.50
```

## Recommended operational model

Use this command as the data/report-generation layer, then have OpenClaw trigger it on a schedule and send the returned text to the user.

### Source of truth split

**Downloader cadence** should be owned by the operating-system scheduler, not by the messaging/reporting layer.

Current recommended downloader cadence on macOS launchd:

- `05:30` Australia/Sydney
- `09:00` Australia/Sydney
- `19:00` Australia/Sydney
- `23:00` Australia/Sydney

**Reporting cadence** should be owned by OpenClaw:

- `06:00` Australia/Sydney
- `19:35` Australia/Sydney

Downloader scheduler semantics are now explicitly dual-tracked:

- trigger times are interpreted in `schedule.timezone`
- all four scheduled runs perform timestamp-based incremental download attempts
- UTC coverage is still recorded alongside local coverage for auditability and interoperability
- the primary incremental checkpoint is the latest confirmed settled timestamp, with a default 2-hour overlap

That preserves a clean separation of concerns:

- downloader repo generates and refreshes data
- downloader repo renders the report body via `dm-report`
- OpenClaw handles user messaging and delivery timing

### Expected semantics

- The `06:00` report is primarily valuable for the *week-to-date* section.
- The `06:00` *today* section may legitimately be `$0.00` if no horse or greyhound settlements exist by that point.
- The `19:35` report is expected to be the more meaningful day-level operational summary.

### Why this design is preferred

- avoids embedding Slack bot delivery into the downloader pipeline
- keeps chat transport concerns out of the data-processing runtime
- makes the report text deterministic and testable from the command line
- allows the downloader schedule and user-facing report schedule to evolve independently

## Validation

The implementation is covered by unit tests in:

- `tests/test_daily_dm_report.py`
- `tests/test_cli_dm_report.py`

Those tests verify:

- Sunday-start week logic
- same-day totals
- Sydney timezone handling
- exclusion of non-horse/greyhound rows
- CLI rendering behavior
- exact canonical CSV preference when present

## Direct Slack delivery

`dm-report --post-slack` posts the rendered report to Slack directly, so a
plain launchd job can deliver it with no agent involved.

### Why this exists

Delivering via an agent turn cost roughly 43k tokens per run to execute one
fixed command and forward its stdout verbatim -- the cron payload itself said
"Do not add commentary -- just deliver the report text". No judgment was
required anywhere in it, and the agent added a failure mode of its own: on
2026-08-29 a run failed at Slack delivery rather than at the data.

This is a deliberate trade against the separation described above. The
judgment is that for a fixed command with no decisions in it, an LLM in the
delivery path costs more reliability than it buys.

### Configuration

Credentials are read from `~/.betfair/slack.json`, falling back to a `slack`
section in credentials.json:

```json
{
  "bot_token": "xoxb-...",
  "channel": "U0000000000",
  "enabled": true
}
```

`channel` may be a channel ID or a user ID; a user ID opens a DM. Create the
file with mode `600`.

The local file takes precedence deliberately. It lives on local disk rather
than in OneDrive, so failures are still announced when credentials.json is
itself the unreadable file -- the failure seen on 2026-08-30, when OneDrive
evicted it and the run died with
`OSError: [Errno 11] Resource deadlock avoided`.

### Failure reporting

With `--post-slack`, failures are posted as well as printed, so a broken run
is announced rather than failing silently into a log. This covers unreadable
or missing credentials, a missing `paths.results_csv_dir`, a malformed
`--at` value, and any error while building the report.

### Scheduling

```xml
<key>ProgramArguments</key>
<array>
  <string>/path/to/repo/.venv/bin/python</string>
  <string>-m</string>
  <string>betfair_results_downloader</string>
  <string>dm-report</string>
  <string>--post-slack</string>
</array>
```

Install as a LaunchAgent with the report times in `StartCalendarInterval`.
Run it after the downloader cadence so the report reflects a completed run.
