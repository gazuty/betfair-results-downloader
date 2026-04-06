from __future__ import annotations

from pathlib import Path
from typing import List
from datetime import datetime

import pandas as pd
import streamlit as st


CANONICAL_HINTS = [
    "cleared_orders_cleaned",
    "cleared_orders",
]


def discover_csv_files(results_dir: str) -> List[Path]:
    """
    Discover candidate cleared orders CSV files in a folder.
    """
    p = Path(results_dir).expanduser()
    if not p.exists() or not p.is_dir():
        return []

    candidates: list[Path] = []
    for f in p.glob("*.csv"):
        name = f.name.lower()
        if "cleared" in name and "order" in name:
            candidates.append(f)

    # Prefer canonical-ish names first, then newest by mtime
    def score(path: Path) -> tuple[int, float]:
        name = path.name.lower()
        canon = any(h in name for h in CANONICAL_HINTS)
        return (0 if canon else 1, -path.stat().st_mtime)

    return sorted(candidates, key=score)


def file_info(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"exists": False}

    stat = p.stat()
    return {
        "exists": True,
        "path": str(p),
        "name": p.name,
        "dir": str(p.parent),
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime),
    }


@st.cache_data(show_spinner=False)
def _load_csv_cached(path: str, modified_ts: float) -> pd.DataFrame:
    """
    Cached CSV loader. The modified timestamp is included to automatically refresh the cache
    when the file changes.
    """
    df = pd.read_csv(path, low_memory=False)
    return df


def load_csv(path: str) -> pd.DataFrame:
    """
    Load selected CSV using caching keyed by last-modified time.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    modified_ts = p.stat().st_mtime
    return _load_csv_cached(str(p), modified_ts)
