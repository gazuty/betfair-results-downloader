"""
One-way backup to paths.backup_dir.

H4 moved the working set off OneDrive onto local disk after Files
On-Demand eviction twice broke reads of the canonical (2026-08-30/31).
OneDrive's remaining job is disaster recovery: it receives compressed
copies after each run and must never be able to fail that run.
"""

from __future__ import annotations

import gzip
import inspect
from pathlib import Path

from betfair_results_downloader.backup import backup_compressed_outputs
from betfair_results_downloader.paths import resolve_backup_dir


def _make_source(tmp_path: Path) -> Path:
    src = tmp_path / "results"
    src.mkdir()
    (src / "cleared_orders_cleaned.csv").write_text("betId\n1\n")
    for day in ("2026-08-29", "2026-08-30", "2026-08-31"):
        with gzip.open(src / f"cleared_orders_cleaned_{day}.csv.gz", "wb") as fh:
            fh.write(f"betId\n{day}\n".encode())
    with gzip.open(src / "cleared_orders_archive_2025.csv.gz", "wb") as fh:
        fh.write(b"betId\nold\n")
    (src / "unrelated.txt").write_text("not ours")
    return src


def test_copies_snapshots_and_archives_only(tmp_path) -> None:
    src = _make_source(tmp_path)
    dst = tmp_path / "backup"

    warning = backup_compressed_outputs(src, dst, retention=5)

    assert warning is None
    names = sorted(f.name for f in dst.iterdir())
    assert "cleared_orders_cleaned_2026-08-31.csv.gz" in names
    assert "cleared_orders_archive_2025.csv.gz" in names
    # The uncompressed canonical is what this design retired from OneDrive.
    assert "cleared_orders_cleaned.csv" not in names
    assert "unrelated.txt" not in names


def test_unchanged_files_are_not_recopied(tmp_path) -> None:
    src = _make_source(tmp_path)
    dst = tmp_path / "backup"
    backup_compressed_outputs(src, dst, retention=5)

    target = dst / "cleared_orders_cleaned_2026-08-31.csv.gz"
    before = target.stat().st_mtime_ns
    backup_compressed_outputs(src, dst, retention=5)
    assert target.stat().st_mtime_ns == before


def test_grown_same_day_snapshot_is_recopied(tmp_path) -> None:
    """A later run the same day rewrites the snapshot with more rows."""
    src = _make_source(tmp_path)
    dst = tmp_path / "backup"
    backup_compressed_outputs(src, dst, retention=5)

    snap = src / "cleared_orders_cleaned_2026-08-31.csv.gz"
    with gzip.open(snap, "wb") as fh:
        fh.write(b"betId\n2026-08-31\nanother-row-entirely\n")

    backup_compressed_outputs(src, dst, retention=5)
    with gzip.open(dst / snap.name, "rb") as fh:
        assert b"another-row-entirely" in fh.read()


def test_prunes_old_snapshots_but_never_archives(tmp_path) -> None:
    src = _make_source(tmp_path)
    dst = tmp_path / "backup"

    warning = backup_compressed_outputs(src, dst, retention=2)

    assert warning is None
    names = sorted(f.name for f in dst.iterdir())
    assert "cleared_orders_cleaned_2026-08-29.csv.gz" not in names
    assert "cleared_orders_cleaned_2026-08-30.csv.gz" in names
    assert "cleared_orders_cleaned_2026-08-31.csv.gz" in names
    assert "cleared_orders_archive_2025.csv.gz" in names


def test_source_is_never_modified(tmp_path) -> None:
    src = _make_source(tmp_path)
    before = sorted(f.name for f in src.iterdir())
    backup_compressed_outputs(src, tmp_path / "backup", retention=1)
    assert sorted(f.name for f in src.iterdir()) == before


