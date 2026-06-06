from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ..config import ScheduleConfig


@dataclass(frozen=True)
class SchedulerNow:
    now_utc: datetime
    now_local: datetime
    today_utc: date
    today_local: date
    timezone_name: str


def get_scheduler_zoneinfo(schedule_cfg: ScheduleConfig) -> ZoneInfo:
    return ZoneInfo(schedule_cfg.timezone)


def get_scheduler_now(schedule_cfg: ScheduleConfig, now_utc: datetime | None = None) -> SchedulerNow:
    current_utc = now_utc or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    else:
        current_utc = current_utc.astimezone(timezone.utc)
    zone = get_scheduler_zoneinfo(schedule_cfg)
    current_local = current_utc.astimezone(zone)
    return SchedulerNow(
        now_utc=current_utc,
        now_local=current_local,
        today_utc=current_utc.date(),
        today_local=current_local.date(),
        timezone_name=schedule_cfg.timezone,
    )
