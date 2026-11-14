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


def _status(rows: list[tuple], source: str = "book") -> pd.DataFrame:
    """(marketId, status, firstPendingUtc, closedObservedUtc)."""
    return pd.DataFrame(
        [
            {
                "marketId": mid,
                "status": st,
                "activeRunners": "0",
                "source": source,
                "checkedUtc": "2026-11-14T11:00:00Z",
                "firstPendingUtc": first,
                "closedObservedUtc": closed,
            }
            for mid, st, first, closed in rows
        ],
        columns=STATUS_COLUMNS,
    )


_ALL_CLOSED = object()


def _closed_for(bets: pd.DataFrame) -> pd.DataFrame:
    """Status rows recording every market in ``bets`` as closed at first sight."""
    ids = sorted({m for m in bets["marketId"] if m})
    return _status([(m, "CLOSED", "", "2026-11-14T11:00:00Z") for m in ids])


def _build(bets, store, status=_ALL_CLOSED, racecard=None, catalogue=None):
    # By default every market has been seen closed, as the status step
    # records every market a run touches; tests about missing or pending
    # status pass their own.
    if status is _ALL_CLOSED:
        status = _closed_for(bets)
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


def test_a_zero_commission_above_15_cents_stays_held_however_late_it_is_read() -> None:
    # The feed and the DM report use one rule: above 15 cents a zero is
    # always the pre-close placeholder, even read days after the close.
    bets = _bets(
        [
            (
                "1",
                "1.500",
                "7",
                "2026-10-28T04:12:07.000Z",
                "412.00",
                "15:12 Bendigo",
                "Win",
            )
        ]
    )
    status = _status([("1.500", "CLOSED", "", "2026-10-28T08:00:20Z")])
    store = _store(
        [
            (
                "1.500",
                "7",
                "2026-10-28T04:12:07.000Z",
                "24",
                "412.00",
                "0.00",
                "2026-11-10T22:00:21Z",
            )
        ]
    )
    result = _build(bets, store, status)
    assert result.feed.empty
    assert result.meta["heldCommissionUnknownMarkets"] == 1
    assert "held back for unknown commission" in result.warnings[0]


