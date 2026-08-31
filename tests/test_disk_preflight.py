"""
Disk-space preflight.

The 2026-08-30 and 2026-08-31 incidents both began as a disk that filled
with nothing reporting it: macOS spent four nights failing to stage an
update, OneDrive evicted the canonical, and the first anyone heard was a
failed 06:00 run. Writing a ~270MB canonical onto a full disk is also the
one moment the pipeline can damage its own system of record.
"""

from __future__ import annotations

import shutil
from collections import namedtuple


from betfair_results_downloader.scheduler import runner
from betfair_results_downloader.scheduler.runner import check_disk_space

Usage = namedtuple("Usage", "total used free")
GB = 1024**3


def _fake_usage(monkeypatch, free_bytes: int) -> None:
    monkeypatch.setattr(
        runner.shutil, "disk_usage", lambda _p: Usage(228 * GB, 0, free_bytes)
    )


def test_healthy_disk_is_silent(monkeypatch) -> None:
    _fake_usage(monkeypatch, 76 * GB)
    ok, warning = check_disk_space("/anywhere")
    assert ok is True
    assert warning is None


def test_soft_floor_warns_but_runs(monkeypatch) -> None:
    _fake_usage(monkeypatch, 6 * GB)
    ok, warning = check_disk_space("/anywhere")
    assert ok is True
    assert "Disk low" in warning
    assert "6.0 GB" in warning


def test_hard_floor_refuses(monkeypatch) -> None:
    """Below the hard floor, nothing may touch the canonical."""
    _fake_usage(monkeypatch, 1 * GB)
    ok, warning = check_disk_space("/anywhere")
    assert ok is False
    assert "Refusing" in warning


def test_unreadable_filesystem_never_blocks_the_run(monkeypatch) -> None:
    """The preflight must not become a new way for runs to fail."""

    def boom(_p):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(runner.shutil, "disk_usage", boom)
    ok, warning = check_disk_space("/gone")
    assert ok is True
    assert warning is None


def test_run_scheduled_refuses_below_the_hard_floor(monkeypatch) -> None:
    """The refusal happens before gap detection, download, or any write."""
    _fake_usage(monkeypatch, 1 * GB)

    def must_not_run(*_a, **_k):
        raise AssertionError("no work may start below the hard floor")

    monkeypatch.setattr(runner, "compute_backfill_window", must_not_run)

    result = runner.run_scheduled(
        {"paths": {"results_csv_dir": "/anywhere"}, "user": {}},
        runner.ScheduleConfig(),
    )

    assert result.ok is False
    assert result.status == "failed"
    assert "critically low" in result.message


def test_dm_report_carries_the_soft_warning(monkeypatch, tmp_path, capsys) -> None:
    """The twice-daily report is the message that is always read."""
    csv_path = tmp_path / "cleared_orders_cleaned.csv"
    csv_path.write_text(
        "betId,eventTypeId,profit,settledDate\n1,7,5.0,2026-06-05T19:30:00Z\n",
        encoding="utf-8",
    )
    _fake_usage(monkeypatch, 6 * GB)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(228 * GB, 0, 6 * GB))

    from betfair_results_downloader.__main__ import main

    assert main(["dm-report", "--csv", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert "Disk low" in out


def test_dm_report_is_clean_on_a_healthy_disk(monkeypatch, tmp_path, capsys) -> None:
    csv_path = tmp_path / "cleared_orders_cleaned.csv"
    csv_path.write_text(
        "betId,eventTypeId,profit,settledDate\n1,7,5.0,2026-06-05T19:30:00Z\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: Usage(228 * GB, 0, 76 * GB))

    from betfair_results_downloader.__main__ import main

    assert main(["dm-report", "--csv", str(csv_path)]) == 0
    assert "Disk low" not in capsys.readouterr().out


def test_missing_results_dir_does_not_bypass_the_floor(monkeypatch, tmp_path) -> None:
    """
    A first run, or a removed/evicted output directory, is exactly when the
    disk is most suspect. The check walks to the nearest existing ancestor
    rather than waving the run through.
    """
    seen: list[str] = []

    def usage(p):
        seen.append(str(p))
        return Usage(228 * GB, 0, 1 * GB)

    monkeypatch.setattr(runner.shutil, "disk_usage", usage)

    ok, warning = check_disk_space(tmp_path / "does" / "not" / "exist")

    assert ok is False, "the hard floor must apply even before the dir exists"
    assert seen and seen[0] == str(tmp_path), "probed the nearest existing ancestor"


def test_backfill_refuses_below_the_hard_floor(monkeypatch) -> None:
    """A manual backfill rewrites the same canonical; same floor."""
    from datetime import date

    _fake_usage(monkeypatch, 1 * GB)

    result = runner.run_backfill(
        {"paths": {"results_csv_dir": "/anywhere"}, "user": {}},
        runner.ScheduleConfig(),
        date(2026, 6, 1),
        date(2026, 6, 2),
    )

    assert result.ok is False
    assert "critically low" in result.message


def test_hard_floor_refusal_is_recorded_in_run_history(monkeypatch, tmp_path) -> None:
    """The refusal is the new failure mode; it belongs in the operational record."""
    _fake_usage(monkeypatch, 1 * GB)
    recorded: list[dict] = []
    monkeypatch.setattr(
        runner, "append_run_history", lambda _d, entry: recorded.append(entry)
    )

    result = runner.run_scheduled(
        {"paths": {"results_csv_dir": "/anywhere"}, "user": {}},
        runner.ScheduleConfig(),
    )

    assert result.ok is False
    assert len(recorded) == 1
    assert recorded[0]["status"] == "failed"
    assert "critically low" in recorded[0]["message"]


def test_successful_run_with_soft_warning_posts_to_slack(monkeypatch) -> None:
    """A warning that only lands in scheduler stdout is not a warning."""
    from betfair_results_downloader.__main__ import main
    from betfair_results_downloader.scheduler.runner import RunResult

    posted: list[str] = []
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._post_to_slack",
        lambda text, creds=None: posted.append(text) or 0,
    )
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda *a, **k: ({"paths": {"results_csv_dir": "/tmp"}}, object()),
    )
    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.runner.run_scheduled",
        lambda *a, **k: RunResult(
            ok=True, status="success", message="All good. ⚠️ Disk low: 6.0 GB free."
        ),
    )

    assert main(["run"]) == 0
    assert len(posted) == 1
    assert "Disk low" in posted[0]


def test_successful_run_without_warning_stays_silent(monkeypatch) -> None:
    from betfair_results_downloader.__main__ import main
    from betfair_results_downloader.scheduler.runner import RunResult

    posted: list[str] = []
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._post_to_slack",
        lambda text, creds=None: posted.append(text) or 0,
    )
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda *a, **k: ({"paths": {"results_csv_dir": "/tmp"}}, object()),
    )
    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.runner.run_scheduled",
        lambda *a, **k: RunResult(ok=True, status="success", message="All good."),
    )

    assert main(["run"]) == 0
    assert posted == []
