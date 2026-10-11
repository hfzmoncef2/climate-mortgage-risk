"""Dependency-free cached downloader with atomic writes."""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


def download(url: str, dest: Path, refresh: bool = False, label: str | None = None) -> Path:
    """Download to a temporary file, then replace the destination; reuse cached files."""
    if dest.exists() and not refresh:
        log.info("Using cached %s", dest.name)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "climrisk-research/0.1"})
    log.info("Downloading %s", label or dest.name)
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(tmp, "wb") as f:
            while chunk := response.read(1 << 20):
                f.write(chunk)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("Saved %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest
