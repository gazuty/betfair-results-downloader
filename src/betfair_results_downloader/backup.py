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
* ``backup_manifest.json``: what the last backup failed to deliver, and
  the size and SHA-256 of every file the backup holds, taken from the local source at copy time. It is the one file a
  run reads back from a backup directory (a few kilobytes). It is what
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
import zlib
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
# In the bundle beside the extras: which path each one was copied from.
EXTRA_INDEX_NAME = "INDEX.json"
# A staging directory this old belongs to a backup that was killed.
_STALE_STAGING_SECONDS = 3600
_STAGING_PREFIX = ".backup-"
# Beside the stores but not stores: lock files, and the "<name>.tmp" or
# "<name>.<pid>.tmp" a writer fills before renaming it over the store. A
# half-written one must not be archived, nor its rename read as a loss.
_NOT_A_STORE_SUFFIXES = frozenset({".lock", ".tmp"})
_STAGING_MARKER = ".betfair-backup-staging"
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


def _entry(source: Path, source_stat: os.stat_result) -> dict[str, object]:
    """
    A manifest entry for ``source``. The mtime is what ties the entry to
    one version of the file: a same-day rewrite keeps the name and can
    keep the size.
    """
    return {
        "bytes": source_stat.st_size,
        "mtimeNs": source_stat.st_mtime_ns,
        "sha256": _sha256(source),
    }


def _entry_matches(entry: Optional[dict[str, object]], stat: os.stat_result) -> bool:
    """Whether ``entry`` describes the version of the file ``stat`` is of."""
    return (
        entry is not None
        and entry.get("bytes") == stat.st_size
        and entry.get("mtimeNs") == stat.st_mtime_ns
    )


def _is_short(manifest: Optional[dict[str, dict[str, object]]], name: str) -> bool:
    """
    Whether the manifest records ``name`` as a bundle short of members.
    With no manifest to go by, a file counts as whole: the cautious side,
    since whole files are the ones kept.
    """
    entry = (manifest or {}).get(name)
    return bool(entry and entry.get("incomplete"))


def _remove_stale_staging(results_csv_dir: Path, now_epoch: float) -> None:
    """
    Remove staging directories a killed backup left behind. Only ones old
    enough that no live backup can own them, and only ones holding the
    marker a backup writes on creating its staging: the name alone could
    be anyone's directory, even a backup target placed under the results
    directory. Best-effort, since a leftover costs disk and nothing else.
    """
    try:
        leftovers = [
            d
            for d in results_csv_dir.glob(f"{_STAGING_PREFIX}*")
            if d.is_dir()
            and not d.is_symlink()
            and (d / _STAGING_MARKER).is_file()
            and now_epoch - d.stat().st_mtime > _STALE_STAGING_SECONDS
        ]
    except OSError:
        return
    for leftover in leftovers:
        shutil.rmtree(leftover, ignore_errors=True)


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
    and each of ``extra_files`` under ``extra/`` with an ``INDEX.json``
    naming the path each came from. Returns what could not
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
            f
            for f in cache_dir.iterdir()
            if f.is_file() and f.suffix not in _NOT_A_STORE_SUFFIXES
        )
    except OSError as exc:
        # Including a directory that is not there: a bundle with no stores
        # in it must not pass for a whole one.
        stores = []
        problems.append(f"{CACHE_DIRNAME}: {type(exc).__name__}: {exc}")
    members.extend((f, f"{CACHE_DIRNAME}/{f.name}") for f in stores)

    origins: dict[str, str] = {}
    for index, extra in enumerate(extra_files):
        if not extra.is_file():
            problems.append(f"extra file not found: {extra}")
            continue
        # Numbered, so two extras sharing a basename (two masters from two
        # folders) both fit; the index below says where each came from.
        arcname = f"{_EXTRA_PREFIX}/{index}_{extra.name}"
        origins[arcname] = str(extra)
        members.append((extra, arcname))

    # Each file is copied to local staging first and the copies are what
    # the archive reads. tarfile sizes a member from one stat and then
    # reads the path again: a store replaced in between (the hourly
    # racecard job writes outside the pipeline lock) would leave a short
    # member and misalign every one after it. A plain copy reads whichever
    # whole file it opened.
    with tempfile.TemporaryDirectory(
        prefix="members-", dir=bundle_path.parent
    ) as staged_dir:
        staged: list[tuple[Path, str]] = []
        for number, (source, arcname) in enumerate(members):
            copy = Path(staged_dir) / f"{number}"
            try:
                shutil.copy2(source, copy)
            except OSError as exc:
                # Removed between listing and reading, or unreadable.
                problems.append(f"{arcname}: {exc}")
                origins.pop(arcname, None)
                continue
            staged.append((copy, arcname))
        if origins:
            index_file = Path(staged_dir) / "index"
            index_file.write_text(
                json.dumps(origins, indent=2) + "\n", encoding="utf-8"
            )
            staged.append((index_file, f"{_EXTRA_PREFIX}/{EXTRA_INDEX_NAME}"))
        # Any failure from here is the archive itself going wrong, and a
        # half-written archive must not be mistaken for a bundle: raise.
        with tarfile.open(bundle_path, "w:gz") as tar:
            for copy, arcname in staged:
                tar.add(copy, arcname=arcname, recursive=False)
    return problems


