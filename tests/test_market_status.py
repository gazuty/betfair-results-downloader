"""
market_status: observing, merging and persisting per-market settlement status.

The two facts these tests encode were measured live on 2026-09-06: CLOSED
markets eventually leave listMarketBook (so absence means closed), and OPEN
markets are always returned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from betfairlightweight.exceptions import APIError

from betfair_results_downloader import market_status as ms

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=timezone.utc)


def _book(market_id: str, status: str, active: int) -> SimpleNamespace:
    return SimpleNamespace(
        market_id=market_id, status=status, number_of_active_runners=active
    )


class _FakeBetting:
    def __init__(self, books: list, *, transient_failures: int = 0) -> None:
        self._books = {ms.decimal_key(b.market_id): b for b in books}
        self.calls: list[list[str]] = []
        self._transient_failures = transient_failures

    def list_market_book(self, market_ids: list[str]):
        self.calls.append(list(market_ids))
        if self._transient_failures:
            self._transient_failures -= 1
            raise APIError(None, exception=RuntimeError("TIMEOUT_ERROR"))
        return [
            self._books[ms.decimal_key(m)]
            for m in market_ids
            if ms.decimal_key(m) in self._books
        ]


class _FakeClient:
    def __init__(self, books: list, **kwargs) -> None:
        self.betting = _FakeBetting(books, **kwargs)


def _status_row(market_id: str, status: str, **overrides) -> dict:
    row = {
        "marketId": market_id,
        "status": status,
        "activeRunners": "0",
        "source": "book",
        "checkedUtc": "2026-09-05T09:00:00Z",
        "firstPendingUtc": "",
        "closedObservedUtc": "" if status != "CLOSED" else "2026-09-05T09:00:00Z",
    }
    row.update(overrides)
    return row


def _frame(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=ms.STATUS_COLUMNS, dtype=str)


# -----------------------------
# fetch_market_statuses
# -----------------------------


class TestFetch:
    def test_batches_ids_and_requests_every_one(self) -> None:
        ids = [f"1.{i:09d}" for i in range(1, 91)]
        client = _FakeClient([_book(m, "CLOSED", 0) for m in ids])
        slept: list[float] = []

        observed = ms.fetch_market_statuses(
            client, ids, batch_size=40, sleep_seconds=0.5, sleep=slept.append
        )

        assert [len(c) for c in client.betting.calls] == [40, 40, 10]
        assert sorted(m for call in client.betting.calls for m in call) == sorted(ids)
        assert set(observed) == set(ids)
        assert slept == [0.5, 0.5], "no sleep before the first batch"

    def test_absent_market_is_closed_by_absence(self) -> None:
        client = _FakeClient([_book("1.247612197", "OPEN", 22)])

        observed = ms.fetch_market_statuses(client, ["1.247612197", "1.261951764"])

        assert observed["1.247612197"] == ms.BookStatus("OPEN", 22, ms.SOURCE_BOOK)
        assert observed["1.261951764"] == ms.BookStatus("CLOSED", 0, ms.SOURCE_ABSENT)

    def test_result_is_keyed_by_the_requested_spelling(self) -> None:
        """A damaged canonical spelling must still find Betfair's clean one."""
        client = _FakeClient([_book("1.251500100", "CLOSED", 0)])

        observed = ms.fetch_market_statuses(client, ["1.2515001"])

        assert observed["1.2515001"].source == ms.SOURCE_BOOK

    def test_transient_error_is_retried(self) -> None:
        client = _FakeClient([_book("1.1", "OPEN", 3)], transient_failures=1)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ms, "retry_betfair_call", _no_sleep_retry)
            observed = ms.fetch_market_statuses(client, ["1.1"])

        assert len(client.betting.calls) == 2
        assert observed["1.1"].status == "OPEN"

    def test_empty_input_makes_no_calls(self) -> None:
        client = _FakeClient([])
        assert ms.fetch_market_statuses(client, ["", "  "]) == {}
        assert client.betting.calls == []


def _no_sleep_retry(fn, **kwargs):
    from betfair_results_downloader.betfair_net import retry_betfair_call

    kwargs["sleep"] = lambda _s: None
    return retry_betfair_call(fn, **kwargs)


# -----------------------------
# select_markets_to_check
# -----------------------------


