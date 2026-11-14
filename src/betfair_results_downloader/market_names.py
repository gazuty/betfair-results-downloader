"""
Website-style market names for the results feed.

The dashboard parses sport, track and country out of the name the Betfair
website gives a market, "Horse Racing / Morphettville (AUS) 14th Nov : R7
1200m Hcp": the sport before the slash, the event before the colon, the
country in brackets (none for UK and Irish meetings, which the dashboard
reads as UK). Every name this module builds keeps that shape.

Sources, best first:

- Racing: the racecard captured before the race (see :mod:`racecard`), then
  the catalogue cache the enrichment step keeps, then a fallback built from
  ``itemDescription``. The fallback's track comes from what earlier
  racecard captures recorded for the same venue; a venue that maps to more
  than one track, or to none (Ascot, Newcastle and York exist in two
  countries), gets the country "Unknown" rather than a guess -- left
  without brackets it would silently count as UK.
- Everything else: ``itemDescription``, which already carries the website's
  event and market names ("Ham-Kam v Molde" / "Over/Under 0.5 Goals"), then
  the catalogue cache.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from .config import EVENTTYPE_GREYHOUNDS, EVENTTYPE_HORSES
from .csv_utils import decimal_key
from .reporting.schema import sport_label

SOURCE_RACECARD = "racecard"
SOURCE_CATALOGUE = "catalogue"
SOURCE_ITEM_DESCRIPTION = "item_description"
SOURCE_FALLBACK = "fallback"

UNKNOWN_COUNTRY = "Unknown"

# The website's sport labels differ from the API's event type names for
# these three; every other label is the same (checked against 21 months of
# website exports: Golf, Tennis, Cycling, Motor Sport, Darts and the rest).
_WEBSITE_LABEL_OVERRIDES = {
    1: "Football",
    EVENTTYPE_HORSES: "Horse Racing",
    EVENTTYPE_GREYHOUNDS: "Greyhound Racing",
}

_RACING_IDS = {EVENTTYPE_HORSES, EVENTTYPE_GREYHOUNDS}

# itemDescription prefixes racing events with the start time in the
# account's time zone: "18:01 Morphettville".
_RE_EVENT_TIME = re.compile(r"^\d{1,2}:\d{2}\s+")
_RE_EVENT_TIME_PARTS = re.compile(r"^(\d{1,2}):(\d{2})\s")

# The trailing date on a racecard event name: "Morphettville (AUS) 14th Nov".
_RE_EVENT_DATE = re.compile(r"\s+\d{1,2}(?:st|nd|rd|th)\s+[A-Za-z]{3}$")


def _event_type_int(event_type_id: Any) -> Optional[int]:
    try:
        if event_type_id is None or pd.isna(event_type_id):
            return None
        return int(float(event_type_id))
    except (TypeError, ValueError):
        return None


def website_sport(event_type_id: Any) -> str:
    """The sport label the Betfair website puts before the slash."""
    numeric = _event_type_int(event_type_id)
    if numeric in _WEBSITE_LABEL_OVERRIDES:
        return _WEBSITE_LABEL_OVERRIDES[numeric]
    return sport_label(event_type_id)


def is_racing(event_type_id: Any) -> bool:
    return _event_type_int(event_type_id) in _RACING_IDS


def item_description_venue(event_desc: Any) -> str:
    """'18:01 Morphettville' -> 'Morphettville'; '' for a missing value."""
    if event_desc is None or pd.isna(event_desc):
        return ""
    return _RE_EVENT_TIME.sub("", str(event_desc)).strip()


def racecard_track(event_name: Any) -> str:
    """'Morphettville (AUS) 14th Nov' -> 'Morphettville (AUS)'."""
    if event_name is None or pd.isna(event_name):
        return ""
    return _RE_EVENT_DATE.sub("", str(event_name)).strip()


def website_day(day: date) -> str:
    """The website's event date: 1st, 2nd, 3rd, 4th ... 11th, 12th, 13th."""
    n = day.day
    suffix = (
        "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    )
    return f"{n}{suffix} {day.strftime('%b')}"


def _clean(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _by_key(frame: Optional[pd.DataFrame], columns: list[str]) -> dict[str, dict]:
    """Rows of ``frame`` keyed by numeric marketId; the last row wins."""
    if frame is None or frame.empty or "marketId" not in frame.columns:
        return {}
    present = [c for c in columns if c in frame.columns]
    out: dict[str, dict] = {}
    for row in frame[["marketId", *present]].to_dict("records"):
        key = decimal_key(row["marketId"])
        if key:
            out[key] = row
    return out


# Venues the website history (Jan 2025 - Nov 2026) shows with racing in two
# countries. Never resolved from a single sighting: the store may only have
# seen one side so far (unbet racecard rows are pruned after three days).
KNOWN_MULTI_COUNTRY_VENUES = frozenset(
    {"Ascot", "Hamilton", "Illawarra Grange", "Newcastle", "Sandown", "Warwick", "York"}
)

# A racecard track usable as venue evidence: no brackets (UK/IRE) or a
# country code ("(AUS)", "(US)"). Pool-bet events such as "Romford (F/C)"
# share the venue but are not a second country, so they are skipped rather
# than making every Romford race ambiguous.
_RE_COUNTRY_TRACK = re.compile(r"^[^()]+(?:\s\([A-Z]{2,3}\))?$")
_RE_BRACKETS = re.compile(r"\s*\([^)]*\)")


def learn_venue_tracks(
    markets: pd.DataFrame, racecard: Optional[pd.DataFrame]
) -> dict[tuple[int, str], str]:
    """
    ``(eventTypeId, venue) -> racecard track`` for venues that only ever
    mapped to one track.

    Evidence comes from two places, and a venue that maps to more than one
    track in either is ambiguous:

    - every racecard row, bet on or not, under both its catalogue venue and
      its bare track name -- so seeing Ascot (AUS) and Ascot (UK) on the
      card is enough, even if only one of them was ever bet on;
    - markets that have both a racecard capture and an ``itemDescription``
      event, which teach the short names ``itemDescription`` uses
      ("Evangeline" for "Evangeline Downs (US)").

    :data:`KNOWN_MULTI_COUNTRY_VENUES` are never resolved. ``markets`` needs
    ``_key``, ``eventTypeId`` and ``evt_eventName``.
    """
    if racecard is None or racecard.empty:
        return {}
    seen: dict[tuple[int, str], set[str]] = defaultdict(set)
    for et, event_name, venue in zip(
        racecard["eventTypeId"], racecard["eventName"], racecard["venue"]
    ):
        numeric = _event_type_int(et)
        track = racecard_track(event_name)
        if numeric is None or not track or not _RE_COUNTRY_TRACK.match(track):
            continue
        for name in {_clean(venue), _RE_BRACKETS.sub("", track).strip()}:
            if name:
                seen[(numeric, name)].add(track)

    cards = _by_key(racecard, ["eventName"])
    for key, et, desc in zip(
        markets["_key"], markets["eventTypeId"], markets["evt_eventName"]
    ):
        card = cards.get(key)
        numeric = _event_type_int(et)
        venue = item_description_venue(desc)
        track = racecard_track(card["eventName"]) if card else ""
        if numeric is None or not venue or not track:
            continue
        if not _RE_COUNTRY_TRACK.match(track):
            continue
        seen[(numeric, venue)].add(track)
    return {
        k: next(iter(v))
        for k, v in seen.items()
        if len(v) == 1 and k[1] not in KNOWN_MULTI_COUNTRY_VENUES
    }


def fallback_event_day(event_desc: Any, settled: pd.Timestamp) -> date:
    """
    The event date for a fallback name: the day the race was advertised to
    start, not the day it settled.

    ``itemDescription`` gives the start time in the account's time zone
    (Sydney), "23:59 Bendigo"; a race that starts before midnight and
    settles after it belongs to the earlier day's card. Without a start
    time the settled date is the best available. For an overseas meeting
    the website's date is the venue's local date, which a fallback cannot
    know; the dashboard drops the date from track names, so this only
    affects how the name reads.
    """
    match = _RE_EVENT_TIME_PARTS.match(_clean(event_desc))
    if not match:
        return settled.date()
    start = settled.replace(
        hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0
    )
    if start > settled:
        start -= timedelta(days=1)
    return start.date()


def resolve_market_names(
    markets: pd.DataFrame,
    *,
    racecard: Optional[pd.DataFrame],
    catalogue: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """
    Add ``sport``, ``market`` and ``nameSource`` to one row per market.

    ``markets`` needs ``_key`` (numeric marketId), ``eventTypeId``,
    ``evt_eventName`` and ``mkt_marketName`` (both from itemDescription),
    and ``settled_dt_local`` for the fallback's event date.
    """
    out = markets.copy()
    cards = _by_key(racecard, ["eventName", "marketName"])
    cats = _by_key(catalogue, ["evt_eventName", "mkt_marketName"])
    venue_tracks = learn_venue_tracks(out, racecard)

    names: list[str] = []
    sources: list[str] = []
    sports: list[str] = []
    for key, et, desc, mdesc, settled in zip(
        out["_key"],
        out["eventTypeId"],
        out["evt_eventName"],
        out["mkt_marketName"],
        out["settled_dt_local"],
    ):
        sport = website_sport(et)
        sports.append(sport)
        card = cards.get(key)
        cat = cats.get(key)
        cat_event = _clean(cat.get("evt_eventName")) if cat else ""
        cat_market = _clean(cat.get("mkt_marketName")) if cat else ""
        item_event, item_market = _clean(desc), _clean(mdesc)

        if is_racing(et):
            if card and _clean(card.get("eventName")):
                event, market, source = (
                    _clean(card["eventName"]),
                    _clean(card.get("marketName")),
                    SOURCE_RACECARD,
                )
            elif cat_event:
                event, market, source = cat_event, cat_market, SOURCE_CATALOGUE
            else:
                venue = item_description_venue(item_event)
                track = venue_tracks.get((_event_type_int(et), venue))
                if not track:
                    track = f"{venue or 'Unknown'} ({UNKNOWN_COUNTRY})"
                when = (
                    f" {website_day(fallback_event_day(item_event, settled))}"
                    if pd.notna(settled)
                    else ""
                )
                event, market, source = (
                    f"{track}{when}",
                    item_market,
                    SOURCE_FALLBACK,
                )
        elif item_event:
            event, market, source = item_event, item_market, SOURCE_ITEM_DESCRIPTION
        elif cat_event:
            event, market, source = cat_event, cat_market, SOURCE_CATALOGUE
        else:
            event, market, source = "Unknown", item_market, SOURCE_FALLBACK

        names.append(f"{sport} / {event} : {market or 'Unknown'}")
        sources.append(source)

    out["sport"] = sports
    out["market"] = names
    out["nameSource"] = sources
    return out
