"""
Package CLI entry point.

Subcommands:
  auth-test     — cert-based non-interactive login test
  run           — one scheduled download (gap-detect, fetch, enrich, CSV, Azure)
  retry         — run again if the last attempt did not deliver (hourly job)
  backfill      — explicit date-range download
  backfill-commission — read per-market commission for a date range
  racecard      — capture the forward racing card (full racing market names)
  export-market-results — rebuild the dashboard results feed from disk
  audit         — report missing settled-date gaps in the canonical CSV
  schedule      — install/uninstall/status/logs for the platform scheduled jobs
  dm-report     — render the OpenClaw daily DM report from local CSV results
"""

from __future__ import annotations

import argparse
import sys


def _mask(s: str | None) -> str:
    if not s:
        return "<empty>"
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _cmd_auth_test(_args: argparse.Namespace) -> int:
    """
    Load credentials via the standard resolver and attempt a cert-based
    non-interactive Betfair login. Prints a concise, secret-free report and
    returns 0 on success, non-zero on any failure.
    """
    from .secrets import credentials_path, load_credentials
    from .scheduler.auth import build_api_client, CERT_FILENAME, KEY_FILENAME
    from pathlib import Path

    print("Betfair auth-test (cert-based non-interactive login)")
    print("-" * 60)

    creds_path = credentials_path()
    print(f"Credentials source : {creds_path}")
    if not creds_path.exists():
        print("FAIL: credentials file does not exist at the resolved path.")
        return 2

    try:
        creds = load_credentials(creds_path)
    except Exception as e:
        print(f"FAIL: could not parse credentials.json: {type(e).__name__}: {e}")
        return 2

    bf = (creds.get("betfair") or {}) if isinstance(creds, dict) else {}
    username = (bf.get("username") or "").strip()
    app_key = (bf.get("app_key") or "").strip()
    certs_dir = (bf.get("certs_dir") or "").strip()

    print(f"  username         : {_mask(username)}")
    print(f"  app_key          : {_mask(app_key)}")
    print(f"  certs_dir        : {certs_dir or '<not set>'}")

    if certs_dir:
        certs_path = Path(certs_dir).expanduser()
        crt_exists = (certs_path / CERT_FILENAME).exists()
        key_exists = (certs_path / KEY_FILENAME).exists()
        print(f"  {CERT_FILENAME}  : {'OK' if crt_exists else 'MISSING'}")
        print(f"  {KEY_FILENAME}  : {'OK' if key_exists else 'MISSING'}")

    print()
    print("Attempting login()...")
    try:
        client = build_api_client(bf)
    except RuntimeError as e:
        print(f"FAIL: {e}")
        return 1
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1

    token = getattr(client, "session_token", None)
    if not token:
        print("FAIL: login() returned but session_token is empty.")
        try:
            client.logout()
        except Exception:
            pass
        return 1

    print(f"OK: session_token obtained (length={len(token)}, masked={_mask(token)})")
    try:
        client.logout()
        print("OK: logout() clean")
    except Exception as e:
        print(f"WARN: logout() raised {type(e).__name__}: {e}")
    return 0


def _load_creds_and_schedule(validate: bool = False):
    """
    Load credentials and parse schedule config; shared by the subcommands.

    With ``validate=True``, run the full credential validation (including the
    schedule section when ``schedule.enabled``) and exit 2 with the collected
    errors — download commands should fail fast on bad configuration rather
    than mid-run with a rawer error. Warnings are printed but not fatal.
    """
    from .secrets import credentials_path, load_credentials
    from .config import parse_schedule_config

    creds_path = credentials_path()
    if not creds_path.exists():
        print(f"FAIL: credentials file not found at {creds_path}")
        sys.exit(2)

    try:
        creds = load_credentials(creds_path)
    except Exception as e:
        print(f"FAIL: could not parse credentials.json: {type(e).__name__}: {e}")
        sys.exit(2)

    if validate:
        from .secrets import validate_credentials

        v = validate_credentials(creds)
        for warning in v.warnings:
            print(f"WARN: {warning}")
        if not v.ok:
            print("FAIL: invalid credentials/configuration:")
            for err in v.errors:
                print(f"  - {err}")
            sys.exit(2)

    schedule_cfg = parse_schedule_config(creds)
    return creds, schedule_cfg


def _cmd_run(args: argparse.Namespace) -> int:
    """
    Run one scheduled download.

    Validates credentials, computes the backfill window via gap detection
    (Azure checkpoint → CSV fallback → cold start), downloads/enriches/
    writes CSV, and optionally publishes to Azure SQL (four-gate model).

    Exit codes: 0=success, 1=failure, 2=bad configuration.
    """
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # The scheduled job is unattended: a failure that only reaches
    # launchd.err.log is a failure nobody sees. Announce every non-success
    # outcome, including a credentials load that exits before the run starts.
    try:
        creds, schedule_cfg = _load_creds_for_report(
            args, validate=True, context="scheduled run"
        )
    except SystemExit:
        # A run that never started is still a failed attempt, and the hourly
        # retry only knows about attempts in the history. A credentials file
        # on a cloud-synced disk can be unreadable at 06:00 and fine an hour
        # later. Without the configuration the log directory is unknown, so
        # the record goes to the default one; an installation with a custom
        # schedule.log_dir gets no retry for this failure.
        from .scheduler.runner import _resolve_repo_root
        from .scheduler.state import append_run_history

        append_run_history(
            str(_resolve_repo_root() / "outputs"),
            {
                "status": "failed",
                "message": "Scheduled run could not load its configuration.",
                "retry_wanted": True,
            },
        )
        raise

    return _run_and_announce(args, creds, schedule_cfg)


