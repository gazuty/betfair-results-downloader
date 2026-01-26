from decimal import Decimal
from pathlib import Path

import pandas as pd

from betfair_results_downloader.azure_publish import AzurePublishResult
from betfair_results_downloader.config import DownloaderConfig
from betfair_results_downloader.downloader_core import AzurePrepResult
from betfair_results_downloader.run import publish_to_azure_from_canonical_incremental


def _base_creds(results_dir: Path) -> dict:
    return {
        "paths": {"results_csv_dir": str(results_dir)},
        "user": {"enable_azure_sql": True, "db_user_id": "user1"},
        "betfair": {"username": "u", "password": "p", "app_key": "k"},
        "azure_sql": {
            "server": "s",
            "database": "d",
            "username": "u",
            "password": "p",
            "driver": "driver",
        },
    }


def test_publish_only_missing_canonical(tmp_path: Path) -> None:
    config = DownloaderConfig(enable_azure_sql=True, dry_run=False)
    creds = _base_creds(tmp_path)

    result = publish_to_azure_from_canonical_incremental(config, creds)

    assert result["ok"] is False
    assert "Canonical CSV not found" in result["message"]
    assert result["publish_only"]["attempted"] is False


def test_publish_only_dry_run_disabled(tmp_path: Path) -> None:
    config = DownloaderConfig(enable_azure_sql=True, dry_run=True)
    creds = _base_creds(tmp_path)

    result = publish_to_azure_from_canonical_incremental(config, creds)

    assert result["ok"] is False
    assert "Dry run is enabled" in result["message"]


def test_publish_only_calls_prep_and_publish(tmp_path: Path, monkeypatch) -> None:
    canonical_path = tmp_path / "cleared_orders_cleaned.csv"
    canonical_path.write_text("stub")

    df_stub = pd.DataFrame(
        {
            "eventTypeId": [7],
            "marketId": ["1"],
            "profit": [1.23],
            "betId": [1],
            "placedDate": ["2020-01-01"],
        }
    )

    called = {"prep": 0, "publish": 0}

    def fake_read_csv(path: Path, low_memory: bool = False) -> pd.DataFrame:
        assert Path(path) == canonical_path
        return df_stub

    def fake_prepare_azure_dataset(
        *, df_co: pd.DataFrame, allowed_event_type_ids: set[int]
    ) -> AzurePrepResult:
        assert df_co is df_stub
        called["prep"] += 1
        return AzurePrepResult(
            attempted=True,
            rows_after_filter=1,
            markets_aggregated=1,
            message="ok",
            df_market_results=pd.DataFrame(),
            rows_to_write=[(Decimal("1"), Decimal("1.00"), "")],
        )

    def fake_publish_to_azure_sql(
        *, creds: dict, rows_to_write, dry_run: bool
    ) -> AzurePublishResult:
        called["publish"] += 1
        assert dry_run is False
        assert rows_to_write == [(Decimal("1"), Decimal("1.00"), "")]
        return AzurePublishResult(
            attempted=True,
            inserted_rows=1,
            updated_rows=1,
            deleted_rows=0,
            existing_rows_in_db=0,
            matching_rows_unchanged=0,
            rows_to_update=1,
            rows_to_insert=1,
            rows_db_only_not_in_new=0,
            message="ok",
        )

    monkeypatch.setattr(pd, "read_csv", fake_read_csv)
    monkeypatch.setattr(
        "betfair_results_downloader.run.prepare_azure_dataset",
        fake_prepare_azure_dataset,
    )
    monkeypatch.setattr(
        "betfair_results_downloader.run.publish_to_azure_sql",
        fake_publish_to_azure_sql,
    )

    config = DownloaderConfig(enable_azure_sql=True, dry_run=False)
    creds = _base_creds(tmp_path)

    result = publish_to_azure_from_canonical_incremental(
        config,
        creds,
        confirm_publish_cb=lambda summary: True,
    )

    assert result["ok"] is True
    assert called["prep"] == 1
    assert called["publish"] == 1
    assert result["azure"]["publish_attempted"] is True
