"""
The parts of the backup added when the orders stopped being the only
thing worth keeping: several destinations, the state bundle, tiered
retention, the manifest and the read-back check.

Invented data throughout; the shapes (gzip snapshots dated by UTC day,
stores under ``.cache``, a lock file beside them) are the real ones.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import tarfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from betfair_results_downloader import backup as backup_mod
from betfair_results_downloader.__main__ import main
from betfair_results_downloader.backup import (
    MANIFEST_NAME,
    RetentionTiers,
    build_state_bundle,
    dates_to_keep,
    read_manifest,
    run_backup,
    verify_backup,
)
from betfair_results_downloader.paths import (
    resolve_backup_dirs,
    resolve_backup_extra_files,
    resolve_backup_retention,
)
from betfair_results_downloader.scheduler import runner
from betfair_results_downloader.secrets import validate_credentials

NOW = datetime(2026, 3, 10, 22, 0, tzinfo=timezone.utc)
KEEP_ALL = RetentionTiers(daily=0)


def _snapshot(directory: Path, day: str, body: str = "betId\n1\n") -> Path:
    path = directory / f"cleared_orders_cleaned_{day}.csv.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(body.encode())
    return path


def _working_set(tmp_path: Path) -> Path:
    src = tmp_path / "results"
    (src / ".cache").mkdir(parents=True)
    (src / "cleared_orders_cleaned.csv").write_text("betId\n1\n")
    _snapshot(src, "2026-03-09")
    _snapshot(src, "2026-03-10")
    (src / ".cache" / "market_commission.csv").write_text(
        "marketId,commission\n1.2,0.5\n"
    )
    (src / ".cache" / "venue_tracks.csv").write_text("eventTypeId,venue,track\n")
    (src / ".cache" / "racecard.csv.lock").write_text("")
    return src


def _bundle_members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


# --- retention tiers --------------------------------------------------------


def _days(start: date, count: int) -> list[date]:
    return [start - timedelta(days=i) for i in range(count)]


def test_daily_tier_alone_is_the_newest_n() -> None:
    days = _days(date(2026, 3, 10), 30)
    assert dates_to_keep(days, RetentionTiers(daily=5)) == set(days[:5])


def test_weekly_tier_keeps_the_last_day_of_each_recent_week() -> None:
    # Tuesday 10 March 2026 back 30 days; ISO weeks end on Sunday.
    kept = dates_to_keep(_days(date(2026, 3, 10), 30), RetentionTiers(0, weekly=3))
    assert kept == {date(2026, 3, 10), date(2026, 3, 8), date(2026, 3, 1)}


def test_monthly_tier_keeps_the_last_day_of_each_recent_month() -> None:
    kept = dates_to_keep(_days(date(2026, 3, 10), 70), RetentionTiers(0, monthly=3))
    assert kept == {date(2026, 3, 10), date(2026, 2, 28), date(2026, 1, 31)}


def test_tiers_combine_without_double_counting() -> None:
    kept = dates_to_keep(
        _days(date(2026, 3, 10), 70), RetentionTiers(daily=2, weekly=2, monthly=2)
    )
    assert kept == {
        date(2026, 3, 10),
        date(2026, 3, 9),
        date(2026, 3, 8),
        date(2026, 2, 28),
    }


def test_all_tiers_zero_never_prunes() -> None:
    days = _days(date(2026, 3, 10), 40)
    assert dates_to_keep(days, KEEP_ALL) == set(days)


def test_a_week_with_gaps_keeps_its_newest_existing_day() -> None:
    """No Sunday copy that week: the week's slot goes to the Friday."""
    days = [date(2026, 3, 10), date(2026, 3, 6), date(2026, 3, 3)]
    assert dates_to_keep(days, RetentionTiers(0, weekly=2)) == {
        date(2026, 3, 10),
        date(2026, 3, 6),
    }


def test_destination_is_thinned_by_tier_and_archives_stay(tmp_path) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    dst.mkdir()
    for day in _days(date(2026, 3, 8), 40):
        _snapshot(dst, day.isoformat())
        (dst / f"pipeline_state_{day.isoformat()}.tar.gz").write_bytes(b"x")
    with gzip.open(dst / "cleared_orders_archive_2024.csv.gz", "wb") as fh:
        fh.write(b"betId\nold\n")
    # The bundles are on record as whole; the snapshots predate manifests.
    recorded = {
        f.name: {"bytes": 1, "mtimeNs": 0, "sha256": "0"}
        for f in dst.glob("pipeline_state_*.tar.gz")
    }
    (dst / MANIFEST_NAME).write_text(json.dumps({"files": recorded}))

    warning = run_backup(
        src, [dst], tiers=RetentionTiers(daily=3, weekly=2, monthly=2), now_utc=NOW
    )

    assert warning is None
    snapshots = sorted(
        f.name[len("cleared_orders_cleaned_") : -len(".csv.gz")]
        for f in dst.glob("cleared_orders_cleaned_*.csv.gz")
    )
    # Newest three days; the Sundays of the two latest weeks (10 March is
    # this week's newest, 8 March last week's); the last day held of the
    # two latest months.
    assert snapshots == ["2026-02-28", "2026-03-08", "2026-03-09", "2026-03-10"]
    bundles = sorted(f.name for f in dst.glob("pipeline_state_*.tar.gz"))
    # Bundles are thinned on their own dates: there is none for 9 March.
    assert bundles == [
        "pipeline_state_2026-02-28.tar.gz",
        "pipeline_state_2026-03-07.tar.gz",
        "pipeline_state_2026-03-08.tar.gz",
        "pipeline_state_2026-03-10.tar.gz",
    ]
    assert (dst / "cleared_orders_archive_2024.csv.gz").exists()


# --- state bundle -----------------------------------------------------------


def test_bundle_holds_the_stores_and_the_extra_files(tmp_path) -> None:
    src = _working_set(tmp_path)
    master = tmp_path / "dash" / "Master.csv"
    master.parent.mkdir()
    master.write_text("Market,Profit\n,\n")  # an empty-string row, as real CSVs have

    problems = build_state_bundle(src, [master], tmp_path / "b.tar.gz")

    assert problems == []
    members = _bundle_members(tmp_path / "b.tar.gz")
    assert sorted(members) == [
        ".cache/market_commission.csv",
        ".cache/venue_tracks.csv",
        "extra/0_Master.csv",
        "extra/INDEX.json",
    ]
    assert members["extra/0_Master.csv"] == master.read_bytes()
    # A restore needs to know where each extra goes back to.
    assert json.loads(members["extra/INDEX.json"]) == {
        "extra/0_Master.csv": str(master)
    }
    assert members[".cache/market_commission.csv"] == b"marketId,commission\n1.2,0.5\n"


