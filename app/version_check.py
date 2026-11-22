from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from app import __version__
from app.agy_runner import extract_semver, is_version_up_to_date
from app.config import Settings


def _with_cache_buster(url: str) -> str:
    """Přidá timestamp pro obejití 5minutové CDN cache na `raw.githubusercontent.com`."""
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != "raw.githubusercontent.com":
        return url
    sep = "&" if parsed.query else "?"
    return f"{url}{sep}_={int(time.time())}"


def fetch_latest_app_version_sync(url: str, timeout: float = 3.0) -> str | None:
    """Synchronně načte nejnovější číslo verze asistenta ze vzdáleného souboru `VERSION`."""
    cleaned = (url or "").strip()
    if not cleaned or not cleaned.startswith(("https://", "http://")):
        return None
    req_url = _with_cache_buster(cleaned)
    req = urllib.request.Request(
        req_url,
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
        """Vrátí nejnovější dostupnou verzi aplikace (s cache 2 min, u chyby 60 s)."""
        url = (self.settings.app_version_check_url or "").strip()
        if not url:
            return None
        now = time.monotonic()
        if not force_refresh and self._cache is not None:
            cached_at, cached_ver = self._cache
            ttl = 120.0 if cached_ver is not None else 60.0
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


def is_valid_update_url(url: str) -> bool:
    """Ověří, že URL aktualizačního webhooku používá HTTP/HTTPS a má platného hostitele."""
    cleaned = (url or "").strip()
    if not cleaned:
        return False
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


@dataclass(frozen=True)
class AppUpdateTriggerResult:
    """Výsledek pokusu o spuštění aktualizace kontejneru přes externí sidecar / webhook."""

    triggered: bool
    status_code: int | None
    message: str
    already_in_progress: bool = False
    async_mode: bool = True


def trigger_app_update_sync(
    url: str,
    token: str = "",
    timeout: float = 15.0,
) -> AppUpdateTriggerResult:
    """Synchronně odešle POST požadavek na aktualizační sidecar (např. Watchtower HTTP API)."""
    cleaned_url = (url or "").strip()
    if not is_valid_update_url(cleaned_url):
        return AppUpdateTriggerResult(
            triggered=False,
            status_code=None,
            message="Neplatná nebo chybějící adresa aktualizační služby (APP_UPDATE_URL).",
        )

    headers: dict[str, str] = {
        "User-Agent": f"asistent-antigravity/{__version__}",
        "Accept": "application/json, text/plain, */*",
    }
    cleaned_token = (token or "").strip()
    if cleaned_token:
        headers["Authorization"] = f"Bearer {cleaned_token}"

    req = urllib.request.Request(cleaned_url, data=b"", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(getattr(resp, "status", None) or resp.getcode() or 200)
            raw_body = resp.read(2048).decode("utf-8", errors="replace").strip()
        if code in {200, 201, 202, 204}:
            async_mode = code == 202
            msg = (
                "Aktualizace kontejneru byla spuštěna na pozadí."
                if async_mode
                else "Požadavek na aktualizaci byl přijat."
            )
            if raw_body.startswith("{"):
                try:
                    parsed = json.loads(raw_body)
                    if isinstance(parsed, dict) and isinstance(parsed.get("summary"), dict):
                        summary = parsed["summary"]
                        updated = int(summary.get("updated") or 0)
                        failed = int(summary.get("failed") or 0)
                        if failed > 0:
                            return AppUpdateTriggerResult(
                                triggered=False,
                                status_code=code,
                                message=f"Aktualizace selhala u {failed} kontejneru.",
                                async_mode=async_mode,
                            )
                        msg = (
                            f"Aktualizace dokončena (aktualizováno kontejnerů: {updated})."
                            if not async_mode
                            else msg
                        )
                except (ValueError, TypeError):
                    pass
            return AppUpdateTriggerResult(
                triggered=True,
                status_code=code,
                message=msg,
                async_mode=async_mode,
            )
        return AppUpdateTriggerResult(
            triggered=False,
            status_code=code,
            message=f"Aktualizační služba vrátila neočekávaný stav HTTP {code}.",
        )
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        raw_err = ""
        try:
            raw_err = exc.read(512).decode("utf-8", errors="replace").strip()
        except Exception:
            raw_err = ""
        if code in {401, 403}:
            return AppUpdateTriggerResult(
                triggered=False,
                status_code=code,
                message="Aktualizační služba odmítla autorizaci (zkontrolujte APP_UPDATE_TOKEN).",
            )
        if code == 429:
            return AppUpdateTriggerResult(
                triggered=False,
                status_code=code,
                message="Aktualizace kontejneru již probíhá.",
                already_in_progress=True,
            )
        snippet = " ".join(raw_err.split())[:200]
        detail = f"Updater vrátil chybu HTTP {code}: {snippet}" if snippet else f"Updater vrátil chybu HTTP {code}."
        return AppUpdateTriggerResult(
            triggered=False,
            status_code=code,
            message=detail,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        snippet = " ".join(str(reason).split())[:200]
        return AppUpdateTriggerResult(
            triggered=False,
            status_code=None,
            message=(
                f"Aktualizační sidecar updater není dostupný: {snippet}"
                if snippet
                else "Aktualizační sidecar updater není dostupný."
            ),
        )


class AppUpdater:
    """Bezpečný spouštěč aktualizace kontejneru přes Watchtower HTTP API sidecar."""

    def __init__(
        self,
        settings: Settings,
        *,
        trigger_fn: Callable[[str, str], AppUpdateTriggerResult] | None = None,
        cooldown_seconds: float = 15.0,
    ) -> None:
        self.settings = settings
        self._trigger_fn = trigger_fn or trigger_app_update_sync
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._lock = asyncio.Lock()
        self._last_triggered_at: float | None = None

    @property
    def is_configured(self) -> bool:
        """Vrací True, pokud je nakonfigurována platná adresa sidecar updateru."""
        return is_valid_update_url(self.settings.app_update_url)

    async def trigger_update(self) -> AppUpdateTriggerResult:
        """Spustí aktualizaci přes nakonfigurovaný webhook s ochranou proti souběžnému volání."""
        async with self._lock:
            if not self.is_configured:
                return AppUpdateTriggerResult(
                    triggered=False,
                    status_code=None,
                    message="Automatická aktualizace z Web UI není nakonfigurována.",
                )
            now = time.monotonic()
            if (
                self._last_triggered_at is not None
                and (now - self._last_triggered_at) < self._cooldown_seconds
            ):
                return AppUpdateTriggerResult(
                    triggered=False,
                    status_code=409,
                    message="Aktualizace kontejneru již byla spuštěna, vyčkejte na restart.",
                    already_in_progress=True,
                )
            result = await asyncio.to_thread(
                self._trigger_fn,
                self.settings.app_update_url.strip(),
                self.settings.app_update_token.strip(),
            )
            if result.triggered:
                self._last_triggered_at = time.monotonic()
            return result
