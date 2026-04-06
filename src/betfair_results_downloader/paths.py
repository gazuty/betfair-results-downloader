from __future__ import annotations

import platform
from pathlib import Path
from typing import Any


_ONEDRIVE_RELATIVE = Path("BF Documentation") / "BF Results and Analysis" / "Results Database"


def get_results_database_dir() -> Path:
    """
    Return the path to the Results Database folder, resolved cross-platform.

    Tries known OneDrive locations in order and returns the first that exists.
    If none exist, returns the OS-appropriate default without crashing.
    """
    system = platform.system()

    if system == "Darwin":
        candidates = [
            Path.home() / "Library" / "CloudStorage" / "OneDrive-Personal" / _ONEDRIVE_RELATIVE,
            Path.home() / "OneDrive" / _ONEDRIVE_RELATIVE,
        ]
    elif system == "Windows":
        candidates = [
            Path("C:/Users/Mark/OneDrive") / _ONEDRIVE_RELATIVE,
            Path.home() / "OneDrive" / _ONEDRIVE_RELATIVE,
        ]
    else:
        # Linux / CI: only try the home-based symlink convention
        candidates = [
            Path.home() / "OneDrive" / _ONEDRIVE_RELATIVE,
        ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    # Nothing found — return the first (most specific) candidate as the default
    # so callers get a sensible path to show in error messages.
    return candidates[0]


def resolve_results_dir(creds: dict[str, Any]) -> Path:
    """
    Resolve the results CSV directory from credentials, falling back to
    :func:`get_results_database_dir` when ``paths.results_csv_dir`` is empty.

    This is the single source of truth for results directory resolution —
    used by both ``scheduler/runner.py`` and ``scheduler/gap_detector.py``.
    """
    raw = (creds.get("paths") or {}).get("results_csv_dir", "")
    return Path(raw) if raw else get_results_database_dir()
