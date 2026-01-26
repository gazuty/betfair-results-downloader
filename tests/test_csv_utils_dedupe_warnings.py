import pandas as pd

from betfair_results_downloader.csv_utils import clean_and_remove_duplicates


def test_warning_when_all_betids_invalid():
    """
    Should warn when all betId values are invalid/non-numeric.
    """
    df = pd.DataFrame({
        "betId": ["invalid1", "invalid2", "invalid3"],
        "profit": [10.0, 20.0, 30.0],
    })

    messages = []

    def status_cb(msg: str):
        messages.append(msg)

    result = clean_and_remove_duplicates(df, status_cb=status_cb)

    # Should have warned about all invalid betIds
    assert len(messages) == 1
    assert "All 3 betId values invalid" in messages[0]
    assert "falling back to full-row dedupe" in messages[0]

    # Behavior unchanged: 3 unique rows remain
    assert len(result) == 3


def test_warning_when_some_betids_invalid():
    """
    Should warn when some betId values are invalid.
    """
    df = pd.DataFrame({
        "betId": ["123", "456", "invalid", "789", "bad"],
        "profit": [10.0, 20.0, 30.0, 40.0, 50.0],
    })

    messages = []

    def status_cb(msg: str):
        messages.append(msg)

    result = clean_and_remove_duplicates(df, status_cb=status_cb)

    # Should warn about invalid betIds
    assert len(messages) == 1
    assert "2 of 5 rows have invalid betId values" in messages[0]

    # Behavior: dedupes on valid betIds, NaN betIds are treated as duplicates of each other
    # Input: 5 rows (3 valid betIds, 2 invalid→NaN)
    # Output: 4 rows (3 valid betIds, 1 NaN - last NaN kept)
    assert len(result) == 4


def test_warning_when_betid_column_missing():
    """
    Should warn when betId column is missing entirely.
    """
    df = pd.DataFrame({
        "profit": [10.0, 20.0, 30.0],
        "marketId": ["1.1", "1.2", "1.3"],
    })

    messages = []

    def status_cb(msg: str):
        messages.append(msg)

    result = clean_and_remove_duplicates(df, status_cb=status_cb)

    # Should warn about missing betId column
    assert len(messages) == 1
    assert "betId column missing" in messages[0]
    assert "full-row dedupe" in messages[0]

    # Behavior unchanged: 3 unique rows remain
    assert len(result) == 3


def test_no_warning_when_all_betids_valid():
    """
    Should NOT warn when all betId values are valid.
    """
    df = pd.DataFrame({
        "betId": ["123", "456", "789"],
        "profit": [10.0, 20.0, 30.0],
    })

    messages = []

    def status_cb(msg: str):
        messages.append(msg)

    result = clean_and_remove_duplicates(df, status_cb=status_cb)

    # Should NOT have any warnings
    assert len(messages) == 0

    # Behavior unchanged: 3 unique rows remain
    assert len(result) == 3


def test_dedupe_behavior_unchanged_with_duplicates():
    """
    Verify dedupe behavior is unchanged when there are actual duplicates.
    """
    df = pd.DataFrame({
        "betId": ["123", "456", "123"],  # Duplicate betId
        "profit": [10.0, 20.0, 30.0],  # Different profit
    })

    messages = []

    def status_cb(msg: str):
        messages.append(msg)

    result = clean_and_remove_duplicates(df, status_cb=status_cb)

    # No warnings (all betIds valid)
    assert len(messages) == 0

    # Behavior unchanged: dedupes to 2 rows (keeps last for betId 123)
    assert len(result) == 2
    # Verify we kept the last occurrence of betId 123 (profit=30.0)
    bet_123 = result[result["betId"] == 123]
    assert len(bet_123) == 1
    assert float(bet_123["profit"].iloc[0]) == 30.0
