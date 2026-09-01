from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


class ResultsDirNotConfigured(RuntimeError):
    """Raised when ``paths.results_csv_dir`` is missing from credentials."""


def resolve_results_dir(creds: dict[str, Any]) -> Path:
    """
    Resolve the results CSV directory from ``paths.results_csv_dir``.

    Fails loudly when unset. Until H4 this silently fell back to guessing
    OneDrive locations -- which is exactly how the working set ended up on
    a cloud-sync directory whose Files On-Demand eviction broke reads of
    the canonical twice (2026-08-30/31). A pipeline that guesses where its
    system of record lives eventually guesses wrong; better to refuse and
    say what to set.

    This is the single source of truth for results directory resolution --
    used by ``scheduler/runner.py``, ``scheduler/gap_detector.py``, and the
    dm-report command.
    """
    raw = (creds.get("paths") or {}).get("results_csv_dir", "")
    if raw is not None and not isinstance(raw, str):
        # str() coercion would quietly resolve `true` to a relative
        # directory named "True" and write the canonical there.
        raise ResultsDirNotConfigured(
            f"paths.results_csv_dir must be a string, got {type(raw).__name__}"
        )
    if not (raw or "").strip():
        raise ResultsDirNotConfigured(
            "paths.results_csv_dir is not set in credentials.json. Set it to "
            "the local working directory that holds cleared_orders_cleaned.csv "
            "(e.g. ~/BetfairData). The old behaviour of guessing OneDrive "
            "locations was removed deliberately: OneDrive eviction corrupted "
            "reads of the canonical."
        )
    return Path(raw.strip()).expanduser()


def resolve_backup_dir(creds: dict[str, Any]) -> Optional[Path]:
    """
    Resolve the one-way backup directory from ``paths.backup_dir``.

    Returns None when unset -- backups are optional, and their absence
    must not fail a run. See :mod:`betfair_results_downloader.backup`.
    """
    raw = (creds.get("paths") or {}).get("backup_dir", "")
    if not isinstance(raw, str):
        # Forgiving at runtime -- the backup must never be able to fail a
        # run -- but validate_credentials rejects a non-string up front so
        # normal dispatch never reaches here with one.
        return None
    raw = raw.strip()
    return Path(raw).expanduser() if raw else None