class ManifestUnreadable(OSError):
    """A manifest exists at the destination but could not be read."""


def _load_manifest(backup_dir: Path) -> tuple[dict[str, dict[str, object]], bool]:
    """
    A destination's ``files`` table and whether it was damaged.

    No manifest is a first backup: an empty table, not damaged. One that
    is there but cannot be read raises :class:`ManifestUnreadable`,
    because rewriting it from this run's files alone would throw away the
    checksums of every older copy the destination holds. One that reads
    but is not a manifest is damaged: nothing in it can be trusted, so the
    table starts again and the caller says so.
    """
    path = backup_dir / MANIFEST_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, False
    except OSError as exc:
        raise ManifestUnreadable(f"{type(exc).__name__}: {exc}") from exc
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}, True
    files = loaded.get("files") if isinstance(loaded, dict) else None
    if not isinstance(files, dict):
        return {}, True
    return (
        {str(name): entry for name, entry in files.items() if isinstance(entry, dict)},
        False,
    )


def read_manifest(backup_dir: Path) -> dict[str, dict[str, object]]:
    """
    The ``files`` table of a destination's manifest, for reporting; empty
    when there is none or it cannot be read. A backup uses
    :func:`_load_manifest`, which tells those cases apart.
    """
    try:
        return _load_manifest(backup_dir)[0]
    except ManifestUnreadable:
        return {}


