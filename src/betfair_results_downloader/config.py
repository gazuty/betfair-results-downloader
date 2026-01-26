from __future__ import annotations

from dataclasses import dataclass


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