def test_writers_temporary_files_are_not_bundled(tmp_path) -> None:
    """
    The hourly racecard job, outside the pipeline lock, fills
    "racecard.csv.<pid>.tmp" and renames it over the store. That file is
    not a store: half-written it must not be archived, and its rename
    must not read as a store that vanished.
    """
    src = _working_set(tmp_path)
    (src / ".cache" / "racecard.csv.4242.tmp").write_text("marketId\n1.")
    (src / ".cache" / "market_commission.csv.tmp").write_text("half")

    problems = build_state_bundle(src, [], tmp_path / "b.tar.gz")

    assert problems == []
    assert sorted(_bundle_members(tmp_path / "b.tar.gz")) == [
        ".cache/market_commission.csv",
        ".cache/venue_tracks.csv",
    ]


def test_missing_extra_file_is_reported_not_fatal(tmp_path) -> None:
    src = _working_set(tmp_path)
    gone = tmp_path / "nowhere" / "Master.csv"

    problems = build_state_bundle(src, [gone], tmp_path / "b.tar.gz")

    assert problems == [f"extra file not found: {gone}"]
    assert ".cache/venue_tracks.csv" in _bundle_members(tmp_path / "b.tar.gz")


def test_extras_sharing_a_name_are_both_kept(tmp_path) -> None:
    src = _working_set(tmp_path)
    one, two = tmp_path / "a" / "Master.csv", tmp_path / "b" / "Master.csv"
    for path, body in ((one, "first"), (two, "second")):
        path.parent.mkdir()
        path.write_text(body)

    assert build_state_bundle(src, [one, two], tmp_path / "b.tar.gz") == []
    members = _bundle_members(tmp_path / "b.tar.gz")
    assert members["extra/0_Master.csv"] == b"first"
    assert members["extra/1_Master.csv"] == b"second"
    assert json.loads(members["extra/INDEX.json"]) == {
        "extra/0_Master.csv": str(one),
        "extra/1_Master.csv": str(two),
    }


def test_missing_cache_directory_is_a_problem_not_an_empty_bundle(tmp_path) -> None:
    """A bundle with no stores in it must not pass for a whole one."""
    src = tmp_path / "results"
    src.mkdir()
    extra = tmp_path / "notes.csv"
    extra.write_text("a\n")

    problems = build_state_bundle(src, [extra], tmp_path / "b.tar.gz")

    assert len(problems) == 1 and problems[0].startswith(".cache: FileNotFoundError")
    assert sorted(_bundle_members(tmp_path / "b.tar.gz")) == [
        "extra/0_notes.csv",
        "extra/INDEX.json",
    ]


def test_bundle_without_stores_cannot_displace_one_that_has_them(tmp_path) -> None:
    import shutil

    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    tiers = RetentionTiers(daily=1)
    assert run_backup(src, [dst], tiers=tiers, now_utc=NOW - timedelta(days=1)) is None
    shutil.rmtree(src / ".cache")

    warning = run_backup(src, [dst], tiers=tiers, now_utc=NOW)

    assert warning is not None and ".cache" in warning
    assert (dst / "pipeline_state_2026-03-09.tar.gz").exists()
    assert not verify_backup(dst).ok


# --- run_backup: destinations, manifest -------------------------------------


def test_every_destination_gets_snapshots_bundle_and_manifest(tmp_path) -> None:
    src = _working_set(tmp_path)
    first, second = tmp_path / "one", tmp_path / "two"

    warning = run_backup(src, [first, second], tiers=KEEP_ALL, now_utc=NOW)

    assert warning is None
    for dst in (first, second):
        assert sorted(f.name for f in dst.iterdir()) == [
            MANIFEST_NAME,
            "cleared_orders_cleaned_2026-03-09.csv.gz",
            "cleared_orders_cleaned_2026-03-10.csv.gz",
            "pipeline_state_2026-03-10.tar.gz",
        ]
    # Nothing left behind beside the working set, and it is untouched.
    assert sorted(f.name for f in src.iterdir()) == [
        ".cache",
        "cleared_orders_cleaned.csv",
        "cleared_orders_cleaned_2026-03-09.csv.gz",
        "cleared_orders_cleaned_2026-03-10.csv.gz",
    ]


def test_manifest_records_size_and_checksum_of_what_was_copied(tmp_path) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"

    run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)

    body = json.loads((dst / MANIFEST_NAME).read_text())
    assert body["writtenUtc"] == "2026-03-10T22:00:00Z"
    for name, entry in body["files"].items():
        data = (dst / name).read_bytes()
        expected = {
            "bytes": len(data),
            "mtimeNs": (dst / name).stat().st_mtime_ns,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if name.startswith("pipeline_state_"):
            expected["stores"] = ["market_commission.csv", "venue_tracks.csv"]
        assert entry == expected, name
    assert body["undelivered"] == []
    assert sorted(body["files"]) == [
        "cleared_orders_cleaned_2026-03-09.csv.gz",
        "cleared_orders_cleaned_2026-03-10.csv.gz",
        "pipeline_state_2026-03-10.tar.gz",
    ]


def test_one_failing_destination_does_not_stop_the_other(tmp_path) -> None:
    src = _working_set(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so nothing can be created under it")
    good = tmp_path / "good"

    warning = run_backup(src, [blocker / "sub", good], tiers=KEEP_ALL, now_utc=NOW)

    assert warning is not None and warning.startswith("⚠️")
    assert str(blocker / "sub") in warning
    assert str(good) not in warning
    assert (good / "pipeline_state_2026-03-10.tar.gz").exists()


def test_bundle_failure_still_backs_up_the_orders_and_says_so(
    tmp_path, monkeypatch
) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backup_mod, "build_state_bundle", _boom)

    warning = run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)

    assert warning is not None
    assert "state bundle not built" in warning and "disk full" in warning
    assert (dst / "cleared_orders_cleaned_2026-03-10.csv.gz").exists()
    assert not list(dst.glob("pipeline_state_*"))
    assert not list(src.glob(".backup-*"))


