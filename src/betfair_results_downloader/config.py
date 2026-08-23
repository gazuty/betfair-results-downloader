from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Betfair event type IDs. Downloads fetch all settled orders regardless of
# sport; Azure publishing is fixed to these two IDs (see
# downloader_core.DEFAULT_AZURE_EVENT_TYPE_IDS).
EVENTTYPE_HORSES = 7
EVENTTYPE_GREYHOUNDS = 4339


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
        min_overlap_hours=int(s.get("min_overlap_hours", 2)),
        log_dir=str(s.get("log_dir", "")),
        history_file=str(s.get("history_file", "")),
    )