# Slack accepts far more, but an alert is read on a phone. Errors are already
# shortened where they are raised (error_text.brief_error); this is the
# backstop for anything that slips through.
_ALERT_MAX_CHARS = 3000


def _alert_text(text: str) -> str:
    if len(text) <= _ALERT_MAX_CHARS:
        return text
    return text[:_ALERT_MAX_CHARS].rstrip() + " … (truncated; full text in the log)"


def _retry_job_ready() -> bool:
    """True when the hourly retry job is installed and loaded in launchd."""
    try:
        from .scheduler.installers import get_installer

        status = get_installer().retry_status()
    except Exception:
        # No installer on this platform, or launchctl unavailable.
        return False
    return bool(status.get("installed") and status.get("loaded"))


def _log_dir_for_retries(creds: dict, schedule_cfg) -> str | None:
    """
    Where the run history lives, for wording the alert. Nothing here may
    stop a run: when it cannot be resolved the reason is printed and the
    alert goes out without a failure count.
    """
    try:
        from .scheduler.runner import _resolve_log_dir

        return str(_resolve_log_dir(creds, schedule_cfg))
    except Exception as exc:
        print(f"WARN: could not locate the run history for retries ({exc}).")
        return None


def _retry_note(failures: int, retry_job_ready: bool, recorded: bool = True) -> str:
    """What happens next after a run that asked for a retry."""
    from .scheduler.retry import MAX_FAILURES_IN_A_ROW

    if not recorded:
        # The retry job's only state is the run history. A promise of a
        # retry here would be one nothing is going to keep.
        return (
            "This attempt could not be written to run_history.jsonl (see the "
            "log), so it cannot be counted. Hourly retries stand down while the "
            "history cannot be written; the next attempt is the next scheduled run."
        )
    if failures >= MAX_FAILURES_IN_A_ROW:
        return (
            f"That is {failures} failures in a row: hourly retries have stopped. "
            f"The next attempt is the next scheduled run."
        )
    if not retry_job_ready:
        return (
            f"Failure {failures} of {MAX_FAILURES_IN_A_ROW} in a row. The hourly "
            f"retry job is not installed and loaded (schedule install-retry), so "
            f"the next attempt is the next scheduled run."
        )
    return (
        f"Failure {failures} of {MAX_FAILURES_IN_A_ROW} in a row; it will be "
        f"retried on the hour."
    )