def test_missing_extra_file_reaches_the_warning(tmp_path) -> None:
    src = _working_set(tmp_path)

    warning = run_backup(
        src,
        [tmp_path / "cloud"],
        tiers=KEEP_ALL,
        extra_files=[tmp_path / "gone.csv"],
        now_utc=NOW,
    )

    assert warning is not None and "gone.csv" in warning
    assert (tmp_path / "cloud" / "pipeline_state_2026-03-10.tar.gz").exists()
    # And it outlives the run's warning: verify must not certify a bundle
    # that is a sound archive but short of a configured file.
    result = verify_backup(tmp_path / "cloud")
    assert not result.ok
    assert result.problems == [
        f"pipeline_state_2026-03-10.tar.gz: short of member(s): "
        f"extra file not found: {tmp_path / 'gone.csv'}"
    ]


def test_store_lost_while_staging_is_recorded_for_verify(tmp_path, monkeypatch) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    real_copy = backup_mod.shutil.copy2

    def _copy(source, target, **kwargs):
        if Path(source).name == "venue_tracks.csv":
            raise OSError("vanished")
        return real_copy(source, target, **kwargs)

    monkeypatch.setattr(backup_mod.shutil, "copy2", _copy)
    warning = run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)
    monkeypatch.undo()

    assert warning is not None and "venue_tracks.csv: vanished" in warning
    assert verify_backup(dst).problems == [
        "pipeline_state_2026-03-10.tar.gz: short of member(s): "
        ".cache/venue_tracks.csv: vanished"
    ]


def test_no_destination_is_a_clean_no_op(tmp_path) -> None:
    src = _working_set(tmp_path)
    assert run_backup(src, [], tiers=KEEP_ALL, now_utc=NOW) is None
    assert not list(src.glob(".backup-*"))


def test_files_copied_before_manifests_existed_get_an_entry(tmp_path) -> None:
    """An existing destination must verify after its first run on this code."""
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)
    (dst / MANIFEST_NAME).unlink()

    run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)

    assert "cleared_orders_cleaned_2026-03-09.csv.gz" in read_manifest(dst)
    assert verify_backup(dst).ok


def test_manifest_drops_entries_for_pruned_files(tmp_path) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)

    run_backup(src, [dst], tiers=RetentionTiers(daily=1), now_utc=NOW)

    assert sorted(read_manifest(dst)) == [
        "cleared_orders_cleaned_2026-03-10.csv.gz",
        "pipeline_state_2026-03-10.tar.gz",
    ]
    assert not (dst / "cleared_orders_cleaned_2026-03-09.csv.gz").exists()


def test_damaged_manifest_is_started_again_and_announced(tmp_path) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    dst.mkdir()
    (dst / MANIFEST_NAME).write_text("{not json")

    warning = run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)

    assert warning is not None and "was damaged and has been started again" in warning
    assert verify_backup(dst).ok
    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW) is None


def test_unreadable_manifest_is_left_alone_so_old_checksums_survive(
    tmp_path, monkeypatch
) -> None:
    """
    With weekly and monthly tiers a target holds copies that no longer
    exist locally. If the manifest cannot be read (a cloud file evicted
    with the client offline), rewriting it from this run's files would
    lose those copies' checksums for good.
    """
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)
    (src / "cleared_orders_cleaned_2026-03-09.csv.gz").unlink()  # pruned locally
    before = (dst / MANIFEST_NAME).read_text()

    real_read = Path.read_text

    def _offline(self, *args, **kwargs):
        if self.name == MANIFEST_NAME:
            raise OSError("Operation timed out")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _offline)
    warning = run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)
    monkeypatch.undo()

    assert warning is not None
    assert "could not be read" in warning and "Operation timed out" in warning
    assert (dst / MANIFEST_NAME).read_text() == before
    assert "cleared_orders_cleaned_2026-03-09.csv.gz" in read_manifest(dst)


def test_entry_is_refreshed_when_an_earlier_manifest_write_failed(
    tmp_path, monkeypatch
) -> None:
    """
    A same-day rewrite is copied but its manifest write fails. The next
    run finds the copy identical and skips it; it must not keep the entry
    for the earlier version, or verify reports a mismatch from then on.
    """
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)
    _snapshot(src, "2026-03-10", body="betId\n1\n2\n")

    def _fail(*_args, **_kwargs):
        raise OSError("quota exceeded")

    monkeypatch.setattr(backup_mod, "_write_manifest", _fail)
    warning = run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)
    assert warning is not None and "manifest not written: quota exceeded" in warning
    monkeypatch.undo()
    assert not verify_backup(dst).ok  # the manifest is one version behind

    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW) is None

    assert verify_backup(dst).ok


def test_a_store_that_cannot_be_copied_is_named_and_the_bundle_stays_valid(
    tmp_path, monkeypatch
) -> None:
    src = _working_set(tmp_path)
    real_copy = backup_mod.shutil.copy2

    def _copy(source, target, **kwargs):
        if Path(source).name == "venue_tracks.csv":
            raise OSError("vanished")
        return real_copy(source, target, **kwargs)

    monkeypatch.setattr(backup_mod.shutil, "copy2", _copy)

    problems = build_state_bundle(src, [], tmp_path / "b.tar.gz")

    assert problems == [".cache/venue_tracks.csv: vanished"]
    assert sorted(_bundle_members(tmp_path / "b.tar.gz")) == [
        ".cache/market_commission.csv"
    ]


def test_bundle_is_built_from_copies_not_the_live_stores(tmp_path, monkeypatch) -> None:
    """
    The racecard job replaces its stores outside the pipeline lock. The
    archive must read a copy taken in one piece, never the live path.
    """
    src = _working_set(tmp_path)
    added: list[Path] = []
    real_add = tarfile.TarFile.add

    def _add(self, name, *args, **kwargs):
        added.append(Path(name))
        return real_add(self, name, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "add", _add)

    build_state_bundle(src, [], tmp_path / "b.tar.gz")

    assert added and all(src not in path.parents for path in added)


