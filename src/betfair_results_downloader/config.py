from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Betfair eventTypeId allowlist
EVENTTYPE_HORSES = 7
EVENTTYPE_GREYHOUNDS = 4339


@dataclass(frozen=True)
class DownloaderConfig:
    days: int = 1
    include_horses: bool = True
    include_greyhounds: bool = True

    # Azure publish toggles
    enable_azure_sql: bool = False
    dry_run: bool = True  # safety gate: defaults to True

    # Optional: who this data belongs to (used by Azure table keying)
    user_id: str | None = None

    def selected_event_type_ids(self) -> list[int]:
        ids: list[int] = []
        if self.include_horses:
            ids.append(EVENTTYPE_HORSES)
        if self.include_greyhounds:
            ids.append(EVENTTYPE_GREYHOUNDS)
        return ids

    def validate(self) -> None:
        if not isinstance(self.days, int):
            raise ValueError("days must be an integer")
        if self.days < 1 or self.days > 365:
            raise ValueError("days must be between 1 and 365")
        if not (self.include_horses or self.include_greyhounds):
            raise ValueError("Select at least one of Horses or Greyhounds.")
        if self.enable_azure_sql and self.dry_run is False:
            # Allowed, but we keep this validation hook in case you later
            # want stricter gating (e.g., require an explicit checkbox)
            pass


@dataclass(frozen=True)
class ScheduleConfig:
    """
    Configuration for scheduled automatic downloads (Phase 1.2+).

    Parsed from the ``schedule`` block in ``credentials.json`` via
    :func:`parse_schedule_config`. All fields have safe defaults so that
    an absent or empty ``schedule`` block behaves identically to ``enabled=False``.
    """

    enabled: bool = False
    """Master toggle. When False, all schedule validation and scheduling behaviour is skipped."""

    timezone: str = "Australia/Sydney"
    """IANA timezone name for primary_time / retry_times interpretation."""

    primary_time: str = "06:00"
    """Primary daily run time in HH:MM (local time, in ``timezone``)."""

    retry_times: tuple[str, ...] = ("09:00", "19:00", "23:00")
    """Additional daily windows to attempt the run if the primary attempt failed or was skipped."""

    publish_to_azure: bool = True
    """Whether the scheduler should attempt Azure SQL publishing (Phase 2.2 fourth gate)."""

    allow_azure_publish: bool = False
    """
    Explicit opt-in required before the scheduler will actually write to Azure SQL.
    A second deliberate gate on top of ``publish_to_azure`` and the credential-level
    ``enable_azure_sql`` / ``dry_run`` flags.
    """

    max_backfill_days: int = 90
    """Maximum number of days to back-fill in a single run. Must be <= 365."""

    chunk_days: int = 30
    """Betfair API window size in days. Must be <= 90."""

    min_coverage_overlap_days: int = 1
    """Re-pull this many days of already-covered data on every run for safety overlap."""

    log_dir: str = ""
    """Directory for run_history.jsonl and success-marker files. Defaults to outputs/ when empty."""

    history_file: str = ""
    """Override path for run_history.jsonl. Derived from log_dir when empty."""


def parse_schedule_config(creds: dict[str, Any]) -> ScheduleConfig:
    """
    Parse the ``schedule`` block from a credentials dict into a :class:`ScheduleConfig`.

    Missing keys are filled with safe defaults. An absent or empty ``schedule`` block
    returns ``ScheduleConfig(enabled=False)``.

    Parameters
    ----------
    creds:
        Full credentials dict (as returned by :func:`secrets.load_credentials`).

    Returns
    -------
    ScheduleConfig
        Fully populated schedule configuration with all defaults applied.
    """
    s: dict[str, Any] = (creds.get("schedule") or {})
    raw_retry = s.get("retry_times", ["09:00", "19:00", "23:00"])
    return ScheduleConfig(
        enabled=bool(s.get("enabled", False)),
        timezone=str(s.get("timezone", "Australia/Sydney")),
        primary_time=str(s.get("primary_time", "06:00")),
        retry_times=tuple(str(t) for t in raw_retry) if raw_retry else (),
        publish_to_azure=bool(s.get("publish_to_azure", True)),
        allow_azure_publish=bool(s.get("allow_azure_publish", False)),
        max_backfill_days=int(s.get("max_backfill_days", 90)),
        chunk_days=int(s.get("chunk_days", 30)),
        min_coverage_overlap_days=int(s.get("min_coverage_overlap_days", 1)),
        log_dir=str(s.get("log_dir", "")),
        history_file=str(s.get("history_file", "")),
    )
