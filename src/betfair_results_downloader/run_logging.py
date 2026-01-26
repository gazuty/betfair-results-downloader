from __future__ import annotations

from pathlib import Path
from typing import Iterable


def normalize_log_line(line: str) -> str:
    return line.replace("…", "...").replace("â€¦", "...")


def write_run_log(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = [normalize_log_line(line) for line in lines]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for line in normalized:
            handle.write(f"{line}\n")