def test_uncreatable_backup_dir_warns_but_never_raises(tmp_path) -> None:
    src = _make_source(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a dir must go")

    warning = backup_compressed_outputs(src, blocker / "backup", retention=5)

    assert warning is not None
    assert warning.startswith("⚠️"), "must carry the marker the Slack path keys on"


def test_copy_failure_warns_and_continues(tmp_path, monkeypatch) -> None:
    import betfair_results_downloader.backup as backup_mod

    src = _make_source(tmp_path)
    dst = tmp_path / "backup"
    real_copy = backup_mod.shutil.copy2
    failed_once: list[str] = []

    def flaky(a, b):
        if "2026-08-29" in str(a) and not failed_once:
            failed_once.append(str(a))
            raise OSError(28, "No space left on device")
        return real_copy(a, b)

    monkeypatch.setattr(backup_mod.shutil, "copy2", flaky)

    warning = backup_compressed_outputs(src, dst, retention=5)

    assert warning is not None and "2026-08-29" in warning
    # The failure of one file must not stop the others.
    assert (dst / "cleared_orders_cleaned_2026-08-31.csv.gz").exists()


def test_resolve_backup_dir_unset_is_none() -> None:
    assert resolve_backup_dir({"paths": {}}) is None
    assert resolve_backup_dir({}) is None
    assert resolve_backup_dir({"paths": {"backup_dir": ""}}) is None


def test_resolve_backup_dir_expands_tilde() -> None:
    got = resolve_backup_dir({"paths": {"backup_dir": "~/OneDriveBackup"}})
    assert got == Path("~/OneDriveBackup").expanduser()


def test_pipeline_calls_the_backup_after_the_csv_write() -> None:
    """The wiring, asserted at source level like the login-retry test."""
    from betfair_results_downloader.scheduler import runner

    source = inspect.getsource(runner._run_pipeline_inner)
    assert "backup_compressed_outputs(" in source
    assert source.index("write_csv_outputs(") < source.index(
        "backup_compressed_outputs("
    )


def test_same_size_rewrite_is_still_recopied(tmp_path) -> None:
    """
    Size equality is not identity: a same-day rewrite can replace a row
    with an incoming one of the same betId and keep the byte count. The
    mtime changes on any rewrite, and that is what the skip check keys on.
    """
    src = _make_source(tmp_path)
    dst = tmp_path / "backup"
    backup_compressed_outputs(src, dst, retention=5)

    snap = src / "cleared_orders_cleaned_2026-08-31.csv.gz"
    original_size = snap.stat().st_size
    snap.write_bytes(b"B" * original_size)
    assert snap.stat().st_size == original_size

    warning = backup_compressed_outputs(src, dst, retention=5)

    assert warning is None
    assert (dst / snap.name).read_bytes() == b"B" * original_size


def test_uncompressed_snapshots_are_never_copied(tmp_path) -> None:
    """
    With user.compress_snapshots=false the dated snapshot is a full
    canonical-sized .csv rewritten every run -- copying it daily is the
    repeated large OneDrive transfer this design exists to retire.
    """
    src = _make_source(tmp_path)
    (src / "cleared_orders_cleaned_2026-08-31.csv").write_text("betId\nbig\n")
    dst = tmp_path / "backup"

    warning = backup_compressed_outputs(src, dst, retention=5)

    assert warning is None
    assert not (dst / "cleared_orders_cleaned_2026-08-31.csv").exists()
    assert (dst / "cleared_orders_cleaned_2026-08-31.csv.gz").exists()


def test_backup_dir_aliasing_the_working_dir_is_refused(tmp_path) -> None:
    """
    Same directory on both sides means no second copy exists -- and the
    prune step would delete real working-set snapshots. Must warn, and
    must not touch a single source file.
    """
    src = _make_source(tmp_path)
    before = sorted(f.name for f in src.iterdir())

    warning = backup_compressed_outputs(src, src, retention=1)

    assert warning is not None and warning.startswith("⚠️")
    assert sorted(f.name for f in src.iterdir()) == before


def test_non_string_backup_dir_resolves_to_none() -> None:
    """Runtime stays forgiving; validate_credentials is the loud gate."""
    assert resolve_backup_dir({"paths": {"backup_dir": True}}) is None


def test_non_string_results_dir_fails_loudly() -> None:
    from betfair_results_downloader.paths import (
        ResultsDirNotConfigured,
        resolve_results_dir,
    )
    import pytest

    with pytest.raises(ResultsDirNotConfigured, match="must be a string"):
        resolve_results_dir({"paths": {"results_csv_dir": True}})


def test_pathological_backup_dir_warns_but_never_raises(tmp_path) -> None:
    """
    A NUL byte in a configured path raises ValueError from pathlib, not
    OSError. The contract is best-effort: a backup must never turn an
    already-successful run into a failure, whatever the exception type.
    """
    src = _make_source(tmp_path)

    warning = backup_compressed_outputs(src, Path("bad\x00dir"), retention=5)

    assert warning is not None and warning.startswith("⚠️")


def test_temp_files_are_globally_unique(tmp_path, monkeypatch) -> None:
    """
    Overlapping runs sharing one backup dir -- including from different
    machines, where pids can collide -- must not truncate or rename each
    other's in-progress copy: the temp name carries pid AND a uuid nonce.
    """
    import betfair_results_downloader.backup as backup_mod

    src = _make_source(tmp_path)
    dst = tmp_path / "backup"
    monkeypatch.setattr(backup_mod.os, "getpid", lambda: 424242)
    tmp_names: list[str] = []
    real_copy = backup_mod.shutil.copy2

    def capture(a, b):
        tmp_names.append(Path(b).name)
        return real_copy(a, b)

    monkeypatch.setattr(backup_mod.shutil, "copy2", capture)

    warning = backup_compressed_outputs(src, dst, retention=5)

    assert warning is None
    assert tmp_names and all(".424242." in n and n.endswith(".tmp") for n in tmp_names)
    # The nonce, not just the pid: same-pid runs on two machines must differ.
    assert all(n.split(".424242.")[1] != "tmp" for n in tmp_names)