def test_archive_failure_raises_rather_than_leaving_half_a_bundle(
    tmp_path, monkeypatch
) -> None:
    src = _working_set(tmp_path)

    def _add(self, *_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(tarfile.TarFile, "add", _add)

    with pytest.raises(OSError, match="No space left"):
        build_state_bundle(src, [], tmp_path / "b.tar.gz")


def test_staging_left_by_a_killed_backup_is_removed_once_old(tmp_path) -> None:
    import os

    src = _working_set(tmp_path)
    old, recent = src / ".backup-killed", src / ".backup-running"
    for leftover in (old, recent):
        leftover.mkdir()
        (leftover / backup_mod._STAGING_MARKER).write_text("")
        (leftover / "pipeline_state_2026-03-01.tar.gz").write_bytes(b"x")
    two_hours_ago = NOW.timestamp() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    os.utime(recent, (NOW.timestamp() - 60, NOW.timestamp() - 60))

    run_backup(src, [tmp_path / "cloud"], tiers=KEEP_ALL, now_utc=NOW)

    assert not old.exists()
    assert recent.exists()  # may belong to a backup still running


def test_failed_copy_is_recorded_so_verify_cannot_pass_a_backup_with_a_hole(
    tmp_path, monkeypatch
) -> None:
    """
    The bundle does not reach a target that has no older one. The manifest
    is still written, with a fresh time; it must say what is missing, or
    verify would report a current, complete backup with no stores in it.
    """
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    real_copy = backup_mod.shutil.copy2

    def _copy(source, target, **kwargs):
        if (
            Path(target).name.startswith("pipeline_state_")
            and dst in Path(target).parents
        ):
            raise OSError("quota exceeded")
        return real_copy(source, target, **kwargs)

    monkeypatch.setattr(backup_mod.shutil, "copy2", _copy)
    warning = run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)
    monkeypatch.undo()

    assert warning is not None and "quota exceeded" in warning
    body = json.loads((dst / MANIFEST_NAME).read_text())
    assert body["undelivered"] == ["pipeline_state_2026-03-10.tar.gz"]
    result = verify_backup(dst)
    assert not result.ok
    assert result.problems == [
        "pipeline_state_2026-03-10.tar.gz: the last backup did not deliver it"
    ]

    # The next complete backup clears it.
    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW) is None
    assert json.loads((dst / MANIFEST_NAME).read_text())["undelivered"] == []
    assert verify_backup(dst).ok


def test_bundle_that_was_never_built_is_recorded_as_undelivered(
    tmp_path, monkeypatch
) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backup_mod, "build_state_bundle", _boom)
    run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)

    assert verify_backup(dst).problems == [
        "pipeline_state_2026-03-10.tar.gz: the last backup did not deliver it"
    ]


def _short_run(src: Path, dst: Path, tiers: RetentionTiers, now: datetime, gone: Path):
    return run_backup(src, [dst], tiers=tiers, extra_files=[gone], now_utc=now)


def test_short_bundle_does_not_push_out_the_last_whole_one(tmp_path) -> None:
    """
    With one daily slot, today's bundle short of a configured file must
    not cost the target yesterday's whole one: a passing source failure
    would otherwise destroy the last complete recovery set.
    """
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    tiers = RetentionTiers(daily=1)
    yesterday = NOW - timedelta(days=1)
    assert run_backup(src, [dst], tiers=tiers, now_utc=yesterday) is None

    warning = _short_run(src, dst, tiers, NOW, tmp_path / "gone.csv")

    assert warning is not None
    assert sorted(f.name for f in dst.glob("pipeline_state_*")) == [
        "pipeline_state_2026-03-09.tar.gz",
        "pipeline_state_2026-03-10.tar.gz",
    ]
    assert read_manifest(dst)["pipeline_state_2026-03-10.tar.gz"]["incomplete"] == [
        f"extra file not found: {tmp_path / 'gone.csv'}"
    ]

    # A whole bundle the next day replaces both.
    assert run_backup(src, [dst], tiers=tiers, now_utc=NOW + timedelta(days=1)) is None
    assert sorted(f.name for f in dst.glob("pipeline_state_*")) == [
        "pipeline_state_2026-03-11.tar.gz"
    ]


def test_short_bundle_does_not_replace_a_whole_one_from_earlier_that_day(
    tmp_path,
) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW) is None
    whole = (dst / "pipeline_state_2026-03-10.tar.gz").read_bytes()

    warning = _short_run(src, dst, KEEP_ALL, NOW, tmp_path / "gone.csv")

    assert warning is not None and "kept the earlier complete copy" in warning
    assert (dst / "pipeline_state_2026-03-10.tar.gz").read_bytes() == whole
    assert "incomplete" not in read_manifest(dst)["pipeline_state_2026-03-10.tar.gz"]
    # The last backup still fell short, and verify says so.
    assert verify_backup(dst).problems == [
        "pipeline_state_2026-03-10.tar.gz (a copy with every member): "
        "the last backup did not deliver it"
    ]


def test_short_bundle_is_replaced_by_a_later_one_the_same_day(tmp_path) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    _short_run(src, dst, KEEP_ALL, NOW, tmp_path / "gone.csv")
    assert "incomplete" in read_manifest(dst)["pipeline_state_2026-03-10.tar.gz"]

    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW) is None

    assert "incomplete" not in read_manifest(dst)["pipeline_state_2026-03-10.tar.gz"]
    assert verify_backup(dst).ok


def test_short_bundles_are_kept_while_no_whole_one_exists(tmp_path) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    tiers = RetentionTiers(daily=1)
    _short_run(src, dst, tiers, NOW - timedelta(days=1), tmp_path / "gone.csv")
    _short_run(src, dst, tiers, NOW, tmp_path / "gone.csv")

    assert len(list(dst.glob("pipeline_state_*"))) == 2


def test_bundles_are_not_pruned_while_the_manifest_is_unreadable(
    tmp_path, monkeypatch
) -> None:
    """
    Which bundles are short is only in the manifest. Unreadable, today's
    short bundle would rank as whole and, with one daily slot, delete
    yesterday's whole one.
    """
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    tiers = RetentionTiers(daily=1)
    assert run_backup(src, [dst], tiers=tiers, now_utc=NOW - timedelta(days=1)) is None

    real_read = Path.read_text

    def _offline(self, *args, **kwargs):
        if self.name == MANIFEST_NAME:
            raise OSError("Operation timed out")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _offline)
    warning = _short_run(src, dst, tiers, NOW, tmp_path / "gone.csv")
    monkeypatch.undo()

    assert warning is not None
    assert "bundles not pruned without a trustworthy manifest" in warning
    assert (dst / "pipeline_state_2026-03-09.tar.gz").exists()
    # The short bundle is not copied at all: with nowhere to record that
    # it is short, it would pass for whole from the next run on.
    assert not (dst / "pipeline_state_2026-03-10.tar.gz").exists()
    assert "the manifest cannot record that" in warning
    # Snapshots do not depend on the manifest and are still thinned.
    assert sorted(f.name for f in dst.glob("cleared_orders_cleaned_*")) == [
        "cleared_orders_cleaned_2026-03-10.csv.gz"
    ]


