from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.i18n import resolve_language


ContextMode = Literal["auto", "pracovni", "osobni"]
ChatChannel = Literal["auto", "pracovni", "osobni"]


def normalize_language(raw: str | None) -> str:
    """Převede kód nebo název jazyka na dostupný kód (`en`, `cs`, `de`, `sk`, `es`, `pl`, ...)."""
    return resolve_language(raw)


def normalize_chat_channel(mode: str | None) -> ChatChannel:
    """Převede kontextový režim na kanál chatu (neznámé hodnoty → `auto`)."""
    cleaned = (mode or "auto").strip().lower()
    if cleaned in ("pracovni", "prace", "firemni", "work", "business", "company"):
        return "pracovni"
    if cleaned in ("osobni", "soukrome", "personal", "private"):
        return "osobni"
    return "auto"


def parse_telegram_ids(raw: str) -> set[int]:
    """Vrátí množinu číselných Telegram ID z čárkou odděleného řetězce."""
    ids: set[int] = set()
    for part in raw.split(","):
        item = part.strip()
        if item.isdigit():
            ids.add(int(item))
    return ids


def resolve_notify_chat_id(notify_raw: str, allowed_raw: str) -> int | None:
    """Vrátí cílové Telegram chat ID (případně první povolené ID)."""
    raw = notify_raw.strip()
    if raw.lstrip("-").isdigit() and raw not in {"", "-"}:
        return int(raw)
    allowed = sorted(parse_telegram_ids(allowed_raw))
    return allowed[0] if allowed else None


class Settings(BaseSettings):
    """Konfigurace single-user asistenta načítaná z proměnných prostředí / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Identita a cesty
    assistant_name: str = Field(
        default="Assistant",
        validation_alias=AliasChoices("assistant_name", "jmeno", "name"),
    )
    language: str = Field(
        default="en",
        validation_alias=AliasChoices("language", "app_language", "jazyk"),
    )
    workspace_dir: Path = Path("/home/asistent/workspace")
    data_dir: Path = Path("/home/asistent/data")
    agy_home_dir: Path | None = None
    template_dir: Path = Path("/app/workspace_template")
    template_dir_cs: Path = Path("/app/workspace_template_cs")

    # Antigravity CLI (agy)
    agy_bin: str = "agy"
    agy_mode: str = "auto"
    agy_model: str = ""
    agy_dangerously_skip_permissions: bool = True
    agy_timeout_seconds: int = 300
    agy_persistent_session: bool = True
    gemini_api_key: str = ""

    # Telegram (podporuje jak TELEGRAM_*, tak krátké TG_TOKEN / TG_ID)
    telegram_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices("telegram_bot_token", "tg_token"),
    )
    telegram_allowed_users: str = Field(
        default="",
        validation_alias=AliasChoices("telegram_allowed_users", "tg_id"),
    )
    telegram_notify_chat_id: str = ""

    # Web UI & API
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_auth_token: str = ""

    # Plánovač
    tz: str = "Europe/Prague"
    heartbeat_enabled: bool = True
    heartbeat_interval_minutes: int = 60
    morning_briefing_enabled: bool = True
    morning_briefing_cron: str = "0 8 * * *"
    nightly_reset_enabled: bool = True
    nightly_reset_cron: str = "15 0 * * *"

    # Výchozí kontext (auto | pracovni | osobni)
    default_context_mode: ContextMode = "auto"

    @field_validator("language", mode="before")
    @classmethod
    def _validate_language(cls, value: object) -> str:
        return normalize_language(None if value is None else str(value))

    @model_validator(mode="after")
    def _resolve_derived_dirs(self) -> Settings:
        """Upraví odvozené cesty a výchozí hodnoty při prázdném `JMENO`."""
        if not self.assistant_name.strip():
            self.assistant_name = "Assistant"
        self.language = normalize_language(self.language)
        if "agy_home_dir" not in self.model_fields_set and self.agy_home_dir is None:
            if self.data_dir != Path("/home/asistent/data"):
                self.agy_home_dir = self.data_dir.parent / "home"
            else:
                self.agy_home_dir = Path("/home/asistent")
        if "template_dir" not in self.model_fields_set and not self.template_dir.is_dir():
            local_tpl = Path(__file__).resolve().parent.parent / "workspace_template"
            if local_tpl.is_dir():
                self.template_dir = local_tpl
        if "template_dir_cs" not in self.model_fields_set and not self.template_dir_cs.is_dir():
            local_tpl_cs = Path(__file__).resolve().parent.parent / "workspace_template_cs"
            if local_tpl_cs.is_dir():
                self.template_dir_cs = local_tpl_cs
        return self

    def template_dir_for_language(self, lang: str | None = None) -> Path:
        """Vrátí cestu k výchozím šablonám podle zvoleného jazyka (`workspace_template_<lang>`, výchozí EN)."""
        resolved = normalize_language(lang or self.language)
        if resolved == "en":
            return self.template_dir
        if resolved == "cs" and self.template_dir_cs.is_dir():
            return self.template_dir_cs
        localized = self.template_dir.parent / f"{self.template_dir.name}_{resolved}"
        if localized.is_dir():
            return localized
        return self.template_dir

    @property
    def effective_home_dir(self) -> Path:
        """Vrátí aktivní HOME adresář pro `agy` (`.gemini/`)."""
        return self.agy_home_dir if self.agy_home_dir is not None else Path.home()

    @property
    def allowed_telegram_user_ids(self) -> set[int]:
        """Vrátí množinu povolených Telegram user ID."""
        return parse_telegram_ids(self.telegram_allowed_users)

    @property
    def notify_chat_id(self) -> int | None:
        """Vrátí cílové chat ID pro proaktivní upozornění (briefing/heartbeat)."""
        return resolve_notify_chat_id(self.telegram_notify_chat_id, self.telegram_allowed_users)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"

    @property
    def runtime_config_path(self) -> Path:
        return self.data_dir / "runtime_config.json"

    def subprocess_env(
        self,
        *,
        gemini_api_key: str | None = None,
        home_dir: Path | None = None,
    ) -> dict[str, str]:
        """Vrátí prostředí pro spouštění `agy`."""
        env = os.environ.copy()
        if gemini_api_key is not None:
            cleaned_key = gemini_api_key.strip()
            if cleaned_key:
                env["GEMINI_API_KEY"] = cleaned_key
            else:
                env.pop("GEMINI_API_KEY", None)
        elif self.gemini_api_key.strip():
            env["GEMINI_API_KEY"] = self.gemini_api_key.strip()
        target_home = home_dir or self.effective_home_dir
        env["HOME"] = str(target_home)
        env["XDG_CONFIG_HOME"] = str(target_home / ".config")
        env["XDG_DATA_HOME"] = str(target_home / ".local" / "share")
        env["XDG_CACHE_HOME"] = str(target_home / ".cache")
        return env
