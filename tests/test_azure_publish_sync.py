from decimal import Decimal

import pandas as pd
import pytest

from betfair_results_downloader.azure_publish import build_sync_plan


def _make_df(rows: list[tuple[object, object, object, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["UserID", "MarketID", "Profit", "Notes"])


def test_build_sync_plan_empty_db_all_inserts() -> None:
    df_new = _make_df(
        [
            ("user1", Decimal("101"), Decimal("1.23"), ""),
            ("user1", Decimal("102"), Decimal("2.34"), ""),
        ]
    )
    df_existing = pd.DataFrame(columns=["UserID", "MarketID", "Profit", "Notes"])

    plan = build_sync_plan(df_new, df_existing)

    assert plan.existing_count == 0
    assert plan.new_count == 2
    assert plan.update_count == 0
    assert plan.unchanged_count == 0
    assert plan.db_only_count == 0
    assert len(plan.rows_to_insert) == 2
    assert len(plan.rows_to_update) == 0


def test_build_sync_plan_identical_db_no_changes() -> None:
    df_new = _make_df(
        [
            ("user1", Decimal("201"), Decimal("1.0"), ""),
            ("user1", Decimal("202"), Decimal("2.0"), ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", 201.0, 1.0, ""),
            ("user1", 202.0, 2.0, ""),
        ]
    )

    plan = build_sync_plan(df_new, df_existing)

    assert plan.existing_count == 2
    assert plan.new_count == 0
    assert plan.update_count == 0
    assert plan.unchanged_count == 2
    assert plan.db_only_count == 0


def test_build_sync_plan_mixed_changes() -> None:
    df_new = _make_df(
        [
            ("user1", 301, Decimal("1.00"), ""),
            ("user1", 302, Decimal("2.50"), ""),
            ("user1", 303, Decimal("3.00"), ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", 301, Decimal("1.00"), ""),
            ("user1", 302, Decimal("2.00"), ""),
            ("user1", 304, Decimal("4.00"), ""),
        ]
    )

    plan = build_sync_plan(df_new, df_existing)

    assert plan.existing_count == 3
    assert plan.new_count == 1
    assert plan.update_count == 1
    assert plan.unchanged_count == 1
    assert plan.db_only_count == 1
    assert len(plan.rows_to_insert) == 1
    assert len(plan.rows_to_update) == 1


def test_build_sync_plan_decimal_float_equal() -> None:
    df_new = _make_df(
        [
            ("user1", 401, Decimal("1.000000000"), ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", Decimal("401.0"), 1.0, ""),
        ]
    )

    plan = build_sync_plan(df_new, df_existing)

    assert plan.update_count == 0
    assert plan.unchanged_count == 1


def test_build_sync_plan_nan_profit_treated_as_changed() -> None:
    df_new = _make_df(
        [
            ("user1", 501, float("nan"), ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", 501, Decimal("1.00"), ""),
        ]
    )

    plan = build_sync_plan(df_new, df_existing)

    assert plan.update_count == 1
    assert plan.unchanged_count == 0


def test_build_sync_plan_none_profit_equal() -> None:
    df_new = _make_df(
        [
            ("user1", 601, None, ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", 601, None, ""),
        ]
    )

    plan = build_sync_plan(df_new, df_existing)

    assert plan.update_count == 0
    assert plan.unchanged_count == 1


def test_build_sync_plan_none_vs_value_is_changed() -> None:
    df_new = _make_df(
        [
            ("user1", 701, None, ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", 701, Decimal("1.00"), ""),
        ]
    )

    plan = build_sync_plan(df_new, df_existing)

    assert plan.update_count == 1
    assert plan.unchanged_count == 0


def test_build_sync_plan_duplicate_marketid_new_raises() -> None:
    df_new = _make_df(
        [
            ("user1", 801, Decimal("1.00"), ""),
            ("user1", 801.0, Decimal("1.00"), ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", 802, Decimal("2.00"), ""),
        ]
    )

    with pytest.raises(ValueError, match="Duplicate MarketID_key in new dataset"):
        build_sync_plan(df_new, df_existing)


def test_build_sync_plan_duplicate_marketid_existing_raises() -> None:
    df_new = _make_df(
        [
            ("user1", 901, Decimal("1.00"), ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", 902, Decimal("2.00"), ""),
            ("user1", "902.0", Decimal("2.00"), ""),
        ]
    )

    with pytest.raises(ValueError, match="Duplicate MarketID_key in existing dataset"):
        build_sync_plan(df_new, df_existing)


def test_build_sync_plan_canonical_marketid_in_updates() -> None:
    df_new = _make_df(
        [
            ("user1", 101.0, Decimal("2.00"), ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", "101", Decimal("1.00"), ""),
        ]
    )

    plan = build_sync_plan(df_new, df_existing)

    assert plan.update_count == 1
    assert plan.rows_to_update[0][2] == "101"


def test_build_sync_plan_canonical_marketid_in_inserts() -> None:
    df_new = _make_df(
        [
            ("user1", 1001.0, Decimal("3.00"), ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", 1002, Decimal("4.00"), ""),
        ]
    )

    plan = build_sync_plan(df_new, df_existing)

    assert plan.new_count == 1
    assert plan.rows_to_insert[0][1] == "1001"


def test_build_sync_plan_blank_marketid_excluded() -> None:
    df_new = _make_df(
        [
            ("user1", "101", Decimal("1.00"), ""),
            ("user1", None, Decimal("2.00"), ""),
        ]
    )
    df_existing = pd.DataFrame(columns=["UserID", "MarketID", "Profit", "Notes"])

    plan = build_sync_plan(df_new, df_existing)

    assert plan.new_count == 1
    assert len(plan.rows_to_insert) == 1
    assert plan.rows_to_insert[0][1] == "101"
    assert len(plan.rows_to_update) == 0


def test_build_sync_plan_decimal_nan_profit_handling() -> None:
    df_new = _make_df(
        [
            ("user1", 1101, Decimal("NaN"), ""),
        ]
    )
    df_existing = _make_df(
        [
            ("user1", 1101, Decimal("1.00"), ""),
        ]
    )

    plan = build_sync_plan(df_new, df_existing)

    assert plan.update_count == 1
    assert plan.unchanged_count == 0

    df_new_both_nan = _make_df(
        [
            ("user1", 1102, Decimal("NaN"), ""),
        ]
    )
    df_existing_both_nan = _make_df(
        [
            ("user1", 1102, Decimal("NaN"), ""),
        ]
    )

    plan_both_nan = build_sync_plan(df_new_both_nan, df_existing_both_nan)

    assert plan_both_nan.update_count == 0
    assert plan_both_nan.unchanged_count == 1