def test_verify_keeps_failing_a_kept_short_bundle_after_later_good_backups(
    tmp_path,
) -> None:
    """With no pruning the short bundle stays, and stays a problem."""
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    gone = tmp_path / "gone.csv"
    _short_run(src, dst, KEEP_ALL, NOW - timedelta(days=1), gone)
    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW) is None

    result = verify_backup(dst)

    assert json.loads((dst / MANIFEST_NAME).read_text())["undelivered"] == []
    assert result.problems == [
        f"pipeline_state_2026-03-09.tar.gz: short of member(s): "
        f"extra file not found: {gone}"
    ]


def test_bundles_are_not_pruned_when_the_manifest_had_to_be_started_again(
    tmp_path,
) -> None:
    """
    A damaged manifest has lost which bundles were short. Ranked as whole,
    a short one could take the slot of the last whole one.
    """
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    tiers = RetentionTiers(daily=1)
    assert run_backup(src, [dst], tiers=tiers, now_utc=NOW - timedelta(days=2)) is None
    _short_run(src, dst, tiers, NOW - timedelta(days=1), tmp_path / "gone.csv")
    (dst / MANIFEST_NAME).write_text("{not json")

    warning = _short_run(src, dst, tiers, NOW, tmp_path / "gone.csv")

    assert warning is not None
    assert "bundles not pruned without a trustworthy manifest" in warning
    assert (dst / "pipeline_state_2026-03-08.tar.gz").exists()


def test_bundles_are_only_pruned_by_a_run_that_delivered_a_whole_one(
    tmp_path, monkeypatch
) -> None:
    """No bundle built today: yesterday's is all there is, so it stays."""
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    tiers = RetentionTiers(daily=1)
    for back in (3, 2):
        assert (
            run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW - timedelta(days=back))
            is None
        )

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(backup_mod, "build_state_bundle", _boom)
    run_backup(src, [dst], tiers=tiers, now_utc=NOW)
    monkeypatch.undo()
    assert len(list(dst.glob("pipeline_state_*"))) == 2

    assert run_backup(src, [dst], tiers=tiers, now_utc=NOW) is None
    assert sorted(f.name for f in dst.glob("pipeline_state_*")) == [
        "pipeline_state_2026-03-10.tar.gz"
    ]


def test_stale_cleanup_leaves_directories_that_are_not_staging(tmp_path) -> None:
    """
    A directory merely named like staging (it could be a backup target
    placed under the results directory) is someone's data.
    """
    import os

    src = _working_set(tmp_path)
    target = src / ".backup-cloud"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    old = NOW.timestamp() - 7200
    os.utime(target, (old, old))

    assert run_backup(src, [target], tiers=KEEP_ALL, now_utc=NOW) is None

    assert (target / "keep.txt").read_text() == "mine"
    assert (target / "pipeline_state_2026-03-10.tar.gz").exists()


def test_bundles_the_manifest_does_not_list_are_neither_ranked_nor_removed(
    tmp_path,
) -> None:
    """
    After a damaged manifest is started again, older bundles have no
    record: one of them may be short. Ranked as whole it could take a
    weekly slot from a whole bundle of the same week, so it is left out of
    the ranking, and kept, until someone decides.
    """
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    tiers = RetentionTiers(daily=1, weekly=2)
    # Monday whole, Tuesday short (same ISO week), then the record is lost.
    monday = datetime(2026, 3, 2, 22, 0, tzinfo=timezone.utc)
    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=monday) is None
    _short_run(src, dst, KEEP_ALL, monday + timedelta(days=1), tmp_path / "gone.csv")
    (dst / MANIFEST_NAME).write_text("{not json")
    run_backup(src, [dst], tiers=tiers, now_utc=NOW - timedelta(days=1))

    # The first run after the reset: the manifest reads cleanly again.
    assert run_backup(src, [dst], tiers=tiers, now_utc=NOW) is None

    names = sorted(f.name for f in dst.glob("pipeline_state_*"))
    assert "pipeline_state_2026-03-02.tar.gz" in names  # the whole one survives
    assert "pipeline_state_2026-03-03.tar.gz" in names
    assert "pipeline_state_2026-03-10.tar.gz" in names
    assert any("no checksum recorded" in p for p in verify_backup(dst).problems)


def test_store_that_went_missing_before_the_backup_marks_the_bundle_short(
    tmp_path,
) -> None:
    """
    A store already gone when the directory is listed is simply not
    there to fail on. Unchecked, the bundle would pass for whole and,
    with one daily slot, delete the last bundle that held the store.
    """
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    tiers = RetentionTiers(daily=1)
    assert run_backup(src, [dst], tiers=tiers, now_utc=NOW - timedelta(days=1)) is None
    assert read_manifest(dst)["pipeline_state_2026-03-09.tar.gz"]["stores"] == [
        "market_commission.csv",
        "venue_tracks.csv",
    ]
    (src / ".cache" / "market_commission.csv").unlink()

    warning = run_backup(src, [dst], tiers=tiers, now_utc=NOW)

    assert warning is not None
    assert ".cache/market_commission.csv: missing, the last whole bundle had it" in (
        warning
    )
    assert (dst / "pipeline_state_2026-03-09.tar.gz").exists()
    assert not verify_backup(dst).ok

    # It keeps being said, and the whole bundle kept, until the store is back.
    assert run_backup(src, [dst], tiers=tiers, now_utc=NOW + timedelta(days=1))
    assert (dst / "pipeline_state_2026-03-09.tar.gz").exists()
    (src / ".cache" / "market_commission.csv").write_text("marketId,commission\n")
    assert run_backup(src, [dst], tiers=tiers, now_utc=NOW + timedelta(days=2)) is None
    assert sorted(f.name for f in dst.glob("pipeline_state_*")) == [
        "pipeline_state_2026-03-12.tar.gz"
    ]


