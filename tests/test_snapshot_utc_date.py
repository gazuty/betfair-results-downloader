from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from betfair_results_downloader.downloader_core import write_csv_outputs


def test_snapshot_uses_utc_date(tmp_path: Path, monkeypatch):
    """
    Snapshot filename should use UTC date, not local machine date.

    This test ensures deterministic snapshot naming across timezones.
    Example: a run at 2026-01-26 23:30 UTC should produce
    cleared_orders_cleaned_2026-01-26.csv (UTC date),
    not 2026-01-27 if local timezone is ahead of UTC.
    """
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    # Sample data
    df = pd.DataFrame({
        "betId": [12345],
        "profit": [10.50],
    })

    # Mock datetime.now() to return a fixed UTC time
    # Scenario: 2026-01-26 23:30:00 UTC
    # In UTC+2 timezone, this would be 2026-01-27 01:30:00 local
    fixed_utc_time = datetime(2026, 1, 26, 23, 30, 0, tzinfo=timezone.utc)

    # Save reference to real datetime for fallback
    real_datetime = datetime

    class MockDatetime:
        """Mock datetime that returns our fixed UTC time."""

        @staticmethod
        def now(tz=None):
            if tz is timezone.utc:
                return fixed_utc_time
            # Fallback to real datetime for other cases
            return real_datetime.now(tz) if tz else real_datetime.now()

        # Delegate other attributes to real datetime
        def __getattribute__(self, name):
            if name in ('now',):
                return object.__getattribute__(self, name)
            return getattr(real_datetime, name)

    # Patch datetime in the downloader_core module
    import betfair_results_downloader.downloader_core as dc
    monkeypatch.setattr(dc, "datetime", MockDatetime)

    # Execute write_csv_outputs
    result = write_csv_outputs(
        df_co=df,
        results_csv_dir=results_dir,
        status_cb=None,
    )

    # Verify snapshot uses UTC date (2026-01-26), not local date
    expected_snapshot_name = "cleared_orders_cleaned_2026-01-26.csv"

    assert result.snapshot_path.name == expected_snapshot_name
    assert result.snapshot_path.exists()

    # Verify the snapshot was actually written
    snapshot_df = pd.read_csv(result.snapshot_path)
    assert len(snapshot_df) == 1
    assert snapshot_df["betId"].iloc[0] == 12345
