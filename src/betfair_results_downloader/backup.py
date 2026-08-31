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

# Compressed-only, deliberately narrower than downloader_core's snapshot
# pattern (duplicated rather than imported so this module does not drag in
# pandas): with user.compress_snapshots=false the dated snapshot is a full
# canonical-sized .csv rewritten every run, and copying that daily is the
# large repeated OneDrive transfer this design exists to retire.
_SNAPSHOT_NAME_RE = re.compile(r"^cleared_orders_cleaned_(\d{4}-\d{2}-\d{2})\.csv\.gz$")
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
    this design retired), and archives are never pruned. Copies preserve
    the source mtime (copy2), and a file already present with the same
    size AND mtime is skipped -- the rsync heuristic. Size alone is not
    identity: a same-day rewrite can replace rows without changing the
    byte count. Hashing would be exact but reads the backup-side file,
    which on a cloud-sync directory re-downloads it every run.

    Returns a warning string describing any problems, or None when clean.
    Never raises.
    """
    problems: list[str] = []

    # A backup_dir aliasing the working directory would silently "succeed"
    # with no second copy in existence -- and worse, the prune step below
    # would then delete real working-set snapshots. Refuse up front.
    try:
        if backup_dir.resolve() == results_csv_dir.resolve():
            return (
                f"⚠️ Backup skipped: backup_dir is the working directory "
                f"itself ({backup_dir}); no disaster-recovery copy exists."
            )
    except OSError:
        pass

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
            src_stat = src.stat()
            if dst.exists():
                dst_stat = dst.stat()
                if (
                    dst_stat.st_size == src_stat.st_size
                    and dst_stat.st_mtime_ns == src_stat.st_mtime_ns
                ):
                    continue
            # tmp + rename: an interrupted copy must not leave a truncated
            # file under a name the retention logic would treat as good.
            # copy2 preserves the source mtime, which is what makes the
            # skip check above safe on the next run.
            shutil.copy2(src, tmp)
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
