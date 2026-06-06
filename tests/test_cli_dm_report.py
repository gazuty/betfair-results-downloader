from __future__ import annotations

from betfair_results_downloader.__main__ import main


def test_cli_dm_report_renders_from_discovered_results_csv(tmp_path, capsys, monkeypatch) -> None:
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    csv_path = results_dir / "cleared_orders_cleaned.csv"
    csv_path.write_text(
        "betId,eventTypeId,profit,settledDate\n"
        "1,7,12.5,2026-06-06T00:30:00Z\n"
        "2,4339,-2.0,2026-06-03T10:00:00Z\n",
        encoding="utf-8",
    )

    def fake_loader():
        return ({"paths": {"results_csv_dir": str(results_dir)}}, None)

    monkeypatch.setattr("betfair_results_downloader.__main__._load_creds_and_schedule", fake_loader)

    exit_code = main([
        "dm-report",
        "--csv",
        str(csv_path),
        "--at",
        "2026-06-06T21:00:00+10:00",
    ])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Betfair results update" in out
    assert "Saturday 6 June, 9:00 PM" in out
    assert "• Total profit: $10.50" in out
