from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .backup import RetentionTiers


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


def _string_list(value: object) -> list[str]:
    """The non-empty strings of a list setting; anything else gives none."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def resolve_backup_dirs(creds: dict[str, Any]) -> list[Path]:
    """
    Every backup destination: ``paths.backup_dir`` (the original single
    setting) followed by ``paths.backup_dirs``, without repeats.

    Two destinations on two cloud services are two independent copies; a
    list rather than a second named setting so a third costs nothing.
    Forgiving like :func:`resolve_backup_dir`: a malformed value yields no
    destination here and is rejected by ``validate_credentials`` up front.
    """
    paths_cfg = creds.get("paths")
    if not isinstance(paths_cfg, dict):
        return []
    found: list[Path] = []
    first = resolve_backup_dir(creds)
    if first is not None:
        found.append(first)
    for raw in _string_list(paths_cfg.get("backup_dirs")):
        candidate = Path(raw).expanduser()
        if candidate not in found:
            found.append(candidate)
    return found


def resolve_backup_extra_files(creds: dict[str, Any]) -> list[Path]:
    """Files outside the results directory that ride in the state bundle."""
    paths_cfg = creds.get("paths")
    if not isinstance(paths_cfg, dict):
        return []
    return [
        Path(raw).expanduser()
        for raw in _string_list(paths_cfg.get("backup_extra_files"))
    ]


def resolve_backup_retention(
    creds: dict[str, Any], snapshot_retention: int
) -> RetentionTiers:
    """
    How long destinations keep dated copies, from ``paths.backup_retention``
    (``{"daily": 7, "weekly": 8, "monthly": 12}``).

    Unset keeps the original rule, the newest ``snapshot_retention`` dates,
    so an existing install behaves as before until it opts in. A tier left
    out of the object is zero.
    """
    paths_cfg = creds.get("paths")
    raw = paths_cfg.get("backup_retention") if isinstance(paths_cfg, dict) else None
    if not isinstance(raw, dict):
        return RetentionTiers(daily=snapshot_retention)

    def _count(key: str) -> int:
        value = raw.get(key, 0)
        # bool is an int: `true` must not quietly mean "keep one".
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    return RetentionTiers(
        daily=_count("daily"), weekly=_count("weekly"), monthly=_count("monthly")
    )
