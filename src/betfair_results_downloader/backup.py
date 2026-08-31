"""
One-way backup of compressed outputs to a secondary directory.

H4 moved the working set (canonical, cache, snapshots, archives) off
OneDrive onto local disk: Files On-Demand eviction twice broke reads of
the very file the pipeline was rewriting (EDEADLK, 2026-08-30/31).
OneDrive's job is now disaster recovery only: after each successful CSV
write it receives copies of the compressed snapshot and yearly archives,
and nothing in the pipeline ever reads from it again.

Deliberately best-effort: a cloud sync directory that is offline, full,
or mid-eviction must never fail the run that just succeeded locally.
Problems come back as a single warning string for the run report --
prefixed "⚠️" so the scheduled-run success path posts it to Slack.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Kept in sync with downloader_core's snapshot naming; duplicated here so
# importing the backup module does not drag in pandas.
_SNAPSHOT_NAME_RE = re.compile(
    r"^cleared_orders_cleaned_(\d{4}-\d{2}-\d{2})\.csv(\.gz)?$"
)
_ARCHIVE_NAME_RE = re.compile(r"^cleared_orders_archive_(\d{4})\.csv\.gz$")


def backup_compressed_outputs(
    results_csv_dir: Path,
    backup_dir: Path,
    *,
    retention: int = 14,
    status_cb: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """
    Copy dated snapshots and yearly archives from ``results_csv_dir`` into
    ``backup_dir``, then prune dated snapshots there beyond ``retention``.

    One-way: the source directory is never modified, the uncompressed
    canonical is never copied (271MB re-uploaded four times a day is what
    this design retired), and archives are never pruned. A file already
    present with the same size is skipped -- snapshots and archives only
    grow, so an equal size means an identical earlier copy.

    Returns a warning string describing any problems, or None when clean.
    Never raises.
    """
    problems: list[str] = []

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"⚠️ Backup skipped: cannot create {backup_dir}: {exc}"

    try:
        candidates = sorted(
            f
            for f in results_csv_dir.iterdir()
            if _SNAPSHOT_NAME_RE.match(f.name) or _ARCHIVE_NAME_RE.match(f.name)
        )
    except OSError as exc:
        return f"⚠️ Backup skipped: cannot list {results_csv_dir}: {exc}"

    copied = 0
    for src in candidates:
        dst = backup_dir / src.name
        tmp = dst.with_name(dst.name + ".tmp")
        try:
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue
            # tmp + rename: an interrupted copy must not leave a truncated
            # file under a name the retention logic would treat as good.
            shutil.copyfile(src, tmp)
            tmp.replace(dst)
            copied += 1
        except OSError as exc:
            problems.append(f"{src.name}: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    if retention > 0:
        try:
            stale = sorted(
                (f for f in backup_dir.iterdir() if _SNAPSHOT_NAME_RE.match(f.name)),
                key=lambda f: f.name,
                reverse=True,
            )[retention:]
        except OSError as exc:
            stale = []
            problems.append(f"prune listing failed: {exc}")
        for f in stale:
            try:
                f.unlink()
            except OSError as exc:
                problems.append(f"prune {f.name}: {exc}")

    if copied and status_cb:
        status_cb(f"Backup: copied {copied} file(s) to {backup_dir}.")
    if problems:
        summary = "; ".join(problems[:3])
        if len(problems) > 3:
            summary += f" (+{len(problems) - 3} more)"
        return f"⚠️ Backup to {backup_dir} incomplete: {summary}"
    return None
