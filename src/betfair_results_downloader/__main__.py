"""
Package CLI entry point.

Phase 1.1 exposes only ``auth-test``; other subcommands are declared but raise
``NotImplementedError`` with a clear "scheduled for a later PR" message so that
they appear in ``--help`` and stable CLI shape is established early.
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


def _cmd_not_implemented(args: argparse.Namespace) -> int:
    print(
        f"'{args.command}' is declared but not yet implemented. "
        "Scheduled for a later PR (Phase 2+)."
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
        help="Run one scheduled download (not yet implemented).",
    )
    bf = sub.add_parser(
        "backfill",
        help="Manual backfill for an explicit date range (not yet implemented).",
    )
    bf.add_argument("--from", dest="from_date", help="YYYY-MM-DD (inclusive)")
    bf.add_argument("--to", dest="to_date", help="YYYY-MM-DD (inclusive)")
    sch = sub.add_parser(
        "schedule",
        help="Install, remove, or inspect the platform scheduled job (not yet implemented).",
    )
    sch.add_argument("action", choices=["install", "uninstall", "status", "logs"])

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "auth-test":
        return _cmd_auth_test(args)
    return _cmd_not_implemented(args)


if __name__ == "__main__":
    sys.exit(main())
