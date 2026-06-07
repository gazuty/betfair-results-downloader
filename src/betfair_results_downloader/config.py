from __future__ import annotations

from dataclasses import dataclass
from typing import Any


EVENTTYPE_HORSES = 7
EVENTTYPE_GREYHOUNDS = 4339


@dataclass(frozen=True)
class DownloaderConfig:
    days: int = 1
    include_horses: bool = True
    include_greyhounds: bool = True
    enable_azure_sql: bool = False
    dry_run: bool = True
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
            pass


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool = False
    timezone: str = "Australia/Sydney"
    primary_time: str = "06:00"
    retry_times: tuple[str, ...] = ("09:00", "19:00", "23:00")
    publish_to_azure: bool = True
    allow_azure_publish: bool = False
    max_backfill_days: int = 90
    chunk_days: int = 30
    min_coverage_overlap_days: int = 1
    min_overlap_hours: int = 2
    log_dir: str = ""
    history_file: str = ""


def parse_schedule_config(creds: dict[str, Any]) -> ScheduleConfig:
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
        min_overlap_hours=int(s.get("min_overlap_hours", 2)),
        log_dir=str(s.get("log_dir", "")),
        history_file=str(s.get("history_file", "")),
    )
