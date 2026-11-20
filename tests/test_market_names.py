"""Website-style names: the shape the dashboard parses sport, track and country from."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from betfair_results_downloader.market_names import (
    fallback_event_start,
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
    """(marketId, eventName, marketName[, eventTypeId[, venue]]); horses by default."""
    full = []
    for row in rows:
        mid, event, market, *rest = row
        et = rest[0] if rest else "7"
        venue = rest[1] if len(rest) > 1 else event.split(" (")[0].rsplit(" ", 2)[0]
        full.append((mid, event, market, et, venue))
    return pd.DataFrame(
        full, columns=["marketId", "eventName", "marketName", "eventTypeId", "venue"]
    )


def test_racing_prefers_the_racecard_then_the_catalogue() -> None:
    markets = _markets(
        [
            ("1.1", "7", "18:01 Morphettville", "Win", "2026-11-14 18:04"),
            ("1.2", "4339", "19:00 The Meadows", "Win", "2026-11-02 22:57"),
        ]
    )
    racecard = _racecard(
        [("1.1", "Morphettville (AUS) 14th Nov", "R7 1200m Hcp", "7", "Morphettville")]
    )
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
    racecard = _racecard(
        [("1.1", "Del Mar (US) 31st Jul", "R3 6f Mdn", "7", "Del Mar")]
    )
    out = resolve_market_names(markets, racecard=racecard, catalogue=None)

    assert out.loc[1, "market"] == "Horse Racing / Del Mar (US) 2nd Aug : Win"
    assert out.loc[1, "nameSource"] == "fallback"


def _venues(rows: list[tuple]) -> pd.DataFrame:
    """(eventTypeId, venue, track)."""
    return pd.DataFrame(rows, columns=["eventTypeId", "venue", "track"])


ASCOT_BOTH = [("7", "Ascot", "Ascot"), ("7", "Ascot", "Ascot (AUS)")]


def test_an_unseen_venue_is_named_unknown_not_uk() -> None:
    markets = _markets(
        [
            ("1.1", "7", "16:00 Nowhere", "Win", "2026-06-21 16:05"),
            ("1.2", "4339", "20:00 Nowhere", "Win", "2026-06-21 20:05"),
        ]
    )

    out = resolve_market_names(markets, racecard=None, catalogue=None)

    # Without brackets the dashboard would count these as UK.
    assert out["market"].tolist() == [
        "Horse Racing / Nowhere (Unknown) 21st Jun : Win",
        "Greyhound Racing / Nowhere (Unknown) 21st Jun : Win",
    ]
    assert not out["countryByStartTime"].any()


def test_the_venue_store_names_a_venue_the_racecard_no_longer_holds() -> None:
    markets = _markets([("1.1", "7", "16:00 Taree", "Win", "2026-06-21 16:05")])

    out = resolve_market_names(
        markets,
        racecard=None,
        catalogue=None,
        venue_store=_venues([("7", "Taree", "Taree (AUS)")]),
    )

    assert out.loc[0, "market"] == "Horse Racing / Taree (AUS) 21st Jun : Win"
    assert not out.loc[0, "countryByStartTime"]


@pytest.mark.parametrize(
    ("event", "settled", "expected", "timed"),
    [
        # Sydney is UTC+10 in June. 14:30 there is 04:30 UTC: Australia races.
        ("14:30 Ascot", "2026-06-21 14:35", "Ascot (AUS) 21st Jun", True),
        ("20:59 Ascot", "2026-06-21 21:03", "Ascot (AUS) 21st Jun", True),
        # 21:00 to 21:29 Sydney is the half hour neither country is given.
        ("21:00 Ascot", "2026-06-21 21:05", "Ascot (Unknown) 21st Jun", False),
        ("21:29 Ascot", "2026-06-21 21:33", "Ascot (Unknown) 21st Jun", False),
        # 21:30 Sydney is 11:30 UTC: the UK has started.
        ("21:30 Ascot", "2026-06-21 21:35", "Ascot 21st Jun", True),
        ("23:50 Ascot", "2026-06-22 00:04", "Ascot 21st Jun", True),
        # Sydney is UTC+11 in December, so the same clock time is an hour
        # earlier in UTC: 21:30 there is 10:30 UTC, still Australian.
        ("21:30 Ascot", "2026-12-12 21:35", "Ascot (AUS) 12th Dec", True),
        ("22:30 Ascot", "2026-12-12 22:35", "Ascot 12th Dec", True),
        # After the UK's evening racing: 07:45 Sydney is 21:45 UTC.
        ("07:45 Ascot", "2026-06-21 07:50", "Ascot (Unknown) 21st Jun", False),
        ("07:29 Ascot", "2026-06-21 07:35", "Ascot 21st Jun", True),
        # No advertised time. The settlement is not trusted to stand in for
        # it: a late one would put the race in the other country's hours.
        ("Ascot", "2026-06-21 14:35", "Ascot (Unknown) 21st Jun", False),
    ],
)
def test_a_venue_shared_with_the_uk_is_told_apart_by_start_time(
    event, settled, expected, timed
) -> None:
    markets = _markets([("1.1", "7", event, "Win", settled)])

    out = resolve_market_names(
        markets, racecard=None, catalogue=None, venue_store=_venues(ASCOT_BOTH)
    )

    assert out.loc[0, "market"] == f"Horse Racing / {expected} : Win"
    assert out.loc[0, "countryByStartTime"] == timed


def test_the_current_racecard_is_evidence_for_a_shared_venue_too() -> None:
    markets = _markets([("1.3", "7", "14:30 Ascot", "Win", "2026-06-21 14:35")])
    racecard = _racecard(
        [
            ("1.1", "Ascot 19th Jun", "1m Hcap"),
            ("1.2", "Ascot (AUS) 20th Jun", "R1 1000m"),
        ]
    )

    out = resolve_market_names(markets, racecard=racecard, catalogue=None)

    assert out.loc[0, "market"] == "Horse Racing / Ascot (AUS) 21st Jun : Win"


def test_one_sighting_of_a_venue_known_in_two_countries_proves_nothing() -> None:
    markets = _markets([("1.1", "7", "14:30 Ascot", "Win", "2026-06-21 14:35")])

    out = resolve_market_names(
        markets,
        racecard=None,
        catalogue=None,
        venue_store=_venues([("7", "Ascot", "Ascot (AUS)")]),
    )

    assert out.loc[0, "market"] == "Horse Racing / Ascot (Unknown) 21st Jun : Win"


@pytest.mark.parametrize(
    ("event_type", "sport", "tracks"),
    [
        # Greyhound meetings in the two countries overlap for most of the day.
        ("4339", "Greyhound Racing", ["Newcastle", "Newcastle (AUS)"]),
        # Start time says nothing between Australia and New Zealand.
        ("7", "Horse Racing", ["Illawarra Grange (AUS)", "Illawarra Grange (NZL)"]),
        # Three tracks are not a pair.
        ("7", "Horse Racing", ["Hamilton", "Hamilton (AUS)", "Hamilton (NZL)"]),
    ],
)
def test_start_time_only_separates_an_australian_and_a_uk_horse_track(
    event_type, sport, tracks
) -> None:
    venue = tracks[0].split(" (")[0]
    markets = _markets(
        [("1.1", event_type, f"14:30 {venue}", "Win", "2026-06-21 14:35")]
    )

    out = resolve_market_names(
        markets,
        racecard=None,
        catalogue=None,
        venue_store=_venues([(event_type, venue, track) for track in tracks]),
    )

    assert out.loc[0, "market"] == f"{sport} / {venue} (Unknown) 21st Jun : Win"
    assert not out.loc[0, "countryByStartTime"]


def test_a_venue_means_its_own_track_over_a_neighbour_filed_under_it() -> None:
    markets = _markets([("1.1", "7", "22:30 Turffontein", "Win", "2026-06-21 22:35")])
    store = _venues(
        [
            ("7", "Turffontein", "Turffontein (RSA)"),
            # The racecard filed a Vaal meeting under the Turffontein venue.
            ("7", "Turffontein", "Vaal (RSA)"),
        ]
    )

    out = resolve_market_names(
        markets, racecard=None, catalogue=None, venue_store=store
    )

    assert out.loc[0, "market"] == "Horse Racing / Turffontein (RSA) 21st Jun : Win"


def test_a_pool_bet_event_does_not_make_its_venue_ambiguous() -> None:
    markets = _markets([("1.9", "7", "23:30 Kempton", "Win", "2026-06-21 23:35")])
    racecard = _racecard(
        [
            ("1.1", "Kempton 20th Jun", "6f Hcap"),
            ("1.2", "Kempton (RFC) 20th Jun", "Reverse FC"),
            ("1.3", "Kempton (F/C) 20th Jun", "Forecast"),
        ]
    ).assign(countryCode="GB")

    out = resolve_market_names(markets, racecard=racecard, catalogue=None)

    assert out.loc[0, "market"] == "Horse Racing / Kempton 21st Jun : Win"


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


def test_an_unbet_racecard_row_is_enough_to_make_a_venue_ambiguous() -> None:
    # Only the AUS meeting was bet on; the card also listed a US meeting of
    # the same name, which the fallback must not ignore.
    markets = _markets(
        [
            ("1.1", "7", "13:00 Bendigo", "Win", "2026-10-01 13:05"),
            ("1.2", "7", "09:00 Bendigo", "Win", "2026-10-02 09:05"),
        ]
    )
    racecard = _racecard(
        [
            ("1.1", "Bendigo (AUS) 1st Oct", "R1 1100m Mdn", "7", "Bendigo"),
            ("9.9", "Bendigo (US) 1st Oct", "R1 6f Mdn", "7", "Bendigo"),
        ]
    )
    out = resolve_market_names(markets, racecard=racecard, catalogue=None)
    assert out.loc[1, "market"] == "Horse Racing / Bendigo (Unknown) 2nd Oct : Win"


def test_a_known_two_country_venue_is_never_resolved_from_one_sighting() -> None:
    markets = _markets(
        [
            ("1.1", "7", "13:00 York", "Win", "2026-10-01 13:05"),
            ("1.2", "7", "14:00 York", "Win", "2026-10-02 14:05"),
        ]
    )
    racecard = _racecard([("1.1", "York (AUS) 1st Oct", "R1 1000m", "7", "York")])
    out = resolve_market_names(markets, racecard=racecard, catalogue=None)
    assert out.loc[1, "market"] == "Horse Racing / York (Unknown) 2nd Oct : Win"


def test_pool_bet_events_do_not_make_a_venue_ambiguous() -> None:
    markets = _markets(
        [
            ("1.1", "4339", "20:00 Romford", "Win", "2026-11-14 20:05"),
            ("1.2", "4339", "21:00 Romford", "Win", "2026-11-14 21:05"),
        ]
    )
    racecard = _racecard(
        [
            ("1.1", "Romford 14th Nov", "A10 400m", "4339", "Romford"),
            ("9.9", "Romford (F/C) 14th Nov", "Forecast", "4339", "Romford"),
        ]
    )
    out = resolve_market_names(markets, racecard=racecard, catalogue=None)
    assert out.loc[1, "market"] == "Greyhound Racing / Romford 14th Nov : Win"


def test_a_race_started_before_midnight_keeps_the_earlier_date() -> None:
    markets = _markets([("1.1", "7", "23:59 Nowhere", "Win", "2026-10-02 00:03")])
    out = resolve_market_names(markets, racecard=None, catalogue=None)
    assert out.loc[0, "market"] == "Horse Racing / Nowhere (Unknown) 1st Oct : Win"


@pytest.mark.parametrize(
    ("event", "settled", "expected"),
    [
        ("18:01 Bendigo", "2026-06-21 18:04", "2026-06-21 18:01+10:00"),
        # Advertised before midnight, settled after it: the day before.
        ("23:55 Bendigo", "2026-06-22 00:05", "2026-06-21 23:55+10:00"),
        # Clocks go back at 03:00 on 5 April 2026. The day before is a wall
        # clock day, not 24 hours: 23:55 on the 4th is still daylight time.
        ("23:55 Bendigo", "2026-04-05 00:05", "2026-04-04 23:55+11:00"),
        ("23:55 Bendigo", "2026-04-06 00:05", "2026-04-05 23:55+10:00"),
        # Clocks go forward at 02:00 on 4 October 2026.
        ("23:55 Bendigo", "2026-10-04 00:05", "2026-10-03 23:55+10:00"),
        ("23:55 Bendigo", "2026-10-05 00:05", "2026-10-04 23:55+11:00"),
    ],
)
def test_the_start_is_the_advertised_wall_clock_time(event, settled, expected) -> None:
    settled_at = pd.Timestamp(settled).tz_localize(SYDNEY_TZ)

    assert fallback_event_start(event, settled_at) == pd.Timestamp(expected)


def test_no_advertised_time_is_no_start_and_a_repeated_hour_is_not_guessed() -> None:
    settled = pd.Timestamp("2026-06-21 18:04").tz_localize(SYDNEY_TZ)
    assert fallback_event_start("Bendigo", settled) is None
    assert fallback_event_start(None, settled) is None

    # 02:30 happens twice on the night the clocks go back.
    after = pd.Timestamp("2026-04-05 04:00").tz_localize(SYDNEY_TZ)
    assert fallback_event_start("02:30 Bendigo", after) is None