def test_a_small_zero_confirmed_after_settlement_is_published() -> None:
    # Market 1.264531699: won $0.11, still $0.00 thirteen days after closing.
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
    store = _store(
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
    assert _build(bets, store).feed["netProfit"].tolist() == [0.11]
    early = store.assign(fetchedUtc="2026-10-28T05:00:00Z")
    assert _build(bets, early).feed.empty


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


def test_a_recent_market_with_no_status_row_is_held_back() -> None:
    # The status step failed or has not reached it: publishing would treat
    # a newly opened outright as finished.
    bets = _bets(
        [("1", "1.100", "2", "2026-11-14T08:00:00.000Z", "-3.0", "Open 2026", "Winner")]
    )
    store = _store(
        [
            (
                "1.100",
                "2",
                "2026-11-14T08:00:00.000Z",
                "1",
                "-3.00",
                "0.00",
                "2026-11-14T09:00:00Z",
            )
        ]
    )
    result = _build(bets, store, status=_status([]))
    assert result.feed.empty
    assert result.meta["heldStatusUncheckedMarkets"] == 1


def test_an_old_race_with_no_status_row_predates_tracking_and_is_final() -> None:
    bets = _bets(
        [("1", "1.100", "7", "2026-08-01T08:00:00.000Z", "-3.0", "18:01 Morph", "Win")]
    )
    store = _store(
        [
            (
                "1.100",
                "7",
                "2026-08-01T08:00:00.000Z",
                "1",
                "-3.00",
                "0.00",
                "2026-08-02T09:00:00Z",
            )
        ]
    )
    result = _build(bets, store, status=None)
    assert result.feed["netProfit"].tolist() == [-3.0]
    assert result.meta["heldStatusUncheckedMarkets"] == 0


def test_an_old_outright_with_no_status_row_is_held_until_checked() -> None:
    # Its latest leg is months old, but outrights stay open for months; a
    # losing leg's zero commission would otherwise publish it as finished.
    bets = _bets(
        [
            (
                "1",
                "1.100",
                "1",
                "2026-08-01T08:00:00.000Z",
                "-3.0",
                "Ballon d'Or",
                "Winner",
            )
        ]
    )
    store = _store(
        [
            (
                "1.100",
                "1",
                "2026-08-01T08:00:00.000Z",
                "1",
                "-3.00",
                "0.00",
                "2026-08-02T09:00:00Z",
            )
        ]
    )
    result = _build(bets, store, status=None)
    assert result.feed.empty
    assert result.meta["heldStatusUncheckedMarkets"] == 1
    # Once the status step records it closed, it is published.
    assert len(_build(bets, store).feed) == 1


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
    save_market_status(_closed_for(bets), resolve_status_path(cache))
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


def test_an_outright_closed_only_by_absence_waits_for_confirmation() -> None:
    # Seen OPEN, then missing from one listMarketBook response: recorded
    # CLOSED by absence, re-checked for 48 hours. Its losing leg's zero
    # commission must not reach the dashboard before then.
    bets = _bets(
        [("1", "1.500", "2", "2026-11-14T08:00:00.000Z", "-3.0", "Open 2026", "Winner")]
    )
    store = _store(
        [
            (
                "1.500",
                "2",
                "2026-11-14T08:00:00.000Z",
                "1",
                "-3.00",
                "0.00",
                "2026-11-14T11:00:00Z",
            )
        ]
    )
    absent = _status(
        [("1.500", "CLOSED", "2026-11-01T00:00:00Z", "2026-11-14T10:00:00Z")],
        source="absent",
    )
    held = _build(bets, store, absent)
    assert held.feed.empty and held.meta["heldStatusUncheckedMarkets"] == 1

    confirmed = absent.assign(closedObservedUtc="2026-11-11T10:00:00Z")
    assert len(_build(bets, store, confirmed).feed) == 1


def test_absence_is_enough_for_a_race_and_for_a_long_finished_market() -> None:
    race = _bets(
        [("1", "1.600", "7", "2026-11-14T08:00:00.000Z", "-2.0", "19:00 Morph", "Win")]
    )
    race_store = _store(
        [
            (
                "1.600",
                "7",
                "2026-11-14T08:00:00.000Z",
                "1",
                "-2.00",
                "0.00",
                "2026-11-14T11:00:00Z",
            )
        ]
    )
    absent_now = [("1.600", "CLOSED", "", "2026-11-14T10:00:00Z")]
    assert len(_build(race, race_store, _status(absent_now, source="absent")).feed) == 1

    # The first-run seed of old untracked outrights: absent because long closed.
    old = _bets(
        [("1", "1.700", "1", "2026-03-01T08:00:00.000Z", "-3.0", "Cup", "Winner")]
    )
    old_store = _store(
        [
            (
                "1.700",
                "1",
                "2026-03-01T08:00:00.000Z",
                "1",
                "-3.00",
                "0.00",
                "2026-09-13T10:00:00Z",
            )
        ]
    )
    seeded = _status([("1.700", "CLOSED", "", "2026-11-14T10:00:00Z")], source="absent")
    assert len(_build(old, old_store, seeded).feed) == 1


def _feed_rows(result) -> pd.DataFrame:
    return result.feed.astype(str)


def test_markets_aged_out_of_the_canonical_are_carried_forward() -> None:
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
                "1.200",
                "1",
                "2026-11-14T09:00:00.000Z",
                "-4.0",
                "C v D",
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
                "1",
                "10.00",
                "0.50",
                "2026-11-14T10:00:00Z",
            ),
            (
                "1.200",
                "1",
                "2026-11-14T09:00:00.000Z",
                "1",
                "-4.00",
                "0.00",
                "2026-11-14T10:00:00Z",
            ),
        ]
    )
    first = _build(bets, store)
    # 1.100's bets move to the archives; 1.200 is re-settled to a new figure.
    later_bets = _bets(
        [
            (
                "2",
                "1.200",
                "1",
                "2026-11-14T09:00:00.000Z",
                "-5.0",
                "C v D",
                "Correct Score",
            )
        ]
    )
    later_store = store.assign(
        grossProfit=["10.00", "-5.00"], fetchedUtc="2026-11-14T11:00:00Z"
    )
    later = build_market_results(
        later_bets,
        market_status=_closed_for(bets),
        market_commission=later_store,
        racecard=None,
        catalogue=None,
        previous_feed=_feed_rows(first),
        now=NOW,
    )

    by_id = later.feed.set_index("marketId")["netProfit"].to_dict()
    assert by_id == {"1.100": 9.5, "1.200": -5.0}
    assert later.meta["carriedForwardMarkets"] == 1


def test_a_damaged_previous_feed_stops_the_rebuild(tmp_path: Path) -> None:
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
    write_market_results(tmp_path, build_market_results_from_disk(tmp_path, now=NOW))
    (tmp_path / FEED_FILENAME).write_text("tampered\n")

    # Rebuilding without it would silently drop every aged-out market.
    with pytest.raises(ValueError, match="does not match"):
        build_market_results_from_disk(tmp_path, now=NOW)


def test_an_interrupted_write_heals_from_the_previous_generation(
    tmp_path: Path,
) -> None:
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
    write_market_results(tmp_path, build_market_results_from_disk(tmp_path, now=NOW))
    write_market_results(tmp_path, build_market_results_from_disk(tmp_path, now=NOW))
    assert (tmp_path / "market_results.prev.csv").exists()

    # The run died after replacing the CSV and before replacing the meta.
    (tmp_path / FEED_FILENAME).write_text("half-written\n")

    rebuilt = build_market_results_from_disk(tmp_path, now=NOW)
    write_market_results(tmp_path, rebuilt)
    meta = json.loads((tmp_path / META_FILENAME).read_text())
    assert (
        meta["csvSha256"]
        == hashlib.sha256((tmp_path / FEED_FILENAME).read_bytes()).hexdigest()
    )
    assert rebuilt.feed["netProfit"].tolist() == [9.5]


