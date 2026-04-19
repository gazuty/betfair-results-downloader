from betfair_results_downloader.run_logging import normalize_log_line


def test_normalize_log_line_replaces_ellipsis() -> None:
    assert normalize_log_line("Downloading…") == "Downloading..."