def test_a_new_store_is_not_a_problem(tmp_path) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    assert (
        run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW - timedelta(days=1)) is None
    )
    (src / ".cache" / "racecard.csv").write_text("marketId\n")

    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW) is None
    assert (
        "racecard.csv"
        in read_manifest(dst)["pipeline_state_2026-03-10.tar.gz"]["stores"]
    )


# --- verify ------------------------------------------------------------------


def _backed_up(tmp_path: Path) -> Path:
    dst = tmp_path / "cloud"
    assert (
        run_backup(_working_set(tmp_path), [dst], tiers=KEEP_ALL, now_utc=NOW) is None
    )
    return dst


def test_verify_passes_a_fresh_backup(tmp_path) -> None:
    result = verify_backup(_backed_up(tmp_path))
    assert result.ok and result.checked == 3
    assert result.written_utc == "2026-03-10T22:00:00Z"


def test_verify_catches_a_changed_file_of_the_same_size(tmp_path) -> None:
    dst = _backed_up(tmp_path)
    target = dst / "cleared_orders_cleaned_2026-03-10.csv.gz"
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))

    result = verify_backup(dst)

    assert not result.ok
    assert result.problems == [
        "cleared_orders_cleaned_2026-03-10.csv.gz: checksum differs from the manifest"
    ]


def test_verify_catches_a_truncated_file(tmp_path) -> None:
    dst = _backed_up(tmp_path)
    target = dst / "pipeline_state_2026-03-10.tar.gz"
    target.write_bytes(target.read_bytes()[:20])

    problems = verify_backup(dst).problems

    assert len(problems) == 1 and "pipeline_state_2026-03-10.tar.gz" in problems[0]
    assert "manifest says" in problems[0]


