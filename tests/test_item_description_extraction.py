"""Tests for _extract_item_description_fields and enrichment coalescing."""

from __future__ import annotations

import copy

import pandas as pd
import pytest

from betfair_results_downloader.downloader_core import _extract_item_description_fields


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_order(*, with_desc: bool = True, partial: bool = False) -> dict:
    """Return a sample cleared-order dict with or without itemDescription."""
    order: dict = {
        "betId": "12345",
        "marketId": "1.234567890",
        "selectionId": 9876,
        "profit": 10.50,
    }
    if with_desc:
        desc: dict = {
            "eventDesc": "Kempton 17th Jan",
            "marketDesc": "2m Hrd",
            "runnerDesc": "Horse Name",
            "marketType": "WIN",
        }
        if not partial:
            desc["eachWayDivisor"] = 5.0
            desc["countryCode"] = "GB"
        order["itemDescription"] = desc
    return order


# ---------------------------------------------------------------------------
# _extract_item_description_fields
# ---------------------------------------------------------------------------

class TestExtractItemDescriptionFields:
    """Unit tests for the extraction helper."""

    def test_extracts_all_fields(self) -> None:
        order = _make_order()
        result = _extract_item_description_fields(order)

        assert result["evt_eventName"] == "Kempton 17th Jan"
        assert result["mkt_marketName"] == "2m Hrd"
        assert result["runner_name"] == "Horse Name"
        assert result["market_type"] == "WIN"
        assert result["each_way_divisor"] == 5.0
        assert result["evt_countryCode"] == "GB"

    def test_removes_raw_item_description(self) -> None:
        order = _make_order()
        result = _extract_item_description_fields(order)

        assert "itemDescription" not in result

    def test_handles_missing_item_description(self) -> None:
        order = _make_order(with_desc=False)
        original = copy.deepcopy(order)
        result = _extract_item_description_fields(order)

        # Should return the dict unchanged
        assert result == original
        assert "evt_eventName" not in result

    def test_handles_partial_item_description(self) -> None:
        order = _make_order(partial=True)
        result = _extract_item_description_fields(order)

        # Required fields present
        assert result["evt_eventName"] == "Kempton 17th Jan"
        assert result["mkt_marketName"] == "2m Hrd"
        assert result["runner_name"] == "Horse Name"
        assert result["market_type"] == "WIN"
        # Optional fields absent
        assert "each_way_divisor" not in result
        assert "evt_countryCode" not in result
        # Raw blob removed
        assert "itemDescription" not in result

    def test_handles_none_item_description(self) -> None:
        order = _make_order(with_desc=False)
        order["itemDescription"] = None
        result = _extract_item_description_fields(order)

        assert "itemDescription" not in result
        assert "evt_eventName" not in result

    def test_returns_same_dict_object(self) -> None:
        """The function mutates and returns the *same* dict, not a copy."""
        order = _make_order()
        result = _extract_item_description_fields(order)
        assert result is order


# ---------------------------------------------------------------------------
# Enrichment coalescing
# ---------------------------------------------------------------------------

class TestEnrichmentCoalescing:
    """Verify that itemDescription values take precedence over catalogue."""

    def test_item_description_wins_over_catalogue(self) -> None:
        """If both sources supply evt_eventName, keep itemDescription's value."""
        df_work = pd.DataFrame(
            {
                "marketId": ["1.111", "1.222"],
                "evt_eventName": ["From ItemDesc", None],
                "profit": [10.0, 20.0],
            }
        )
        df_catalogue = pd.DataFrame(
            {
                "marketId": ["1.111", "1.222"],
                "evt_eventName": ["From Catalogue", "From Catalogue Only"],
            }
        )

        overlap_cols = [
            c for c in df_catalogue.columns if c != "marketId" and c in df_work.columns
        ]
        df_out = df_work.merge(
            df_catalogue, on="marketId", how="left", suffixes=("", "_cat")
        )
        for col in overlap_cols:
            cat_col = f"{col}_cat"
            if cat_col in df_out.columns:
                df_out[col] = df_out[col].fillna(df_out[cat_col])
                df_out.drop(columns=[cat_col], inplace=True)

        # itemDescription value kept for market 1.111
        assert df_out.loc[df_out["marketId"] == "1.111", "evt_eventName"].iloc[0] == "From ItemDesc"
        # catalogue value used as fallback for market 1.222
        assert df_out.loc[df_out["marketId"] == "1.222", "evt_eventName"].iloc[0] == "From Catalogue Only"
        # No leftover _cat column
        assert "evt_eventName_cat" not in df_out.columns
