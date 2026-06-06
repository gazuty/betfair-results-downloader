from pathlib import Path


def test_create_schedulestate_includes_dual_coverage_fields() -> None:
    script = Path("scripts/azure_create_schedulestate.py").read_text(encoding="utf-8")
    assert "LastCoveredDateUtc" in script
    assert "LastCoveredDateLocal" in script
    assert "LastCoveredTimezone" in script


def test_upgrade_schedulestate_script_adds_missing_columns_idempotently() -> None:
    script = Path("scripts/azure_upgrade_schedulestate.py").read_text(encoding="utf-8")
    assert "COL_LENGTH('dbo.ScheduleState', 'LastCoveredDateLocal') IS NULL" in script
    assert "COL_LENGTH('dbo.ScheduleState', 'LastCoveredTimezone') IS NULL" in script
    assert "UPDATE dbo.ScheduleState" in script
    assert "LastCoveredDateLocal = LastCoveredDateUtc" in script
