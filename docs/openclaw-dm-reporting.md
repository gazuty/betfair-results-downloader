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
- **Yesterday** is the full previous calendar day, `12:00 AM` to `12:00 AM`
- **Today** starts at the current day at `12:00 AM`
- **Month to date** starts at `12:00 AM` on the 1st of the current month
- **Year to date** starts at `12:00 AM` on 1 January

Week to date, Today, Month to date and Year to date end at the report
timestamp. Yesterday is a closed day, so the 6:00 AM and 7:35 PM reports show
the same figures for it, and it ignores the week boundary: on a Sunday it is
the previous week's Saturday. Month to date and Year to date exist because
commission is only meaningful summed over a period, not per bet — see
[Commission](#commission) below.

All sports are included. Each breakdown always shows **Horses** and
**Greyhounds** (even at `$0.00`), followed by one line per other sport that
had a settlement in the window, ordered by absolute profit with ties broken
alphabetically. Sport names come from the expanded Betfair event-type map in
`reporting/schema.py`; an id not in that map renders as `Other (<id>)`.

## Settlement status gating

Betfair settles the losing runners of an outright or tournament market as
they are eliminated, so `listClearedOrders` can deliver "settled" bets for a
market whose outcome is still open — a tournament-winner market can settle
lay bets over several timestamps spanning weeks while most of its runners
are still active. Cleared orders carry no market status, so those rows are
indistinguishable from a finished market on their own.

The pipeline (`market_status.py`, run after the CSV write and before Azure
publishing, in both `run` and `backfill`) asks Betfair's `listMarketBook`
about every market it has reason to care about and records what it hears in
`<results_csv_dir>/.cache/market_settlement_status.csv`: a `status` of OPEN
or SUSPENDED means the market is still only partially settled; CLOSED means
it is fully settled. A CLOSED market stays in `listMarketBook` for a
variable period (most racing markets were still returned two weeks after
settlement, a minority were gone within a day), after which it is simply
absent — open markets are always returned, so for an id that came from a
cleared order, absence is recorded as CLOSED too. That recording is
provisional: a market closed by absence is asked about again on each run
for the next 48 hours, so a single dropped row in a response cannot
permanently mark a live outright as fully settled. Because retention cannot
be relied on, the check runs in every pipeline run — including a run whose
download returned no rows, which is usually the run that finally sees a
long-open outright CLOSED.

This is essentially a non-racing phenomenon: no horse or greyhound market
has been observed to settle over more than a day, while most soccer,
tennis, golf, AFL, cycling, GAA, and politics outright markets do.

`dm-report` uses that file to hold every non-CLOSED market's rows out of
every total in Week to date and Today, and summarises them in a final
"Pending" section instead:

```text
Pending (partially settled, not counted above)
• 2 markets, $17.40 settled so far — each counts in full on the day it closes
```

or `• None` when nothing is pending. The pending amount is deliberately not
bounded by the week: it is everything Betfair has settled on those markets
since they opened, and the whole amount lands in Today on the day the
market closes.

A market that was seen pending and is later observed CLOSED has all of its
bets counted on the day the pipeline first observed it CLOSED (Sydney
time) — not scattered across the days on which Betfair actually settled
each leg — so early-settled legs of an outright are not lost in reports
that already went out for those weeks. A market that was CLOSED the first
time the pipeline ever looked at it (racing, match-odds markets) keeps its
original `settledDate`, so racing reporting is unchanged. If a market stays
partially settled for longer than `user.canonical_archive_months`, its early
legs sit in the yearly archives rather than the canonical; the report reads
them back for every market that was ever seen pending, so both the Pending
amount and the close-day total cover the whole market.

A market with no row in the status file at all — the file is missing,
unreadable, or the status step hasn't reached it yet — counts as final,
exactly as it did before this feature existed. A status-step outage
therefore degrades the report to its old (pre-gating) behaviour rather than
reporting `$0.00` for the affected markets.

`dm-report --csv PATH` looks for the status file at `<directory of
PATH>/.cache/market_settlement_status.csv`. If that file exists but can't be
read, the report logs a warning and treats it as absent (same
degrade-to-old-behaviour rule).

## Commission

The per-bet rows the downloader stores carry no commission at all — Betfair
charges it per market, on the net winnings of the whole market, so there is
no per-bet figure to show. `commission.py` reads `listClearedOrders` with
`groupBy=MARKET` instead, which returns one row per market with the gross
profit (equal to the sum of the per-bet profits) and the commission charged
on that market (`0.0` on a losing or still-pending market). The pipeline
step (run after the settlement-status step and before Azure publishing, in
both `run` and `backfill`, on every run including an empty download) reads
that grouped endpoint for the run's window and persists the observations to
`<results_csv_dir>/.cache/market_commission.csv`.

The grouped row sits at the market's *latest settled leg*, so `dm-report`
attributes a market's whole commission to the window containing that leg,
using the `settledDateUtc` the store recorded rather than the latest
canonical row: a report rendered for an earlier time (`--at`) has dropped
the rows after its cutoff, and a figure read after the cutoff is reported
as unknown for that time instead of landing on an earlier leg.
Gross stays per bet row, exactly as before: a racing market whose legs
straddle midnight has yesterday's gross in Yesterday and today's in Today,
with the commission in Today. A market
that is still only partially settled reports commission `0.0` until it
closes — that reading is a placeholder, not the answer — so every market the
status file has ever seen pending is asked about again by explicit id until
a read is taken after the observed close. Grouped rows are available for
about a year back, the same depth as the per-bet rows, so
`backfill-commission` can fill the whole canonical after an upgrade.

Commission rates vary by market and are not recoverable once a market has
left the catalogue, so the store only ever holds amounts; `dm-report`
derives the effective percentage at the sport-and-period level, never per
market. A window's commission percentage is commission divided by gross
profit for that sport and period, shown as `n/a` when gross is zero or a
loss — Betfair charges commission on winning markets only, so the ratio has
no meaning for a losing period — and also when any market in that line is
commission unknown, since a rate over a partly missing numerator would read
as real. Over a single day the ratio is also distorted by markets whose legs
straddle midnight (gross is per leg, commission lands with the latest leg);
Month to date and Year to date are where it settles to the effective rate.

A window the step missed (Betfair unreachable at 06:00, say) heals itself:
on every run the step also reads, by explicit id, every canonical market
settled in the last fortnight that has no usable row in the store, newest
first and capped at 2,000 per run. A stored row that shows a winning market
with zero commission counts as unread for that purpose: Betfair charges on
every winning market, so that reading is the pre-close placeholder of a
market the status step had not yet recorded as pending (it had failed that
run), and it is read again until the final figure appears.
`backfill-commission` does the same for any range at once.

A market counts as **commission unknown** when the store has no row for it;
when — for a market the status file ever saw pending — its row was read
before the close was observed (a stale `0.0` placeholder); when the store
row predates a leg the canonical already holds; or when the store row's
settlement is after the report's `--at` cutoff. Unknown markets
contribute `$0.00` to their section's commission and net, and the section
adds a line saying how many markets are unknown:

```text
• ⚠️ Commission unknown for 2 markets (counted as $0.00)
```

`dm-report --csv PATH` looks for the commission store at `<directory of
PATH>/.cache/market_commission.csv`. If that file is missing or can't be
read, every market is reported commission unknown rather than
commission-free.

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

To backfill the commission store itself (e.g. after upgrading, or to fill a
gap) without touching bets or the canonical:

```bash
python -m betfair_results_downloader backfill-commission --from YYYY-MM-DD --to YYYY-MM-DD
```

## Example output

```text
Betfair results update

Saturday 6 June, 6:00 AM

Week to date (since Sunday 12:00 AM)
• Total: gross $412.35, commission $32.51 (7.9%), net $379.84
• Horses: gross $355.10, commission $28.41 (8.0%), net $326.69
• Greyhounds: gross $57.25, commission $3.44 (6.0%), net $53.81
• Tennis: gross $12.25, commission $0.66 (5.4%), net $11.59
• Soccer: gross -$12.25, commission $0.00 (n/a), net -$12.25

Yesterday (Friday 5 June)
• Total: gross $121.80, commission $9.18 (7.5%), net $112.62
• Horses: gross $97.30, commission $7.78 (8.0%), net $89.52
• Greyhounds: gross $12.25, commission $0.74 (6.0%), net $11.51
• Tennis: gross $12.25, commission $0.66 (5.4%), net $11.59

Today (since 12:00 AM)
• Total: gross $48.90, commission $3.66 (7.5%), net $45.24
• Horses: gross $36.40, commission $2.91 (8.0%), net $33.49
• Greyhounds: gross $12.50, commission $0.75 (6.0%), net $11.75

Month to date (since Monday 1 June)
• Total: gross $1,845.60, commission $138.79 (n/a), net $1,706.81
• Horses: gross $1,502.20, commission $120.18 (8.0%), net $1,382.02
• Greyhounds: gross $310.15, commission $18.61 (6.0%), net $291.54
• Golf: gross $33.25, commission $0.00 (n/a), net $33.25
• ⚠️ Commission unknown for 1 market (counted as $0.00)

Year to date (since Thursday 1 January)
• Total: gross $21,960.85, commission $1,701.49 (7.7%), net $20,259.36
• Horses: gross $18,960.40, commission $1,516.83 (8.0%), net $17,443.57
• Greyhounds: gross $2,910.70, commission $174.64 (6.0%), net $2,736.06
• Tennis: gross $185.50, commission $10.02 (5.4%), net $175.48
• Soccer: gross -$95.75, commission $0.00 (n/a), net -$95.75

Pending (partially settled, not counted above)
• 2 markets, $17.40 settled so far — each counts in full on the day it closes
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

- The `06:00` report is primarily valuable for the *week-to-date* and *yesterday* sections: it is the first full picture of the previous day.
- The `06:00` *today* section may legitimately be `$0.00` if nothing has settled since midnight Sydney time.
- The `06:00` *today* section may also legitimately show an outright market sitting in the Pending section instead of a total — Betfair settling early legs of a still-open tournament is expected, not a bug.
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
- `tests/test_report_settlement_gating.py` — pending-market gating, close-date attribution, degrade-to-old-behaviour on a missing/unreadable status file
- `tests/test_market_status.py` — `market_status.py`: `listMarketBook` batching/absence handling, market selection, merge/prune of the status file
- `tests/test_runner_market_status.py` — the pipeline step's wiring into `run`/`backfill`, including its non-fatal failure handling

Those tests verify:

- Sunday-start week logic
- same-day totals
- the full-previous-day Yesterday window, including across the week boundary
- Sydney timezone handling
- all-sports coverage, ordered by absolute profit
- partially-settled markets held out of totals and summarised in the Pending section
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
