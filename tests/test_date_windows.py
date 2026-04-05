from datetime import date, timedelta

import pytest

from betfair_results_downloader.scheduler.date_windows import chunk_date_range


BASE = date(2026, 1, 1)


def _offset(days: int) -> date:
    return BASE + timedelta(days=days)


def test_single_day_same_from_and_to() -> None:
    chunks = chunk_date_range(BASE, BASE, chunk_days=30)
    assert chunks == [(BASE, BASE)]


def test_exact_chunk_size_fits_one_chunk() -> None:
    # 30 calendar days inclusive: [d, d+29]
    chunks = chunk_date_range(BASE, _offset(29), chunk_days=30)
    assert chunks == [(BASE, _offset(29))]


def test_chunk_size_plus_one_day_splits_into_two() -> None:
    # 31 days: one full chunk + a 1-day tail
    chunks = chunk_date_range(BASE, _offset(30), chunk_days=30)
    assert chunks == [
        (BASE, _offset(29)),
        (_offset(30), _offset(30)),
    ]


def test_exact_multiple_of_chunk_size() -> None:
    # 60 days: exactly 2 chunks, no remainder
    chunks = chunk_date_range(BASE, _offset(59), chunk_days=30)
    assert chunks == [
        (BASE, _offset(29)),
        (_offset(30), _offset(59)),
    ]


def test_multiple_chunks_with_remainder() -> None:
    # 61 days: 2 full chunks + 1-day tail
    chunks = chunk_date_range(BASE, _offset(60), chunk_days=30)
    assert chunks == [
        (BASE, _offset(29)),
        (_offset(30), _offset(59)),
        (_offset(60), _offset(60)),
    ]


def test_inverted_range_returns_empty() -> None:
    chunks = chunk_date_range(_offset(5), BASE, chunk_days=30)
    assert chunks == []


def test_small_chunk_size_splits_finely() -> None:
    # 7 days with chunk_days=2 → ceil(7/2) = 4 chunks: [0,1],[2,3],[4,5],[6,6]
    chunks = chunk_date_range(BASE, _offset(6), chunk_days=2)
    assert chunks == [
        (BASE, _offset(1)),
        (_offset(2), _offset(3)),
        (_offset(4), _offset(5)),
        (_offset(6), _offset(6)),
    ]


def test_chunk_days_one_produces_one_tuple_per_day() -> None:
    chunks = chunk_date_range(BASE, _offset(3), chunk_days=1)
    assert chunks == [
        (BASE, BASE),
        (_offset(1), _offset(1)),
        (_offset(2), _offset(2)),
        (_offset(3), _offset(3)),
    ]


def test_chunks_are_contiguous_and_non_overlapping() -> None:
    chunks = chunk_date_range(BASE, _offset(100), chunk_days=17)
    # every next chunk starts exactly one day after the previous chunk ends
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt[0] == prev[1] + timedelta(days=1)
    # union covers the full range
    assert chunks[0][0] == BASE
    assert chunks[-1][1] == _offset(100)


def test_invalid_chunk_days_zero_raises() -> None:
    with pytest.raises(ValueError):
        chunk_date_range(BASE, _offset(5), chunk_days=0)


def test_invalid_chunk_days_negative_raises() -> None:
    with pytest.raises(ValueError):
        chunk_date_range(BASE, _offset(5), chunk_days=-3)
