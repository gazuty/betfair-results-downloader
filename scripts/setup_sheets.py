#!/usr/bin/env python3
"""
One-time setup script: adds google_sheets config to credentials.json
and installs required dependencies.

Run from the repo root with your venv active:

    python scripts/setup_sheets.py

"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Add src/ to path so we can import the secrets module
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from betfair_results_downloader.secrets import credentials_path, load_credentials, save_credentials


SERVICE_ACCOUNT_PATH = str(
    Path(__file__).resolve().parents[1].parent
    / "betfair-dashboard-py" / ".secrets" / "service-account.json"
)
SHEET_NAME = "Betfair Dashboard"


def main() -> int:
    # --- Step 1: Update credentials.json ---
    creds_path = credentials_path()
    print(f"Credentials file: {creds_path}")

    if not creds_path.exists():
        print(f"ERROR: credentials file not found at {creds_path}")
        return 1

    creds = load_credentials(creds_path)

    gs = creds.get("google_sheets") or {}
    existing_sheet = (gs.get("sheet_name") or "").strip()
    existing_sa = (gs.get("service_account_path") or "").strip()

    if existing_sheet and existing_sa:
        print(f"  google_sheets already configured:")
        print(f"    sheet_name: {existing_sheet}")
        print(f"    service_account_path: {existing_sa}")
        print("  Skipping credentials update.")
    else:
        # Resolve service account path
        sa_path = Path(SERVICE_ACCOUNT_PATH)
        if not sa_path.exists():
            print(f"WARNING: Expected service account at {sa_path}")
            print("  You'll need to set google_sheets.service_account_path manually.")
            sa_str = ""
        else:
            sa_str = str(sa_path)
            print(f"  Found service account: {sa_str}")

        creds["google_sheets"] = {
            "sheet_name": SHEET_NAME,
            "service_account_path": sa_str,
        }

        save_credentials(creds, creds_path)
        print(f"  Updated credentials.json with google_sheets config.")
        print(f"    sheet_name: {SHEET_NAME}")
        print(f"    service_account_path: {sa_str}")

    # --- Step 2: Install dependencies ---
    print()
    print("Installing gspread + google-auth...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "gspread>=6.0", "google-auth>=2.0"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  Dependencies installed OK.")
    else:
        print(f"  pip install failed (exit {result.returncode}):")
        print(result.stderr)
        return 1

    # --- Done ---
    print()
    print("Setup complete! You can now run:")
    print("  betfair-results publish-sheet           # interactive (review pending markets)")
    print("  betfair-results publish-sheet --no-interactive  # auto-approved only")
    print()
    print("The scheduled 'betfair-results run' will also auto-publish racing + soccer")
    print("markets to Google Sheets after each download.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