def test_verify_catches_a_bundle_that_gunzips_but_is_not_an_archive(tmp_path) -> None:
    """A valid gzip stream of the right size and checksum, but a broken tar."""
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW)
    target = dst / "pipeline_state_2026-03-10.tar.gz"
    with gzip.open(target, "wb") as fh:
        fh.write(b"these bytes are not a tar archive" * 40)
    data = target.read_bytes()
    body = json.loads((dst / MANIFEST_NAME).read_text())
    body["files"][target.name] = {
        "bytes": len(data),
        "mtimeNs": target.stat().st_mtime_ns,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    (dst / MANIFEST_NAME).write_text(json.dumps(body))

    problems = verify_backup(dst).problems

    assert len(problems) == 1
    assert "pipeline_state_2026-03-10.tar.gz: unreadable" in problems[0]


def test_verify_reports_a_damaged_deflate_stream_instead_of_crashing(tmp_path) -> None:
    """
    A good gzip header around a broken stream raises zlib.error, not an
    OSError; with a matching checksum (the source was already damaged
    when backed up) only the read-through finds it.
    """
    dst = _backed_up(tmp_path)
    target = dst / "cleared_orders_cleaned_2026-03-10.csv.gz"
    data = bytearray(gzip.compress(b"betId\n" + b"123456789\n" * 500))
    data[10:40] = b"\xff" * 30  # past the 10-byte header, inside the stream
    target.write_bytes(bytes(data))
    body = json.loads((dst / MANIFEST_NAME).read_text())
    body["files"][target.name] = {
        "bytes": len(data),
        "mtimeNs": target.stat().st_mtime_ns,
        "sha256": hashlib.sha256(bytes(data)).hexdigest(),
    }
    (dst / MANIFEST_NAME).write_text(json.dumps(body))

    problems = verify_backup(dst).problems

    assert len(problems) == 1
    assert problems[0].startswith(
        "cleared_orders_cleaned_2026-03-10.csv.gz: unreadable (error:"
    )


def test_verify_catches_a_missing_file(tmp_path) -> None:
    dst = _backed_up(tmp_path)
    (dst / "cleared_orders_cleaned_2026-03-09.csv.gz").unlink()

    assert verify_backup(dst).problems == [
        "cleared_orders_cleaned_2026-03-09.csv.gz: listed in the manifest but missing"
    ]


def test_verify_reports_an_unrecorded_file_that_is_not_gzip(tmp_path) -> None:
    dst = _backed_up(tmp_path)
    (dst / "cleared_orders_cleaned_2026-03-01.csv.gz").write_bytes(b"not gzip")

    result = verify_backup(dst)

    assert not result.ok and result.notes == []
    assert len(result.problems) == 1 and "unreadable" in result.problems[0]


def test_verify_notes_a_readable_snapshot_from_before_the_manifest(tmp_path) -> None:
    """
    A folder that was a backup target before manifests existed holds
    snapshots the source has since pruned, so no run can ever record a
    checksum for them. One that reads to the end is said, not failed.
    """
    dst = _backed_up(tmp_path)
    _snapshot(dst, "2026-03-01")

    result = verify_backup(dst)

    assert result.ok and result.problems == []
    assert result.checked == 4
    assert result.notes == [
        "cleared_orders_cleaned_2026-03-01.csv.gz: not in the manifest "
        "(no checksum to compare), reads to the end"
    ]


def test_verify_still_fails_a_bundle_the_manifest_does_not_list(tmp_path) -> None:
    """Only the manifest says whether a bundle holds every store."""
    dst = _backed_up(tmp_path)
    bundle = next(dst.glob("pipeline_state_*.tar.gz"))
    (dst / "pipeline_state_2026-03-01.tar.gz").write_bytes(bundle.read_bytes())

    result = verify_backup(dst)

    assert not result.ok
    assert result.problems == [
        "pipeline_state_2026-03-01.tar.gz: no checksum recorded for it"
    ]


def test_verify_of_an_empty_or_absent_destination_is_not_ok(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not verify_backup(empty).ok
    assert not verify_backup(tmp_path / "absent").ok


# --- configuration ------------------------------------------------------------


def test_backup_dirs_merge_the_old_setting_and_the_list_without_repeats() -> None:
    creds = {"paths": {"backup_dir": "/a", "backup_dirs": ["/b", "/a", " ", "/b"]}}
    assert resolve_backup_dirs(creds) == [Path("/a"), Path("/b")]
    assert resolve_backup_dirs({"paths": {"backup_dirs": ["~/x"]}}) == [
        Path("~/x").expanduser()
    ]
    assert resolve_backup_dirs({}) == []
    assert resolve_backup_dirs({"paths": {"backup_dirs": "/not-a-list"}}) == []


def test_extra_files_resolve_and_tolerate_nonsense() -> None:
    assert resolve_backup_extra_files(
        {"paths": {"backup_extra_files": ["~/m.csv"]}}
    ) == [Path("~/m.csv").expanduser()]
    assert resolve_backup_extra_files({"paths": {"backup_extra_files": 3}}) == []
    assert resolve_backup_extra_files({"paths": None}) == []


def test_retention_defaults_to_the_snapshot_rule_until_configured() -> None:
    assert resolve_backup_retention({"paths": {}}, 5) == RetentionTiers(daily=5)
    tiers = {"daily": 7, "weekly": 8, "monthly": 12}
    assert resolve_backup_retention(
        {"paths": {"backup_retention": tiers}}, 5
    ) == RetentionTiers(7, 8, 12)
    # A tier left out is zero; a malformed one never means "keep one".
    assert resolve_backup_retention(
        {"paths": {"backup_retention": {"weekly": 4, "daily": True}}}, 5
    ) == RetentionTiers(0, 4, 0)


def _valid_creds(paths: dict) -> dict:
    return {
        "betfair": {"username": "u", "password": "p", "app_key": "k"},
        "paths": {"results_csv_dir": "~/BetfairData", **paths},
    }


@pytest.mark.parametrize(
    ("paths", "fragment"),
    [
        ({"backup_dirs": "/one"}, "paths.backup_dirs must be a list of strings"),
        ({"backup_dirs": ["/one", 2]}, "paths.backup_dirs must be a list of strings"),
        ({"backup_extra_files": {"a": 1}}, "paths.backup_extra_files must be a list"),
        ({"backup_retention": 7}, "paths.backup_retention must be an object"),
        ({"backup_retention": {"yearly": 1}}, "backup_retention.yearly is not a tier"),
        ({"backup_retention": {"daily": -1}}, "backup_retention.daily must be"),
        ({"backup_retention": {"weekly": True}}, "backup_retention.weekly must be"),
        ({"backup_retention": {"monthly": 1.5}}, "backup_retention.monthly must be"),
    ],
)
def test_malformed_backup_settings_are_rejected_up_front(paths, fragment) -> None:
    result = validate_credentials(_valid_creds(paths))
    assert not result.ok
    assert any(fragment in error for error in result.errors), result.errors


def test_well_formed_backup_settings_validate() -> None:
    result = validate_credentials(
        _valid_creds(
            {
                "backup_dirs": ["/one", "/two"],
                "backup_extra_files": ["/m.csv"],
                "backup_retention": {"daily": 7, "weekly": 8, "monthly": 12},
            }
        )
    )
    assert result.ok, result.errors


# --- wiring: the run's backup step ---------------------------------------------


def test_run_step_backs_up_with_the_configured_settings(tmp_path) -> None:
    src = _working_set(tmp_path)
    master = tmp_path / "Master.csv"
    master.write_text("Market\n")
    creds = {
        "user": {"snapshot_retention_days": 5},
        "paths": {
            "backup_dir": str(tmp_path / "one"),
            "backup_dirs": [str(tmp_path / "two")],
            "backup_extra_files": [str(master)],
        },
    }
    said: list[str] = []

    assert runner._back_up(creds, src, said.append) is None

    for name in ("one", "two"):
        bundle = next((tmp_path / name).glob("pipeline_state_*.tar.gz"))
        assert "extra/0_Master.csv" in _bundle_members(bundle)
    assert len(said) == 2 and all("copied 3 file(s)" in line for line in said)


def test_run_step_without_a_destination_does_nothing(tmp_path) -> None:
    src = _working_set(tmp_path)
    assert runner._back_up({"paths": {}}, src, lambda _line: None) is None
    assert not list(src.glob(".backup-*"))


def test_run_step_returns_the_warning_and_never_raises(tmp_path) -> None:
    src = _working_set(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    creds = {"paths": {"backup_dir": str(blocker / "sub")}}

    warning = runner._back_up(creds, src, lambda _line: None)

    assert warning is not None and warning.startswith("⚠️")


def test_run_step_survives_a_setting_that_cannot_be_resolved(tmp_path) -> None:
    """ "~nosuchuser" has no home to expand to; the run's data is already written."""
    src = _working_set(tmp_path)
    creds = {"paths": {"backup_dirs": ["~no-such-user-here/backups"]}}

    warning = runner._back_up(creds, src, lambda _line: None)

    assert warning is not None
    assert warning.startswith("⚠️ Backup failed unexpectedly: RuntimeError")


# --- a destination that stops responding ------------------------------------------


@pytest.fixture
def stuck():
    """An event a fake blocked call waits on; released when the test ends."""
    release = threading.Event()
    yield release
    release.set()


def _hang_copy_to(monkeypatch, target: Path, release: threading.Event) -> None:
    real = backup_mod.backup_compressed_outputs

    def _copy(results_csv_dir, backup_dir, **kwargs):
        if backup_dir == target:
            release.wait(30)
            return None
        return real(results_csv_dir, backup_dir, **kwargs)

    monkeypatch.setattr(backup_mod, "backup_compressed_outputs", _copy)


def test_destination_that_hangs_is_left_behind_and_the_next_is_served(
    tmp_path, monkeypatch, stuck
) -> None:
    """
    The fault this guards: a cloud folder blocked a directory listing for
    88 minutes, and the run waited with its publish still to do.
    """
    src = _working_set(tmp_path)
    hung, good = tmp_path / "hung", tmp_path / "good"
    _hang_copy_to(monkeypatch, hung, stuck)

    started = time.monotonic()
    warning = run_backup(
        src, [hung, good], tiers=KEEP_ALL, now_utc=NOW, target_time_limit=0.2
    )

    assert time.monotonic() - started < 10
    assert warning == (
        f"⚠️ Backup to {hung} did not finish within 0.2s and was left behind "
        f"(the folder is not responding); check it with backup --verify."
    )
    assert verify_backup(good).ok


def test_destination_that_hangs_on_its_first_read_is_asked_only_once(
    tmp_path, monkeypatch, stuck
) -> None:
    """One wait per stuck destination, not one for the manifest and one for the copy."""
    src = _working_set(tmp_path)
    hung, good = tmp_path / "hung", tmp_path / "good"
    real_read = backup_mod.read_manifest
    copied_to: list[Path] = []
    real_copy = backup_mod.backup_compressed_outputs

    def _read(backup_dir):
        if backup_dir == hung:
            stuck.wait(30)
        return real_read(backup_dir)

    def _copy(results_csv_dir, backup_dir, **kwargs):
        copied_to.append(backup_dir)
        return real_copy(results_csv_dir, backup_dir, **kwargs)

    monkeypatch.setattr(backup_mod, "read_manifest", _read)
    monkeypatch.setattr(backup_mod, "backup_compressed_outputs", _copy)

    warning = run_backup(
        src, [hung, good], tiers=KEEP_ALL, now_utc=NOW, target_time_limit=0.2
    )

    assert warning is not None and warning.count("did not finish") == 1
    assert copied_to == [good]
    assert verify_backup(good).ok


def test_without_a_limit_a_slow_destination_is_waited_for(
    tmp_path, monkeypatch
) -> None:
    """The command's case: a person running a first backup to a slow folder."""
    src = _working_set(tmp_path)
    dst = tmp_path / "slow"
    real = backup_mod.backup_compressed_outputs

    def _slow(results_csv_dir, backup_dir, **kwargs):
        time.sleep(0.3)
        return real(results_csv_dir, backup_dir, **kwargs)

    monkeypatch.setattr(backup_mod, "backup_compressed_outputs", _slow)

    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW) is None
    assert verify_backup(dst).ok


def test_failure_inside_a_limited_destination_is_still_a_warning(
    tmp_path, monkeypatch
) -> None:
    """The limit must not swallow what the copy step would have reported."""
    src = _working_set(tmp_path)

    def _boom(results_csv_dir, backup_dir, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(backup_mod, "backup_compressed_outputs", _boom)

    warning = run_backup(
        src, [tmp_path / "cloud"], tiers=KEEP_ALL, now_utc=NOW, target_time_limit=5
    )

    assert warning == "⚠️ Backup failed unexpectedly: RuntimeError: disk on fire"


def test_run_step_limits_each_destination_and_the_command_does_not(
    tmp_path, monkeypatch
) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    limits: list[object] = []

    def _record(results_csv_dir, backup_dirs, **kwargs):
        limits.append(kwargs.get("target_time_limit"))
        return None

    monkeypatch.setattr(backup_mod, "run_backup", _record)
    _write_creds(
        tmp_path, monkeypatch, {"results_csv_dir": str(src), "backup_dirs": [str(dst)]}
    )

    runner._back_up({"paths": {"backup_dir": str(dst)}}, src, lambda _line: None)
    assert main(["backup"]) == 0

    assert limits == [runner._BACKUP_TARGET_TIME_LIMIT_SECONDS, None]
    assert 30 <= runner._BACKUP_TARGET_TIME_LIMIT_SECONDS <= 300


def test_backup_command_prints_notes_without_failing(
    tmp_path, monkeypatch, capsys
) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    _write_creds(
        tmp_path, monkeypatch, {"results_csv_dir": str(src), "backup_dirs": [str(dst)]}
    )
    assert main(["backup"]) == 0
    _snapshot(dst, "2026-03-01")
    capsys.readouterr()

    assert main(["backup", "--verify"]) == 0

    out = capsys.readouterr().out
    assert f"OK: {dst}: 4 file(s) read back" in out
    assert "note: cleared_orders_cleaned_2026-03-01.csv.gz: not in the manifest" in out


# --- the command ------------------------------------------------------------------


def _write_creds(tmp_path: Path, monkeypatch, paths: dict) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps(_valid_creds(paths)))
    monkeypatch.setattr(
        "betfair_results_downloader.secrets.credentials_path", lambda: creds
    )


def test_backup_command_backs_up_then_verifies(tmp_path, monkeypatch, capsys) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    _write_creds(
        tmp_path, monkeypatch, {"results_csv_dir": str(src), "backup_dirs": [str(dst)]}
    )

    assert main(["backup"]) == 0
    assert "OK: backed up to 1 destination(s)." in capsys.readouterr().out
    assert main(["backup", "--verify"]) == 0
    assert f"OK: {dst}: 3 file(s) read back" in capsys.readouterr().out


def test_backup_command_verify_fails_on_damage(tmp_path, monkeypatch, capsys) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    _write_creds(
        tmp_path, monkeypatch, {"results_csv_dir": str(src), "backup_dirs": [str(dst)]}
    )
    assert main(["backup"]) == 0
    (dst / "cleared_orders_cleaned_2026-03-10.csv.gz").write_bytes(b"gone")
    capsys.readouterr()

    assert main(["backup", "--verify"]) == 1
    out = capsys.readouterr().out
    assert f"FAIL: {dst}: 1 problem(s)" in out
    assert "cleared_orders_cleaned_2026-03-10.csv.gz" in out


def test_backup_command_without_a_destination_is_bad_configuration(
    tmp_path, monkeypatch, capsys
) -> None:
    src = _working_set(tmp_path)
    _write_creds(tmp_path, monkeypatch, {"results_csv_dir": str(src)})

    assert main(["backup"]) == 2
    assert "no backup destination" in capsys.readouterr().out


def test_backup_command_reports_an_incomplete_backup(
    tmp_path, monkeypatch, capsys
) -> None:
    src = _working_set(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    _write_creds(
        tmp_path,
        monkeypatch,
        {"results_csv_dir": str(src), "backup_dirs": [str(blocker / "sub")]},
    )

    assert main(["backup"]) == 1
    assert "⚠️ Backup skipped" in capsys.readouterr().out


@pytest.mark.parametrize(
    "retention", [{"dayly": 7, "weekly": 8}, {"daily": "7", "weekly": 8}]
)
def test_backup_command_refuses_malformed_retention_before_pruning(
    tmp_path, monkeypatch, capsys, retention
) -> None:
    """A mistyped tier resolves to zero and would thin the copies at once."""
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    dst.mkdir()
    for day in ("2026-03-02", "2026-03-03", "2026-03-04"):
        _snapshot(dst, day)
    _write_creds(
        tmp_path,
        monkeypatch,
        {
            "results_csv_dir": str(src),
            "backup_dirs": [str(dst)],
            "backup_retention": retention,
        },
    )

    assert main(["backup"]) == 2
    assert "invalid backup configuration" in capsys.readouterr().out
    assert len(list(dst.glob("cleared_orders_cleaned_*.csv.gz"))) == 3
