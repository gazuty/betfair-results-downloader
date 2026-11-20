"""
The venue store: which racecard track each racing venue name has meant.

The results feed names a settled racing market from ``itemDescription``,
which gives only a bare venue ("18:01 Ascot"). The country comes from what
the racecard showed for that venue, "Ascot (AUS)" or plain "Ascot" for a UK
or Irish meeting. The racecard store forgets a market nobody bet on after
three days, so on its own it can only vouch for venues raced in the last
few days, and about half of all racing markets were left with the country
"Unknown".

This store remembers every venue-to-track pairing ever seen, one row per
pairing, at ``<results_csv_dir>/.cache/venue_tracks.csv``. Every racecard
capture adds to it, and :func:`observations_from_names` seeds it from a
file of website-style market names. It is data, not code: which tracks an
account bets on stays out of the repository.

A venue with one track resolves to it. A venue with several is ambiguous,
and :mod:`market_names` decides whether the start time can tell them apart.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from .config import EVENTTYPE_GREYHOUNDS, EVENTTYPE_HORSES

VENUE_TRACKS_FILENAME = "venue_tracks.csv"
VENUE_TRACK_COLUMNS = ["eventTypeId", "venue", "track", "source", "firstSeenUtc"]

SOURCE_RACECARD = "racecard"
SOURCE_NAMES = "names"

# A track usable as evidence: no brackets (UK/IRE) or a country code
# ("(AUS)", "(US)"). Pool-bet events such as "Romford (F/C)" and "Kempton
# (RFC)" share the venue but are not a second country, so they are skipped
# rather than making every Romford or Kempton race ambiguous.
_RE_COUNTRY_TRACK = re.compile(r"^[^()]+(?:\s\((?P<tag>[A-Z]{2,3})\))?$")
# Bracketed tags shaped like a country code that are pool-bet markers.
POOL_BET_TAGS = frozenset({"RFC"})
# Country tags a seed from market names may teach the store. A name carries
# no country code to tell a country from a pool-bet marker, so only tags
# known to be countries are taken; a meeting in any other country is learnt
# from the racecard, which does carry the code.
SEED_COUNTRY_TAGS = frozenset(
    {"AUS", "NZL", "US", "FRA", "RSA", "UAE", "GER", "CHI", "ARG", "BRZ", "SIN"}
)
# The racecard's own country codes for meetings the website shows without
# brackets. A bracketed event in one of these is a pool-bet variant of the
# plain meeting whatever its tag, known to this module or not.
UNBRACKETED_COUNTRY_CODES = frozenset({"GB", "IE"})
_RE_BRACKETS = re.compile(r"\s*\([^)]*\)")
# The trailing date on a racecard event name: "Morphettville (AUS) 14th Nov".
_RE_EVENT_DATE = re.compile(r"\s+\d{1,2}(?:st|nd|rd|th)\s+[A-Za-z]{3}$")

_WEBSITE_SPORTS = {
    "Horse Racing": EVENTTYPE_HORSES,
    "Greyhound Racing": EVENTTYPE_GREYHOUNDS,
}
# "Horse Racing / Ascot (AUS) 14th Nov : R7 1200m Hcp"
_RE_WEBSITE_NAME = re.compile(r"^(?P<sport>[^/]+?) / (?P<event>.+?) : ")

Observation = tuple[int, str, str]


def resolve_venue_tracks_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / VENUE_TRACKS_FILENAME


def empty_venue_tracks_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=str) for c in VENUE_TRACK_COLUMNS})


def load_venue_tracks(path: Path) -> pd.DataFrame:
    """
    Load the store as strings. A missing file is an empty frame; an
    unreadable one raises so the caller decides -- an update must not
    replace a damaged store with one that has forgotten every venue.
    """
    path = Path(path)
    if not path.exists():
        return empty_venue_tracks_frame()
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [c for c in ("eventTypeId", "venue", "track") if c not in df.columns]
    if missing:
        raise ValueError(f"venue store has no '{missing[0]}' column")
    for col in VENUE_TRACK_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[VENUE_TRACK_COLUMNS]


def save_venue_tracks(df: pd.DataFrame, path: Path) -> None:
    """Write via a temp file and rename so a crash cannot leave a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        df.reindex(columns=VENUE_TRACK_COLUMNS).to_csv(tmp_path, index=False)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def event_track(event_name: Any) -> str:
    """'Morphettville (AUS) 14th Nov' -> 'Morphettville (AUS)'."""
    if event_name is None or pd.isna(event_name):
        return ""
    return _RE_EVENT_DATE.sub("", str(event_name)).strip()


def track_tag(track: str) -> Optional[str]:
    """'Ascot (AUS)' -> 'AUS'; None for a plain track or anything else."""
    match = _RE_COUNTRY_TRACK.match(track) if track else None
    return match.group("tag") if match else None


