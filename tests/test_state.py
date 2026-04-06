from pathlib import Path

from betfair_results_downloader.state import load_run_state, save_run_state


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "run_state.json"
    state = {"last_success_utc": "2026-01-26T00:00:00Z", "rows_written": 123}

    save_run_state(path, state)
    loaded = load_run_state(path)

    assert loaded == state