def _write_manifest(
    backup_dir: Path,
    files: dict[str, dict[str, object]],
    now_utc: datetime,
    undelivered: Sequence[str] = (),
) -> None:
    """
    ``undelivered`` names what this backup should have put here and did
    not. Without it a manifest written after a failed copy would list only
    what arrived, and a later verify would pass a backup with a hole in it
    under a fresh timestamp.
    """
    body = {
        "writtenUtc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "undelivered": sorted(undelivered),
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
    undelivered: list[str] = []
    bundle_missing: list[str] = []
    try:
        try:
            # Staged beside the working set (same volume, and never in a
            # cloud folder), under a dot-name no pattern here matches.
            _remove_stale_staging(results_csv_dir, now_utc.timestamp())
            staging = Path(
                tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=results_csv_dir)
            )
            (staging / _STAGING_MARKER).write_text("", encoding="utf-8")
            bundle = staging / f"pipeline_state_{now_utc:%Y-%m-%d}.tar.gz"
            missing = build_state_bundle(results_csv_dir, extra_files, bundle)
            if missing:
                # Each target's manifest records this against the bundle:
                # what arrives is a sound archive, so only that record
                # says it is short of members.
                bundle_missing = missing
                warnings.append(
                    "⚠️ Backup state bundle is incomplete: " + _summarise(missing)
                )
        except Exception as exc:
            # The orders are the larger loss: still back those up, and say
            # that the stores did not go with them.
            bundle = None
            undelivered = [f"pipeline_state_{now_utc:%Y-%m-%d}.tar.gz"]
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
                undelivered=undelivered,
                bundle_missing=bundle_missing,
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
    undelivered: Sequence[str] = (),
    bundle_missing: Sequence[str] = (),
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
            undelivered=undelivered,
            bundle_missing=bundle_missing,
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
    undelivered: Sequence[str],
    bundle_missing: Sequence[str],
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

    # The one read a run makes of a backup directory: the manifest is a
    # few kilobytes, and without it the checksums of copies that no longer
    # exist locally could not be carried forward.
    manifest: Optional[dict[str, dict[str, object]]]
    damaged = False
    try:
        manifest, damaged = _load_manifest(backup_dir)
        if damaged:
            problems.append(
                f"{MANIFEST_NAME} was damaged and has been started again; "
                f"older copies have no recorded checksum"
            )
    except ManifestUnreadable as exc:
        manifest = None
        problems.append(
            f"{MANIFEST_NAME} could not be read ({exc}), so it was left as it "
            f"is; this run's checksums are not recorded"
        )
    copied = 0
    failed = list(undelivered)
    for src in candidates:
        dst = backup_dir / src.name
        # Globally unique temp name: overlapping runs sharing one backup
        # directory -- including from different machines, where pids can
        # collide -- must not truncate or rename each other's in-progress
        # copy. The final replace() is atomic either way.
        tmp = dst.with_name(f"{dst.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
        short = src is bundle and bool(bundle_missing)
        try:
            src_stat = src.stat()
            if short and manifest is None:
                # Nowhere to record that it is short, and unrecorded it
                # would pass for whole from the next run on.
                problems.append(
                    f"{src.name}: not copied, it is short of "
                    f"{len(bundle_missing)} member(s) and the manifest "
                    f"cannot record that"
                )
                continue
            if short and dst.exists() and not _is_short(manifest, src.name):
                # An earlier run today delivered a whole bundle under this
                # name. One short of members must not replace it: a few
                # hours staler is better than missing a store.
                problems.append(
                    f"{src.name}: kept the earlier complete copy, this "
                    f"run's is short of {len(bundle_missing)} member(s)"
                )
                failed.append(f"{src.name} (a copy with every member)")
                continue
            if dst.exists():
                dst_stat = dst.stat()
                if (
                    dst_stat.st_size == src_stat.st_size
                    and dst_stat.st_mtime_ns == src_stat.st_mtime_ns
                ):
                    if manifest is not None and not _entry_matches(
                        manifest.get(src.name), src_stat
                    ):
                        # No entry (copied before manifests existed), or
                        # one for an earlier version of the file (its
                        # manifest write failed): take it from the source
                        # this copy was judged identical to.
                        manifest[src.name] = _entry(src, src_stat)
                    continue
            # tmp + rename: an interrupted copy must not leave a truncated
            # file under a name the retention logic would treat as good.
            # copy2 preserves the source mtime, which is what makes the
            # skip check above safe on the next run.
            entry = _entry(src, src_stat)
            shutil.copy2(src, tmp)
            tmp.replace(dst)
            copied += 1
            if short:
                entry["incomplete"] = list(bundle_missing)
            if manifest is not None:
                manifest[src.name] = entry
        except OSError as exc:
            problems.append(f"{src.name}: {exc}")
            failed.append(src.name)
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
        patterns = [_SNAPSHOT_NAME_RE]
        # Bundles are thinned only by a run that has just put a whole one
        # here and can trust the record of which older ones are short. So
        # the newest bundle after a prune is always whole, and a short one
        # is never ranked as whole on the strength of a manifest that was
        # unreadable or had to be started again.
        delivered_whole = (
            bundle is not None
            and not bundle_missing
            and bundle.name not in failed
            and (backup_dir / bundle.name).is_file()
        )
        if manifest is None or damaged:
            problems.append("bundles not pruned without a trustworthy manifest")
        elif delivered_whole:
            patterns.append(_BUNDLE_NAME_RE)
        for pattern in patterns:
            mine = {f: d for f, d in dated.items() if d and pattern.match(f.name)}
            # A bundle short of members takes no retention slot: ranked
            # with the rest, today's short one could push out the last
            # whole one. It goes once a newer whole bundle exists. A
            # bundle the manifest does not list (its record lost with a
            # damaged manifest) may be either, so it is neither ranked
            # nor removed; verify reports it until someone decides.
            unknown = (
                {f for f in mine if f.name not in (manifest or {})}
                if pattern is _BUNDLE_NAME_RE
                else set()
            )
            whole = {
                f: d
                for f, d in mine.items()
                if f not in unknown and not _is_short(manifest, f.name)
            }
            keep = dates_to_keep(whole.values(), tiers)
            newest_whole = max(whole.values(), default=None)
            for f, day in mine.items():
                if f in unknown:
                    continue
                if f in whole:
                    if day in keep:
                        continue
                elif newest_whole is None or day >= newest_whole:
                    continue
                try:
                    f.unlink()
                except OSError as exc:
                    problems.append(f"prune {f.name}: {exc}")

    # The manifest describes what the destination holds now: entries for
    # pruned files go, and a file with no recorded checksum (copied before
    # manifests existed) is simply absent from it.
    if manifest is not None:
        try:
            present = {f.name for f in backup_dir.iterdir()}
            _write_manifest(
                backup_dir,
                {name: entry for name, entry in manifest.items() if name in present},
                now_utc,
                failed,
            )
        except OSError as exc:
            problems.append(f"manifest not written: {exc}")

    if copied and status_cb:
        status_cb(f"Backup: copied {copied} file(s) to {backup_dir}.")
    if problems:
        return f"⚠️ Backup to {backup_dir} incomplete: {_summarise(problems)}"
    return None


def _read_to_end(path: Path) -> None:
    """
    Read a backup file as a restore would: a bundle member by member, any
    other file as one gzip stream. Raises on the first thing unreadable.
    A bundle that merely gunzips can still be a broken archive.
    """
    if _BUNDLE_NAME_RE.match(path.name):
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                while handle.read(_HASH_CHUNK):
                    pass
        return
    with gzip.open(path, "rb") as fh:
        while fh.read(_HASH_CHUNK):
            pass


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

    Anything the last backup failed to deliver is a problem, as is any
    kept bundle recorded as short of members, and every
    file the manifest lists must be present with the recorded size
    and SHA-256, and must read to the end (a bundle member by member). A
    backup file the manifest does not list is read too and reported as
    unrecorded. Run it while no backup is writing: it takes no lock, and
    a file replaced part-way through the check reads as a mismatch.
    This reads every byte, which on a cloud folder downloads it: it is the
    occasional check a person asks for, never part of a run.
    """
    problems: list[str] = []
    written: Optional[str] = None
    try:
        loaded = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("writtenUtc"), str):
            written = loaded["writtenUtc"]
        missed = loaded.get("undelivered") if isinstance(loaded, dict) else None
        if isinstance(missed, list):
            problems.extend(
                f"{name}: the last backup did not deliver it" for name in missed
            )
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
                short_of = entry.get("incomplete")
                if short_of:
                    # A sound archive that never held everything: it
                    # stays a problem for as long as it is kept, whatever
                    # later backups delivered.
                    members = "; ".join(str(item) for item in short_of)
                    problems.append(f"{name}: short of member(s): {members}")
                size = path.stat().st_size
                if size != entry.get("bytes"):
                    problems.append(
                        f"{name}: {size} bytes, manifest says {entry.get('bytes')}"
                    )
                    continue
                if _sha256(path) != entry.get("sha256"):
                    problems.append(f"{name}: checksum differs from the manifest")
                    continue
            _read_to_end(path)
        except (OSError, EOFError, ValueError, tarfile.TarError, zlib.error) as exc:
            # gzip raises EOFError for a truncated file, BadGzipFile
            # (an OSError) for one that is not gzip at all, and a bare
            # zlib.error for a damaged deflate stream inside a good header.
            problems.append(f"{name}: unreadable ({type(exc).__name__}: {exc})")
    return VerifyResult(checked, problems, written)