def is_country_track(track: str, country_code: Any = "") -> bool:
    """
    Whether ``track`` names a meeting and its country: no brackets, or a
    country tag. ``country_code`` is the racecard's ISO code for the event
    when there is one; it catches a pool-bet tag this module has not met.
    """
    match = _RE_COUNTRY_TRACK.match(track) if track else None
    if not match:
        return False
    tag = match.group("tag")
    if tag is None:
        return True
    if tag in POOL_BET_TAGS:
        return False
    return str(country_code or "").strip().upper() not in UNBRACKETED_COUNTRY_CODES


def bare_venue(track: str) -> str:
    """'Ascot (AUS)' -> 'Ascot'."""
    return _RE_BRACKETS.sub("", track).strip()


def _event_type(value: Any) -> Optional[int]:
    try:
        if value is None or pd.isna(value):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def observations_from_racecard(racecard: Optional[pd.DataFrame]) -> set[Observation]:
    """
    Every ``(eventTypeId, venue, track)`` a racecard frame vouches for.

    Each row counts under both its catalogue venue and its bare track name,
    so seeing Ascot (AUS) and Ascot on the card is enough to know the name
    is shared, even if only one of them was ever bet on.
    """
    found: set[Observation] = set()
    if racecard is None or racecard.empty:
        return found
    codes = (
        racecard["countryCode"]
        if "countryCode" in racecard.columns
        else [""] * len(racecard)
    )
    for et, event_name, venue, code in zip(
        racecard["eventTypeId"], racecard["eventName"], racecard["venue"], codes
    ):
        numeric = _event_type(et)
        track = event_track(event_name)
        if numeric is None or not is_country_track(track, code):
            continue
        for name in {str(venue or "").strip(), bare_venue(track)}:
            if name:
                found.add((numeric, name, track))
    return found


def observations_from_names(names: Iterable[Any]) -> set[Observation]:
    """
    Pairings read from website-style market names, for seeding the store.

    "Horse Racing / Ascot (AUS) 14th Nov : R7 1200m Hcp" vouches for Ascot
    meaning "Ascot (AUS)". A name that is not racing, has no event date, or
    whose track is not a plain one or tagged with a known country (a
    pool-bet event, a track already marked Unknown) is skipped: the seed
    must not teach the store a guess.
    """
    found: set[Observation] = set()
    for name in names:
        if name is None or pd.isna(name):
            continue
        match = _RE_WEBSITE_NAME.match(str(name).strip())
        if not match:
            continue
        numeric = _WEBSITE_SPORTS.get(match.group("sport").strip())
        event = match.group("event").strip()
        track = event_track(event)
        if numeric is None or track == event or not is_country_track(track):
            continue
        tag = track_tag(track)
        if tag is not None and tag not in SEED_COUNTRY_TAGS:
            continue
        found.add((numeric, bare_venue(track), track))
    return found


def merge_venue_tracks(
    existing: pd.DataFrame,
    observations: Iterable[Observation],
    *,
    source: str,
    seen_utc: str,
) -> tuple[pd.DataFrame, int]:
    """
    Add the pairings the store does not hold yet. Returns ``(store, how
    many were new)``. Nothing is ever removed or rewritten: a pairing that
    was true once stays evidence that the name has had that meaning.
    """
    held = {
        (_event_type(et), venue, track)
        for et, venue, track in zip(
            existing["eventTypeId"], existing["venue"], existing["track"]
        )
    }
    fresh = sorted(set(observations) - held)
    if not fresh:
        return existing.reindex(columns=VENUE_TRACK_COLUMNS), 0
    rows = pd.DataFrame(
        [
            {
                "eventTypeId": str(et),
                "venue": venue,
                "track": track,
                "source": source,
                "firstSeenUtc": seen_utc,
            }
            for et, venue, track in fresh
        ],
        columns=VENUE_TRACK_COLUMNS,
    )
    merged = pd.concat([existing, rows], ignore_index=True)
    return merged.reindex(columns=VENUE_TRACK_COLUMNS), len(fresh)


def venue_evidence(store: Optional[pd.DataFrame]) -> dict[tuple[int, str], set[str]]:
    """``(eventTypeId, venue) -> every track the store has seen for it``."""
    evidence: dict[tuple[int, str], set[str]] = {}
    if store is None or store.empty:
        return evidence
    for et, venue, track in zip(store["eventTypeId"], store["venue"], store["track"]):
        numeric = _event_type(et)
        venue, track = str(venue).strip(), str(track).strip()
        if numeric is None or not venue or not is_country_track(track):
            continue
        evidence.setdefault((numeric, venue), set()).add(track)
    return evidence
