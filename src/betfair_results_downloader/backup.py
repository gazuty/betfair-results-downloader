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

Since the orders stopped being the only thing worth keeping, a backup is
three kinds of file per destination:

* the dated order snapshots and yearly archives, as before;
* a dated *state bundle* (``pipeline_state_<date>.tar.gz``): the stores
  under ``.cache`` (settlement status, commission, racecard, venue tracks,
  catalogue caches) plus any ``paths.backup_extra_files``. Betfair stops
  answering for old markets, so these cannot all be rebuilt from the
  orders alone;
* ``backup_manifest.json``: size and SHA-256 of every file the backup
  holds, taken from the local source at copy time. It is what
  ``backup --verify`` checks a destination against, and its timestamp is
  the one thing that shows, from the cloud side, when a backup last
  arrived.

What this cannot see: whether the sync client has uploaded what was
copied into its folder. A stopped client leaves a perfectly good local
directory and nothing off the machine; only looking at the cloud side
(the manifest's timestamp there) shows that.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

# Compressed-only, deliberately narrower than downloader_core's snapshot
# pattern (duplicated rather than imported so this module does not drag in
# pandas): with user.compress_snapshots=false the dated snapshot is a full
# canonical-sized .csv rewritten every run, and copying that daily is the
# large repeated OneDrive transfer this design exists to retire.
_SNAPSHOT_NAME_RE = re.compile(r"^cleared_orders_cleaned_(\d{4}-\d{2}-\d{2})\.csv\.gz$")
_ARCHIVE_NAME_RE = re.compile(r"^cleared_orders_archive_(\d{4})\.csv\.gz$")
_BUNDLE_NAME_RE = re.compile(r"^pipeline_state_(\d{4}-\d{2}-\d{2})\.tar\.gz$")

MANIFEST_NAME = "backup_manifest.json"
CACHE_DIRNAME = ".cache"
# Where a bundle keeps the files named in paths.backup_extra_files, apart
# from the stores so a restore can tell which directory each came from.
_EXTRA_PREFIX = "extra"
_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class RetentionTiers:
    """
    How many dated copies a destination keeps: the newest ``daily`` dates,
    the newest copy of each of the latest ``weekly`` ISO weeks and of each
    of the latest ``monthly`` calendar months.

    Newest-N alone (the original rule) keeps a few days, so a fault that
    goes unnoticed for longer than that is in every copy. The weekly and
    monthly tiers are what give an older state to go back to. All three at
    zero means "never prune".
    """

    daily: int
    weekly: int = 0
    monthly: int = 0

    @property
    def prunes(self) -> bool:
        return self.daily > 0 or self.weekly > 0 or self.monthly > 0


def dates_to_keep(dates: Iterable[date], tiers: RetentionTiers) -> set[date]:
    """The dates ``tiers`` retains out of ``dates`` (all of them if it never prunes)."""
    ordered = sorted(set(dates), reverse=True)
    if not tiers.prunes:
        return set(ordered)
    newest_of_week: dict[tuple[int, int], date] = {}
    newest_of_month: dict[tuple[int, int], date] = {}
    for day in ordered:
        iso = day.isocalendar()
        newest_of_week.setdefault((iso[0], iso[1]), day)
        newest_of_month.setdefault((day.year, day.month), day)
    keep = set(ordered[: max(tiers.daily, 0)])
    keep.update(sorted(newest_of_week.values(), reverse=True)[: max(tiers.weekly, 0)])
    keep.update(sorted(newest_of_month.values(), reverse=True)[: max(tiers.monthly, 0)])
    return keep


def _dated(name: str) -> Optional[date]:
    """The date in a snapshot or bundle name; None for any other file."""
    match = _SNAPSHOT_NAME_RE.match(name) or _BUNDLE_NAME_RE.match(name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_state_bundle(
    results_csv_dir: Path,
    extra_files: Sequence[Path],
    bundle_path: Path,
) -> list[str]:
    """
    Write the state bundle for ``results_csv_dir`` to ``bundle_path``.

    Holds every regular file directly under ``.cache`` except lock files,
    and each of ``extra_files`` under ``extra/``. Returns what could not
    be included, one line each; a missing extra file is reported rather
    than fatal, because a bundle without it is still worth having and a
    silent gap would only be found at restore time.

    Raises ``OSError`` or ``tarfile.TarError`` when the bundle itself
    cannot be written.
    """
    problems: list[str] = []
    members: list[tuple[Path, str]] = []
    cache_dir = results_csv_dir / CACHE_DIRNAME
    try:
        stores = sorted(
            f for f in cache_dir.iterdir() if f.is_file() and f.suffix != ".lock"
        )
    except FileNotFoundError:
        stores = []
    except OSError as exc:
        stores = []
        problems.append(f"{CACHE_DIRNAME}: {exc}")
    members.extend((f, f"{CACHE_DIRNAME}/{f.name}") for f in stores)

    used: set[str] = set()
    for index, extra in enumerate(extra_files):
        if not extra.is_file():
            problems.append(f"extra file not found: {extra}")
            continue
        # Two extras may share a basename (two masters from two folders).
        name = extra.name if extra.name not in used else f"{index}_{extra.name}"
        used.add(name)
        members.append((extra, f"{_EXTRA_PREFIX}/{name}"))

    with tarfile.open(bundle_path, "w:gz") as tar:
        for source, arcname in members:
            try:
                tar.add(source, arcname=arcname, recursive=False)
            except OSError as exc:
                # A store replaced or removed between listing and reading.
                problems.append(f"{arcname}: {exc}")
    return problems


def read_manifest(backup_dir: Path) -> dict[str, dict[str, object]]:
    """
    The ``files`` table of a destination's manifest; empty when there is
    none or it cannot be read (a first backup, or a damaged file, which the
    next write replaces).
    """
    try:
        loaded = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = loaded.get("files") if isinstance(loaded, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        str(name): entry for name, entry in files.items() if isinstance(entry, dict)
    }


def _write_manifest(
    backup_dir: Path, files: dict[str, dict[str, object]], now_utc: datetime
) -> None:
    body = {
        "writtenUtc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": dict(sorted(files.items())),
    }
    target = backup_dir / MANIFEST_NAME
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def run_backup(
    results_csv_dir: Path,
    backup_dirs: Sequence[Path],
    *,
    tiers: RetentionTiers,
    extra_files: Sequence[Path] = (),
    status_cb: Optional[Callable[[str], None]] = None,
    now_utc: Optional[datetime] = None,
) -> Optional[str]:
    """
    Back the working set up to every directory in ``backup_dirs``.

    Builds one state bundle (dated like the snapshots, by UTC day), then
    gives each destination the snapshots, archives, bundle and a manifest.
    The destinations are independent: one that fails does not stop the
    others, and each failure is named in the returned warning.

    Returns a warning string, or None when every destination is complete.
    Never raises, for the reason :func:`backup_compressed_outputs` gives.
    """
    if not backup_dirs:
        return None
    now_utc = now_utc or datetime.now(timezone.utc)
    warnings: list[str] = []
    staging: Optional[Path] = None
    bundle: Optional[Path] = None
    try:
        try:
            # Staged beside the working set (same volume, and never in a
            # cloud folder), under a dot-name no pattern here matches.
            staging = Path(tempfile.mkdtemp(prefix=".backup-", dir=results_csv_dir))
            bundle = staging / f"pipeline_state_{now_utc:%Y-%m-%d}.tar.gz"
            missing = build_state_bundle(results_csv_dir, extra_files, bundle)
            if missing:
                warnings.append(
                    "⚠️ Backup state bundle is incomplete: " + _summarise(missing)
                )
        except Exception as exc:
            # The orders are the larger loss: still back those up, and say
            # that the stores did not go with them.
            bundle = None
            logger.warning("State bundle not built: %s: %s", type(exc).__name__, exc)
            warnings.append(
                f"⚠️ Backup state bundle not built ({type(exc).__name__}: {exc}); "
                f"only the order snapshots were backed up."
            )
        for backup_dir in backup_dirs:
            warning = backup_compressed_outputs(
                results_csv_dir,
                backup_dir,
                tiers=tiers,
                bundle=bundle,
                status_cb=status_cb,
                now_utc=now_utc,
            )
            if warning:
                warnings.append(warning)
    except Exception as exc:
        logger.warning("Backup failed unexpectedly: %s: %s", type(exc).__name__, exc)
        warnings.append(f"⚠️ Backup failed unexpectedly: {type(exc).__name__}: {exc}")
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    return " ".join(warnings) if warnings else None


def _summarise(problems: Sequence[str]) -> str:
    summary = "; ".join(problems[:3])
    if len(problems) > 3:
        summary += f" (+{len(problems) - 3} more)"
    return summary


def backup_compressed_outputs(
    results_csv_dir: Path,
    backup_dir: Path,
    *,
    retention: int = 14,
    tiers: Optional[RetentionTiers] = None,
    bundle: Optional[Path] = None,
    status_cb: Optional[Callable[[str], None]] = None,
    now_utc: Optional[datetime] = None,
) -> Optional[str]:
    """
    Best-effort wrapper around :func:`_backup_compressed_outputs_inner`.

    ``tiers`` overrides ``retention`` (which alone means "the newest N
    dates"); ``bundle`` is a state bundle already built on local disk.

    The contract is "never raises", and the specific handlers below catch
    what we anticipate (OSError). Pathological inputs can still surface
    other types -- ValueError for a NUL byte in a configured path,
    RuntimeError for a symlink loop under resolve() -- and a backup must
    never turn an already-successful run into a failure, so anything
    unanticipated is converted to a warning here.
    """
    try:
        return _backup_compressed_outputs_inner(
            results_csv_dir,
            backup_dir,
            tiers=tiers or RetentionTiers(daily=retention),
            bundle=bundle,
            status_cb=status_cb,
            now_utc=now_utc or datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.warning("Backup failed unexpectedly: %s: %s", type(exc).__name__, exc)
        return f"⚠️ Backup failed unexpectedly: {type(exc).__name__}: {exc}"


def _backup_compressed_outputs_inner(
    results_csv_dir: Path,
    backup_dir: Path,
    *,
    tiers: RetentionTiers,
    bundle: Optional[Path],
    status_cb: Optional[Callable[[str], None]],
    now_utc: datetime,
) -> Optional[str]:
    """
    Copy dated snapshots and yearly archives from ``results_csv_dir`` (and
    ``bundle``, when there is one) into ``backup_dir``, prune the dated
    files there that ``tiers`` no longer keeps, and rewrite the manifest.

    One-way: the source directory is never modified, the uncompressed
    canonical is never copied (271MB re-uploaded four times a day is what
    this design retired), and archives are never pruned. Copies preserve
    the source mtime (copy2), and a file already present with the same
    size AND mtime is skipped -- the rsync heuristic. Size alone is not
    identity: a same-day rewrite can replace rows without changing the
    byte count. Hashing would be exact but reads the backup-side file,
    which on a cloud-sync directory re-downloads it every run. For the
    same reason the manifest's checksums are taken from the local source
    as each file is copied, and carried forward for files left alone.

    Single writer per backup directory, by contract (see the README's
    concurrency note): since the working set moved to local disk there is
    no shared canonical for two machines to converge on, so this module
    does not attempt cross-machine freshness ranking -- neither mtime
    (writer wall-clock; lies under skew) nor compressed size (not
    monotonic with row count once archival trims old rows) can order two
    divergent sources correctly. A second machine gets its own
    backup_dir; the uuid temp names below stay as insurance against a
    misconfigured overlap ever corrupting a file in transit.

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
    if bundle is not None:
        candidates.append(bundle)

    manifest = read_manifest(backup_dir)
    copied = 0
    for src in candidates:
        dst = backup_dir / src.name
        # Globally unique temp name: overlapping runs sharing one backup
        # directory -- including from different machines, where pids can
        # collide -- must not truncate or rename each other's in-progress
        # copy. The final replace() is atomic either way.
        tmp = dst.with_name(f"{dst.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
        try:
            src_stat = src.stat()
            if dst.exists():
                dst_stat = dst.stat()
                if (
                    dst_stat.st_size == src_stat.st_size
                    and dst_stat.st_mtime_ns == src_stat.st_mtime_ns
                ):
                    if src.name not in manifest:
                        # Copied before manifests existed: record it now,
                        # from the source it was judged identical to.
                        manifest[src.name] = {
                            "bytes": src_stat.st_size,
                            "sha256": _sha256(src),
                        }
                    continue
            # tmp + rename: an interrupted copy must not leave a truncated
            # file under a name the retention logic would treat as good.
            # copy2 preserves the source mtime, which is what makes the
            # skip check above safe on the next run.
            checksum = _sha256(src)
            shutil.copy2(src, tmp)
            tmp.replace(dst)
            copied += 1
            manifest[src.name] = {"bytes": src_stat.st_size, "sha256": checksum}
        except OSError as exc:
            problems.append(f"{src.name}: {exc}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    if tiers.prunes:
        try:
            dated = {f: _dated(f.name) for f in backup_dir.iterdir()}
        except OSError as exc:
            dated = {}
            problems.append(f"prune listing failed: {exc}")
        # Snapshots and bundles are thinned separately: a day that has one
        # but not the other must not cost the other kind a slot.
        for pattern in (_SNAPSHOT_NAME_RE, _BUNDLE_NAME_RE):
            mine = {f: d for f, d in dated.items() if d and pattern.match(f.name)}
            keep = dates_to_keep(mine.values(), tiers)
            for f, day in mine.items():
                if day in keep:
                    continue
                try:
                    f.unlink()
                except OSError as exc:
                    problems.append(f"prune {f.name}: {exc}")

    # The manifest describes what the destination holds now: entries for
    # pruned files go, and a file with no recorded checksum (copied before
    # manifests existed) is simply absent from it.
    try:
        present = {f.name for f in backup_dir.iterdir()}
        _write_manifest(
            backup_dir,
            {name: entry for name, entry in manifest.items() if name in present},
            now_utc,
        )
    except OSError as exc:
        problems.append(f"manifest: {exc}")

    if copied and status_cb:
        status_cb(f"Backup: copied {copied} file(s) to {backup_dir}.")
    if problems:
        return f"⚠️ Backup to {backup_dir} incomplete: {_summarise(problems)}"
    return None


@dataclass(frozen=True)
class VerifyResult:
    """What ``verify_backup`` found in one destination."""

    checked: int
    problems: list[str]
    written_utc: Optional[str]

    @property
    def ok(self) -> bool:
        return self.checked > 0 and not self.problems


def verify_backup(backup_dir: Path) -> VerifyResult:
    """
    Read a destination back and check it is restorable.

    Every file the manifest lists must be present with the recorded size
    and SHA-256, and must decompress to the end. A backup file the
    manifest does not list is decompressed too and reported as unrecorded.
    This reads every byte, which on a cloud folder downloads it: it is the
    occasional check a person asks for, never part of a run.
    """
    problems: list[str] = []
    written: Optional[str] = None
    try:
        loaded = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("writtenUtc"), str):
            written = loaded["writtenUtc"]
    except (OSError, ValueError) as exc:
        problems.append(f"{MANIFEST_NAME}: unreadable ({type(exc).__name__}: {exc})")
    manifest = read_manifest(backup_dir)

    try:
        on_disk = sorted(
            f.name
            for f in backup_dir.iterdir()
            if _SNAPSHOT_NAME_RE.match(f.name)
            or _ARCHIVE_NAME_RE.match(f.name)
            or _BUNDLE_NAME_RE.match(f.name)
        )
    except OSError as exc:
        return VerifyResult(0, [f"cannot list {backup_dir}: {exc}"], written)

    checked = 0
    for name in sorted(set(on_disk) | set(manifest)):
        path = backup_dir / name
        entry = manifest.get(name)
        if name not in on_disk:
            problems.append(f"{name}: listed in the manifest but missing")
            continue
        checked += 1
        try:
            if entry is None:
                problems.append(f"{name}: no checksum recorded for it")
            else:
                size = path.stat().st_size
                if size != entry.get("bytes"):
                    problems.append(
                        f"{name}: {size} bytes, manifest says {entry.get('bytes')}"
                    )
                    continue
                if _sha256(path) != entry.get("sha256"):
                    problems.append(f"{name}: checksum differs from the manifest")
                    continue
            with gzip.open(path, "rb") as fh:
                while fh.read(_HASH_CHUNK):
                    pass
        except (OSError, EOFError, ValueError) as exc:
            # gzip raises EOFError for a truncated file, BadGzipFile
            # (an OSError) for one that is not gzip at all.
            problems.append(f"{name}: unreadable ({type(exc).__name__}: {exc})")
    return VerifyResult(checked, problems, written)
