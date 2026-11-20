"""The venue store: what each racing venue name has meant, remembered for good."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from betfair_results_downloader.__main__ import main
from betfair_results_downloader.config import ScheduleConfig
from betfair_results_downloader.racecard import (
    load_racecard,
    resolve_racecard_path,
    seed_venue_tracks,
    update_racecard,
)
from betfair_results_downloader.venue_tracks import (
    VENUE_TRACK_COLUMNS,
    empty_venue_tracks_frame,
    is_country_track,
    load_venue_tracks,
    merge_venue_tracks,
    observations_from_names,
    observations_from_racecard,
    resolve_venue_tracks_path,
    save_venue_tracks,
    venue_evidence,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _card(rows: list[tuple]) -> pd.DataFrame:
    """(eventTypeId, eventName, venue, countryCode)."""
    return pd.DataFrame(
        rows, columns=["eventTypeId", "eventName", "venue", "countryCode"]
    )


@pytest.mark.parametrize(
    ("track", "code", "expected"),
    [
        ("Kempton", "GB", True),
        ("Ascot (AUS)", "AU", True),
        ("Del Mar (US)", "", True),  # a seed has no code to go by
        ("Romford (F/C)", "GB", False),
        ("Kempton (RFC)", "GB", False),
        ("Kempton (RFC)", "", False),
        # A pool-bet tag this module has never met, on a British meeting.
        ("Kempton (XYZ)", "GB", False),
        ("Leopardstown (TRI)", "ie", False),
        ("Ascot (Unknown)", "", False),
        ("", "", False),
    ],
)
def test_only_a_plain_or_country_tagged_track_is_evidence(
    track, code, expected
) -> None:
    assert is_country_track(track, code) is expected


def test_a_racecard_row_vouches_under_its_venue_and_its_bare_track_name() -> None:
    card = _card(
        [
            ("7", "Evangeline Downs (US) 14th Nov", "Evangeline", "US"),
            ("7", "Kempton 14th Nov", "Kempton", "GB"),
            ("7", "Kempton (RFC) 14th Nov", "Kempton", "GB"),
            ("4339", "Romford (F/C) 14th Nov", "Romford", "GB"),
            ("x", "Nowhere 14th Nov", "Nowhere", "GB"),
        ]
    )

    assert observations_from_racecard(card) == {
        (7, "Evangeline", "Evangeline Downs (US)"),
        (7, "Evangeline Downs", "Evangeline Downs (US)"),
        (7, "Kempton", "Kempton"),
    }
    assert observations_from_racecard(None) == set()


def test_website_names_seed_racing_tracks_and_nothing_else() -> None:
    names = [
        "Horse Racing / Ascot (AUS) 4th Apr : R1 1400m 3yo",
        "Horse Racing / Ascot 17th Oct : 2m Grp 1",
        "Greyhound Racing / Mandurah (AUS) 31st Dec : R9 300m Gr5",
        # Not evidence: a tag that is not a known country (a name has no
        # country code to say whether it is one), a guess, a pool event,
        # another sport, no date, junk.
        "Greyhound Racing / Romford (TRI) 3rd Oct : Tricast",
        "Horse Racing / Taree (Unknown) 3rd Oct : Win",
        "Greyhound Racing / Romford (F/C) 3rd Oct : Forecast",
        "Football / Ham-Kam v Molde : Over/Under 0.5 Goals",
        "Horse Racing / Ascot : Win",
        "not a market name",
        None,
    ]

    assert observations_from_names(names) == {
        (7, "Ascot", "Ascot (AUS)"),
        (7, "Ascot", "Ascot"),
        (4339, "Mandurah", "Mandurah (AUS)"),
    }


def test_merging_adds_new_pairings_and_never_rewrites_old_ones() -> None:
    first, added = merge_venue_tracks(
        empty_venue_tracks_frame(),
        {(7, "Ascot", "Ascot (AUS)")},
        source="names",
        seen_utc="2026-11-01T00:00:00Z",
    )
    assert added == 1

    second, added = merge_venue_tracks(
        first,
        {(7, "Ascot", "Ascot (AUS)"), (7, "Ascot", "Ascot")},
        source="racecard",
        seen_utc="2026-11-14T12:00:00Z",
    )

    assert added == 1
    assert second.to_dict("records") == [
        {
            "eventTypeId": "7",
            "venue": "Ascot",
            "track": "Ascot (AUS)",
            "source": "names",
            "firstSeenUtc": "2026-11-01T00:00:00Z",
        },
        {
            "eventTypeId": "7",
            "venue": "Ascot",
            "track": "Ascot",
            "source": "racecard",
            "firstSeenUtc": "2026-11-14T12:00:00Z",
        },
    ]
    assert venue_evidence(second) == {(7, "Ascot"): {"Ascot (AUS)", "Ascot"}}


def test_the_store_round_trips_and_a_missing_file_is_empty(tmp_path: Path) -> None:
    path = resolve_venue_tracks_path(tmp_path)
    assert load_venue_tracks(path).empty

    store, _ = merge_venue_tracks(
        empty_venue_tracks_frame(),
        {(4339, "Mandurah", "Mandurah (AUS)")},
        source="names",
        seen_utc="2026-11-01T00:00:00Z",
    )
    save_venue_tracks(store, path)

    again = load_venue_tracks(path)
    assert list(again.columns) == VENUE_TRACK_COLUMNS
    assert again.to_dict("records") == store.to_dict("records")
    assert not list(tmp_path.glob("*.tmp"))


def test_a_store_without_its_columns_is_refused_not_read_as_empty(tmp_path) -> None:
    path = resolve_venue_tracks_path(tmp_path)
    path.write_text("venue,country\nAscot,AUS\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no 'eventTypeId' column"):
        load_venue_tracks(path)


def _catalogue(market_id: str, event_name: str, venue: str, country: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        market_id=market_id,
        market_name="Win",
        market_start_time=datetime(2026, 11, 14, 7, 0),
        event=SimpleNamespace(
            id="35000001", name=event_name, venue=venue, country_code=country
        ),
        event_type=SimpleNamespace(id="7", name="Horse Racing"),
    )


def test_a_capture_remembers_venues_the_racecard_will_later_forget(tmp_path) -> None:
    client = MagicMock()
    client.betting.list_market_catalogue.return_value = [
        _catalogue("1.1", "Ascot (AUS) 14th Nov", "Ascot", "AU"),
        _catalogue("1.2", "Kempton (RFC) 14th Nov", "Kempton", "GB"),
    ]

    update_racecard(client=client, cache_dir=tmp_path, bet_market_keys=set(), now=NOW)

    # A week on, nobody bet on either market and the racecard drops them.
    client.betting.list_market_catalogue.return_value = []
    later = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    update_racecard(client=client, cache_dir=tmp_path, bet_market_keys=set(), now=later)

    assert load_racecard(resolve_racecard_path(tmp_path)).empty
    store = load_venue_tracks(resolve_venue_tracks_path(tmp_path))
    assert store[["venue", "track", "source", "firstSeenUtc"]].values.tolist() == [
        ["Ascot", "Ascot (AUS)", "racecard", "2026-11-14T12:00:00Z"]
    ]


def test_a_damaged_venue_store_does_not_cost_the_capture(tmp_path) -> None:
    resolve_venue_tracks_path(tmp_path).mkdir(parents=True)
    client = MagicMock()
    client.betting.list_market_catalogue.return_value = [
        _catalogue("1.1", "Ascot (AUS) 14th Nov", "Ascot", "AU")
    ]

    result = update_racecard(
        client=client, cache_dir=tmp_path, bet_market_keys=set(), now=NOW
    )

    assert load_racecard(resolve_racecard_path(tmp_path))["marketId"].tolist() == [
        "1.1"
    ]
    assert "WARNING: venue store not updated" in result.message


def test_the_run_announces_a_capture_that_could_not_update_the_venue_store(
    tmp_path,
) -> None:
    from betfair_results_downloader.commission import (
        COMMISSION_COLUMNS,
        resolve_commission_path,
        save_market_commission,
    )
    from betfair_results_downloader.scheduler.runner import _refresh_racecard

    cache = tmp_path / ".cache"
    save_market_commission(
        pd.DataFrame(columns=COMMISSION_COLUMNS), resolve_commission_path(cache)
    )
    client = MagicMock()
    client.betting.list_market_catalogue.return_value = [
        _catalogue("1.1", "Ascot (AUS) 14th Nov", "Ascot", "AU")
    ]

    assert _refresh_racecard(client, tmp_path, None) is None

    resolve_venue_tracks_path(cache).unlink()
    resolve_venue_tracks_path(cache).mkdir()
    warning = _refresh_racecard(client, tmp_path, None)

    assert warning.startswith("⚠️ Racecard: captured")
    assert "venue store not updated" in warning


def test_seeding_a_damaged_store_fails_and_leaves_it_alone(
    tmp_path, capsys, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    store = resolve_venue_tracks_path(tmp_path / ".cache")
    store.parent.mkdir()
    store.write_text("venue,country\nAscot,AUS\n", encoding="utf-8")
    names = tmp_path / "names.csv"
    names.write_text("Market\nHorse Racing / Ascot 17th Oct : Win\n", encoding="utf-8")

    assert main(["seed-venues", "--from-names", str(names)]) == 1

    assert "FAIL: venue store not updated" in capsys.readouterr().out
    assert store.read_text(encoding="utf-8") == "venue,country\nAscot,AUS\n"


def test_a_store_row_with_a_blank_venue_or_track_is_not_evidence() -> None:
    store = pd.DataFrame(
        [("7", "", "Ascot"), ("7", "Ascot", ""), ("7", "Ascot", "Ascot (AUS)")],
        columns=["eventTypeId", "venue", "track"],
    )

    assert venue_evidence(store) == {(7, "Ascot"): {"Ascot (AUS)"}}


def test_seeding_reports_what_it_found_and_is_safe_to_repeat(tmp_path) -> None:
    names = [
        "Horse Racing / Ascot (AUS) 4th Apr : R1 1400m 3yo",
        "Horse Racing / Ascot (AUS) 11th Apr : R2 1000m",
        "Horse Racing / Ascot 17th Oct : 2m Grp 1",
    ]

    assert seed_venue_tracks(tmp_path, names, now=NOW) == (2, 2)
    assert seed_venue_tracks(tmp_path, names, now=NOW) == (2, 0)
    store = load_venue_tracks(resolve_venue_tracks_path(tmp_path))
    assert sorted(store["track"]) == ["Ascot", "Ascot (AUS)"]
    assert set(store["source"]) == {"names"}


def _use_creds(monkeypatch, results_dir: Path) -> None:
    creds = {"paths": {"results_csv_dir": str(results_dir)}}
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda validate=False: (creds, ScheduleConfig()),
    )


def test_seed_venues_command_fills_the_store_from_a_csv(
    tmp_path, capsys, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    names = tmp_path / "names.csv"
    pd.DataFrame(
        {
            "Market": [
                "Horse Racing / Ascot (AUS) 4th Apr : R1 1400m 3yo",
                "Football / Ham-Kam v Molde : Over/Under 0.5 Goals",
            ],
            "Profit_Loss": ["1.00", "2.00"],
        }
    ).to_csv(names, index=False)

    assert main(["seed-venues", "--from-names", str(names)]) == 0

    out = capsys.readouterr().out
    assert "OK: 1 venue-to-track pairing(s) in 2 name(s); 1 new to the store." in out
    store = load_venue_tracks(resolve_venue_tracks_path(tmp_path / ".cache"))
    assert store["track"].tolist() == ["Ascot (AUS)"]


def test_seed_venues_command_fails_plainly_on_a_file_it_cannot_use(
    tmp_path, capsys, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    names = tmp_path / "names.csv"
    names.write_text("Name\nHorse Racing / Ascot 17th Oct : Win\n", encoding="utf-8")

    assert main(["seed-venues", "--from-names", str(names)]) == 1
    assert main(["seed-venues", "--from-names", str(tmp_path / "missing.csv")]) == 1

    assert capsys.readouterr().out.count("FAIL: could not read names") == 2
    assert not resolve_venue_tracks_path(tmp_path / ".cache").exists()
    assert main(["seed-venues", "--from-names", str(names), "--column", "Name"]) == 0
