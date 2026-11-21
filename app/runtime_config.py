from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

OVERRIDE_FIELDS = (
    "gemini_api_key",
    "telegram_bot_token",
    "telegram_allowed_users",
    "web_auth_token",
)


def load_runtime_overrides(path: Path) -> dict[str, str]:
    """Načte uživatelská nastavení uložená přes Web UI."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Nepodařilo se načíst runtime_config.json: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items() if k in OVERRIDE_FIELDS and isinstance(v, str)}


def apply_runtime_overrides(settings: Settings) -> None:
    """Aplikuje uložená runtime nastavení na objekt Settings."""
    overrides = load_runtime_overrides(settings.runtime_config_path)
    for key, value in overrides.items():
        if value.strip():
            setattr(settings, key, value.strip())
    sync_agy_settings_file(settings)


def save_runtime_overrides(settings: Settings, updates: dict[str, str | None]) -> None:
    """Uloží a aplikuje vybrané změny konfigurace z Web UI."""
    current = load_runtime_overrides(settings.runtime_config_path)
    for key in OVERRIDE_FIELDS:
        if key not in updates:
            continue
        val = updates[key]
        if val is None:
            continue
        cleaned = val.strip()
        if cleaned:
            current[key] = cleaned
            setattr(settings, key, cleaned)
        else:
            current.pop(key, None)
            setattr(settings, key, "")

    settings.runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
    settings.runtime_config_path.write_text(
        json.dumps(current, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    sync_agy_settings_file(settings)


def sync_agy_settings_file(
    settings: Settings,
    home_dir: Path | None = None,
    model: str | None = None,
    gemini_api_key: str | None = None,
    managed_deny_rules: list[str] | None = None,
    managed_deny_prefixes: tuple[str, ...] = (),
) -> None:
    """Sladí `~/.gemini/antigravity-cli/settings.json` podle nastavení klíče, modelu a oprávnění."""
    base_home = home_dir or Path.home()
    agy_settings = base_home / ".gemini" / "antigravity-cli" / "settings.json"
    agy_settings.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if agy_settings.is_file():
        try:
            loaded = json.loads(agy_settings.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}

    effective_key = (
        gemini_api_key.strip() if gemini_api_key is not None else settings.gemini_api_key.strip()
    )
    if effective_key:
        data["modelProvider"] = "gemini"
    else:
        data.pop("modelProvider", None)
    data.setdefault("agentMode", settings.agy_mode or "auto")

    effective_model = (model if model is not None else settings.agy_model).strip()
    if effective_model:
        data["model"] = effective_model
    elif model is not None:
        data.pop("model", None)

    if managed_deny_rules is not None:
        raw_perms = data.get("permissions")
        perms: dict[str, Any] = dict(raw_perms) if isinstance(raw_perms, dict) else {}
        raw_deny = perms.get("deny")
        preserved = [
            str(entry)
            for entry in (raw_deny if isinstance(raw_deny, list) else [])
            if isinstance(entry, str)
            and not any(entry.startswith(prefix) for prefix in managed_deny_prefixes)
        ]
        merged_deny = list(dict.fromkeys([*preserved, *managed_deny_rules]))
        if merged_deny:
            perms["deny"] = merged_deny
        else:
            perms.pop("deny", None)
        if perms:
            data["permissions"] = perms
        else:
            data.pop("permissions", None)

    serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        agy_settings.write_text(serialized, encoding="utf-8")
    except PermissionError:
        agy_settings.unlink(missing_ok=True)
        agy_settings.write_text(serialized, encoding="utf-8")
