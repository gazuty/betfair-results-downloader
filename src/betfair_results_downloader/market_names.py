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
  ``itemDescription``. The fallback's track comes from what racecard
  captures have recorded for the same venue, now and in the past (see
  :mod:`venue_tracks`). A horse racing venue that has meant both an
  Australian and a UK or Irish track (Ascot, Newcastle, York) is told
  apart by its start time, since the two countries never race at the same
  hour. Any other venue that maps to more than one track, or to none, gets
  the country "Unknown" rather than a guess -- left without brackets it
  would silently count as UK.
- Everything else: ``itemDescription``, which already carries the website's
  event and market names ("Ham-Kam v Molde" / "Over/Under 0.5 Goals"), then
  the catalogue cache.
"""

from __future__ import annotations

import re
from datetime import date, time, timedelta, timezone
from typing import Any, Optional

import pandas as pd

from .config import EVENTTYPE_GREYHOUNDS, EVENTTYPE_HORSES
from .csv_utils import decimal_key
from .reporting.schema import sport_label
from .venue_tracks import (
    bare_venue,
    event_track,
    is_country_track,
    observations_from_racecard,
    venue_evidence,
)

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
    return event_track(event_name)


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
# countries. Never resolved from a single sighting: the evidence may only
# have seen one side so far.
KNOWN_MULTI_COUNTRY_VENUES = frozenset(
    {"Ascot", "Hamilton", "Illawarra Grange", "Newcastle", "Sandown", "Warwick", "York"}
)

AUSTRALIAN_TAG = "(AUS)"

# Australian and UK or Irish horse racing do not overlap in the day. Over
# the website history (Jan 2025 - 8 Nov 2026; 10,878 Australian and 13,993
# UK and Irish horse markets) the six venue names the two share never had
# an Australian race settle after 10:22 UTC or a UK one before 11:58 UTC,
# summer and winter alike. A start in the half hour between the two limits
# is left unresolved rather than given to either side. So is a start after
# the UK's evening racing has finished (the latest in that history settled
# at 20:40 UTC): by then it is mid-morning in eastern Australia.
AUSTRALIAN_STARTS_END_UTC = time(11, 0)
UK_STARTS_BEGIN_UTC = time(11, 30)
UK_STARTS_END_UTC = time(21, 30)


def venue_track_evidence(
    markets: pd.DataFrame,
    racecard: Optional[pd.DataFrame],
    venue_store: Optional[pd.DataFrame] = None,
) -> dict[tuple[int, str], set[str]]:
    """
    ``(eventTypeId, venue) -> every racecard track that venue has meant``.

    Evidence comes from three places:

    - the venue store, which remembers every pairing a racecard capture or
      a seed has ever recorded;
    - every row of the current racecard, bet on or not, under both its
      catalogue venue and its bare track name;
    - markets that have both a racecard capture and an ``itemDescription``
      event, which teach the short names ``itemDescription`` uses
      ("Evangeline" for "Evangeline Downs (US)").

    ``markets`` needs ``_key``, ``eventTypeId`` and ``evt_eventName``.
    """
    seen = venue_evidence(venue_store)
    for numeric, name, track in observations_from_racecard(racecard):
        seen.setdefault((numeric, name), set()).add(track)

    cards = _by_key(racecard, ["eventName", "countryCode"])
    if cards:
        for key, et, desc in zip(
            markets["_key"], markets["eventTypeId"], markets["evt_eventName"]
        ):
            card = cards.get(key)
            numeric = _event_type_int(et)
            venue = item_description_venue(desc)
            track = racecard_track(card["eventName"]) if card else ""
            code = card.get("countryCode", "") if card else ""
            if numeric is None or not venue or not is_country_track(track, code):
                continue
            seen.setdefault((numeric, venue), set()).add(track)
    return seen


def _track_by_start_time(tracks: set[str], start: Optional[pd.Timestamp]) -> str:
    """
    The one of an Australian and a UK or Irish track that races at
    ``start``; '' when the pair is not that, or the time does not decide.
    """
    if start is None or pd.isna(start) or len(tracks) != 2:
        return ""
    australian = [t for t in tracks if t.endswith(AUSTRALIAN_TAG)]
    home = [t for t in tracks if "(" not in t]
    if len(australian) != 1 or len(home) != 1:
        return ""
    at = start.tz_convert(timezone.utc).time()
    if at < AUSTRALIAN_STARTS_END_UTC:
        return australian[0]
    if UK_STARTS_BEGIN_UTC <= at < UK_STARTS_END_UTC:
        return home[0]
    return ""


def resolve_track(
    tracks: Optional[set[str]],
    *,
    event_type_id: Optional[int],
    venue: str,
    start: Optional[pd.Timestamp],
) -> tuple[str, bool]:
    """
    The track a fallback name should carry, and whether the start time was
    what decided it. ``('', False)`` when the evidence does not settle it.

    One track is the answer unless the venue is known to exist in two
    countries, where a single sighting proves nothing. Two tracks are
    separated by start time for horse racing only: greyhound meetings in
    Australia and the UK overlap for most of the day.
    """
    if not tracks:
        return "", False
    # The racecard files some meetings under a neighbouring course's venue
    # (a Vaal card under Turffontein). Where the venue has a track of its
    # own name, that is the one a bare venue means.
    own = {track for track in tracks if bare_venue(track) == venue}
    tracks = own or tracks
    if len(tracks) == 1 and venue not in KNOWN_MULTI_COUNTRY_VENUES:
        return next(iter(tracks)), False
    if event_type_id != EVENTTYPE_HORSES:
        return "", False
    timed = _track_by_start_time(tracks, start)
    return timed, bool(timed)


def fallback_event_start(
    event_desc: Any, settled: pd.Timestamp
) -> Optional[pd.Timestamp]:
    """
    When the race was advertised to start, in ``settled``'s time zone;
    ``None`` when ``itemDescription`` gives no time.

    ``itemDescription`` gives the start time in the account's time zone
    (Sydney), "23:59 Bendigo"; it is the latest such time not after the
    settlement. The settlement is not offered in its place: an inquiry or a
    late settlement can put it hours after the race, in another country's
    racing hours.

    The day is stepped back on the wall clock and then given its zone: a
    day subtracted from a zoned time is 24 hours, which lands an hour out
    across a daylight-saving change.
    """
    match = _RE_EVENT_TIME_PARTS.match(_clean(event_desc))
    if not match:
        return None
    wall = settled.tz_localize(None)
    start = wall.replace(
        hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0
    )
    if start > wall:
        start -= timedelta(days=1)
    # A time on a clock-change night can be missing or occur twice; either
    # way the hour is not known, so it is moved forward or left unset.
    zoned = start.tz_localize(settled.tz, nonexistent="shift_forward", ambiguous="NaT")
    return None if pd.isna(zoned) else zoned


def fallback_event_day(event_desc: Any, settled: pd.Timestamp) -> date:
    """
    The event date for a fallback name: the day the race was advertised to
    start, not the day it settled.

    A race that starts before midnight and settles after it belongs to the
    earlier day's card. Without a start time the settled date is the best
    available. For an overseas meeting the website's date is the venue's
    local date, which a fallback cannot know; the dashboard drops the date
    from track names, so this only affects how the name reads.
    """
    start = fallback_event_start(event_desc, settled)
    return (settled if start is None else start).date()


def resolve_market_names(
    markets: pd.DataFrame,
    *,
    racecard: Optional[pd.DataFrame],
    catalogue: Optional[pd.DataFrame],
    venue_store: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Add ``sport``, ``market``, ``nameSource`` and ``countryByStartTime``
    (true where a shared venue name was settled by the race's start time)
    to one row per market.

    ``markets`` needs ``_key`` (numeric marketId), ``eventTypeId``,
    ``evt_eventName`` and ``mkt_marketName`` (both from itemDescription),
    and ``settled_dt_local`` for the fallback's event date.
    """
    out = markets.copy()
    cards = _by_key(racecard, ["eventName", "marketName"])
    cats = _by_key(catalogue, ["evt_eventName", "mkt_marketName"])
    evidence = venue_track_evidence(out, racecard, venue_store)

    names: list[str] = []
    sources: list[str] = []
    sports: list[str] = []
    timed: list[bool] = []
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
        by_time = False

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
                numeric = _event_type_int(et)
                start = (
                    fallback_event_start(item_event, settled)
                    if pd.notna(settled)
                    else None
                )
                track, by_time = resolve_track(
                    evidence.get((numeric, venue)),
                    event_type_id=numeric,
                    venue=venue,
                    start=start,
                )
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
        timed.append(by_time)

    out["sport"] = sports
    out["countryByStartTime"] = timed
    out["market"] = names
    out["nameSource"] = sources
    return out
