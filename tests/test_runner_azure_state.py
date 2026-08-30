"""
A failed Azure checkpoint write must downgrade the run -- but only when Azure
is actually configured. CSV-only is a supported setup, and alerting on every
successful run of it would be worse than the silence it replaces.
"""

from __future__ import annotations

from betfair_results_downloader.scheduler.runner import (
    RunResult,
    apply_azure_state_outcome,
    azure_state_configured,
)

CONFIGURED = {"azure_sql": {"server": "x.database.windows.net"}}
CSV_ONLY = {"azure_sql": {}}


def _success() -> RunResult:
    return RunResult(ok=True, status="success", message="1,234 rows")


def test_csv_only_setup_is_not_treated_as_a_failure() -> None:
    """upsert returns False when Azure was never configured; that is fine."""
    assert azure_state_configured(CSV_ONLY) is False

    result = apply_azure_state_outcome(_success(), CSV_ONLY, state_written=False)

    assert result.status == "success"
    assert result.errors == []


def test_configured_azure_that_fails_downgrades_to_partial() -> None:
    result = apply_azure_state_outcome(_success(), CONFIGURED, state_written=False)

    assert result.status == "partial"
    assert "repeat this window" in result.errors[0]
    assert "repeat this window" in result.message


def test_configured_azure_that_succeeds_is_untouched() -> None:
    result = apply_azure_state_outcome(_success(), CONFIGURED, state_written=True)

    assert result.status == "success"
    assert result.errors == []


def test_missing_azure_section_entirely() -> None:
    assert azure_state_configured({}) is False
    assert apply_azure_state_outcome(_success(), {}, False).status == "success"


def test_downgraded_run_is_no_longer_success_for_marker_purposes() -> None:
    """
    The success markers are written only while status == "success". A
    downgraded run exits 1, alerts, and is recorded as partial, so a success
    marker for the same day would contradict every other signal.
    """
    result = apply_azure_state_outcome(_success(), CONFIGURED, state_written=False)
    assert result.status != "success"

    still_ok = apply_azure_state_outcome(_success(), CONFIGURED, state_written=True)
    assert still_ok.status == "success"