class TestSelect:
    def test_window_plus_pending_plus_recent_unknown(self) -> None:
        status = _frame(
            _status_row("1.100", "OPEN", firstPendingUtc="2026-09-01T00:00:00Z"),
            _status_row("1.200", "CLOSED"),
        )
        canonical = pd.DataFrame(
            {
                "marketId": ["1.200", "1.300", "1.400"],
                "settledDate": [
                    "2026-09-05T10:00:00Z",  # known: CLOSED already
                    "2026-09-01T10:00:00Z",  # unknown, recent
                    "2026-07-01T10:00:00Z",  # unknown, too old
                ],
            }
        )

        chosen = ms.select_markets_to_check(
            ["1.500"], status, canonical, now=NOW, recent_days=14
        )

        assert sorted(chosen) == ["1.100", "1.300", "1.500"]

    def test_damaged_and_clean_spellings_collapse_to_the_full_one(self) -> None:
        """
        Betfair cannot resolve a truncated id, and an unresolved id looks
        exactly like a closed market -- so the longest spelling must win
        whatever order the ids arrive in.
        """
        damaged_first = ms.select_markets_to_check(
            ["1.2515001", "1.251500100"], ms.empty_status_frame(), None, now=NOW
        )
        clean_first = ms.select_markets_to_check(
            ["1.251500100", "1.2515001"], ms.empty_status_frame(), None, now=NOW
        )
        assert damaged_first == clean_first == ["1.251500100"]

    def test_markets_closed_by_absence_are_rechecked_for_a_while(self) -> None:
        """
        One dropped response row must not permanently mark a live outright
        CLOSED: absence stays provisional for absent_recheck_hours.
        """
        status = _frame(
            _status_row(
                "1.fresh",
                "CLOSED",
                source="absent",
                closedObservedUtc="2026-09-06T00:00:00Z",  # 9h ago
            ),
            _status_row(
                "1.stale",
                "CLOSED",
                source="absent",
                closedObservedUtc="2026-09-01T00:00:00Z",  # 5 days ago
            ),
            _status_row(
                "1.book",
                "CLOSED",
                source="book",
                closedObservedUtc="2026-09-06T00:00:00Z",
            ),
        )

        chosen = ms.select_markets_to_check(
            [], status, None, now=NOW, absent_recheck_hours=48
        )

        assert chosen == ["1.fresh"]

    def test_unknown_seed_is_capped_newest_first(self) -> None:
        canonical = pd.DataFrame(
            {
                "marketId": ["1.old", "1.mid", "1.new"],
                "settledDate": [
                    "2026-09-01T00:00:00Z",
                    "2026-09-03T00:00:00Z",
                    "2026-09-05T00:00:00Z",
                ],
            }
        )

        chosen = ms.select_markets_to_check(
            [], ms.empty_status_frame(), canonical, now=NOW, max_recent_unknown=2
        )

        assert chosen == ["1.new", "1.mid"]

    def test_no_canonical_and_no_file_is_just_the_window(self) -> None:
        chosen = ms.select_markets_to_check(
            ["1.1", float("nan"), ""], None, None, now=NOW
        )
        assert chosen == ["1.1"]


# -----------------------------
# merge_statuses
# -----------------------------


class TestMerge:
    def test_first_sight_closed_has_no_pending_timestamp(self) -> None:
        merged = ms.merge_statuses(
            ms.empty_status_frame(),
            {"1.1": ms.BookStatus("CLOSED", 0, ms.SOURCE_BOOK)},
            now=NOW,
        )
        row = merged.iloc[0]
        assert row["status"] == "CLOSED"
        assert row["firstPendingUtc"] == ""
        assert row["closedObservedUtc"] == "2026-09-06T09:00:00Z"
        assert row["checkedUtc"] == "2026-09-06T09:00:00Z"

    def test_pending_then_closed_keeps_first_pending_and_stamps_close(self) -> None:
        first = ms.merge_statuses(
            ms.empty_status_frame(),
            {"1.1": ms.BookStatus("OPEN", 22, ms.SOURCE_BOOK)},
            now=NOW,
        )
        later = NOW + timedelta(days=3)
        again = ms.merge_statuses(
            first, {"1.1": ms.BookStatus("OPEN", 4, ms.SOURCE_BOOK)}, now=later
        )
        closed = ms.merge_statuses(
            again,
            {"1.1": ms.BookStatus("CLOSED", 0, ms.SOURCE_ABSENT)},
            now=later + timedelta(days=4),
        )

        assert len(closed) == 1
        row = closed.iloc[0]
        assert again.iloc[0]["firstPendingUtc"] == "2026-09-06T09:00:00Z"
        assert row["firstPendingUtc"] == "2026-09-06T09:00:00Z", "set once"
        assert row["closedObservedUtc"] == "2026-09-13T09:00:00Z"
        assert row["status"] == "CLOSED"
        assert row["source"] == ms.SOURCE_ABSENT
        assert row["activeRunners"] == "0"

    def test_reopened_market_is_recorded_honestly(self) -> None:
        closed = _frame(_status_row("1.1", "CLOSED"))
        reopened = ms.merge_statuses(
            closed, {"1.1": ms.BookStatus("OPEN", 2, ms.SOURCE_BOOK)}, now=NOW
        )
        row = reopened.iloc[0]
        assert row["status"] == "OPEN"
        assert row["closedObservedUtc"] == ""
        assert row["firstPendingUtc"] == "2026-09-06T09:00:00Z"

    def test_damaged_spelling_updates_the_same_row(self) -> None:
        existing = _frame(
            _status_row("1.251500100", "OPEN", firstPendingUtc="2026-09-01T00:00:00Z")
        )
        merged = ms.merge_statuses(
            existing, {"1.2515001": ms.BookStatus("CLOSED", 0, ms.SOURCE_BOOK)}, now=NOW
        )
        assert len(merged) == 1
        assert merged.iloc[0]["marketId"] == "1.251500100", "file spelling kept"
        assert merged.iloc[0]["status"] == "CLOSED"

    def test_blank_market_ids_are_dropped_not_merged(self) -> None:
        existing = _frame(_status_row("", "OPEN"), _status_row("1.1", "OPEN"))
        merged = ms.merge_statuses(
            existing, {"": ms.BookStatus("CLOSED", 0, ms.SOURCE_BOOK)}, now=NOW
        )
        assert list(merged["marketId"]) == ["1.1"]

    def test_cells_keep_zero_and_drop_nan(self) -> None:
        existing = pd.DataFrame(
            [
                {
                    "marketId": "1.1",
                    "status": "OPEN",
                    "activeRunners": 0,
                    "source": "book",
                    "checkedUtc": "x",
                    "firstPendingUtc": float("nan"),
                    "closedObservedUtc": None,
                }
            ]
        )
        merged = ms.merge_statuses(existing, {}, now=NOW)
        row = merged.iloc[0]
        assert row["activeRunners"] == "0"
        assert row["firstPendingUtc"] == ""
        assert row["closedObservedUtc"] == ""

    def test_untouched_rows_survive(self) -> None:
        existing = _frame(_status_row("1.9", "OPEN", firstPendingUtc="x"))
        merged = ms.merge_statuses(
            existing, {"1.1": ms.BookStatus("CLOSED", 0, ms.SOURCE_BOOK)}, now=NOW
        )
        assert sorted(merged["marketId"]) == ["1.1", "1.9"]


# -----------------------------
# prune_closed
# -----------------------------


def test_prune_drops_only_old_closed_rows() -> None:
    frame = _frame(
        _status_row("1.old", "CLOSED", closedObservedUtc="2026-01-01T00:00:00Z"),
        _status_row("1.new", "CLOSED", closedObservedUtc="2026-09-01T00:00:00Z"),
        _status_row("1.pending", "OPEN", firstPendingUtc="2026-01-01T00:00:00Z"),
    )
    pruned = ms.prune_closed(frame, now=NOW, keep_days=90)
    assert sorted(pruned["marketId"]) == ["1.new", "1.pending"]
    assert ms.prune_closed(frame, now=NOW, keep_days=0).equals(frame)


# -----------------------------
# persistence
# -----------------------------


