from __future__ import annotations

import asyncio
import time
import urllib.request
from typing import Any, Callable

from app import __version__
from app.agy_runner import extract_semver, is_version_up_to_date
from app.config import Settings


def fetch_latest_app_version_sync(url: str, timeout: float = 3.0) -> str | None:
    """Synchronně načte nejnovější číslo verze asistenta ze vzdáleného souboru `VERSION`."""
    cleaned = (url or "").strip()
    if not cleaned or not cleaned.startswith(("https://", "http://")):
        return None
    req = urllib.request.Request(
        cleaned,
        headers={
            "User-Agent": f"asistent-antigravity/{__version__}",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(256).decode("utf-8", errors="replace").strip()
        sem = extract_semver(raw)
        if sem is not None:
            return f"{sem[0]}.{sem[1]}.{sem[2]}"
    except Exception:
        return None
    return None


class AppVersionChecker:
    """Kontrola dostupnosti nové verze aplikace Asistent Antigravity s TTL cache."""

    def __init__(
        self,
        settings: Settings,
        *,
        current_version: str | None = None,
        fetcher: Callable[[str], str | None] | None = None,
    ) -> None:
        self.settings = settings
        self.current_version = (current_version or __version__).strip()
        self._fetcher = fetcher or fetch_latest_app_version_sync
        self._cache: tuple[float, str | None] | None = None

    async def check_latest_version(self, *, force_refresh: bool = False) -> str | None:
        """Vrátí nejnovější dostupnou verzi aplikace (s cache 15 min, u chyby 60 s)."""
        url = (self.settings.app_version_check_url or "").strip()
        if not url:
            return None
        now = time.monotonic()
        if not force_refresh and self._cache is not None:
            cached_at, cached_ver = self._cache
            ttl = 900.0 if cached_ver is not None else 60.0
            if (now - cached_at) < ttl:
                return cached_ver
        latest = await asyncio.to_thread(self._fetcher, url)
        self._cache = (time.monotonic(), latest)
        return latest

    async def get_version_info(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Vrátí kompletní stav aktuální a nejnovější verze asistenta."""
        latest = await self.check_latest_version(force_refresh=force_refresh)
        up_to_date = is_version_up_to_date(self.current_version, latest)
        return {
            "app_version": self.current_version,
            "app_latest_version": latest,
            "app_up_to_date": up_to_date,
            "app_update_available": up_to_date is False,
        }
