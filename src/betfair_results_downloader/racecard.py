"""
Racecard capture: the full name of every racing market, taken before it runs.

The Betfair website names a market "Horse Racing / Morphettville (AUS) 14th
Nov : R7 1200m Hcp" -- event name, then market name. The downloader's own
sources cannot recover that after the race:

- ``listClearedOrders`` ``itemDescription`` names a racing market
  "18:01 Morphettville" / "Win": no country, race number, distance or class.
- ``listMarketCatalogue`` has the full name but stops returning a market
  once it is settled. Verified live on 2026-11-14: 0 of 288 settled racing
  markets came back, including every one settled only 2-6 hours earlier.

So the names are captured from the forward racecard instead: every racing
market starting in the next :data:`DEFAULT_HORIZON_HOURS`, on every run and
on a cheap hourly job. Racing markets are loaded about a day ahead, so an
hourly capture with a 30-hour horizon sees each one many times. Sports and
outright markets do not need this: ``itemDescription`` already carries their
full names ("Ham-Kam v Molde" / "Over/Under 0.5 Goals").

The store lives at ``<results_csv_dir>/.cache/racecard.csv`` keyed by
marketId. Only markets that were bet on are kept beyond
:data:`DEFAULT_KEEP_UNBET_HOURS`; the rest of the worldwide racecard is
dropped once it can no longer be bet on.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

import pandas as pd
from betfairlightweight import filters

from .betfair_net import retry_betfair_call
from .config import EVENTTYPE_GREYHOUNDS, EVENTTYPE_HORSES
from .csv_utils import decimal_key
from .market_status import _fmt_utc, _now_utc, _parse_utc

logger = logging.getLogger(__name__)

# The store lock is advisory and platform-specific: flock on macOS/Linux,
# msvcrt.locking on Windows (still documented for manual use). Imported
# defensively so the module, and the feed that imports it, load anywhere.
try:  # pragma: no cover - which branch runs depends on the platform
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
try:  # pragma: no cover
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]

RACECARD_FILENAME = "racecard.csv"

RACECARD_COLUMNS: list[str] = [
    "marketId",
    "eventTypeId",
    "eventId",
    # The website's event name: "Morphettville (AUS) 14th Nov". UK and
    # Irish meetings carry no country suffix ("Uttoxeter 31st Dec").
    "eventName",
    "venue",
    "countryCode",
    # The website's market name: "R7 1200m Hcp".
    "marketName",
    "marketStartTime",
    "capturedUtc",
]

RACING_EVENT_TYPE_IDS: tuple[str, ...] = (
    str(EVENTTYPE_HORSES),
    str(EVENTTYPE_GREYHOUNDS),
)

# How far ahead to capture. Racing markets are loaded about a day ahead;
# 30 hours covers tomorrow's card from any run time.
DEFAULT_HORIZON_HOURS = 30

# How far back. A market that has already started but not yet settled is
# still returned (suspended or in play), so a run that starts mid-race
# still records it.
DEFAULT_LOOKBACK_HOURS = 3

# The first split of the capture range. listMarketCatalogue returns at most
# MAX_RESULTS markets per call, and a full day of worldwide racing across
# all market types is more than that, so the range is fetched in slices.
DEFAULT_SLICE_HOURS = 6

# Betfair's cap for listMarketCatalogue. EVENT, EVENT_TYPE and
# MARKET_START_TIME weigh nothing, so the full page is allowed.
MAX_RESULTS = 1000

# A slice that comes back full is halved until it fits. Below this width
# the slice is kept as it is and the capture is reported as truncated.
MIN_SLICE = timedelta(minutes=15)

# Racecard rows for markets nobody bet on are dropped after this long. A
# market can only be bet on before it settles, so three days leaves the
# commission store (the record of which markets were bet on) ample time to
# catch up even after a run of failed downloads.
DEFAULT_KEEP_UNBET_HOURS = 72


@dataclass(frozen=True)
class RacecardResult:
    captured: int
    stored: int
    truncated_slices: int
    message: str


def resolve_racecard_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / RACECARD_FILENAME


def empty_racecard_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=str) for c in RACECARD_COLUMNS})


def load_racecard(path: Path) -> pd.DataFrame:
    """
    Load the store as strings. A missing file is an empty frame; an
    unreadable one raises so the caller decides -- an update must not
    replace a damaged store with one that has forgotten every name.
    """
    path = Path(path)
    if not path.exists():
        return empty_racecard_frame()
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in RACECARD_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[RACECARD_COLUMNS]


def save_racecard(df: pd.DataFrame, path: Path) -> None:
    """
    Write via a temp file and rename so a crash cannot leave a torn file.
    The temp name carries the process id so two writers never share it;
    :func:`update_racecard` also serialises them with :func:`_store_lock`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        df.reindex(columns=RACECARD_COLUMNS).to_csv(tmp_path, index=False)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _iso(value: Any) -> str:
    """A catalogue datetime as ISO-8601 UTC; '' when absent."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return _fmt_utc(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    return str(value)


def catalogue_to_row(cat: Any, captured_utc: str) -> Optional[dict[str, str]]:
    """
    One racecard row from a betfairlightweight ``MarketCatalogue``.

    Returns ``None`` when the catalogue has no market id or no event name:
    a row without the event name is no better than the fallback name and
    would shadow a later complete capture of the same market.
    """
    market_id = str(getattr(cat, "market_id", "") or "").strip()
    event = getattr(cat, "event", None)
    event_name = str(getattr(event, "name", "") or "").strip() if event else ""
    if not market_id or not event_name:
        return None
    event_type = getattr(cat, "event_type", None)
    return {
        "marketId": market_id,
        "eventTypeId": str(getattr(event_type, "id", "") or ""),
        "eventId": str(getattr(event, "id", "") or ""),
        "eventName": event_name,
        "venue": str(getattr(event, "venue", "") or ""),
        "countryCode": str(getattr(event, "country_code", "") or ""),
        "marketName": str(getattr(cat, "market_name", "") or "").strip(),
        "marketStartTime": _iso(getattr(cat, "market_start_time", None)),
        "capturedUtc": captured_utc,
    }


def _call_catalogue(client: Any, start: datetime, end: datetime) -> list[Any]:
    return retry_betfair_call(
        lambda: client.betting.list_market_catalogue(
            filter=filters.market_filter(
                event_type_ids=list(RACING_EVENT_TYPE_IDS),
                market_start_time={"from": _fmt_utc(start), "to": _fmt_utc(end)},
            ),
            max_results=MAX_RESULTS,
            market_projection=["EVENT", "EVENT_TYPE", "MARKET_START_TIME"],
        )
    )


def fetch_racecard(
    client: Any,
    *,
    now: Optional[datetime] = None,
    horizon_hours: int = DEFAULT_HORIZON_HOURS,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    slice_hours: int = DEFAULT_SLICE_HOURS,
) -> tuple[list[Any], int]:
    """
    Every racing market starting in ``[now - lookback, now + horizon]``.

    Returns ``(catalogues, truncated_slices)``. A slice that returns a full
    page may have been cut short, so it is halved and fetched again; a
    slice that is still full at :data:`MIN_SLICE` is kept and counted as
    truncated so the caller can say the capture is incomplete.
    """
    now_utc = _now_utc(now)
    pending = []
    cursor = now_utc - timedelta(hours=lookback_hours)
    end = now_utc + timedelta(hours=horizon_hours)
    while cursor < end:
        upper = min(cursor + timedelta(hours=slice_hours), end)
        pending.append((cursor, upper))
        cursor = upper

    found: list[Any] = []
    truncated = 0
    while pending:
        start, stop = pending.pop(0)
        cats = list(_call_catalogue(client, start, stop))
        if len(cats) >= MAX_RESULTS and (stop - start) > MIN_SLICE:
            middle = start + (stop - start) / 2
            pending[:0] = [(start, middle), (middle, stop)]
            continue
        if len(cats) >= MAX_RESULTS:
            truncated += 1
        found.extend(cats)
    return found, truncated


def merge_racecard(existing: pd.DataFrame, rows: Iterable[dict]) -> pd.DataFrame:
    """Upsert by market; the latest capture of a market wins."""
    fresh = pd.DataFrame(list(rows), columns=RACECARD_COLUMNS)
    if fresh.empty:
        return existing.reindex(columns=RACECARD_COLUMNS)
    merged = pd.concat([existing, fresh], ignore_index=True)
    merged["_key"] = merged["marketId"].map(decimal_key)
    merged = merged.drop_duplicates(subset=["_key"], keep="last")
    return merged.drop(columns="_key").reset_index(drop=True)


def prune_racecard(
    df: pd.DataFrame,
    *,
    bet_market_keys: set[str],
    now: Optional[datetime] = None,
    keep_unbet_hours: int = DEFAULT_KEEP_UNBET_HOURS,
) -> pd.DataFrame:
    """
    Keep markets that were bet on, and any market recent enough that it
    may still be. ``bet_market_keys`` are :func:`decimal_key` values.

    A row with an unparseable start time is kept: dropping it would lose a
    name the feed may need, and the store stays small either way.
    """
    if df.empty:
        return df
    cutoff = _now_utc(now) - timedelta(hours=keep_unbet_hours)
    starts = _parse_utc(df["marketStartTime"])
    recent = starts.isna() | (starts >= cutoff)
    was_bet = df["marketId"].map(decimal_key).isin(bet_market_keys)
    return df.loc[recent | was_bet].reset_index(drop=True)


@contextmanager
def _store_lock(path: Path) -> Iterator[None]:
    """
    Exclusive lock on the store for one load-merge-save.

    The hourly job and a scheduled run can overlap (a run that lasts past
    half past, or a run time configured at :30). Without the lock the later
    save would replace the store with a merge that never saw the other
    process's capture. ``flock`` is released when the file closes, including
    when the process dies, so a crash cannot leave the store locked.
    """
    lock_path = Path(path).with_name(Path(path).name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows only
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            logger.warning(
                "No file locking on this platform; overlapping racecard "
                "captures may lose one capture's rows until the next run."
            )
            yield


def update_racecard(
    *,
    client: Any,
    cache_dir: Path,
    bet_market_keys: set[str],
    now: Optional[datetime] = None,
    status_cb: Optional[Callable[[str], None]] = None,
) -> RacecardResult:
    """
    Capture the forward racecard, merge it into the store, prune, save.

    Raises on an API failure or an unreadable store, leaving the store on
    disk untouched; the caller decides how loudly to report it. Concurrent
    updates are serialised, so each merges into the other's result.
    """
    now_utc = _now_utc(now)
    path = resolve_racecard_path(cache_dir)
    # Fetch outside the lock: the API calls are the slow part, and the
    # store is only read and written once the capture is in hand.
    cats, truncated = fetch_racecard(client, now=now_utc)
    captured_utc = _fmt_utc(now_utc)
    rows = [r for r in (catalogue_to_row(c, captured_utc) for c in cats) if r]
    with _store_lock(path):
        store = load_racecard(path)
        merged = merge_racecard(store, rows)
        pruned = prune_racecard(merged, bet_market_keys=bet_market_keys, now=now_utc)
        save_racecard(pruned, path)

    message = (
        f"Racecard: captured {len(rows):,} racing markets; store holds {len(pruned):,}."
    )
    if truncated:
        message += (
            f" WARNING: {truncated} slice(s) returned a full page at the "
            f"minimum width, so some markets may be missing."
        )
    if status_cb:
        status_cb(message)
    logger.info(message)
    return RacecardResult(
        captured=len(rows),
        stored=len(pruned),
        truncated_slices=truncated,
        message=message,
    )
