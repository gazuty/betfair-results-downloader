from __future__ import annotations

from pathlib import Path
from typing import List

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


def load_csv(path: str) -> pd.DataFrame:
    """
    Load a cleared-orders CSV for the CLI/reporting paths.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    return pd.read_csv(str(p), low_memory=False)
