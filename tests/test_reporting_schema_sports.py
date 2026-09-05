from __future__ import annotations

import pandas as pd

from betfair_results_downloader.reporting.schema import (
    normalize_cleared_orders_schema,
    sport_label,
)


def test_sport_label_known_ids():
    assert sport_label(7) == "Horses"
    assert sport_label(4339) == "Greyhounds"
    assert sport_label(1) == "Soccer"
    assert sport_label(2) == "Tennis"
    assert sport_label(61420) == "Australian Rules"
    assert sport_label(2378961) == "Politics"


def test_sport_label_unknown_numeric_id():
    assert sport_label(12345) == "Other (12345)"


def test_sport_label_unparseable_values():
    assert sport_label(None) == "Unknown"
    assert sport_label(float("nan")) == "Unknown"
    assert sport_label("abc") == "Unknown"


def test_normalize_cleared_orders_schema_sport_column_agrees_across_dtypes():
    int_df = pd.DataFrame({"eventTypeId": [7, 1]})
    str_df = pd.DataFrame({"eventTypeId": ["7", "1"]})
    float_df = pd.DataFrame({"eventTypeId": [7.0, 1.0]})

    int_sport = normalize_cleared_orders_schema(int_df)["sport"].tolist()
    str_sport = normalize_cleared_orders_schema(str_df)["sport"].tolist()
    float_sport = normalize_cleared_orders_schema(float_df)["sport"].tolist()

    assert int_sport == ["Horses", "Soccer"]
    assert str_sport == int_sport
    assert float_sport == int_sport


def test_normalize_cleared_orders_schema_without_event_type_id_column():
    df = pd.DataFrame({"profit": [1.0, -2.0, 0.0]})

    out = normalize_cleared_orders_schema(df)

    assert out["sport"].tolist() == ["Unknown", "Unknown", "Unknown"]
