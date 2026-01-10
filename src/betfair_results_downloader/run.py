from __future__ import annotations

from typing import Any

from .config import DownloaderConfig
from .pipeline import run_pipeline
from .secrets import validate_credentials


def run_downloader(config: DownloaderConfig, creds: dict[str, Any]) -> dict[str, Any]:
    """
    GUI/CLI entrypoint:
    - validates config + creds
    - delegates to pipeline
    """
    config.validate()

    v = validate_credentials(creds)
    if not v.ok:
        raise ValueError("Invalid credentials:\n- " + "\n- ".join(v.errors))

    return run_pipeline(config=config, creds=creds)
