from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from app.config import Settings, normalize_language, parse_telegram_ids, resolve_notify_chat_id
from app.i18n import t

OWNER_UPDATABLE_FIELDS = frozenset(
    {
        "username",
        "email",
        "display_name",
        "web_token",
        "assistant_name",
        "language",
        "tz",
        "default_context_mode",
        "telegram_bot_token",
        "telegram_allowed_users",
        "telegram_notify_chat_id",
        "heartbeat_enabled",
        "heartbeat_interval_minutes",
        "morning_briefing_enabled",
        "morning_briefing_cron",
        "nightly_reset_enabled",
        "nightly_reset_cron",
        "agy_model",
        "gemini_api_key",
        "password_hash",
        "totp_enabled",
        "totp_secret",
        "totp_pending_secret",
        "totp_recovery_hashes_json",
    }
)


@dataclass(frozen=True)
class OwnerConfig:
    """Konfigurace a účet jediného vlastníka této instance asistenta."""

    username: str
    display_name: str
    email: str
    web_token: str
    assistant_name: str
    language: str
    tz: str
    default_context_mode: str
    telegram_bot_token: str
    telegram_allowed_users: str
    telegram_notify_chat_id: str
    heartbeat_enabled: bool
    heartbeat_interval_minutes: int
    morning_briefing_enabled: bool
    morning_briefing_cron: str
    nightly_reset_enabled: bool
    nightly_reset_cron: str
    agy_model: str
    gemini_api_key: str
    password_hash: str
    totp_enabled: bool
    totp_secret: str
    totp_pending_secret: str
    totp_recovery_hashes_json: str
    created_at: str

    @property
    def allowed_telegram_user_ids(self) -> set[int]:
        return parse_telegram_ids(self.telegram_allowed_users)

    @property
    def notify_chat_id(self) -> int | None:
        return resolve_notify_chat_id(self.telegram_notify_chat_id, self.telegram_allowed_users)


@dataclass(frozen=True)
class McpServerRecord:
    """Konfigurace jednoho MCP serveru."""

    id: int
    name: str
    scope: str
    transport: str
    url: str
    auth_token: str
    headers_json: str
    command: str
    args_json: str
    env_json: str
    enabled: bool
    created_at: str


@dataclass(frozen=True)
class PluginConfigRecord:
    """Konfigurace oprávnění a stavu připojení jednoho pluginu `agy`."""

    plugin_name: str
    permissions_json: str
    connected: bool
    connected_at: str
    updated_at: str
    account_label: str = ""


@dataclass(frozen=True)
class RunRecord:
    """Záznam o jednom spuštění asistenta."""

    id: int
    created_at: str
    source: str
    session_key: str
    context_mode: str
    prompt: str
    response: str
    conversation_id: str | None
    duration_ms: int
    success: bool
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tokens_estimated: bool = False


@dataclass(frozen=True)
class ActivityStats:
    """Souhrn aktivity a spotřeby tokenů."""

    total_runs: int
    success_runs: int
    web_runs: int
    telegram_runs: int
    cron_runs: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    exact_runs: int
    estimated_runs: int
    total_duration_ms: int
    last_active_at: str | None


@dataclass(frozen=True)
class ScheduledTaskRecord:
    """Vlastní naplánovaná úloha nebo připomínka."""

    id: int
    name: str
    prompt: str
    schedule_type: str  # 'cron' | 'once'
    cron_expr: str
    run_at: str
    action_type: str  # 'message' | 'agent'
    enabled: bool
    last_run_at: str | None
    created_at: str


