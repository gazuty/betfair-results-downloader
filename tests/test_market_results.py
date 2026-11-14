"""The results feed the dashboard ingests: what is held back, what is final."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from betfair_results_downloader.commission import (
    COMMISSION_COLUMNS,
    resolve_commission_path,
    save_market_commission,
)
from betfair_results_downloader.market_results import (
    FEED_COLUMNS,
    FEED_FILENAME,
    META_FILENAME,
    SCHEMA_VERSION,
    build_market_results,
    build_market_results_from_disk,
    write_market_results,
)
from betfair_results_downloader.market_status import (
    STATUS_COLUMNS,
    resolve_status_path,
    save_market_status,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _bets(rows: list[tuple]) -> pd.DataFrame:
    """(betId, marketId, eventTypeId, settledDate, profit, eventDesc, marketDesc)."""
    return pd.DataFrame(
        rows,
        columns=[
            "betId",
            "marketId",
            "eventTypeId",
            "settledDate",
            "profit",
            "evt_eventName",
            "mkt_marketName",
        ],
    ).astype(str)


def _store(rows: list[tuple]) -> pd.DataFrame:
    """(marketId, eventTypeId, settledDateUtc, betCount, gross, commission, fetchedUtc)."""
    return pd.DataFrame(rows, columns=COMMISSION_COLUMNS).astype(str)


def _status(rows: list[tuple]) -> pd.DataFrame:
    """(marketId, status, firstPendingUtc, closedObservedUtc)."""
    return pd.DataFrame(
        [
            {
                "marketId": mid,
                "status": st,
                "activeRunners": "0",
                "source": "book",
                "checkedUtc": "2026-11-14T11:00:00Z",
                "firstPendingUtc": first,
                "closedObservedUtc": closed,
            }
            for mid, st, first, closed in rows
        ],
        columns=STATUS_COLUMNS,
    )


def _build(bets, store, status=None, racecard=None, catalogue=None):
    return build_market_results(
        bets,
        market_status=status,
        market_commission=store,
        racecard=racecard,
        catalogue=catalogue,
        now=NOW,
    )


def test_a_settled_market_is_one_row_net_of_commission() -> None:
    bets = _bets(
        [
            (
                "1",
                "1.100",
                "1",
                "2026-11-14T08:00:00.000Z",
                "10.0",
                "A v B",
                "Correct Score",
            ),
            (
                "2",
                "1.100",
                "1",
                "2026-11-14T08:00:00.000Z",
                "-4.0",
                "A v B",
                "Correct Score",
            ),
        ]
    )
    store = _store(
        [
            (
                "1.100",
                "1",
                "2026-11-14T08:00:00.000Z",
                "2",
                "6.00",
                "0.30",
                "2026-11-14T09:00:00Z",
            )
        ]
    )
    result = _build(bets, store)

    assert list(result.feed.columns) == FEED_COLUMNS
    row = result.feed.iloc[0].to_dict()
    assert row["market"] == "Football / A v B : Correct Score"
    assert row["nameSource"] == "item_description"
    assert (row["grossProfit"], row["commission"], row["netProfit"]) == (6.0, 0.3, 5.7)
    assert row["betCount"] == 2
    # Sydney local time, the shape of the website download's Settled date.
    assert row["settledLocal"] == "2026-11-14 19:00:00"
    assert row["settledUtc"] == "2026-11-14T08:00:00Z"
    assert result.warnings == []


def test_a_partially_settled_market_is_held_until_it_closes_then_counted_once() -> None:
    legs = [
        (
            "1",
            "1.200",
            "3",
            "2026-03-12T13:43:05.000Z",
            "130.56",
            "Players 2026",
            "Winner",
        ),
        (
            "2",
            "1.200",
            "3",
            "2026-03-15T22:19:26.000Z",
            "-140.27",
            "Players 2026",
            "Winner",
        ),
    ]
    store = _store(
        [
            (
                "1.200",
                "3",
                "2026-03-15T22:19:26.000Z",
                "2",
                "-9.71",
                "0.00",
                "2026-03-16T13:00:00Z",
            )
        ]
    )

    still_open = _build(
        _bets(legs[:1]),
        store,
        _status([("1.200", "OPEN", "2026-03-12T19:00:00Z", "")]),
    )
    assert still_open.feed.empty
    assert still_open.meta["heldPendingMarkets"] == 1

    closed = _build(
        _bets(legs),
        store,
        _status([("1.200", "CLOSED", "2026-03-12T19:00:00Z", "2026-03-16T08:00:00Z")]),
    )
    assert len(closed.feed) == 1
    row = closed.feed.iloc[0]
    # The whole market, once, on the day the close was observed -- not the
    # interim leg in one week and the whole market again in another.
    assert row["netProfit"] == -9.71
    assert row["settledUtc"] == "2026-03-16T08:00:00Z"


def test_unknown_commission_is_held_back_not_published_as_zero() -> None:
    bets = _bets(
        [("1", "1.300", "7", "2026-11-14T08:00:00.000Z", "20.0", "18:01 Morph", "Win")]
    )
    # Zero commission on a $20 win: the pre-close placeholder.
    store = _store(
        [
            (
                "1.300",
                "7",
                "2026-11-14T08:00:00.000Z",
                "1",
                "20.00",
                "0.00",
                "2026-11-14T08:05:00Z",
            )
        ]
    )
    result = _build(bets, store)

    assert result.feed.empty
    assert result.meta["heldCommissionUnknownMarkets"] == 1
    assert result.warnings == []  # four hours old: late, not stuck


def test_a_market_stuck_without_commission_past_48_hours_warns() -> None:
    bets = _bets(
        [("1", "1.300", "7", "2026-11-11T08:00:00.000Z", "20.0", "18:01 Morph", "Win")]
    )
    result = _build(bets, store=_store([]))

    assert result.meta["heldCommissionUnknownMarkets"] == 1
    assert result.meta["oldestCommissionUnknownHours"] == 76.0
    assert len(result.warnings) == 1 and "76h ago" in result.warnings[0]


def test_a_zero_commission_confirmed_a_day_after_close_is_final() -> None:
    bets = _bets(
        [
            (
                "1",
                "1.264531699",
                "7",
                "2026-10-28T04:12:07.000Z",
                "0.11",
                "15:12 Bendigo",
                "Win",
            )
        ]
    )
    status = _status([("1.264531699", "CLOSED", "", "2026-10-28T08:00:20Z")])

    read_late = _store(
        [
            (
                "1.264531699",
                "7",
                "2026-10-28T04:12:07.000Z",
                "24",
                "0.11",
                "0.00",
                "2026-11-10T22:00:21Z",
            )
        ]
    )
    accepted = _build(bets, read_late, status)
    assert accepted.feed["netProfit"].tolist() == [0.11]

    read_early = read_late.assign(fetchedUtc="2026-10-28T09:00:00Z")
    held = _build(bets, read_early, status)
    assert held.feed.empty and held.meta["heldCommissionUnknownMarkets"] == 1


def test_a_zero_on_a_market_that_was_ever_pending_stays_unknown() -> None:
    bets = _bets(
        [("1", "1.400", "2", "2026-10-28T04:12:07.000Z", "5.0", "Open 2026", "Winner")]
    )
    status = _status(
        [("1.400", "CLOSED", "2026-10-01T00:00:00Z", "2026-10-28T08:00:00Z")]
    )
    store = _store(
        [
            (
                "1.400",
                "2",
                "2026-10-28T04:12:07.000Z",
                "1",
                "5.00",
                "0.00",
                "2026-10-25T00:00:00Z",
            )
        ]
    )
    assert _build(bets, store, status).feed.empty


def test_betfairs_whole_market_gross_wins_over_the_canonical_sum() -> None:
    # The canonical still holds a bet Betfair later took out of the market
    # (Belmont Park, 25 Oct 2026); the website and the store both say -2.00.
    bets = _bets(
        [
            (
                "1",
                "1.264368196",
                "7",
                "2026-10-24T18:55:30.000Z",
                "-2.0",
                "05:48 Belmont Park",
                "Win",
            ),
            (
                "2",
                "1.264368196",
                "7",
                "2026-10-24T19:05:38.000Z",
                "-2.0",
                "05:48 Belmont Park",
                "Win",
            ),
        ]
    )
    store = _store(
        [
            (
                "1.264368196",
                "7",
                "2026-10-24T19:05:38.000Z",
                "1",
                "-2.00",
                "0.00",
                "2026-10-25T00:00:00Z",
            )
        ]
    )
    result = _build(bets, store)

    row = result.feed.iloc[0]
    assert (row["grossProfit"], row["netProfit"], row["betCount"]) == (-2.0, -2.0, 1)
    assert result.meta["canonicalGrossMismatchMarkets"] == 1


def test_a_float_damaged_market_id_is_spelled_as_betfair_spells_it() -> None:
    bets = _bets(
        [
            (
                "1",
                "1.25194156",
                "1",
                "2026-01-18T12:37:43.000Z",
                "-44.0",
                "Bochum v SVD",
                "Over/Under 0.5 Goals",
            )
        ]
    )
    store = _store(
        [
            (
                "1.251941560",
                "1",
                "2026-01-18T12:37:43.000Z",
                "1",
                "-44.00",
                "0.00",
                "2026-09-13T10:13:10Z",
            )
        ]
    )
    assert _build(bets, store).feed["marketId"].tolist() == ["1.251941560"]


def test_rows_without_a_market_id_are_counted_but_never_alert() -> None:
    bets = _bets([("1", "", "7", "2026-01-01T00:00:00.000Z", "3.0", "", "")])
    result = _build(bets, _store([]))
    assert result.feed.empty
    assert result.meta["heldNoMarketIdRows"] == 1
    assert result.meta["heldCommissionUnknownMarkets"] == 0
    assert result.warnings == []


def test_written_feed_round_trips_and_the_meta_names_its_hash(tmp_path: Path) -> None:
    bets = _bets(
        [
            (
                "1",
                "1.100",
                "1",
                "2026-11-14T08:00:00.000Z",
                "10.0",
                "A v B",
                "Correct Score",
            )
        ]
    )
    store = _store(
        [
            (
                "1.100",
                "1",
                "2026-11-14T08:00:00.000Z",
                "1",
                "10.00",
                "0.50",
                "2026-11-14T09:00:00Z",
            )
        ]
    )
    path = write_market_results(tmp_path, _build(bets, store))

    assert path == tmp_path / FEED_FILENAME
    meta = json.loads((tmp_path / META_FILENAME).read_text())
    assert meta["csvSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert meta["schemaVersion"] == SCHEMA_VERSION
    assert meta["generatedUtc"] == "2026-11-14T12:00:00Z"
    back = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert back.loc[0, "marketId"] == "1.100"
    assert back.loc[0, "netProfit"] == "9.5"
    assert not list(tmp_path.glob("*.tmp"))


def _seed_disk(tmp_path: Path, bets: pd.DataFrame, store: pd.DataFrame) -> Path:
    bets.to_csv(tmp_path / "cleared_orders_cleaned.csv", index=False)
    cache = tmp_path / ".cache"
    save_market_commission(store, resolve_commission_path(cache))
    save_market_status(_status([]), resolve_status_path(cache))
    return cache


def test_from_disk_builds_the_same_feed_as_in_memory(tmp_path: Path) -> None:
    bets = _bets(
        [
            (
                "1",
                "1.100",
                "1",
                "2026-11-14T08:00:00.000Z",
                "10.0",
                "A v B",
                "Correct Score",
            )
        ]
    )
    store = _store(
        [
            (
                "1.100",
                "1",
                "2026-11-14T08:00:00.000Z",
                "1",
                "10.00",
                "0.50",
                "2026-11-14T09:00:00Z",
            )
        ]
    )
    _seed_disk(tmp_path, bets, store)

    result = build_market_results_from_disk(tmp_path, now=NOW)
    assert result.feed["netProfit"].tolist() == [9.5]
    assert result.warnings == []


def test_from_disk_refuses_an_unreadable_status_file(tmp_path: Path) -> None:
    bets = _bets(
        [
            (
                "1",
                "1.100",
                "1",
                "2026-11-14T08:00:00.000Z",
                "10.0",
                "A v B",
                "Correct Score",
            )
        ]
    )
    cache = _seed_disk(tmp_path, bets, _store([]))
    status_path = resolve_status_path(cache)
    status_path.unlink()
    status_path.mkdir()  # unreadable as a CSV

    # Without the status a pending outright would be published as final.
    with pytest.raises(OSError):
        build_market_results_from_disk(tmp_path, now=NOW)


def test_from_disk_names_racing_from_the_fallback_when_the_racecard_is_damaged(
    tmp_path: Path,
) -> None:
    bets = _bets(
        [
            (
                "1",
                "1.500",
                "7",
                "2026-11-14T07:04:37.000Z",
                "-2.0",
                "18:01 Morphettville",
                "Win",
            )
        ]
    )
    store = _store(
        [
            (
                "1.500",
                "7",
                "2026-11-14T07:04:37.000Z",
                "1",
                "-2.00",
                "0.00",
                "2026-11-14T09:00:00Z",
            )
        ]
    )
    cache = _seed_disk(tmp_path, bets, store)
    (cache / "racecard.csv").mkdir()

    result = build_market_results_from_disk(tmp_path, now=NOW)
    assert result.feed["nameSource"].tolist() == ["fallback"]
    assert "racecard unreadable" in result.warnings[0]
