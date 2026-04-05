from __future__ import annotations

from datetime import date, timedelta


def chunk_date_range(
    from_date: date,
    to_date: date,
    chunk_days: int = 30,
) -> list[tuple[date, date]]:
    """
    Split an inclusive [from_date, to_date] range into consecutive chunks of at most
    ``chunk_days`` calendar days each.

    Each returned tuple ``(chunk_from, chunk_to)`` is inclusive on both ends and
    adjacent chunks do not overlap (``next.from == prev.to + 1 day``).

    Behaviour:
    - ``from_date > to_date`` returns an empty list (empty/inverted range).
    - ``from_date == to_date`` returns ``[(from_date, to_date)]`` (a single 1-day chunk).
    - ``chunk_days < 1`` raises ``ValueError``.

    A chunk of ``chunk_days=30`` spans 30 calendar days inclusive
    (e.g. ``[d, d+29]``), so the next chunk begins at ``d+30``.
    """
    if chunk_days < 1:
        raise ValueError(f"chunk_days must be >= 1, got {chunk_days}")
    if from_date > to_date:
        return []

    chunks: list[tuple[date, date]] = []
    cursor = from_date
    span = timedelta(days=chunk_days - 1)
    while cursor <= to_date:
        chunk_end = cursor + span
        if chunk_end > to_date:
            chunk_end = to_date
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks
