from pathlib import Path

from betfair_results_downloader.downloader_core import resolve_enrichment_cache_dir


def test_cache_location_independent_of_cwd(tmp_path: Path, monkeypatch):
    """
    Enrichment cache location should not depend on current working directory.

    The cache should be under <results_csv_dir>/.cache regardless of where
    the application is launched from.
    """
    # Create a results directory
    results_dir = tmp_path / "my_results"
    results_dir.mkdir()

    # Compute cache directory
    cache_dir_1 = resolve_enrichment_cache_dir(results_dir)

    # Change current working directory to something completely different
    other_dir = tmp_path / "other_location"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)

    # Compute cache directory again with same results_dir
    cache_dir_2 = resolve_enrichment_cache_dir(results_dir)

    # Cache location should be identical regardless of cwd
    assert cache_dir_1 == cache_dir_2
    assert cache_dir_1 == results_dir / ".cache"

    # Verify it does NOT depend on cwd
    assert cache_dir_1 != Path.cwd() / "outputs"
    assert cache_dir_1 != Path.cwd() / ".cache"
