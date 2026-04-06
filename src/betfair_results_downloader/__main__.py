"""
Package CLI entry point.

Implemented subcommands:
  auth-test  — cert-based non-interactive login test (Phase 1.1)
  run        — one scheduled download (Phase 2.2)
  backfill   — explicit date-range download (Phase 2.2)

Stub subcommands (Phase 3+):
  schedule   — install/uninstall/status/logs for the platform scheduled job
"""
from __future__ import annotations

import argparse
import sys
from typing import Any


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


def _load_creds_and_schedule():
    """Load credentials and parse schedule config; shared by run/backfill."""
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

    schedule_cfg = parse_schedule_config(creds)
    return creds, schedule_cfg


def _cmd_run(_args: argparse.Namespace) -> int:
    """
    Run one scheduled download.

    Checks today's success marker, computes the backfill window via gap
    detection, downloads/enriches/writes CSV, and optionally publishes to
    Azure SQL (four-gate model).

    Exit codes: 0=success or skipped, 1=failure.
    """
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    creds, schedule_cfg = _load_creds_and_schedule()

    from .scheduler.runner import run_scheduled
    result = run_scheduled(creds, schedule_cfg)

    if result.skipped:
        print(f"Skipped: {result.skip_reason}")
        return 0

    if result.ok:
        print(f"OK ({result.status}): {result.message}")
        return 0

    print(f"FAIL ({result.status}): {result.message}")
    for err in result.errors:
        print(f"  - {err}")
    return 1


def _cmd_backfill(args: argparse.Namespace) -> int:
    """
    Manual backfill for an explicit date range.

    Downloads/enriches/writes CSV for [--from, --to].  Does not update
    LastCoveredDateUtc or write a success marker.

    Exit codes: 0=success, 1=failure, 2=bad arguments.
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

    creds, schedule_cfg = _load_creds_and_schedule()

    from .scheduler.runner import run_backfill
    result = run_backfill(creds, schedule_cfg, from_date, to_date)

    if result.ok:
        print(f"OK ({result.status}): {result.message}")
        return 0

    print(f"FAIL ({result.status}): {result.message}")
    for err in result.errors:
        print(f"  - {err}")
    return 1


def _cmd_not_implemented(args: argparse.Namespace) -> int:
    print(
        f"'{args.command}' is declared but not yet implemented. "
        "Scheduled for a later PR (Phase 3+)."
    )
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
    sub.add_parser(
        "run",
        help="Run one scheduled download (gap-detect, fetch, enrich, CSV, optional Azure).",
    )
    bf = sub.add_parser(
        "backfill",
        help="Manual backfill for an explicit date range.",
    )
    bf.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD",
                    help="Inclusive start date (required)")
    bf.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD",
                    help="Inclusive end date (required)")
    sch = sub.add_parser(
        "schedule",
        help="Install, remove, or inspect the platform scheduled job (Phase 3+, not yet implemented).",
    )
    sch.add_argument("action", choices=["install", "uninstall", "status", "logs"])

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
    return _cmd_not_implemented(args)


if __name__ == "__main__":
    sys.exit(main())