def _run_and_announce(
    args: argparse.Namespace, creds: dict, schedule_cfg, *, retry: bool = False
) -> int:
    """
    Run the scheduled pipeline once and announce the outcome; shared by
    ``run`` and ``retry``.

    Every non-success outcome reaches Slack, shortened, with a line saying
    whether and when it will be retried. A success that follows failures
    is announced once, so the last word in the channel is not a failure.

    The failure counts are read from the history after this attempt has
    recorded itself, never before: two attempts can overlap, and each must
    count what the other has already written.

    Exit codes: 0=success (or a retry that stood down), 1=failure.
    """
    from .error_text import brief_error
    from .scheduler.retry import (
        KIND_RETRY,
        KIND_SCHEDULED,
        claim_attempt,
        failures_before_latest,
        failures_in_a_row,
    )
    from .scheduler.runner import run_scheduled
    from .scheduler.state import append_run_history

    post = getattr(args, "post_slack", True)
    log_dir = _log_dir_for_retries(creds, schedule_cfg)

    def failures_now() -> int:
        # Including this attempt. With no history to read, this attempt is
        # the only failure known.
        return max(failures_in_a_row(log_dir), 1) if log_dir else 1

    import uuid

    # Ties this attempt's records together in the history: a retry's claim
    # and its outcome are one attempt, whatever is written between them.
    attempt_id = uuid.uuid4().hex
    claimed = False

    def claim() -> bool:
        # Called by run_scheduled once it holds the pipeline lock.
        nonlocal claimed
        decision = claim_attempt(log_dir, attempt_id)
        if not decision.run:
            print(decision.reason)
        claimed = decision.run
        return decision.run

    def counted(outcome_recorded: bool) -> bool:
        # A retry's claim is already on disk and counts as the failure even
        # if its outcome could not be written after it, so the hourly job
        # will see it. Only an attempt with no record at all is uncounted.
        return outcome_recorded or claimed

    try:
        if retry and log_dir:
            result = run_scheduled(
                creds, schedule_cfg, claim=claim, attempt_id=attempt_id
            )
        else:
            result = run_scheduled(creds, schedule_cfg, attempt_id=attempt_id)
    except Exception as exc:
        # Not everything reaches _run_pipeline's RunResult conversion: a
        # malformed optional section (a `paths` that is a string, say) passes
        # validation and then raises during gap detection. Announcing by
        # default means nothing may exit on a traceback.
        import logging

        logging.getLogger(__name__).exception("Scheduled run raised.")
        msg = f"FAIL: scheduled run raised {brief_error(exc)}"
        print(msg)
        # run_scheduled did not get as far as recording the attempt; without
        # a record the hourly retry would not know this one failed.
        recorded = bool(log_dir) and append_run_history(
            log_dir,
            {
                "kind": KIND_RETRY if retry else KIND_SCHEDULED,
                "attempt_id": attempt_id,
                "status": "failed",
                "message": msg,
                "retry_wanted": True,
            },
        )
        note = _retry_note(failures_now(), _retry_job_ready(), counted(recorded))
        print(note)
        if post:
            _post_to_slack(
                _alert_text(f":warning: Betfair scheduled run failed\n{msg}\n{note}"),
                creds,
            )
        return 1

    if retry and result.busy:
        # Another command holds the lock and is the attempt. Nothing was
        # recorded and nothing failed, so there is nothing to announce.
        print("Another downloader command is running; this retry stands down.")
        return 0
    if retry and result.superseded:
        # Another attempt delivered while this one waited to start, or the
        # attempt could not be counted; claim() has printed which.
        return 0

    note = (
        _retry_note(
            failures_now(), _retry_job_ready(), counted(result.history_recorded)
        )
        if result.retry_wanted
        else ""
    )
    delivered = not result.retry_wanted
    # A recovery is only announced once it is on record. If this success
    # could not be written, the history still ends in the earlier failure
    # (or this retry's claim) and the hourly job will run again; saying
    # "recovered" would be contradicted an hour later.
    recovered_from = (
        failures_before_latest(log_dir)
        if log_dir and delivered and result.history_recorded
        else 0
    )
    recovered = (
        f"Recovered after {recovered_from} failed attempt(s)." if recovered_from else ""
    )
    unrecorded = ""
    if delivered and not result.history_recorded and log_dir:
        pending = failures_in_a_row(log_dir)
        if pending:
            unrecorded = (
                "⚠️ This run delivered but could not be written to "
                "run_history.jsonl (see the log), so the history still shows "
                f"{pending} failure(s) in a row and a retry may run again."
            )

    if result.ok and result.status == "success":
        print(f"OK ({result.status}): {result.message}")
        for line in (note, recovered, unrecorded):
            if line:
                print(line)
        # A success can still carry a warning. It must reach Slack from
        # here: a low disk gave this pipeline both of its incidents, and a
        # warning that only lands in scheduler stdout is not a warning. A
        # run that asks for a retry is announced even if it carries none.
        if ("⚠️" in result.message or result.retry_wanted or unrecorded) and post:
            body = "\n".join(
                line for line in (result.message, note, recovered, unrecorded) if line
            )
            _post_to_slack(
                _alert_text(
                    f":warning: Betfair scheduled run succeeded with a warning\n{body}"
                ),
                creds,
            )
        elif recovered_from and post:
            _post_to_slack(
                _alert_text(
                    f":white_check_mark: Betfair scheduled run recovered after "
                    f"{recovered_from} failed attempt(s)\n{result.message}"
                ),
                creds,
            )
        return 0

    label = "PARTIAL" if result.ok else "FAIL"
    lines = [f"{label} ({result.status}): {result.message}"]
    lines.extend(f"  - {err}" for err in result.errors)
    if note:
        lines.append(note)
    for line in lines:
        print(line)

    if post:
        _post_to_slack(
            _alert_text(
                ":warning: Betfair scheduled run "
                + ("completed with problems" if result.ok else "failed")
                + "\n"
                + "\n".join(lines)
            ),
            creds,
        )

    # A partial run is not a success: exiting 0 hides an Azure publish failure
    # from launchd and from any monitoring built on the exit code.
    return 1


def _cmd_retry(args: argparse.Namespace) -> int:
    """
    Run the scheduled pipeline again if the last attempt did not deliver.

    Behind the hourly ``com.betfair.results.retry`` job (``schedule
    install-retry``), which fires on the hours with no scheduled run. Most
    hours there is nothing to do and this exits at once without logging in
    to Betfair. It runs while the most recent attempts asked for a retry,
    and stops after six in a row (see ``scheduler.retry``).

    A configuration that cannot be loaded exits 2 without a Slack post: the
    scheduled run announces that, and an hourly repeat would bury it.

    Exit codes: 0=nothing to do or success, 1=failure, 2=bad configuration.
    """
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    creds, schedule_cfg = _load_creds_and_schedule(validate=True)

    from .scheduler.retry import decide_retry
    from .scheduler.runner import _resolve_log_dir

    decision = decide_retry(_resolve_log_dir(creds, schedule_cfg))
    print(decision.reason)
    if not decision.run:
        # Standing down is not a failure of this command; the reason is in
        # retry.out.log, and the scheduled run's own alert has already said
        # when the history could not be written.
        return 0
    return _run_and_announce(args, creds, schedule_cfg, retry=True)


