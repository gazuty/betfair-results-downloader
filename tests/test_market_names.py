"""Website-style names: the shape the dashboard parses sport, track and country from."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from betfair_results_downloader.market_names import (
    item_description_venue,
    racecard_track,
    resolve_market_names,
    website_day,
    website_sport,
)
from betfair_results_downloader.reporting.schema import SYDNEY_TZ


@pytest.mark.parametrize(
    "event_type_id,label",
    [
        ("1", "Football"),
        ("7", "Horse Racing"),
        ("4339", "Greyhound Racing"),
        ("2", "Tennis"),
        ("3503", "Darts"),
        ("999999", "Other (999999)"),
        ("", "Unknown"),
    ],
)
def test_sport_labels_match_the_website(event_type_id: str, label: str) -> None:
    assert website_sport(event_type_id) == label


@pytest.mark.parametrize(
    "day,text",
    [
        (date(2026, 11, 1), "1st Nov"),
        (date(2026, 11, 2), "2nd Nov"),
        (date(2026, 11, 3), "3rd Nov"),
        (date(2026, 11, 11), "11th Nov"),
        (date(2026, 11, 12), "12th Nov"),
        (date(2026, 11, 13), "13th Nov"),
        (date(2026, 11, 21), "21st Nov"),
        (date(2026, 11, 22), "22nd Nov"),
        (date(2026, 12, 31), "31st Dec"),
    ],
)
def test_website_day(day: date, text: str) -> None:
    assert website_day(day) == text


def test_venue_and_track_parsing() -> None:
    assert item_description_venue("18:01 Morphettville") == "Morphettville"
    assert item_description_venue("9:05 Del Mar") == "Del Mar"
    assert item_description_venue(None) == ""
    assert racecard_track("Morphettville (AUS) 14th Nov") == "Morphettville (AUS)"
    assert racecard_track("Uttoxeter 31st Dec") == "Uttoxeter"


def _markets(rows: list[tuple]) -> pd.DataFrame:
    """(key, eventTypeId, eventDesc, marketDesc, settled local ISO)."""
    df = pd.DataFrame(
        rows,
        columns=["_key", "eventTypeId", "evt_eventName", "mkt_marketName", "s"],
    )
    df["settled_dt_local"] = pd.to_datetime(df.pop("s")).dt.tz_localize(SYDNEY_TZ)
    return df


def _racecard(rows: list[tuple]) -> pd.DataFrame:
    """(marketId, eventName, marketName)."""
    return pd.DataFrame(rows, columns=["marketId", "eventName", "marketName"])


def test_racing_prefers_the_racecard_then_the_catalogue() -> None:
    markets = _markets(
        [
            ("1.1", "7", "18:01 Morphettville", "Win", "2026-11-14 18:04"),
            ("1.2", "4339", "19:00 The Meadows", "Win", "2026-11-02 22:57"),
        ]
    )
    racecard = _racecard([("1.1", "Morphettville (AUS) 14th Nov", "R7 1200m Hcp")])
    catalogue = pd.DataFrame(
        [("1.2", "The Meadows (AUS) 2nd Nov", "R12 525m Gr5")],
        columns=["marketId", "evt_eventName", "mkt_marketName"],
    )
    out = resolve_market_names(markets, racecard=racecard, catalogue=catalogue)

    assert out["market"].tolist() == [
        "Horse Racing / Morphettville (AUS) 14th Nov : R7 1200m Hcp",
        "Greyhound Racing / The Meadows (AUS) 2nd Nov : R12 525m Gr5",
    ]
    assert out["nameSource"].tolist() == ["racecard", "catalogue"]


def test_the_fallback_uses_the_track_learned_for_that_venue() -> None:
    markets = _markets(
        [
            # A market the racecard named, which teaches the venue...
            ("1.1", "7", "06:30 Del Mar", "Win", "2026-08-01 06:33"),
            # ...and one it missed at the same venue.
            ("1.2", "7", "07:00 Del Mar", "Win", "2026-08-02 07:03"),
        ]
    )
    racecard = _racecard([("1.1", "Del Mar (US) 31st Jul", "R3 6f Mdn")])
    out = resolve_market_names(markets, racecard=racecard, catalogue=None)

    assert out.loc[1, "market"] == "Horse Racing / Del Mar (US) 2nd Aug : Win"
    assert out.loc[1, "nameSource"] == "fallback"


def test_an_ambiguous_or_unseen_venue_is_named_unknown_not_uk() -> None:
    markets = _markets(
        [
            ("1.1", "7", "02:00 Ascot", "Win", "2026-06-20 02:05"),
            ("1.2", "7", "15:00 Ascot", "Win", "2026-06-20 15:05"),
            ("1.3", "7", "16:00 Ascot", "Win", "2026-06-21 16:05"),
            ("1.4", "4339", "20:00 Nowhere", "Win", "2026-06-21 20:05"),
        ]
    )
    # Ascot UK and Ascot AUS: the venue alone cannot say which.
    racecard = _racecard(
        [
            ("1.1", "Ascot 19th Jun", "1m Hcap"),
            ("1.2", "Ascot (AUS) 20th Jun", "R1 1000m"),
        ]
    )
    out = resolve_market_names(markets, racecard=racecard, catalogue=None)

    # Without brackets the dashboard would count these as UK.
    assert out.loc[2, "market"] == "Horse Racing / Ascot (Unknown) 21st Jun : Win"
    assert out.loc[3, "market"] == "Greyhound Racing / Nowhere (Unknown) 21st Jun : Win"


def test_sports_use_the_item_description_names() -> None:
    markets = _markets(
        [
            ("1.1", "1", "Ham-Kam v Molde", "Over/Under 0.5 Goals", "2026-09-14 01:06"),
            (
                "1.2",
                "3",
                "PGA The Players Championship 2026",
                "Winner",
                "2026-03-16 09:19",
            ),
        ]
    )
    out = resolve_market_names(markets, racecard=None, catalogue=None)
    assert out["market"].tolist() == [
        "Football / Ham-Kam v Molde : Over/Under 0.5 Goals",
        "Golf / PGA The Players Championship 2026 : Winner",
    ]
    assert set(out["nameSource"]) == {"item_description"}
