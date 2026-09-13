"""
commission: reading, merging and persisting per-market commission.

The facts these tests encode were measured live on 2026-09-13: grouped rows
carry ``commission``, per-bet rows do not; a grouped row sits at the market's
latest leg; a partially settled market reports 0.0 until it closes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from betfairlightweight.exceptions import APIError

from betfair_results_downloader import commission as cm
from betfair_results_downloader.market_status import STATUS_COLUMNS

NOW = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc)


def _grouped(market_id: str, profit: float, commission: float, **extra) -> dict:
    row = {
        "eventTypeId": "7",
        "marketId": market_id,
        "settledDate": "2026-09-12T12:00:00.000Z",
        "betCount": 10,
        "profit": profit,
        "commission": commission,
    }
    row.update(extra)
    return row


class _FakeBetting:
    """
    Serves grouped rows page by page, the way Betfair does, and records
    every call so tests can see the filters that were sent.
    """

    def __init__(
        self, rows: list[dict], *, page_size: int = 200, transient_failures: int = 0
    ) -> None:
        self._rows = rows
        self._page_size = page_size
        self._transient_failures = transient_failures
        self.calls: list[dict] = []

    def list_cleared_orders(self, **kwargs):
        self.calls.append(kwargs)
        if self._transient_failures:
            self._transient_failures -= 1
            raise APIError(None, exception=RuntimeError("TIMEOUT_ERROR"))
        assert kwargs["group_by"] == "MARKET"
        assert kwargs["lightweight"] is True
        # Betfair places a grouped row at its latest leg: only rows whose
        # settledDate falls in the requested range come back.
        rng = kwargs["settled_date_range"]
        rows = [r for r in self._rows if rng["from"] <= r["settledDate"] < rng["to"]]
        wanted = kwargs.get("market_ids")
        if wanted:
            rows = [r for r in rows if r["marketId"] in wanted]
        start = kwargs["from_record"]
        count = kwargs["record_count"]
        page = rows[start : start + count]
        return {"clearedOrders": page, "moreAvailable": start + count < len(rows)}


class _FakeClient:
    def __init__(self, rows: list[dict], **kwargs) -> None:
        self.betting = _FakeBetting(rows, **kwargs)


def _status(market_id: str, status: str, first_pending="", closed="") -> dict:
    return {
        "marketId": market_id,
        "status": status,
        "activeRunners": "0",
        "source": "book",
        "checkedUtc": "2026-09-13T08:00:00Z",
        "firstPendingUtc": first_pending,
        "closedObservedUtc": closed,
    }


def _status_frame(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=STATUS_COLUMNS, dtype=str)


def _commission_frame(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=cm.COMMISSION_COLUMNS, dtype=str)


# -----------------------------
# Fetch
# -----------------------------


def test_fetch_pages_until_betfair_says_no_more() -> None:
    rows = [_grouped(f"1.{i}", 1.0, 0.05) for i in range(5)]
    client = _FakeClient(rows, page_size=2)

    got = cm.fetch_grouped_markets(client, NOW - timedelta(days=1), NOW, page_size=2)

    assert [g.market_id for g in got] == [r["marketId"] for r in rows]
    assert len(client.betting.calls) == 3
    assert [c["from_record"] for c in client.betting.calls] == [0, 2, 4]
    first = client.betting.calls[0]
    assert first["settled_date_range"] == {
        "from": "2026-09-12T09:00:00Z",
        "to": "2026-09-13T09:00:00Z",
    }
    assert "market_ids" not in first


def test_fetch_retries_transient_failures() -> None:
    client = _FakeClient([_grouped("1.1", 2.0, 0.1)], transient_failures=2)

    got = cm.fetch_grouped_markets(client, NOW - timedelta(days=1), NOW)

    assert [g.commission for g in got] == [0.1]
    assert len(client.betting.calls) == 3


def test_fetch_rejects_a_non_dict_response() -> None:
    class _Odd:
        class betting:
            @staticmethod
            def list_cleared_orders(**_kwargs):
                return object()

    with pytest.raises(TypeError, match="expected dict"):
        cm.fetch_grouped_markets(_Odd(), NOW - timedelta(days=1), NOW)


def test_fetch_raises_on_an_unparseable_amount() -> None:
    """A skipped or zeroed market would be silently wrong; raising is loud."""
    client = _FakeClient([_grouped("1.1", "lots", 0.1)])

    with pytest.raises(ValueError, match="1.1"):
        cm.fetch_grouped_markets(client, NOW - timedelta(days=1), NOW)


def test_fetch_ignores_rows_without_a_market_id() -> None:
    client = _FakeClient([_grouped("", 2.0, 0.1), _grouped("1.2", 3.0, 0.15)])

    got = cm.fetch_grouped_markets(client, NOW - timedelta(days=1), NOW)

    assert [g.market_id for g in got] == ["1.2"]


def test_fetch_defaults_a_missing_commission_key_to_zero_on_a_losing_market() -> None:
    """Betfair charges nothing on a loss, so an omitted key is honestly 0.0."""
    row = _grouped("1.3", -6.5, 0.0)
    del row["commission"]
    client = _FakeClient([row])

    got = cm.fetch_grouped_markets(client, NOW - timedelta(days=1), NOW)

    assert got[0].gross_profit == -6.5 and got[0].commission == 0.0


def test_fetch_skips_a_winning_row_without_commission(caplog) -> None:
    """
    A winning market with no commission key is not a $0.00 charge; recording
    it as one would be trusted forever. Skipped and announced instead, so
    the recent-market seed reads it again next run.
    """
    row = _grouped("1.4", 42.0, 0.0)
    del row["commission"]
    client = _FakeClient([row, _grouped("1.5", 3.0, 0.15)])

    with caplog.at_level("WARNING"):
        got = cm.fetch_grouped_markets(client, NOW - timedelta(days=1), NOW)

    assert [g.market_id for g in got] == ["1.5"]
    assert "Market 1.4: winning grouped row carries no commission" in caplog.text


def test_window_fetch_uses_the_same_chunks_as_the_bet_download() -> None:
    client = _FakeClient([])
    from_dt = datetime(2026, 7, 1, tzinfo=timezone.utc)
    to_dt = datetime(2026, 9, 1, tzinfo=timezone.utc)
    seen: list[str] = []

    cm.fetch_commission_for_window(
        client,
        from_dt,
        to_dt,
        chunk_days=30,
        status_cb=seen.append,
        sleep=lambda _s: None,
    )

    ranges = [c["settled_date_range"] for c in client.betting.calls]
    assert len(ranges) == 3
    assert ranges[0]["from"] == "2026-07-01T00:00:00Z"
    assert ranges[-1]["to"] == "2026-09-01T00:00:00Z"
    # Contiguous: each chunk's exclusive end is the next chunk's start.
    assert all(a["to"] == b["from"] for a, b in zip(ranges, ranges[1:]))
    assert len(seen) == 3


def test_market_requery_sends_explicit_ids_in_batches() -> None:
    rows = [_grouped(f"1.{i}", 1.0, 0.0) for i in range(3)]
    client = _FakeClient(rows)

    got = cm.fetch_commission_for_markets(
        client, ["1.0", "1.1", "1.2", " "], now=NOW, batch_size=2, sleep=lambda _s: None
    )

    assert sorted(g.market_id for g in got) == ["1.0", "1.1", "1.2"]
    assert [c["market_ids"] for c in client.betting.calls] == [["1.0", "1.1"], ["1.2"]]
    assert client.betting.calls[0]["settled_date_range"]["to"] == "2026-09-13T09:00:00Z"


# -----------------------------
# Merge and selection
# -----------------------------


def test_merge_upserts_by_market_and_the_latest_read_wins() -> None:
    existing = _commission_frame(
        {
            "marketId": "1.100",
            "eventTypeId": "2",
            "settledDateUtc": "2026-09-10T00:00:00Z",
            "betCount": "5",
            "grossProfit": "10.00",
            "commission": "0.00",
            "fetchedUtc": "2026-09-10T01:00:00Z",
        }
    )
    fresh = [
        cm.MarketCommission("1.100", "2", "2026-09-12T00:00:00Z", 9, 25.0, 1.3),
        cm.MarketCommission("1.200", "7", "2026-09-12T04:00:00Z", 3, -2.0, 0.0),
    ]

    merged = cm.merge_commission(existing, fresh, now=NOW).set_index("marketId")

    assert merged.loc["1.100", "commission"] == "1.30"
    assert merged.loc["1.100", "grossProfit"] == "25.00"
    assert merged.loc["1.100", "betCount"] == "9"
    assert merged.loc["1.100", "fetchedUtc"] == "2026-09-13T09:00:00Z"
    assert merged.loc["1.200", "commission"] == "0.00"


def test_merge_upgrades_a_float_damaged_spelling_to_the_full_id() -> None:
    existing = _commission_frame(
        {
            "marketId": "1.2515001",
            "eventTypeId": "7",
            "settledDateUtc": "",
            "betCount": "1",
            "grossProfit": "1.00",
            "commission": "0.07",
            "fetchedUtc": "2026-09-10T01:00:00Z",
        }
    )
    fresh = [cm.MarketCommission("1.251500100", "7", "", 1, 1.0, 0.07)]

    merged = cm.merge_commission(existing, fresh, now=NOW)

    assert merged["marketId"].tolist() == ["1.251500100"]


def test_merge_of_nothing_keeps_the_frame_shape() -> None:
    assert list(
        cm.merge_commission(cm.empty_commission_frame(), [], now=NOW).columns
    ) == (cm.COMMISSION_COLUMNS)


def test_requery_selects_only_ever_pending_markets_without_a_post_close_read() -> None:
    status = _status_frame(
        _status("1.1", "CLOSED", closed="2026-09-10T00:00:00Z"),  # never pending
        _status("1.2", "OPEN", first_pending="2026-09-01T00:00:00Z"),  # still open
        _status(  # closed, never read at all
            "1.3",
            "CLOSED",
            first_pending="2026-09-01T00:00:00Z",
            closed="2026-09-12T00:00:00Z",
        ),
        _status(  # closed, read before the close: still the 0.0 placeholder
            "1.4",
            "CLOSED",
            first_pending="2026-09-01T00:00:00Z",
            closed="2026-09-12T00:00:00Z",
        ),
        _status(  # closed, read after the close: final
            "1.5",
            "CLOSED",
            first_pending="2026-09-01T00:00:00Z",
            closed="2026-09-12T00:00:00Z",
        ),
    )
    store = _commission_frame(
        {"marketId": "1.4", "commission": "0.00", "fetchedUtc": "2026-09-11T00:00:00Z"},
        {"marketId": "1.5", "commission": "3.20", "fetchedUtc": "2026-09-12T06:00:00Z"},
    )

    assert cm.select_requery_markets(status, store) == ["1.2", "1.3", "1.4"]


def test_requery_with_no_status_file_is_empty() -> None:
    assert cm.select_requery_markets(None, cm.empty_commission_frame()) == []
    assert cm.select_requery_markets(_status_frame(), cm.empty_commission_frame()) == []
    # A hand-trimmed status frame without the close column cannot decide
    # anything; it must not abort the window read with a KeyError.
    trimmed = _status_frame(
        _status("1.2", "OPEN", first_pending="2026-09-01T00:00:00Z")
    )
    trimmed = trimmed.drop(columns=["closedObservedUtc"])
    assert cm.select_requery_markets(trimmed, cm.empty_commission_frame()) == []


def _canonical(*rows: tuple[str, str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"marketId": m, "settledDate": d, "betId": str(i)}
            for i, (m, d) in enumerate(rows)
        ],
        dtype=str,
    )


def test_recent_unknown_seed_reads_recent_markets_the_store_has_never_seen() -> None:
    """
    The self-heal: a window the step missed is picked up by id from the
    canonical on a later run, newest first, capped, and never for markets
    the store already holds (a damaged spelling counts as held).
    """
    canonical = _canonical(
        ("1.100", "2026-09-12T10:00:00Z"),  # unread, newest
        ("1.100", "2026-09-11T10:00:00Z"),  # same market, older leg
        ("1.2515001", "2026-09-11T09:00:00Z"),  # held under its full spelling
        ("1.300", "2026-09-10T10:00:00Z"),  # unread
        ("1.400", "2026-08-01T10:00:00Z"),  # too old
        ("", "2026-09-12T11:00:00Z"),  # blank id
    )
    store = _commission_frame(
        {
            "marketId": "1.251500100",
            "commission": "0.07",
            "fetchedUtc": "2026-09-11T12:00:00Z",
        },
        {
            "marketId": "1.300",
            "commission": "",
            "fetchedUtc": "",
        },  # unparseable: unread
    )

    got = cm.select_recent_unknown_markets(canonical, store, now=NOW, recent_days=14)

    assert got == ["1.100", "1.300"]
    assert cm.select_recent_unknown_markets(
        canonical, store, now=NOW, max_markets=1
    ) == ["1.100"]
    assert cm.select_recent_unknown_markets(None, store, now=NOW) == []
    assert (
        cm.select_recent_unknown_markets(canonical.drop(columns=["settledDate"]), store)
        == []
    )


# -----------------------------
# Persistence
# -----------------------------


def test_load_and_save_round_trip_keeps_ids_as_strings(tmp_path: Path) -> None:
    path = tmp_path / ".cache" / cm.COMMISSION_FILENAME
    frame = cm.merge_commission(
        cm.empty_commission_frame(),
        [
            cm.MarketCommission(
                "1.251500100", "7", "2026-09-12T00:00:00Z", 2, 8.77, 0.63
            )
        ],
        now=NOW,
    )

    cm.save_market_commission(frame, path)
    loaded = cm.load_market_commission(path)

    assert loaded["marketId"].tolist() == ["1.251500100"]
    assert loaded["commission"].tolist() == ["0.63"]
    assert not path.with_name(path.name + ".tmp").exists()


def test_load_missing_file_is_empty_and_unreadable_raises(tmp_path: Path) -> None:
    assert cm.load_market_commission(tmp_path / "none.csv").empty
    bad = tmp_path / "bad.csv"
    bad.write_bytes(b"\xff\xfe\x00garbage")
    with pytest.raises(Exception):
        cm.load_market_commission(bad)


# -----------------------------
# Orchestration
# -----------------------------


def test_update_reads_the_window_and_requeries_pending_markets(tmp_path: Path) -> None:
    rows = [
        _grouped("1.10", 20.0, 1.08, settledDate="2026-09-12T10:00:00.000Z"),
        _grouped("1.20", -6.5, 0.0, settledDate="2026-09-12T11:00:00.000Z"),
        # Outright whose latest leg is outside this window; only reachable
        # by explicit id.
        _grouped("1.30", 11.19, 0.6, settledDate="2026-09-01T00:00:00.000Z"),
    ]
    client = _FakeClient(rows)
    status = _status_frame(
        _status(
            "1.30",
            "CLOSED",
            first_pending="2026-08-20T00:00:00Z",
            closed="2026-09-13T08:00:00Z",
        )
    )
    said: list[str] = []

    result = cm.update_market_commission(
        client=client,
        cache_dir=tmp_path / ".cache",
        from_dt=NOW - timedelta(days=1),
        to_dt=NOW,
        df_status=status,
        now=NOW,
        status_cb=said.append,
        sleep_seconds=0.0,
    )

    assert result.attempted and result.fetched == 3 and result.requeried == 1
    assert any("re-reading 1 ever-pending and 0 unread" in m for m in said)
    stored = cm.load_market_commission(result.path).set_index("marketId")
    assert stored.loc["1.10", "commission"] == "1.08"
    assert stored.loc["1.20", "commission"] == "0.00"
    assert stored.loc["1.30", "commission"] == "0.60"
    assert stored.loc["1.30", "fetchedUtc"] == "2026-09-13T09:00:00Z"
    assert "3 market(s) on file" in result.message
    # The window call and the explicit-id call are distinct requests.
    assert [("market_ids" in c) for c in client.betting.calls] == [False, True]


def test_recent_seed_rereads_a_winning_market_stored_with_zero_commission() -> None:
    """
    The status step failed while 1.100 was partially settled, so its 0.00
    placeholder was stored with no pending record; it closed before the
    next run and was CLOSED at first sight. Betfair charges on every win,
    so the zero is not final and the market is read again. A losing market
    with zero commission is final.
    """
    canonical = _canonical(
        ("1.100", "2026-09-12T10:00:00Z"),
        ("1.200", "2026-09-12T10:00:00Z"),
    )
    store = _commission_frame(
        {
            "marketId": "1.100",
            "grossProfit": "412.00",
            "commission": "0.00",
            "fetchedUtc": "2026-09-12T12:00:00Z",
        },
        {
            "marketId": "1.200",
            "grossProfit": "-6.50",
            "commission": "0.00",
            "fetchedUtc": "2026-09-12T12:00:00Z",
        },
    )

    assert cm.select_recent_unknown_markets(canonical, store, now=NOW) == ["1.100"]


def test_update_seeds_recent_canonical_markets_missing_from_the_store(
    tmp_path: Path,
) -> None:
    """A window missed by a failed step is re-read on the next run by id."""
    rows = [_grouped("1.900", 8.0, 0.56, settledDate="2026-09-11T10:00:00.000Z")]
    client = _FakeClient(rows)
    canonical = _canonical(("1.900", "2026-09-11T10:00:00Z"))

    result = cm.update_market_commission(
        client=client,
        cache_dir=tmp_path / ".cache",
        from_dt=NOW - timedelta(hours=2),  # a window that does not contain it
        to_dt=NOW,
        df_canonical=canonical,
        now=NOW,
        sleep_seconds=0.0,
    )

    assert result.requeried == 1 and result.fetched == 1
    stored = cm.load_market_commission(result.path).set_index("marketId")
    assert stored.loc["1.900", "commission"] == "0.56"
    explicit = [c for c in client.betting.calls if "market_ids" in c]
    assert explicit and explicit[0]["market_ids"] == ["1.900"]


def test_update_with_nothing_to_read_writes_nothing(tmp_path: Path) -> None:
    client = _FakeClient([])

    result = cm.update_market_commission(
        client=client, cache_dir=tmp_path / ".cache", from_dt=None, to_dt=None, now=NOW
    )

    assert result.fetched == 0 and "nothing to read" in result.message
    assert not result.path.exists()
    assert client.betting.calls == []


def test_update_leaves_the_file_untouched_when_the_fetch_fails(tmp_path: Path) -> None:
    path = tmp_path / ".cache" / cm.COMMISSION_FILENAME
    before = cm.merge_commission(
        cm.empty_commission_frame(),
        [cm.MarketCommission("1.1", "7", "", 1, 1.0, 0.07)],
        now=NOW - timedelta(days=1),
    )
    cm.save_market_commission(before, path)
    client = _FakeClient([_grouped("1.2", "bad", 0.0)])

    with pytest.raises(ValueError):
        cm.update_market_commission(
            client=client,
            cache_dir=tmp_path / ".cache",
            from_dt=NOW - timedelta(days=1),
            to_dt=NOW,
            now=NOW,
        )

    assert cm.load_market_commission(path)["marketId"].tolist() == ["1.1"]