class AssistantDB:
    """Čistá single-user SQLite perzistence (vč. jednorázové migrace ze starší multi-user DB)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            return True
        return False

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    username TEXT NOT NULL DEFAULT 'admin',
                    email TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT 'Administrator',
                    web_token TEXT NOT NULL DEFAULT '',
                    assistant_name TEXT NOT NULL DEFAULT 'Assistant',
                    language TEXT NOT NULL DEFAULT 'en',
                    tz TEXT NOT NULL DEFAULT 'Europe/Prague',
                    default_context_mode TEXT NOT NULL DEFAULT 'auto',
                    telegram_bot_token TEXT NOT NULL DEFAULT '',
                    telegram_allowed_users TEXT NOT NULL DEFAULT '',
                    telegram_notify_chat_id TEXT NOT NULL DEFAULT '',
                    heartbeat_enabled INTEGER NOT NULL DEFAULT 1,
                    heartbeat_interval_minutes INTEGER NOT NULL DEFAULT 60,
                    morning_briefing_enabled INTEGER NOT NULL DEFAULT 1,
                    morning_briefing_cron TEXT NOT NULL DEFAULT '0 8 * * *',
                    nightly_reset_enabled INTEGER NOT NULL DEFAULT 1,
                    nightly_reset_cron TEXT NOT NULL DEFAULT '15 0 * * *',
                    agy_model TEXT NOT NULL DEFAULT '',
                    gemini_api_key TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL DEFAULT '',
                    totp_enabled INTEGER NOT NULL DEFAULT 0,
                    totp_secret TEXT NOT NULL DEFAULT '',
                    totp_pending_secret TEXT NOT NULL DEFAULT '',
                    totp_recovery_hashes_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL DEFAULT 'all',
                    transport TEXT NOT NULL DEFAULT 'http',
                    url TEXT NOT NULL DEFAULT '',
                    auth_token TEXT NOT NULL DEFAULT '',
                    headers_json TEXT NOT NULL DEFAULT '{}',
                    command TEXT NOT NULL DEFAULT '',
                    args_json TEXT NOT NULL DEFAULT '[]',
                    env_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_key TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    context_mode TEXT NOT NULL DEFAULT 'auto',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    context_mode TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    response TEXT NOT NULL,
                    conversation_id TEXT,
                    duration_ms INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    error TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    tokens_estimated INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    prompt TEXT NOT NULL,
                    schedule_type TEXT NOT NULL DEFAULT 'cron',
                    cron_expr TEXT NOT NULL DEFAULT '',
                    run_at TEXT NOT NULL DEFAULT '',
                    action_type TEXT NOT NULL DEFAULT 'message',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plugin_configs (
                    plugin_name TEXT PRIMARY KEY,
                    permissions_json TEXT NOT NULL DEFAULT '{}',
                    connected INTEGER NOT NULL DEFAULT 0,
                    connected_at TEXT NOT NULL DEFAULT '',
                    account_label TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "owner_config", "language", "TEXT NOT NULL DEFAULT 'en'")
            self._ensure_column(conn, "plugin_configs", "account_label", "TEXT NOT NULL DEFAULT ''")
            self._migrate_from_legacy_multiuser(conn)
            self._normalize_legacy_context_modes(conn)
            self._ensure_column(conn, "runs", "input_tokens", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "output_tokens", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "total_tokens", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "runs", "tokens_estimated", "INTEGER NOT NULL DEFAULT 0")

    def _migrate_from_legacy_multiuser(self, conn: sqlite3.Connection) -> None:
        """Jednorázově přenese data admina (id=1) ze starší multi-user databáze bez ztráty dat."""
        # 1) Přenos users(id=1) -> owner_config
        has_owner = conn.execute("SELECT 1 FROM owner_config WHERE id = 1").fetchone() is not None
        if not has_owner and self._table_exists(conn, "users"):
            row = conn.execute("SELECT * FROM users WHERE id = 1").fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM users ORDER BY id ASC LIMIT 1").fetchone()
            if row is not None:
                keys = set(row.keys())
                conn.execute(
                    """
                    INSERT OR IGNORE INTO owner_config (
                        id, username, email, display_name, web_token, assistant_name, language, tz,
                        default_context_mode, telegram_bot_token, telegram_allowed_users,
                        telegram_notify_chat_id, heartbeat_enabled, heartbeat_interval_minutes,
                        morning_briefing_enabled, morning_briefing_cron,
                        nightly_reset_enabled, nightly_reset_cron,
                        agy_model, gemini_api_key, password_hash, totp_enabled, totp_secret,
                        totp_pending_secret, totp_recovery_hashes_json, created_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["username"] if "username" in keys else "admin",
                        row["email"] if "email" in keys else "",
                        row["display_name"] if "display_name" in keys else "Administrator",
                        row["web_token"] if "web_token" in keys else "",
                        row["assistant_name"] if "assistant_name" in keys else "Assistant",
                        normalize_language(row["language"]) if "language" in keys else "en",
                        row["tz"] if "tz" in keys else "Europe/Prague",
                        row["default_context_mode"] if "default_context_mode" in keys else "auto",
                        row["telegram_bot_token"] if "telegram_bot_token" in keys else "",
                        row["telegram_allowed_users"] if "telegram_allowed_users" in keys else "",
                        row["telegram_notify_chat_id"] if "telegram_notify_chat_id" in keys else "",
                        int(row["heartbeat_enabled"]) if "heartbeat_enabled" in keys else 1,
                        int(row["heartbeat_interval_minutes"]) if "heartbeat_interval_minutes" in keys else 60,
                        int(row["morning_briefing_enabled"]) if "morning_briefing_enabled" in keys else 1,
                        row["morning_briefing_cron"] if "morning_briefing_cron" in keys else "0 8 * * *",
                        int(row["nightly_reset_enabled"]) if "nightly_reset_enabled" in keys else 1,
                        row["nightly_reset_cron"] if "nightly_reset_cron" in keys else "15 0 * * *",
                        row["agy_model"] if "agy_model" in keys else "",
                        row["gemini_api_key"] if "gemini_api_key" in keys else "",
                        row["password_hash"] if "password_hash" in keys else "",
                        int(row["totp_enabled"]) if "totp_enabled" in keys else 0,
                        row["totp_secret"] if "totp_secret" in keys else "",
                        row["totp_pending_secret"] if "totp_pending_secret" in keys else "",
                        row["totp_recovery_hashes_json"] if "totp_recovery_hashes_json" in keys else "[]",
                        row["created_at"] if "created_at" in keys else self._now_iso(),
                    ),
                )

        # 2) Přenos user_mcp_servers(user_id=1) -> mcp_servers
        if self._table_exists(conn, "user_mcp_servers"):
            existing_mcp = conn.execute("SELECT COUNT(*) AS cnt FROM mcp_servers").fetchone()["cnt"]
            if existing_mcp == 0:
                rows = conn.execute(
                    "SELECT * FROM user_mcp_servers WHERE user_id = 1 ORDER BY id ASC"
                ).fetchall()
                for r in rows:
                    rkeys = set(r.keys())
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO mcp_servers (
                            name, scope, transport, url, auth_token, headers_json,
                            command, args_json, env_json, enabled, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            r["name"],
                            r["scope"] if "scope" in rkeys else "all",
                            r["transport"],
                            r["url"],
                            r["auth_token"],
                            r["headers_json"],
                            r["command"],
                            r["args_json"],
                            r["env_json"],
                            int(r["enabled"]),
                            r["created_at"],
                        ),
                    )
            conn.execute("DROP TABLE IF EXISTS user_mcp_servers")

        # 3) Přestavba legacy tabulky scheduled_tasks pokud má NOT NULL user_id s FK na users
        if "user_id" in self._table_columns(conn, "scheduled_tasks"):
            old_tasks = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE user_id = 1 ORDER BY id ASC"
            ).fetchall()
            conn.execute("DROP TABLE scheduled_tasks")
            conn.execute(
                """
                CREATE TABLE scheduled_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    prompt TEXT NOT NULL,
                    schedule_type TEXT NOT NULL DEFAULT 'cron',
                    cron_expr TEXT NOT NULL DEFAULT '',
                    run_at TEXT NOT NULL DEFAULT '',
                    action_type TEXT NOT NULL DEFAULT 'message',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            for t in old_tasks:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO scheduled_tasks (
                        name, prompt, schedule_type, cron_expr, run_at,
                        action_type, enabled, last_run_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        t["name"],
                        t["prompt"],
                        t["schedule_type"],
                        t["cron_expr"],
                        t["run_at"],
                        t["action_type"],
                        int(t["enabled"]),
                        t["last_run_at"],
                        t["created_at"],
                    ),
                )

        # 4) Přestavba legacy auth_sessions pokud má FK na users
        if "user_id" in self._table_columns(conn, "auth_sessions"):
            old_auth = conn.execute(
                "SELECT token_hash, expires_at, created_at FROM auth_sessions WHERE user_id = 1"
            ).fetchall()
            conn.execute("DROP TABLE auth_sessions")
            conn.execute(
                """
                CREATE TABLE auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            for s in old_auth:
                conn.execute(
                    "INSERT OR IGNORE INTO auth_sessions (token_hash, expires_at, created_at) VALUES (?, ?, ?)",
                    (s["token_hash"], s["expires_at"], s["created_at"]),
                )

        # 5) Normalizace session_key (odstranění u:1: prefixu) a vyčištění cizích user_id
        if "user_id" in self._table_columns(conn, "sessions"):
            old_sess = conn.execute(
                "SELECT session_key, conversation_id, context_mode, updated_at FROM sessions WHERE user_id = 1"
            ).fetchall()
            conn.execute("DROP TABLE sessions")
            conn.execute(
                """
                CREATE TABLE sessions (
                    session_key TEXT PRIMARY KEY,
                    conversation_id TEXT,
                    context_mode TEXT NOT NULL DEFAULT 'auto',
                    updated_at TEXT NOT NULL
                )
                """
            )
            for s in old_sess:
                skey = str(s["session_key"])
                if skey.startswith("u:1:"):
                    skey = skey[4:]
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sessions (session_key, conversation_id, context_mode, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (skey, s["conversation_id"], s["context_mode"], s["updated_at"]),
                )

        if "user_id" in self._table_columns(conn, "runs"):
            conn.execute("DELETE FROM runs WHERE user_id != 1")

        # 6) Odstranění zastaralých multi-user tabulek
        conn.execute("DROP TABLE IF EXISTS invites")
        conn.execute("DROP TABLE IF EXISTS users")

    @staticmethod
    def _normalize_legacy_context_modes(conn: sqlite3.Connection) -> None:
        """Převede starší názvy kontextů na `auto` / `pracovni` / `osobni`."""
        for table, col in (
            ("sessions", "context_mode"),
            ("owner_config", "default_context_mode"),
        ):
            conn.execute(f"UPDATE {table} SET {col} = 'osobni' WHERE {col} = 'soukrome'")
            conn.execute(
                f"UPDATE {table} SET {col} = 'pracovni' "
                f"WHERE {col} NOT IN ('auto', 'pracovni', 'osobni')"
            )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_owner(row: sqlite3.Row) -> OwnerConfig:
        keys = set(row.keys())
        return OwnerConfig(
            username=row["username"],
            email=row["email"],
            display_name=row["display_name"],
            web_token=row["web_token"],
            assistant_name=row["assistant_name"],
            language=normalize_language(row["language"] if "language" in keys else "en"),
            tz=row["tz"],
            default_context_mode=row["default_context_mode"],
            telegram_bot_token=row["telegram_bot_token"],
            telegram_allowed_users=row["telegram_allowed_users"],
            telegram_notify_chat_id=row["telegram_notify_chat_id"],
            heartbeat_enabled=bool(row["heartbeat_enabled"]),
            heartbeat_interval_minutes=int(row["heartbeat_interval_minutes"]),
            morning_briefing_enabled=bool(row["morning_briefing_enabled"]),
            morning_briefing_cron=row["morning_briefing_cron"],
            nightly_reset_enabled=bool(row["nightly_reset_enabled"]),
            nightly_reset_cron=row["nightly_reset_cron"],
            agy_model=row["agy_model"],
            gemini_api_key=row["gemini_api_key"],
            password_hash=row["password_hash"],
            totp_enabled=bool(row["totp_enabled"]),
            totp_secret=row["totp_secret"],
            totp_pending_secret=row["totp_pending_secret"],
            totp_recovery_hashes_json=row["totp_recovery_hashes_json"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_mcp(row: sqlite3.Row) -> McpServerRecord:
        return McpServerRecord(
            id=int(row["id"]),
            name=row["name"],
            scope=row["scope"],
            transport=row["transport"],
            url=row["url"],
            auth_token=row["auth_token"],
            headers_json=row["headers_json"],
            command=row["command"],
            args_json=row["args_json"],
            env_json=row["env_json"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> ScheduledTaskRecord:
        return ScheduledTaskRecord(
            id=int(row["id"]),
            name=row["name"],
            prompt=row["prompt"],
            schedule_type=row["schedule_type"],
            cron_expr=row["cron_expr"],
            run_at=row["run_at"],
            action_type=row["action_type"],
            enabled=bool(row["enabled"]),
            last_run_at=row["last_run_at"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_plugin_config(row: sqlite3.Row) -> PluginConfigRecord:
        keys = set(row.keys())
        return PluginConfigRecord(
            plugin_name=row["plugin_name"],
            permissions_json=row["permissions_json"],
            connected=bool(row["connected"]),
            connected_at=row["connected_at"],
            updated_at=row["updated_at"],
            account_label=row["account_label"] if "account_label" in keys else "",
        )

    def ensure_owner(self, settings: Settings) -> OwnerConfig:
        """Zajistí existenci záznamu vlastníka v `owner_config` (inicializuje ze `Settings`)."""
        existing = self.get_owner()
        if existing is not None:
            return existing

        lang = normalize_language(settings.language)
        default_display = t("backend.default_display_name", lang)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO owner_config (
                    id, username, email, display_name, web_token, assistant_name, language,
                    tz, default_context_mode, telegram_bot_token, telegram_allowed_users,
                    telegram_notify_chat_id, heartbeat_enabled, heartbeat_interval_minutes,
                    morning_briefing_enabled, morning_briefing_cron,
                    nightly_reset_enabled, nightly_reset_cron,
                    agy_model, gemini_api_key, created_at
                ) VALUES (1, 'admin', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    default_display,
                    settings.web_auth_token.strip(),
                    settings.assistant_name,
                    lang,
                    settings.tz,
                    settings.default_context_mode,
                    settings.telegram_bot_token.strip(),
                    settings.telegram_allowed_users.strip(),
                    settings.telegram_notify_chat_id.strip(),
                    1 if settings.heartbeat_enabled else 0,
                    max(1, settings.heartbeat_interval_minutes),
                    1 if settings.morning_briefing_enabled else 0,
                    settings.morning_briefing_cron,
                    1 if settings.nightly_reset_enabled else 0,
                    settings.nightly_reset_cron,
                    settings.agy_model.strip(),
                    settings.gemini_api_key.strip(),
                    self._now_iso(),
                ),
            )
        owner = self.get_owner()
        assert owner is not None
        return owner

    def get_owner(self) -> OwnerConfig | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM owner_config WHERE id = 1").fetchone()
            return self._row_to_owner(row) if row else None

    def update_owner(self, **updates: Any) -> OwnerConfig:
        filtered: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in OWNER_UPDATABLE_FIELDS or value is None:
                continue
            if key in {
                "heartbeat_enabled",
                "morning_briefing_enabled",
                "nightly_reset_enabled",
                "totp_enabled",
            }:
                filtered[key] = 1 if bool(value) else 0
            elif key == "heartbeat_interval_minutes":
                filtered[key] = max(1, int(value))
            elif key in {"email", "username"} and isinstance(value, str):
                filtered[key] = value.strip().lower()
            elif key == "language" and isinstance(value, str):
                filtered[key] = normalize_language(value)
            elif isinstance(value, str):
                filtered[key] = value.strip()
            else:
                filtered[key] = value

        if filtered:
            assignments = ", ".join(f"{col} = ?" for col in filtered)
            params = list(filtered.values())
            with self._connect() as conn:
                conn.execute(f"UPDATE owner_config SET {assignments} WHERE id = 1", params)

        owner = self.get_owner()
        if owner is None:
            raise RuntimeError("Konfigurace vlastníka nebyla inicializována.")
        return owner

    def match_owner_login(self, identifier: str) -> OwnerConfig | None:
        cleaned = identifier.strip().lower()
        if not cleaned:
            return None
        owner = self.get_owner()
        if owner is None:
            return None
        if owner.username.lower() == cleaned or (owner.email and owner.email.lower() == cleaned):
            return owner
        return None

    def match_owner_token(self, token: str, fallback_global_token: str = "") -> OwnerConfig | None:
        cleaned = token.strip()
        if not cleaned:
            return None
        owner = self.get_owner()
        if owner is None:
            return None
        if owner.web_token.strip() and secrets.compare_digest(owner.web_token.strip(), cleaned):
            return owner
        if fallback_global_token.strip() and secrets.compare_digest(
            fallback_global_token.strip(), cleaned
        ):
            return owner
        return None

    @staticmethod
    def _hash_session_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_auth_session(self, *, ttl_days: int = 30) -> str:
        raw_token = f"sess_{secrets.token_urlsafe(32)}"
        token_hash = self._hash_session_token(raw_token)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=max(1, ttl_days))).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO auth_sessions (token_hash, expires_at, created_at) VALUES (?, ?, ?)",
                (token_hash, expires_at, self._now_iso()),
            )
        return raw_token

    def verify_auth_session(self, token: str) -> bool:
        cleaned = token.strip()
        if not cleaned:
            return False
        token_hash = self._hash_session_token(cleaned)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT expires_at FROM auth_sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return False
            try:
                exp = datetime.fromisoformat(row["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= exp:
                    conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))
                    return False
            except ValueError:
                return False
            return True

    def revoke_auth_session(self, token: str) -> bool:
        cleaned = token.strip()
        if not cleaned:
            return False
        token_hash = self._hash_session_token(cleaned)
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))
            return cur.rowcount > 0

    def revoke_all_auth_sessions(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM auth_sessions")

    def cleanup_expired_auth_sessions(self) -> int:
        """Smaže expirované záznamy v `auth_sessions` a vrátí počet odstraněných řádků."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?",
                (self._now_iso(),),
            )
            return cur.rowcount

    def list_mcp_servers(self) -> list[McpServerRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM mcp_servers ORDER BY name ASC").fetchall()
            return [self._row_to_mcp(row) for row in rows]

    def upsert_mcp_server(
        self,
        *,
        server_id: int | None = None,
        name: str,
        scope: str = "all",
        transport: str = "http",
        url: str = "",
        auth_token: str = "",
        headers_json: str = "{}",
        command: str = "",
        args_json: str = "[]",
        env_json: str = "{}",
        enabled: bool = True,
    ) -> McpServerRecord:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Název MCP serveru nesmí být prázdný.")
        clean_scope = scope if scope in ("all", "pracovni", "osobni") else "all"
        clean_transport = "stdio" if transport == "stdio" else "http"

        with self._connect() as conn:
            if server_id is not None:
                conn.execute(
                    """
                    UPDATE mcp_servers
                    SET name = ?, scope = ?, transport = ?, url = ?, auth_token = ?,
                        headers_json = ?, command = ?, args_json = ?, env_json = ?, enabled = ?
                    WHERE id = ?
                    """,
                    (
                        clean_name,
                        clean_scope,
                        clean_transport,
                        url.strip(),
                        auth_token.strip(),
                        headers_json or "{}",
                        command.strip(),
                        args_json or "[]",
                        env_json or "{}",
                        1 if enabled else 0,
                        server_id,
                    ),
                )
                row = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
            else:
                conn.execute(
                    """
                    INSERT INTO mcp_servers (
                        name, scope, transport, url, auth_token, headers_json,
                        command, args_json, env_json, enabled, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        scope = excluded.scope,
                        transport = excluded.transport,
                        url = excluded.url,
                        auth_token = excluded.auth_token,
                        headers_json = excluded.headers_json,
                        command = excluded.command,
                        args_json = excluded.args_json,
                        env_json = excluded.env_json,
                        enabled = excluded.enabled
                    """,
                    (
                        clean_name,
                        clean_scope,
                        clean_transport,
                        url.strip(),
                        auth_token.strip(),
                        headers_json or "{}",
                        command.strip(),
                        args_json or "[]",
                        env_json or "{}",
                        1 if enabled else 0,
                        self._now_iso(),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM mcp_servers WHERE name = ?", (clean_name,)
                ).fetchone()
        if row is None:
            raise KeyError("MCP server nebyl nalezen.")
        return self._row_to_mcp(row)

    def delete_mcp_server(self, server_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
            return cur.rowcount > 0

    def list_plugin_configs(self) -> dict[str, PluginConfigRecord]:
        """Vrátí uložené konfigurace pluginů indexované malým názvem pluginu."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM plugin_configs ORDER BY plugin_name ASC").fetchall()
            return {row["plugin_name"].lower(): self._row_to_plugin_config(row) for row in rows}

    def get_plugin_config(self, plugin_name: str) -> PluginConfigRecord | None:
        clean_name = plugin_name.strip().lower()
        if not clean_name:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plugin_configs WHERE plugin_name = ?",
                (clean_name,),
            ).fetchone()
            return self._row_to_plugin_config(row) if row else None

    def upsert_plugin_config(
        self,
        plugin_name: str,
        *,
        permissions_json: str | None = None,
        connected: bool | None = None,
        connected_at: str | None = None,
        account_label: str | None = None,
    ) -> PluginConfigRecord:
        """Uloží nebo aktualizuje oprávnění a stav připojení konkrétního pluginu."""
        clean_name = plugin_name.strip().lower()
        if not clean_name:
            raise ValueError("Název pluginu nesmí být prázdný.")
        existing = self.get_plugin_config(clean_name)
        eff_perms = (
            permissions_json
            if permissions_json is not None
            else (existing.permissions_json if existing is not None else "{}")
        )
        eff_connected = (
            bool(connected)
            if connected is not None
            else (existing.connected if existing is not None else False)
        )
        if connected_at is not None:
            eff_connected_at = connected_at
        elif connected is True:
            eff_connected_at = (
                existing.connected_at
                if (existing is not None and existing.connected and existing.connected_at)
                else self._now_iso()
            )
        elif connected is False:
            eff_connected_at = ""
        else:
            eff_connected_at = existing.connected_at if existing is not None else ""

        if account_label is not None:
            eff_account = account_label.strip()
        elif connected is False:
            eff_account = ""
        else:
            eff_account = existing.account_label if existing is not None else ""

        now_iso = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plugin_configs (
                    plugin_name, permissions_json, connected, connected_at, account_label, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(plugin_name) DO UPDATE SET
                    permissions_json = excluded.permissions_json,
                    connected = excluded.connected,
                    connected_at = excluded.connected_at,
                    account_label = excluded.account_label,
                    updated_at = excluded.updated_at
                """,
                (
                    clean_name,
                    eff_perms or "{}",
                    1 if eff_connected else 0,
                    eff_connected_at,
                    eff_account,
                    now_iso,
                ),
            )
            row = conn.execute(
                "SELECT * FROM plugin_configs WHERE plugin_name = ?",
                (clean_name,),
            ).fetchone()
        assert row is not None
        return self._row_to_plugin_config(row)

    def delete_plugin_config(self, plugin_name: str) -> bool:
        clean_name = plugin_name.strip().lower()
        if not clean_name:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM plugin_configs WHERE plugin_name = ?",
                (clean_name,),
            )
            return cur.rowcount > 0

    def list_scheduled_tasks(self, *, enabled_only: bool = False) -> list[ScheduledTaskRecord]:
        sql = "SELECT * FROM scheduled_tasks"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
            return [self._row_to_task(row) for row in rows]

    def get_scheduled_task(self, task_id: int) -> ScheduledTaskRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
            return self._row_to_task(row) if row else None

    def upsert_scheduled_task(
        self,
        *,
        name: str,
        prompt: str,
        schedule_type: str = "cron",
        cron_expr: str = "",
        run_at: str = "",
        action_type: str = "message",
        enabled: bool = True,
    ) -> ScheduledTaskRecord:
        clean_name = name.strip()
        clean_prompt = prompt.strip()
        if not clean_name:
            raise ValueError("Název úlohy nesmí být prázdný.")
        if not clean_prompt:
            raise ValueError("Text/prompt úlohy nesmí být prázdný.")
        clean_type = "once" if schedule_type == "once" else "cron"
        clean_action = "agent" if action_type == "agent" else "message"

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    name, prompt, schedule_type, cron_expr, run_at,
                    action_type, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    prompt = excluded.prompt,
                    schedule_type = excluded.schedule_type,
                    cron_expr = excluded.cron_expr,
                    run_at = excluded.run_at,
                    action_type = excluded.action_type,
                    enabled = excluded.enabled
                """,
                (
                    clean_name,
                    clean_prompt,
                    clean_type,
                    cron_expr.strip(),
                    run_at.strip(),
                    clean_action,
                    1 if enabled else 0,
                    self._now_iso(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE name = ?", (clean_name,)
            ).fetchone()
        assert row is not None
        return self._row_to_task(row)

    def update_scheduled_task(
        self,
        task_id: int,
        *,
        enabled: bool | None = None,
        last_run_at: str | None = None,
    ) -> ScheduledTaskRecord | None:
        assignments: list[str] = []
        params: list[Any] = []
        if enabled is not None:
            assignments.append("enabled = ?")
            params.append(1 if enabled else 0)
        if last_run_at is not None:
            assignments.append("last_run_at = ?")
            params.append(last_run_at)
        if assignments:
            params.append(task_id)
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE scheduled_tasks SET {', '.join(assignments)} WHERE id = ?",
                    params,
                )
        return self.get_scheduled_task(task_id)

    def delete_scheduled_task(self, task_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0

    def delete_scheduled_task_by_name(self, name: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM scheduled_tasks WHERE LOWER(name) = LOWER(?)",
                (name.strip(),),
            )
            return cur.rowcount > 0

    def cleanup_expired_once_tasks(self, *, tz_name: str = "Europe/Prague") -> int:
        """Smaže proběhlé nebo prošlé jednorázové (`once`) úlohy."""
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        now_tz = datetime.now(tz)
        tasks = self.list_scheduled_tasks(enabled_only=False)
        to_delete: list[int] = []
        for task in tasks:
            if task.schedule_type != "once":
                continue
            if not task.enabled:
                to_delete.append(task.id)
                continue
            if task.run_at:
                try:
                    dt = datetime.fromisoformat(task.run_at)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=tz)
                    if dt <= now_tz:
                        to_delete.append(task.id)
                except ValueError:
                    continue
        if not to_delete:
            return 0
        with self._connect() as conn:
            placeholders = ", ".join("?" for _ in to_delete)
            cur = conn.execute(
                f"DELETE FROM scheduled_tasks WHERE id IN ({placeholders})",
                to_delete,
            )
            return cur.rowcount

    def get_session(
        self, session_key: str, default_context: str = "auto"
    ) -> tuple[str | None, str]:
        """Vrátí (conversation_id, context_mode) pro danou relaci."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT conversation_id, context_mode FROM sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if row is None:
                return None, default_context
            return row["conversation_id"], row["context_mode"]

    def set_conversation_id(
        self,
        session_key: str,
        conversation_id: str | None,
        context_mode: str,
    ) -> None:
        """Uloží nebo aktualizuje conversation_id pro relaci."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_key, conversation_id, context_mode, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    context_mode = excluded.context_mode,
                    updated_at = excluded.updated_at
                """,
                (session_key, conversation_id, context_mode, self._now_iso()),
            )

    def set_context_mode(self, session_key: str, context_mode: str) -> None:
        """Změní kontextový režim relace bez smazání conversation_id."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_key, conversation_id, context_mode, updated_at)
                VALUES (?, NULL, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    context_mode = excluded.context_mode,
                    updated_at = excluded.updated_at
                """,
                (session_key, context_mode, self._now_iso()),
            )

    def reset_conversation(self, session_key: str, default_context: str = "auto") -> None:
        """Smaže navázané conversation_id a zahájí čistou konverzaci."""
        _, current_ctx = self.get_session(session_key, default_context=default_context)
        self.set_conversation_id(session_key, None, current_ctx)

    def reset_all_sessions(self) -> None:
        """Smaže navázaná conversation_id všech relací."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET conversation_id = NULL, updated_at = ?",
                (self._now_iso(),),
            )

    def list_active_sessions(self) -> list[tuple[str, str, str]]:
        """Vrátí seznam `(session_key, conversation_id, context_mode)` pro aktivní relace."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_key, conversation_id, context_mode
                FROM sessions
                WHERE conversation_id IS NOT NULL
                  AND TRIM(conversation_id) != ''
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [
                (row["session_key"], row["conversation_id"], row["context_mode"])
                for row in rows
            ]

    def get_conversation_stats(self, conversation_id: str) -> tuple[int, int]:
        """Vrátí `(počet_běhů, součet_tokenů)` pro konkrétní `conversation_id`."""
        cleaned = conversation_id.strip()
        if not cleaned:
            return 0, 0
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(id) AS cnt, COALESCE(SUM(total_tokens), 0) AS tok
                FROM runs
                WHERE conversation_id = ?
                """,
                (cleaned,),
            ).fetchone()
            if row is None:
                return 0, 0
            return int(row["cnt"]), int(row["tok"])

    def log_run(
        self,
        *,
        source: str,
        session_key: str,
        context_mode: str,
        prompt: str,
        response: str,
        conversation_id: str | None,
        duration_ms: int,
        success: bool,
        error: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        tokens_estimated: bool = False,
    ) -> int:
        """Uloží záznam o běhu asistenta do historie včetně tokenů."""
        in_tok = max(0, int(input_tokens))
        out_tok = max(0, int(output_tokens))
        tot_tok = max(0, int(total_tokens)) or (in_tok + out_tok)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO runs (
                    created_at, source, session_key, context_mode,
                    prompt, response, conversation_id, duration_ms, success, error,
                    input_tokens, output_tokens, total_tokens, tokens_estimated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._now_iso(),
                    source,
                    session_key,
                    context_mode,
                    prompt,
                    response,
                    conversation_id,
                    duration_ms,
                    1 if success else 0,
                    error,
                    in_tok,
                    out_tok,
                    tot_tok,
                    1 if tokens_estimated else 0,
                ),
            )
            return int(cursor.lastrowid)

    def list_runs(
        self,
        limit: int = 30,
        session_key: str | None = None,
    ) -> list[RunRecord]:
        """Vrátí posledních N běhů seřazených od nejnovějšího."""
        bounded = max(1, min(limit, 200))
        params: list[Any] = []
        where_sql = ""
        if session_key is not None:
            where_sql = "WHERE session_key = ?"
            params.append(session_key)
        params.append(bounded)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs {where_sql} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
            keys = set(rows[0].keys()) if rows else set()
            return [
                RunRecord(
                    id=int(row["id"]),
                    created_at=row["created_at"],
                    source=row["source"],
                    session_key=row["session_key"],
                    context_mode=row["context_mode"],
                    prompt=row["prompt"],
                    response=row["response"],
                    conversation_id=row["conversation_id"],
                    duration_ms=int(row["duration_ms"]),
                    success=bool(row["success"]),
                    error=row["error"],
                    input_tokens=int(row["input_tokens"]) if "input_tokens" in keys else 0,
                    output_tokens=int(row["output_tokens"]) if "output_tokens" in keys else 0,
                    total_tokens=int(row["total_tokens"]) if "total_tokens" in keys else 0,
                    tokens_estimated=bool(row["tokens_estimated"]) if "tokens_estimated" in keys else False,
                )
                for row in rows
            ]

    def get_activity_stats(
        self, *, days: int | None = None, hours: int | None = None
    ) -> ActivityStats:
        """Vrátí agregovanou statistiku aktivity a tokenů (volitelně za posledních N dní/hodin)."""
        where_sql = ""
        params: list[Any] = []
        window: timedelta | None = None
        if hours is not None and hours > 0:
            window = timedelta(hours=hours)
        elif days is not None and days > 0:
            window = timedelta(days=days)
        if window is not None:
            cutoff = (datetime.now(timezone.utc) - window).isoformat()
            where_sql = "WHERE created_at >= ?"
            params.append(cutoff)

        sql = f"""
            SELECT
                COUNT(id) AS total_runs,
                COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS success_runs,
                COALESCE(SUM(CASE WHEN source = 'web' THEN 1 ELSE 0 END), 0) AS web_runs,
                COALESCE(SUM(CASE WHEN source = 'telegram' THEN 1 ELSE 0 END), 0) AS telegram_runs,
                COALESCE(SUM(CASE WHEN source IN ('heartbeat', 'briefing', 'nightly_reset', 'task') THEN 1 ELSE 0 END), 0) AS cron_runs,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(CASE WHEN total_tokens > 0 AND tokens_estimated = 0 THEN 1 ELSE 0 END), 0) AS exact_runs,
                COALESCE(SUM(CASE WHEN total_tokens > 0 AND tokens_estimated = 1 THEN 1 ELSE 0 END), 0) AS estimated_runs,
                COALESCE(SUM(duration_ms), 0) AS total_duration_ms,
                MAX(created_at) AS last_active_at
            FROM runs
            {where_sql}
        """
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            assert row is not None
            return ActivityStats(
                total_runs=int(row["total_runs"]),
                success_runs=int(row["success_runs"]),
                web_runs=int(row["web_runs"]),
                telegram_runs=int(row["telegram_runs"]),
                cron_runs=int(row["cron_runs"]),
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                total_tokens=int(row["total_tokens"]),
                exact_runs=int(row["exact_runs"]),
                estimated_runs=int(row["estimated_runs"]),
                total_duration_ms=int(row["total_duration_ms"]),
                last_active_at=row["last_active_at"],
            )
