"""
Package CLI entry point.

Subcommands:
  auth-test     — cert-based non-interactive login test
  run           — one scheduled download (gap-detect, fetch, enrich, CSV, Azure)
  backfill      — explicit date-range download
  audit         — report missing settled-date gaps in the canonical CSV
  schedule      — install/uninstall/status/logs for the platform scheduled job
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
    creds, schedule_cfg = _load_creds_for_report(
        args, validate=True, context="scheduled run"
    )

    from .scheduler.runner import run_scheduled

    try:
        result = run_scheduled(creds, schedule_cfg)
    except Exception as exc:
        # Not everything reaches _run_pipeline's RunResult conversion: a
        # malformed optional section (a `paths` that is a string, say) passes
        # validation and then raises during gap detection. Announcing by
        # default means nothing may exit on a traceback.
        msg = f"FAIL: scheduled run raised {type(exc).__name__}: {exc}"
        print(msg)
        if getattr(args, "post_slack", True):
            _post_to_slack(f":warning: Betfair scheduled run failed\n{msg}", creds)
        return 1

    if result.ok and result.status == "success":
        print(f"OK ({result.status}): {result.message}")
        return 0

    label = "PARTIAL" if result.ok else "FAIL"
    lines = [f"{label} ({result.status}): {result.message}"]
    lines.extend(f"  - {err}" for err in result.errors)
    for line in lines:
        print(line)

    if getattr(args, "post_slack", True):
        _post_to_slack(
            ":warning: Betfair scheduled run "
            + ("completed with problems" if result.ok else "failed")
            + "\n"
            + "\n".join(lines),
            creds,
        )

    # A partial run is not a success: exiting 0 hides an Azure publish failure
    # from launchd and from any monitoring built on the exit code.
    return 1


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


def _cmd_audit(args: argparse.Namespace) -> int:
    """
    Report missing settled-date gaps in the canonical CSV.

    Scans ``cleared_orders_cleaned.csv`` in the resolved results directory
    and prints any missing calendar days inside the audit window (bounded by
    ``--window`` days back from today, default 90).

    Exit codes: 0=report produced (gaps or not), 2=bad configuration.
    """
    from .audit import compute_missing_settled_dates
    from .paths import resolve_results_dir

    creds, _schedule_cfg = _load_creds_and_schedule()
    results_dir = resolve_results_dir(creds)
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
    """Post ``text`` with ``creds``, reporting any failure on stdout."""
    from .slack_notify import SlackNotConfigured, post_message

    try:
        post_message(text, creds)
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
    Render the OpenClaw-oriented daily DM report from the local results CSV.

    This command computes week-to-date from the most recent Sunday 00:00
    Australia/Sydney time and day-to-date from the current day 00:00, then
    prints the final message body to stdout.
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
        # paths may be any JSON shape; a bare string would raise AttributeError
        # here, outside every notification handler.
        paths_cfg = creds.get("paths")
        if not isinstance(paths_cfg, dict):
            paths_cfg = {}
        results_dir = str(paths_cfg.get("results_csv_dir") or "").strip()
        if not results_dir:
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

    from .reporting.daily_dm_report import build_daily_dm_report_from_results_dir

    try:
        report = build_daily_dm_report_from_results_dir(
            results_dir,
            report_dt=report_dt,
            csv_path=getattr(args, "csv", None),
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

    if getattr(args, "show_source", False):
        print(f"Source CSV: {report.source_csv}")
        print()
    print(report.text)
    if getattr(args, "post_slack", False):
        return _post_to_slack(report.text, creds)
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    """
    Install, uninstall, query status, or show logs for the platform scheduler.

    Actions: install | uninstall | status | logs [--tail N]
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

        venv_python = Path(getattr(args, "python", None) or sys.executable)

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
    sch.add_argument("action", choices=["install", "uninstall", "status", "logs"])
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
    if args.command == "backfill":
        return _cmd_backfill(args)
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
