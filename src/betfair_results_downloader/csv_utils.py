from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _betid_keys(df: pd.DataFrame) -> set[str]:
    """The set of usable betId values in ``df``; empty when the column is absent."""
    if df is None or df.empty or "betId" not in df.columns:
        return set()
    col = df["betId"].astype(str).str.strip()
    return set(col[col.ne("") & col.ne("nan") & col.ne("None")])


def clean_and_remove_duplicates(
    df: pd.DataFrame,
    *,
    status_cb: Optional[Callable[[str], None]] = None,
) -> pd.DataFrame:
    """
    Minimal, robust dedupe for cleared orders-like data.

    Primary key: betId (best identifier for cleared orders).
    Falls back to full row dedupe if betId missing.

    Args:
        df: DataFrame to deduplicate
        status_cb: Optional callback for status messages (warnings visible in GUI)

    Returns:
        The deduplicated frame.
    """

    def warn(msg: str) -> None:
        """Log and emit warning via status callback."""
        logger.warning(msg)
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass

    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame()

    out = df.copy()
    total_rows = len(out)

    if "betId" in out.columns:
        # Coerce into a temporary column rather than over the real one.
        # Overwriting betId made the numeric form the value that got written
        # back to disk, which is how a text column becomes a float column and
        # loses its trailing digits.
        bet_key = "_betid_numeric"
        out[bet_key] = pd.to_numeric(out["betId"], errors="coerce")

        nan_count = int(out[bet_key].isna().sum())
        valid_count = total_rows - nan_count

        if nan_count > 0 and valid_count > 0:
            # Some betIds are invalid, but we can still dedupe on valid ones
            warn(
                f"DEDUPE WARNING: {nan_count:,} of {total_rows:,} rows have invalid betId values"
            )

        if out[bet_key].notna().any():
            # Stable ordering improves reproducibility. Sort on typed
            # temporary keys rather than the raw columns: date columns
            # round-trip through CSV as strings whose rendering can differ
            # for the same instant (e.g. "2026-07-13 04:58:46+00:00" vs
            # "2026-07-13T04:58:46Z"), so a lexicographic sort could place
            # a stale existing row after the fresh incoming one and
            # keep="last" would keep the stale row. With typed keys, equal
            # instants compare equal and the stable mergesort preserves
            # input order (existing before incoming), so incoming wins.
            sort_key_parsers: dict[str, Callable[[pd.Series], pd.Series]] = {
                "settledDate": lambda s: pd.to_datetime(
                    s, utc=True, errors="coerce", format="ISO8601"
                ),
                "placedDate": lambda s: pd.to_datetime(
                    s, utc=True, errors="coerce", format="ISO8601"
                ),
                "marketId": lambda s: pd.to_numeric(s, errors="coerce"),
                bet_key: lambda s: s,  # numeric copy built above
            }
            sort_keys = []
            for col, parse in sort_key_parsers.items():
                if col in out.columns:
                    key = f"_sort_{col}"
                    out[key] = parse(out[col])
                    sort_keys.append(key)
            if sort_keys:
                out = out.sort_values(sort_keys, kind="mergesort").drop(
                    columns=sort_keys
                )

            # Dedupe only rows that have a usable key. pandas treats NaN as
            # equal to NaN in drop_duplicates, so including the unparseable
            # ones would collapse every last one of them into a single row --
            # harmless while the result stays in memory, destructive on the
            # archival path where the originals are then deleted.
            has_key = out[bet_key].notna()
            keyed = out[has_key].drop_duplicates(subset=[bet_key], keep="last")
            unkeyed = out[~has_key].drop_duplicates(keep="last")
            if len(unkeyed):
                out = pd.concat([keyed, unkeyed], ignore_index=True)
            else:
                out = keyed.reset_index(drop=True)
            return out.drop(columns=[bet_key]).reset_index(drop=True)
        else:
            # All betIds are NaN - must fall back
            warn(
                f"DEDUPE WARNING: All {total_rows:,} betId values invalid — falling back to full-row dedupe"
            )

    else:
        # betId column missing entirely
        warn("DEDUPE WARNING: betId column missing — using full-row dedupe")

    # fallback: full-row dedupe. Drop the temporary numeric key first -- it
    # must never reach the written file, and leaving it in would also make
    # every row unique-looking to a full-row dedupe.
    out = out.drop(columns=["_betid_numeric"], errors="ignore")
    return out.drop_duplicates(keep="last").reset_index(drop=True)


def update_csv_with_new_data(
    existing_csv_path: str | Path,
    new_data_df: pd.DataFrame,
    *,
    status_cb: Optional[Callable[[str], None]] = None,
) -> tuple[Path, pd.DataFrame]:
    """
    Idempotently update a canonical CSV with new data:
    - create parent directory if missing
    - read existing if present
    - union columns
    - concat + dedupe
    - atomic write (tmp then replace)

    Args:
        existing_csv_path: Path to canonical CSV
        new_data_df: New data to add
        status_cb: Optional callback for status messages (warnings visible in GUI)

    Returns:
        (path, combined) -- the frame is returned so callers do not have to
        read back the file that was just written. On the live canonical that
        reload costs ~3s per run and re-triggers dtype inference over ~1M
        rows for no benefit.
    """
    path = Path(existing_csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    incoming = clean_and_remove_duplicates(new_data_df, status_cb=status_cb)

    existing_keys: Optional[set[str]] = None
    if path.exists():
        # dtype=str is load-bearing, not a style choice. With inferred types
        # pandas reads marketId ("1.251500100") as float64 and writes it back
        # as "1.2515001", permanently destroying trailing digits -- measured
        # at 8.6% of rows on the live canonical. keep_default_na=False stops
        # empty strings becoming NaN and then the literal "nan" on write.
        existing = pd.read_csv(path, dtype=str, keep_default_na=False)
        existing_keys = _betid_keys(existing)

        # union schema and align
        cols = sorted(set(existing.columns).union(set(incoming.columns)))
        existing = existing.reindex(columns=cols)
        incoming = incoming.reindex(columns=cols)

        combined = pd.concat([existing, incoming], ignore_index=True)
    else:
        combined = incoming

    combined = clean_and_remove_duplicates(combined, status_cb=status_cb)

    # No record that was in the file may disappear from it. Writing would
    # atomically replace the system of record, so a dedupe or schema fault
    # here is unrecoverable.
    #
    # Membership, not row count: if the canonical already contains duplicate
    # betIds, deduping it legitimately produces fewer rows than it started
    # with. Comparing counts would abort every run from then on and leave the
    # file unrepairable through the normal path -- worse than the fault it
    # is meant to catch.
    if existing_keys is not None:
        lost = existing_keys - _betid_keys(combined)
        if lost:
            raise ValueError(
                f"Refusing to write {path.name}: {len(lost):,} betIds present "
                f"in the existing file are absent after merging {len(incoming):,} "
                f"incoming rows (e.g. {sorted(lost)[:3]}). The existing file has "
                f"been left untouched."
            )

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    combined.to_csv(tmp_path, index=False)
    tmp_path.replace(path)

    return path, combined