class TestPersistence:
    def test_roundtrip_preserves_market_id_spelling_and_empty_fields(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / ".cache" / ms.STATUS_FILENAME
        frame = _frame(
            _status_row("1.251500100", "OPEN", firstPendingUtc="2026-09-01T00:00:00Z")
        )

        ms.save_market_status(frame, path)
        loaded = ms.load_market_status(path)

        assert loaded.iloc[0]["marketId"] == "1.251500100", "trailing zero kept"
        assert loaded.iloc[0]["closedObservedUtc"] == "", "empty stays empty"
        assert list(loaded.columns) == ms.STATUS_COLUMNS
        assert not path.with_name(path.name + ".tmp").exists()

    def test_missing_file_is_an_empty_frame(self, tmp_path: Path) -> None:
        loaded = ms.load_market_status(tmp_path / "nope.csv")
        assert loaded.empty
        assert list(loaded.columns) == ms.STATUS_COLUMNS

    def test_unreadable_file_raises_rather_than_forgetting_pending(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / ms.STATUS_FILENAME
        path.write_bytes(b"\xff\xfe\x00garbage,,,\n\x00")
        with pytest.raises(Exception):
            ms.load_market_status(path)

    def test_older_file_without_new_columns_loads(self, tmp_path: Path) -> None:
        path = tmp_path / ms.STATUS_FILENAME
        path.write_text("marketId,status\n1.1,OPEN\n", encoding="utf-8")
        loaded = ms.load_market_status(path)
        assert loaded.iloc[0]["firstPendingUtc"] == ""
        assert list(loaded.columns) == ms.STATUS_COLUMNS


# -----------------------------
# update_market_status (orchestration)
# -----------------------------


class TestUpdate:
    def test_end_to_end_writes_merged_file_and_reports(self, tmp_path: Path) -> None:
        cache = tmp_path / ".cache"
        ms.save_market_status(
            _frame(
                _status_row("1.pending", "OPEN", firstPendingUtc="2026-09-01T00:00:00Z")
            ),
            ms.resolve_status_path(cache),
        )
        client = _FakeClient(
            [
                _book("1.race", "CLOSED", 0),
                _book("1.outright", "OPEN", 22),
                # "1.pending" is absent from the book: it has closed.
            ]
        )
        window = pd.DataFrame({"marketId": ["1.race", "1.outright"]})
        canonical = pd.DataFrame(
            {
                "marketId": ["1.race", "1.outright", "1.recent"],
                "settledDate": ["2026-09-06T08:00:00Z"] * 3,
            }
        )

        result = ms.update_market_status(
            client=client,
            cache_dir=cache,
            df_window=window,
            df_canonical=canonical,
            now=NOW,
            sleep_seconds=0,
        )

        assert result.attempted is True
        assert result.checked == 4
        assert result.closed == 3  # race, pending-by-absence, recent-by-absence
        assert result.pending == 1  # outright
        assert "checked 4" in result.message and "2 by absence" in result.message
        assert "Oldest pending market first seen 0 day(s) ago" in result.message

        saved = ms.load_market_status(ms.resolve_status_path(cache)).set_index(
            "marketId"
        )
        assert saved.loc["1.pending", "status"] == "CLOSED"
        assert saved.loc["1.pending", "source"] == ms.SOURCE_ABSENT
        assert saved.loc["1.pending", "firstPendingUtc"] == "2026-09-01T00:00:00Z"
        assert saved.loc["1.pending", "closedObservedUtc"] == "2026-09-06T09:00:00Z"
        assert saved.loc["1.outright", "status"] == "OPEN"
        assert saved.loc["1.outright", "firstPendingUtc"] == "2026-09-06T09:00:00Z"
        assert saved.loc["1.race", "firstPendingUtc"] == ""

    def test_nothing_to_check_touches_nothing(self, tmp_path: Path) -> None:
        client = _FakeClient([])
        result = ms.update_market_status(
            client=client,
            cache_dir=tmp_path / ".cache",
            df_window=pd.DataFrame(),
            df_canonical=None,
            now=NOW,
        )
        assert result.checked == 0
        assert client.betting.calls == []
        assert not (tmp_path / ".cache" / ms.STATUS_FILENAME).exists()

    def test_fetch_failure_leaves_the_existing_file_intact(
        self, tmp_path: Path
    ) -> None:
        cache = tmp_path / ".cache"
        path = ms.resolve_status_path(cache)
        original = _frame(_status_row("1.pending", "OPEN", firstPendingUtc="x"))
        ms.save_market_status(original, path)
        before = path.read_text(encoding="utf-8")

        class _Broken:
            class betting:  # noqa: N801 - mirrors the client attribute
                @staticmethod
                def list_market_book(market_ids):
                    raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            ms.update_market_status(
                client=_Broken(),
                cache_dir=cache,
                df_window=pd.DataFrame({"marketId": ["1.new"]}),
                df_canonical=None,
                now=NOW,
            )

        assert path.read_text(encoding="utf-8") == before
