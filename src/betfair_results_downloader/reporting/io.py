from __future__ import annotations

from pathlib import Path
from typing import Callable, List
from datetime import datetime

import pandas as pd


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


def _read_csv_uncached(path: str) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False)


def load_csv(path: str) -> pd.DataFrame:
    """
    Load a CSV without any UI-framework coupling.

    This is the safe loader for CLI, tests, and non-Streamlit runtime paths.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    return _read_csv_uncached(str(p))


def build_cached_csv_loader(cache_decorator: Callable[..., Callable[[Callable[..., pd.DataFrame]], Callable[..., pd.DataFrame]]]):
    """
    Build a cached CSV loader using a caller-provided cache decorator.

    This keeps Streamlit-specific caching out of core CLI/reporting paths while
    still allowing the dashboard to opt into caching explicitly.
    """
    @cache_decorator(show_spinner=False)
    def _load_csv_cached(path: str, modified_ts: float) -> pd.DataFrame:
        return _read_csv_uncached(path)

    def _loader(path: str) -> pd.DataFrame:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"CSV not found: {path}")
        return _load_csv_cached(str(p), p.stat().st_mtime)

    return _loader
