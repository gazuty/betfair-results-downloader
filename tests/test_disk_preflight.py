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
