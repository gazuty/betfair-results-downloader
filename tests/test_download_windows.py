"""Tests for half-open download windows and the paginated fetch loop."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from betfair_results_downloader.downloader_core import (
    _build_datetime_chunks,
    _to_utc_datetime,
    fetch_cleared_orders_df_range,
)


class TestToUtcDatetime:
    def test_date_expands_to_exclusive_end_of_day(self) -> None:
        result = _to_utc_datetime(date(2026, 3, 15), end_of_day=True)
        assert result == datetime(2026, 3, 16, 0, 0, 0, tzinfo=timezone.utc)

    def test_date_expands_to_start_of_day(self) -> None:
        result = _to_utc_datetime(date(2026, 3, 15), end_of_day=False)
        assert result == datetime(2026, 3, 15, 0, 0, 0, tzinfo=timezone.utc)

    def test_naive_datetime_assumed_utc(self) -> None:
        result = _to_utc_datetime(datetime(2026, 3, 15, 12, 30), end_of_day=True)
        assert result == datetime(2026, 3, 15, 12, 30, tzinfo=timezone.utc)


class TestBuildDatetimeChunks:
    def test_chunks_are_contiguous_with_no_gap(self) -> None:
        """Each chunk's exclusive end must be exactly the next chunk's start
        — the old 23:59:59 endings left a sub-second blind spot."""
        from_dt = _to_utc_datetime(date(2026, 1, 1), end_of_day=False)
        to_dt = _to_utc_datetime(date(2026, 3, 15), end_of_day=True)
        chunks = _build_datetime_chunks(from_dt, to_dt, 30)
        assert len(chunks) > 1
        for (_, prev_to), (next_from, _) in zip(chunks, chunks[1:]):
            assert prev_to == next_from
        assert chunks[0][0] == from_dt
        assert chunks[-1][1] == to_dt

    def test_single_chunk_preserves_exact_bounds(self) -> None:
        from_dt = datetime(2026, 6, 6, 17, 30, tzinfo=timezone.utc)
        to_dt = datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)
        assert _build_datetime_chunks(from_dt, to_dt, 30) == [(from_dt, to_dt)]


class _FakeClearedOrders:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def json(self) -> str:
        return json.dumps({"clearedOrders": self._rows})


class _FakeBetting:
    def __init__(self, pages: list[list[dict]]) -> None:
        self._pages = list(pages)
        self.calls: list[dict] = []

    def list_cleared_orders(self, **kwargs):
        self.calls.append(kwargs)
        rows = self._pages.pop(0) if self._pages else []
        return _FakeClearedOrders(rows)


class _FakeClient:
    def __init__(self, pages: list[list[dict]]) -> None:
        self.betting = _FakeBetting(pages)


def _order(bet_id: str) -> dict:
    return {
        "betId": bet_id,
        "eventTypeId": "7",
        "marketId": "1.234",
        "side": "BACK",
        "betOutcome": "WON",
        "profit": 5.0,
        "placedDate": "2026-01-01T05:00:00Z",
        "settledDate": "2026-01-01T06:00:00Z",
        "itemDescription": {
            "eventDesc": "Test Event",
            "marketDesc": "Test Market",
            "runnerDesc": "Test Runner",
            "marketType": "WIN",
        },
    }


class TestFetchClearedOrdersRange:
    def test_paginates_until_empty_page_and_flattens_descriptions(self) -> None:
        client = _FakeClient(pages=[[_order("1"), _order("2")], [_order("3")], []])
        result = fetch_cleared_orders_df_range(
            betfair={},
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 1),
            api_client=client,
            page_size=2,
        )
        assert result.attempted is True
        assert result.rows_downloaded == 3
        assert list(result.df_co["evt_eventName"].unique()) == ["Test Event"]
        assert "itemDescription" not in result.df_co.columns
        assert (result.df_co["Win"] == 1).all()
        # Three pages requested: from_record 0, 2, 4
        froms = [c["from_record"] for c in client.betting.calls]
        assert froms == [0, 2, 4]

    def test_api_window_covers_the_full_final_day(self) -> None:
        client = _FakeClient(pages=[[]])
        fetch_cleared_orders_df_range(
            betfair={},
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 1),
            api_client=client,
        )
        settled_range = client.betting.calls[0]["settled_date_range"]
        assert settled_range["from"] == "2026-01-01T00:00:00Z"
        assert settled_range["to"] == "2026-01-02T00:00:00Z"

    def test_empty_range_returns_normalized_empty_frame(self) -> None:
        result = fetch_cleared_orders_df_range(
            betfair={},
            from_date=date(2026, 1, 2),
            to_date=date(2026, 1, 1),
            api_client=_FakeClient(pages=[]),
        )
        assert result.attempted is True
        assert result.rows_downloaded == 0
        assert "betId" in result.df_co.columns