def _cmd_backfill(args: argparse.Namespace) -> int:
    """
    Manual backfill for an explicit date range.

    Downloads/enriches/writes CSV for [--from, --to]. Does not update
    scheduler coverage dates (UTC or local) or write a success marker.

    Exit codes: 0=success, 1=failure, 2=bad arguments or configuration.
    """
    import logging
    from datetime import date

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if not args.from_date or not args.to_date:
        print("FAIL: --from and --to are required for backfill.")
        return 2

    try:
        from_date = date.fromisoformat(args.from_date)
        to_date = date.fromisoformat(args.to_date)
    except ValueError as e:
        print(f"FAIL: invalid date format (expected YYYY-MM-DD): {e}")
        return 2

    creds, schedule_cfg = _load_creds_and_schedule(validate=True)

    from .scheduler.runner import run_backfill

    result = run_backfill(creds, schedule_cfg, from_date, to_date)

    if result.ok:
        print(f"OK ({result.status}): {result.message}")
        return 0

    print(f"FAIL ({result.status}): {result.message}")
    for err in result.errors:
        print(f"  - {err}")
    return 1


def _cmd_backfill_commission(args: argparse.Namespace) -> int:
    """
    One-off read of per-market commission for an explicit settled-date range.

    Reads grouped-by-market cleared orders for [--from, --to] and upserts
    them into ``<results_csv_dir>/.cache/market_commission.csv``. Touches
    nothing else: no bets are downloaded, the canonical is not rewritten,
    and Azure is not involved.

    Exit codes: 0=success, 1=failure, 2=bad arguments or configuration.
    """
    import logging
    from datetime import date, datetime, timedelta, timezone

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if not args.from_date or not args.to_date:
        print("FAIL: --from and --to are required for backfill-commission.")
        return 2
    try:
        from_date = date.fromisoformat(args.from_date)
        to_date = date.fromisoformat(args.to_date)
    except ValueError as e:
        print(f"FAIL: invalid date format (expected YYYY-MM-DD): {e}")
        return 2
    if from_date > to_date:
        print(f"FAIL: --from ({from_date}) is after --to ({to_date}).")
        return 2

    creds, schedule_cfg = _load_creds_and_schedule(validate=True)

    from .commission import (
        DEFAULT_REQUERY_LOOKBACK_DAYS,
        update_market_commission,
    )
    from .downloader_core import resolve_enrichment_cache_dir
    from .market_status import (
        load_canonical_market_dates,
        load_market_status,
        resolve_status_path,
    )
    from .paths import resolve_results_dir
    from .run_lock import PipelineBusy, pipeline_lock
    from .scheduler.auth import build_api_client

    if from_date < date.today() - timedelta(days=DEFAULT_REQUERY_LOOKBACK_DAYS):
        # Betfair serves grouped rows for about a year; an older range comes
        # back empty and would otherwise look like a bet-free period.
        print(
            f"WARNING: --from {from_date} is more than "
            f"{DEFAULT_REQUERY_LOOKBACK_DAYS} days ago; Betfair does not serve "
            f"grouped cleared orders that far back, so those days will read as empty."
        )

    from_dt = datetime(
        from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc
    )
    end_day = to_date + timedelta(days=1)
    to_dt = datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc)
    results_dir = resolve_results_dir(creds)
    cache_dir = resolve_enrichment_cache_dir(results_dir)

    # The lock comes before the login: a scheduled run rewriting the same
    # commission store would lose one side's rows, and a command refused for
    # that reason should not have opened a Betfair session first.
    try:
        with pipeline_lock(results_dir, command="backfill-commission"):
            try:
                client = build_api_client(creds.get("betfair") or {})
            except Exception as exc:
                print(f"FAIL: Betfair authentication failed: {exc}")
                return 1
            try:
                result = update_market_commission(
                    client=client,
                    cache_dir=cache_dir,
                    from_dt=from_dt,
                    to_dt=to_dt,
                    df_status=load_market_status(resolve_status_path(cache_dir)),
                    df_canonical=load_canonical_market_dates(results_dir),
                    chunk_days=schedule_cfg.chunk_days,
                    status_cb=print,
                )
            except Exception as exc:
                print(f"FAIL: commission backfill raised {type(exc).__name__}: {exc}")
                return 1
            finally:
                try:
                    client.logout()
                except Exception:
                    pass
    except PipelineBusy as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"OK: {result.message}")
    return 0


