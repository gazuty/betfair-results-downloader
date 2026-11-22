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
        "extra/Master.csv",
    ]
    assert members["extra/Master.csv"] == master.read_bytes()
    assert members[".cache/market_commission.csv"] == b"marketId,commission\n1.2,0.5\n"


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
    assert members["extra/Master.csv"] == b"first"
    assert members["extra/1_Master.csv"] == b"second"


def test_no_cache_directory_still_bundles_the_extras(tmp_path) -> None:
    src = tmp_path / "results"
    src.mkdir()
    extra = tmp_path / "notes.csv"
    extra.write_text("a\n")

    assert build_state_bundle(src, [extra], tmp_path / "b.tar.gz") == []
    assert sorted(_bundle_members(tmp_path / "b.tar.gz")) == ["extra/notes.csv"]


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
        assert entry == {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }, name
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


def test_damaged_manifest_is_replaced_not_fatal(tmp_path) -> None:
    src = _working_set(tmp_path)
    dst = tmp_path / "cloud"
    dst.mkdir()
    (dst / MANIFEST_NAME).write_text("{not json")

    assert run_backup(src, [dst], tiers=KEEP_ALL, now_utc=NOW) is None
    assert verify_backup(dst).ok


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


def test_verify_catches_a_missing_file(tmp_path) -> None:
    dst = _backed_up(tmp_path)
    (dst / "cleared_orders_cleaned_2026-03-09.csv.gz").unlink()

    assert verify_backup(dst).problems == [
        "cleared_orders_cleaned_2026-03-09.csv.gz: listed in the manifest but missing"
    ]


def test_verify_reports_an_unrecorded_file_that_is_not_gzip(tmp_path) -> None:
    dst = _backed_up(tmp_path)
    (dst / "cleared_orders_cleaned_2026-03-01.csv.gz").write_bytes(b"not gzip")

    problems = verify_backup(dst).problems

    assert any("no checksum recorded" in p for p in problems)
    assert any("unreadable" in p for p in problems)


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
        assert "extra/Master.csv" in _bundle_members(bundle)
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
    assert f"OK: {dst}: 3 file(s) match" in capsys.readouterr().out


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
