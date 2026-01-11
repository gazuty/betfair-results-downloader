from __future__ import annotations

from typing import Any, Callable, Optional

from .config import DownloaderConfig
from .pipeline import run_pipeline
from .secrets import validate_credentials


def run_downloader(
    config: DownloaderConfig,
    creds: dict[str, Any],
    *,
    status_cb: Optional[Callable[[str], None]] = None,
    confirm_publish_cb: Optional[Callable[[dict[str, Any]], bool]] = None,
) -> dict[str, Any]:
    """
    GUI/CLI entrypoint:
    - validates config + creds
    - delegates to pipeline
    """
    config.validate()

    v = validate_credentials(creds)
    if not v.ok:
        raise ValueError("Invalid credentials:\n- " + "\n- ".join(v.errors))

    return run_pipeline(
        config=config,
        creds=creds,
        status_cb=status_cb,
        confirm_publish_cb=confirm_publish_cb,
    )