def test_a_backfilled_archive_market_reaches_the_feed_once(
    tmp_path: Path, monkeypatch
) -> None:
    """
    A backfill of old dates writes an ordinary market straight into a yearly
    archive. It has no feed row to carry forward, so the changed archive is
    read; after that it is carried forward without reading the archive.
    """
    from betfair_results_downloader import market_results as mr

    recent = _bets(
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
    archived = _bets(
        [
            (
                "9",
                "1.900",
                "1",
                "2026-01-10T08:00:00.000Z",
                "6.0",
                "E v F",
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
            ),
            (
                "1.900",
                "1",
                "2026-01-10T08:00:00.000Z",
                "1",
                "6.00",
                "0.30",
                "2026-09-13T10:00:00Z",
            ),
        ]
    )
    _seed_disk(tmp_path, recent, store)
    archived.to_csv(tmp_path / "cleared_orders_archive_2026.csv.gz", index=False)
    # Status rows for both, as the seed would record them.
    save_market_status(
        _closed_for(pd.concat([recent, archived])),
        resolve_status_path(tmp_path / ".cache"),
    )

    first = build_market_results_from_disk(tmp_path, now=NOW)
    assert sorted(first.feed["marketId"]) == ["1.100", "1.900"]
    write_market_results(tmp_path, first)

    def must_not_read(_dir):
        raise AssertionError("unchanged archives must not be re-read")

    monkeypatch.setattr(mr, "_load_archives", must_not_read)
    second = build_market_results_from_disk(tmp_path, now=NOW)
    assert sorted(second.feed["marketId"]) == ["1.100", "1.900"]
    assert second.meta["carriedForwardMarkets"] == 1


def test_a_market_older_than_betfairs_commission_reach_is_counted_not_alerted() -> None:
    bets = _bets(
        [
            (
                "1",
                "1.100",
                "1",
                "2025-06-01T08:00:00.000Z",
                "10.0",
                "A v B",
                "Correct Score",
            )
        ]
    )
    result = _build(bets, store=_store([]))
    assert result.feed.empty
    assert result.meta["heldCommissionUnreachableMarkets"] == 1
    assert result.meta["heldCommissionUnknownMarkets"] == 0
    assert result.warnings == []


def test_rereading_archives_keeps_carried_rows_and_adds_only_new_markets(
    tmp_path: Path,
) -> None:
    """
    An archive change must not rebuild markets the feed already carries
    (their CLOSED status may have been pruned), only add new ones.
    """
    carried = _bets(
        [
            (
                "1",
                "1.800",
                "1",
                "2025-11-20T08:00:00.000Z",
                "4.0",
                "G v H",
                "Correct Score",
            )
        ]
    )
    backfilled = _bets(
        [("2", "1.900", "2", "2025-10-25T08:00:00.000Z", "6.0", "Open 2025", "Winner")]
    )
    recent = _bets(
        [
            (
                "3",
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
            ),
            (
                "1.800",
                "1",
                "2025-11-20T08:00:00.000Z",
                "1",
                "4.00",
                "0.20",
                "2026-09-13T10:00:00Z",
            ),
            (
                "1.900",
                "2",
                "2025-11-25T08:00:00.000Z",
                "1",
                "6.00",
                "0.30",
                "2026-09-13T10:00:00Z",
            ),
        ]
    )
    # The previous feed already carries 1.800 (built while it was current).
    _seed_disk(tmp_path, pd.concat([recent, carried]), store)
    write_market_results(tmp_path, build_market_results_from_disk(tmp_path, now=NOW))

    # Later: 1.800's bets are archived and its status row pruned; a backfill
    # adds 1.900 straight into the archive, also with no status row.
    recent.to_csv(tmp_path / "cleared_orders_cleaned.csv", index=False)
    save_market_status(_closed_for(recent), resolve_status_path(tmp_path / ".cache"))
    pd.concat([carried, backfilled]).to_csv(
        tmp_path / "cleared_orders_archive_2025.csv.gz", index=False
    )

    result = build_market_results_from_disk(tmp_path, now=NOW)
    by_id = result.feed.set_index("marketId")["netProfit"].to_dict()
    assert by_id == {"1.100": 9.5, "1.800": 3.8, "1.900": 5.7}
    assert result.meta["carriedForwardMarkets"] == 1


def test_an_archive_only_outright_under_a_year_old_still_needs_a_status_row() -> None:
    # A shorter canonical_archive_months can archive a still-open outright.
    bets = _bets(
        [("1", "1.950", "2", "2026-06-01T08:00:00.000Z", "6.0", "Open 2026", "Winner")]
    ).assign(_fromArchive="1")
    store = _store(
        [
            (
                "1.950",
                "2",
                "2026-06-01T08:00:00.000Z",
                "1",
                "6.00",
                "0.30",
                "2026-09-13T10:00:00Z",
            )
        ]
    )
    result = _build(bets, store, status=None)
    assert result.feed.empty and result.meta["heldStatusUncheckedMarkets"] == 1