def _cmd_racecard(_args: argparse.Namespace) -> int:
    """
    Capture the forward racing card into the racecard store.

    The hourly job behind this keeps racing names complete between the four
    daily runs: Betfair stops naming a racing market once it settles, so a
    market loaded and run between two runs would otherwise only get the
    fallback name. Deliberately quiet: a failure is printed and exits 1 but
    does not post to Slack, because every run captures too and announces
    its own failure, and an hourly alert would bury the ones that matter.

    Exit codes: 0=success, 1=failure, 2=bad configuration.
    """
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    creds, _schedule_cfg = _load_creds_and_schedule(validate=True)

    from .commission import load_market_commission, resolve_commission_path
    from .csv_utils import decimal_key
    from .downloader_core import resolve_enrichment_cache_dir
    from .paths import resolve_results_dir
    from .racecard import update_racecard
    from .scheduler.auth import build_api_client

    cache_dir = resolve_enrichment_cache_dir(resolve_results_dir(creds))
    try:
        store = load_market_commission(resolve_commission_path(cache_dir))
    except Exception as exc:
        print(f"FAIL: commission store unreadable ({type(exc).__name__}: {exc})")
        return 1
    try:
        client = build_api_client(creds.get("betfair") or {})
    except Exception as exc:
        print(f"FAIL: Betfair authentication failed: {exc}")
        return 1
    try:
        result = update_racecard(
            client=client,
            cache_dir=cache_dir,
            bet_market_keys=set(store["marketId"].map(decimal_key)),
        )
    except Exception as exc:
        print(f"FAIL: racecard capture raised {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            client.logout()
        except Exception:
            pass
    print(f"OK: {result.message}")
    return 0


def _cmd_export_market_results(_args: argparse.Namespace) -> int:
    """
    Rebuild the dashboard's results feed from the files on disk.

    Makes no Betfair calls: it reads the canonical, the status and
    commission stores and the racecard, and rewrites
    ``market_results.csv`` and its meta. Every run does this already; the
    command is for a rebuild after a manual repair or a first install.

    Exit codes: 0=success, 1=failure, 2=bad configuration.
    """
    creds, _schedule_cfg = _load_creds_and_schedule(validate=False)

    from .market_results import build_market_results_from_disk, write_market_results
    from .paths import resolve_results_dir
    from .run_lock import PipelineBusy, pipeline_lock

    results_dir = resolve_results_dir(creds)
    try:
        # Under the lock so the feed is never built from a canonical or a
        # store that a run is half-way through replacing one after another.
        with pipeline_lock(results_dir, command="export-market-results"):
            result = build_market_results_from_disk(results_dir)
            path = write_market_results(results_dir, result)
    except PipelineBusy as exc:
        print(f"FAIL: {exc}")
        return 1
    except Exception as exc:
        print(f"FAIL: results feed not rebuilt ({type(exc).__name__}: {exc})")
        return 1
    print(f"OK: {result.message} Written to {path}.")
    for warning in result.warnings:
        print(warning)
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """
    Report missing settled-date gaps in the canonical CSV.

    Scans ``cleared_orders_cleaned.csv`` in the resolved results directory
    and prints any missing calendar days inside the audit window (bounded by
    ``--window`` days back from today, default 90).

    Exit codes: 0=report produced (gaps or not), 2=bad configuration.
    """
    from .audit import compute_missing_settled_dates
    from .paths import ResultsDirNotConfigured, resolve_results_dir

    creds, _schedule_cfg = _load_creds_and_schedule()
    try:
        results_dir = resolve_results_dir(creds)
    except (ResultsDirNotConfigured, AttributeError) as exc:
        # AttributeError: paths may be any JSON shape; a bare string must
        # exit as bad configuration, not with a traceback.
        print(f"FAIL: {exc}")
        return 2
    canonical = results_dir / "cleared_orders_cleaned.csv"

    result = compute_missing_settled_dates(canonical, window_days=args.window)

    print(f"Canonical CSV : {canonical}")
    if result.get("message"):
        print(result["message"])
        return 0

    print(f"Audit window  : {result['window_start']} .. {result['window_end']}")
    print(f"Data range    : {result['earliest']} .. {result['latest']}")
    days_stale = result.get("days_stale")
    if days_stale:
        print(f"Data is STALE : newest row is {days_stale} day(s) old")
    missing = result.get("missing_ranges") or []
    if not missing:
        print("No missing settled-date gaps found in the audit window.")
        return 0

    print(f"Missing days  : {result['num_missing']}")
    for r in missing:
        print(f"  - {r['start']} .. {r['end']} ({r['days']} day(s))")
    print("Use 'betfair-results backfill --from ... --to ...' to fill a gap.")
    return 0


def _post_to_slack(text: str, creds: dict | None = None) -> int:
    """
    Post ``text`` to Slack. Returns 0 on success, 1 on failure (reason printed).

    Credentials come from ~/.betfair/slack.json when present, so this still
    works when credentials.json itself is what could not be read.

    Callers that already hold parsed credentials should pass them: re-reading
    a cloud-backed credentials.json can fail after a long run (OneDrive may
    evict it mid-report), which would otherwise lose an embedded slack
    section and silence a report that had actually succeeded.
    """
    import contextlib
    import io

    from .secrets import credentials_path, load_credentials

    # Deliberately not _load_creds_and_schedule: that also parses the schedule
    # section, so one malformed schedule value would discard a perfectly good
    # embedded slack section and leave the failure unreportable.
    if creds is not None:
        return _send_slack(text, creds)

    creds = {}
    try:
        # Silenced: the reason has already been reported by the caller.
        with contextlib.redirect_stdout(io.StringIO()):
            creds_file = credentials_path()
            if creds_file.exists():
                creds = load_credentials(creds_file) or {}
    except (SystemExit, Exception):
        # credentials.json may be the very thing that is broken; the local
        # ~/.betfair/slack.json still lets us report it.
        creds = {}
    return _send_slack(text, creds)


def _send_slack(text: str, creds: dict) -> int:
    """
    Post ``text`` with ``creds``, reporting any failure on stdout.

    Every message this CLI posts is built from plain text plus Slack
    ``*bold*`` and ``:emoji:``; none uses ``<link>`` syntax, so the whole
    text is escaped here, once, for whatever exception detail it carries.
    """
    from .slack_notify import SlackNotConfigured, escape_mrkdwn, post_message

    try:
        post_message(escape_mrkdwn(text), creds)
    except SlackNotConfigured as exc:
        print(f"FAIL: Slack not configured: {exc}")
        return 1
    except Exception as exc:
        print(f"FAIL: Slack post failed: {type(exc).__name__}: {exc}")
        return 1
    print("Posted to Slack.")
    return 0


def _load_creds_for_report(
    args: argparse.Namespace,
    *,
    validate: bool = False,
    context: str = "DM report",
):
    """
    Load credentials, announcing setup failures to Slack before they exit.

    ``validate`` is passed through to _load_creds_and_schedule: the scheduled
    run needs the full fail-fast validation, so that a bad timezone or an
    oversized chunk setting exits 2 with collected errors rather than raising
    from deep inside the pipeline, outside every notification handler.

    ``context`` names the failing command in the alert. A run that never
    started must not report itself as a broken report.

    ``_load_creds_and_schedule`` prints the reason and raises SystemExit, so
    without this wrapper the very failure the local Slack config exists to
    report -- an unreadable credentials.json -- would exit before any post is
    attempted. stdout is captured so the reason can be forwarded to Slack as
    well as printed for the launchd log.
    """
    import contextlib
    import io

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            creds, schedule_cfg = _load_creds_and_schedule(validate=validate)
    except SystemExit:
        detail = buf.getvalue().strip() or "FAIL: could not load credentials.json"
        print(detail)
        if getattr(args, "post_slack", False):
            _post_to_slack(f":warning: Betfair {context} failed\n{detail}")
        raise
    except Exception as exc:
        # parse_schedule_config raises ValueError rather than exiting, e.g. on
        # a non-numeric schedule.max_backfill_days. Report it the same way and
        # exit cleanly instead of surfacing a traceback from a scheduled job.
        printed = buf.getvalue().strip()
        if printed:
            print(printed)
        detail = f"FAIL: could not load configuration: {type(exc).__name__}: {exc}"
        print(detail)
        if getattr(args, "post_slack", False):
            _post_to_slack(f":warning: Betfair {context} failed\n{detail}")
        raise SystemExit(2) from exc
    captured = buf.getvalue()
    if captured:
        print(captured, end="")
    return creds, schedule_cfg


def _cmd_dm_report(args: argparse.Namespace) -> int:
    """
    Render the daily DM report from the local results CSV.

    Prints the message body to stdout and, with ``--post-slack``, posts it.
    The sections, all in Australia/Sydney time, are Today, Yesterday and
    Week to date, with Week to date replaced on Sundays by Last week and
    followed by Month to date and Year to date; see
    ``reporting.daily_dm_report``.
    """
    from datetime import datetime

    # An explicit --csv is self-sufficient: results_dir is ignored downstream
    # when csv_path is set, so requiring credentials.json here would demand
    # configuration this run never reads. Leave creds as None in that case so
    # the notifier still resolves them itself if a post is needed.
    creds: dict | None = None
    results_dir = ""
    if not getattr(args, "csv", None):
        creds, _schedule_cfg = _load_creds_for_report(args)
        from .paths import ResultsDirNotConfigured, resolve_results_dir

        try:
            # The shared resolver, so ~ expands here exactly as it does for
            # the scheduled run that wrote the CSV this report reads.
            results_dir = str(resolve_results_dir(creds))
        except (ResultsDirNotConfigured, AttributeError):
            # AttributeError: paths may be any JSON shape; a bare string must
            # fail this handler's way, not with a traceback.
            msg = "FAIL: paths.results_csv_dir is not configured in credentials.json"
            print(msg)
            if getattr(args, "post_slack", False):
                _post_to_slack(f":warning: Betfair DM report failed\n{msg}", creds)
            return 2

    report_dt = None
    if getattr(args, "at", None):
        try:
            report_dt = datetime.fromisoformat(args.at)
        except ValueError as exc:
            msg = f"FAIL: invalid --at datetime, expected ISO-8601: {exc}"
            print(msg)
            if getattr(args, "post_slack", False):
                _post_to_slack(f":warning: Betfair DM report failed\n{msg}", creds)
            return 2

    from .reporting.breakout import parse_breakouts
    from .reporting.daily_dm_report import build_daily_dm_report_from_results_dir

    # Breakouts live in credentials.json, so a run with --csv alone (which
    # never loads it) reports none.
    breakouts, breakout_problems = parse_breakouts(creds)

    try:
        report = build_daily_dm_report_from_results_dir(
            results_dir,
            report_dt=report_dt,
            csv_path=getattr(args, "csv", None),
            breakouts=breakouts,
            breakout_problems=breakout_problems,
        )
    except FileNotFoundError as exc:
        msg = f"FAIL: {exc}"
        print(msg)
        if getattr(args, "post_slack", False):
            _post_to_slack(f":warning: Betfair DM report failed\n{msg}", creds)
        return 1
    except Exception as exc:
        msg = f"FAIL: could not build DM report: {type(exc).__name__}: {exc}"
        print(msg)
        if getattr(args, "post_slack", False):
            _post_to_slack(f":warning: Betfair DM report failed\n{msg}", creds)
        return 1

    # The twice-daily report is the one message that is always read, which
    # makes it the right carrier for a disk warning: the 2026-08-30/31
    # incidents both began as a disk that filled with nothing reporting it.
    from pathlib import Path as _Path

    from .scheduler.runner import check_disk_space

    _ok, disk_warning = check_disk_space(_Path(report.source_csv).parent)
    text = report.text
    if disk_warning:
        text = f"{text}\n\n{disk_warning}"

    if getattr(args, "show_source", False):
        print(f"Source CSV: {report.source_csv}")
        print()
    print(text)
    if getattr(args, "post_slack", False):
        return _post_to_slack(text, creds)
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    """
    Install, uninstall, query status, or show logs for the platform scheduler.

    Actions: install | uninstall | install-racecard | uninstall-racecard |
    install-retry | uninstall-retry | status | logs [--tail N]
    """
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    from pathlib import Path

    creds, schedule_cfg = _load_creds_and_schedule()

    try:
        from .scheduler.installers import get_installer

        installer = get_installer()
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1

    action = args.action

    if action == "install":
        repo_root = Path(__file__).resolve().parents[2]
        import sys

        venv_python = Path(sys.executable)

        # Apply CLI time overrides to schedule config if provided
        if getattr(args, "time", None) or getattr(args, "retries", None):
            from dataclasses import replace

            retry_list = [
                r.strip() for r in (args.retries or "").split(",") if r.strip()
            ]
            schedule_cfg = replace(
                schedule_cfg,
                primary_time=args.time or schedule_cfg.primary_time,
                retry_times=tuple(retry_list)
                if retry_list
                else schedule_cfg.retry_times,
            )

        log_dir_raw = schedule_cfg.log_dir or str(repo_root / "outputs")
        log_dir = Path(log_dir_raw).expanduser()

        result = installer.install(
            schedule_cfg=schedule_cfg,
            repo_root=repo_root,
            venv_python_path=venv_python,
            log_dir=log_dir,
        )
        print(result["message"])
        return 0 if result["ok"] else 1

    if action == "uninstall":
        result = installer.uninstall()
        print(result["message"])
        return 0 if result["ok"] else 1

    if action == "install-racecard":
        import sys

        repo_root = Path(__file__).resolve().parents[2]
        log_dir = Path(schedule_cfg.log_dir or str(repo_root / "outputs")).expanduser()
        result = installer.install_racecard(
            repo_root=repo_root,
            venv_python_path=Path(sys.executable),
            log_dir=log_dir,
        )
        print(result["message"])
        return 0 if result["ok"] else 1

    if action == "uninstall-racecard":
        result = installer.uninstall_racecard()
        print(result["message"])
        return 0 if result["ok"] else 1

    if action == "install-retry":
        import sys

        repo_root = Path(__file__).resolve().parents[2]
        log_dir = Path(schedule_cfg.log_dir or str(repo_root / "outputs")).expanduser()
        result = installer.install_retry(
            schedule_cfg=schedule_cfg,
            repo_root=repo_root,
            venv_python_path=Path(sys.executable),
            log_dir=log_dir,
        )
        print(result["message"])
        return 0 if result["ok"] else 1

    if action == "uninstall-retry":
        result = installer.uninstall_retry()
        print(result["message"])
        return 0 if result["ok"] else 1

    if action == "status":
        info = installer.status()
        print(info["message"])
        if info.get("installed"):
            print(f"  installed     : {info['installed']}")
            print(f"  loaded        : {info['loaded']}")
            print(f"  pid           : {info.get('pid', 'N/A')}")
            print(f"  last_exit     : {info.get('last_exit', 'N/A')}")

            # Show both: `schedule install --time` overrides the plist for
            # that install only and writes nothing back, so the configured
            # and installed schedules can diverge with nothing to reveal it.
            configured = sorted({schedule_cfg.primary_time, *schedule_cfg.retry_times})
            installed_times = info.get("installed_times") or []
            print(f"  configured    : {', '.join(configured) or 'none'}")
            print(f"  installed at  : {', '.join(installed_times) or 'unknown'}")
            if installed_times and installed_times != configured:
                print(
                    "  WARNING       : the installed schedule does not match "
                    "credentials.json; re-run 'schedule install' to reconcile."
                )
        rc = installer.racecard_status()
        print(
            f"  racecard job  : installed={rc['installed']} loaded={rc['loaded']}"
            + ("" if rc["installed"] else " (run 'schedule install-racecard')")
        )
        rt = installer.retry_status()
        print(
            f"  retry job     : installed={rt['installed']} loaded={rt['loaded']}"
            + ("" if rt["installed"] else " (run 'schedule install-retry')")
        )
        return 0

    if action == "logs":
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        log_dir_raw = schedule_cfg.log_dir or str(repo_root / "outputs")
        log_dir = Path(log_dir_raw).expanduser()
        tail_n = getattr(args, "tail", 50) or 50
        output = installer.logs(log_dir=log_dir, tail_n=tail_n)
        print(output)
        return 0

    print(f"Unknown schedule action: {action!r}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="betfair-results",
        description="Betfair Results Downloader — CLI entry point.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser(
        "auth-test",
        help="Test cert-based non-interactive Betfair login and report the outcome.",
    )
    rn = sub.add_parser(
        "run",
        help="Run one scheduled download (gap-detect, fetch, enrich, CSV, optional Azure).",
    )
    # Announcing is the default here, unlike dm-report: this is the unattended
    # job, so silence is the wrong default when something goes wrong.
    rn.add_argument(
        "--no-post-slack",
        dest="post_slack",
        action="store_false",
        help="Do not announce failures to Slack (announcing is the default).",
    )
    rn.set_defaults(post_slack=True)
    bf = sub.add_parser(
        "backfill",
        help="Manual backfill for an explicit date range.",
    )
    bf.add_argument(
        "--from",
        dest="from_date",
        metavar="YYYY-MM-DD",
        help="Inclusive start date (required)",
    )
    bf.add_argument(
        "--to",
        dest="to_date",
        metavar="YYYY-MM-DD",
        help="Inclusive end date (required)",
    )
    bc = sub.add_parser(
        "backfill-commission",
        help="Read per-market commission for an explicit settled-date range into the commission store.",
    )
    bc.add_argument(
        "--from",
        dest="from_date",
        metavar="YYYY-MM-DD",
        help="Inclusive start date (required)",
    )
    bc.add_argument(
        "--to",
        dest="to_date",
        metavar="YYYY-MM-DD",
        help="Inclusive end date (required)",
    )
    rt = sub.add_parser(
        "retry",
        help="Run the scheduled download again if the last attempt did not deliver.",
    )
    rt.add_argument(
        "--no-post-slack",
        dest="post_slack",
        action="store_false",
        help="Do not announce the outcome to Slack (announcing is the default).",
    )
    rt.set_defaults(post_slack=True)
    sub.add_parser(
        "racecard",
        help="Capture the forward racing card so racing markets keep their full names.",
    )
    sub.add_parser(
        "export-market-results",
        help="Rebuild the dashboard results feed from the files on disk (no API calls).",
    )
    au = sub.add_parser(
        "audit",
        help="Report missing settled-date gaps in the canonical CSV.",
    )
    au.add_argument(
        "--window",
        type=int,
        default=90,
        metavar="DAYS",
        help="Audit window in days back from today (default: 90)",
    )
    dm = sub.add_parser(
        "dm-report",
        help="Render the OpenClaw daily DM report from local results CSV.",
    )
    dm.add_argument(
        "--at",
        metavar="YYYY-MM-DDTHH:MM[:SS][+TZ]",
        help="Optional report timestamp override in ISO-8601. Naive values are treated as Australia/Sydney time.",
    )
    dm.add_argument(
        "--csv",
        metavar="PATH",
        help="Optional explicit CSV path. Defaults to the best discovered cleared-orders CSV in paths.results_csv_dir.",
    )
    dm.add_argument(
        "--show-source",
        action="store_true",
        help="Print the source CSV path before the report body.",
    )
    dm.add_argument(
        "--post-slack",
        action="store_true",
        help="Also post the report (or the failure reason) to Slack.",
    )
    sch = sub.add_parser(
        "schedule",
        help="Install, remove, or inspect the platform scheduled job.",
    )
    sch.add_argument(
        "action",
        choices=[
            "install",
            "uninstall",
            "install-racecard",
            "uninstall-racecard",
            "install-retry",
            "uninstall-retry",
            "status",
            "logs",
        ],
    )
    sch.add_argument(
        "--time",
        metavar="HH:MM",
        help="Override primary_time for this install (e.g. 07:00)",
    )
    sch.add_argument(
        "--retries",
        metavar="HH:MM,...",
        help="Override retry_times (comma-separated, e.g. 10:00,20:00)",
    )
    sch.add_argument(
        "--tail",
        type=int,
        default=50,
        help="Number of log lines to show for 'logs' action (default: 50)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "auth-test":
        return _cmd_auth_test(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "retry":
        return _cmd_retry(args)
    if args.command == "backfill":
        return _cmd_backfill(args)
    if args.command == "backfill-commission":
        return _cmd_backfill_commission(args)
    if args.command == "racecard":
        return _cmd_racecard(args)
    if args.command == "export-market-results":
        return _cmd_export_market_results(args)
    if args.command == "audit":
        return _cmd_audit(args)
    if args.command == "dm-report":
        return _cmd_dm_report(args)
    if args.command == "schedule":
        return _cmd_schedule(args)
    print(f"Unknown command: {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
