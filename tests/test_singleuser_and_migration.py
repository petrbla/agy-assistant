from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import AssistantDB
from app.memory import WorkspaceMemory
from app.security import generate_totp_code
from app.user_env import has_agy_oauth, migrate_legacy_home_if_needed
from app.web import create_app


def test_legacy_multiuser_db_and_home_migration(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "assistant.db"

    # Vytvoříme starou multi-user databázi se 2 uživateli, MCP a relacemi
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'admin',
            web_token TEXT NOT NULL DEFAULT '',
            assistant_name TEXT NOT NULL DEFAULT 'Jarvis',
            tz TEXT NOT NULL DEFAULT 'Europe/Prague',
            default_context_mode TEXT NOT NULL DEFAULT 'auto',
            telegram_bot_token TEXT NOT NULL DEFAULT '',
            telegram_allowed_users TEXT NOT NULL DEFAULT '',
            telegram_notify_chat_id TEXT NOT NULL DEFAULT '',
            heartbeat_enabled INTEGER NOT NULL DEFAULT 1,
            heartbeat_interval_minutes INTEGER NOT NULL DEFAULT 60,
            morning_briefing_enabled INTEGER NOT NULL DEFAULT 1,
            morning_briefing_cron TEXT NOT NULL DEFAULT '0 8 * * *',
            agy_model TEXT NOT NULL DEFAULT '',
            gemini_api_key TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL DEFAULT '',
            totp_enabled INTEGER NOT NULL DEFAULT 0,
            totp_secret TEXT NOT NULL DEFAULT '',
            totp_pending_secret TEXT NOT NULL DEFAULT '',
            totp_recovery_hashes_json TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        INSERT INTO users (id, username, email, assistant_name, telegram_bot_token, agy_model, created_at)
        VALUES (1, 'petr', 'petr@example.com', 'JarvisPetr', 'tg-tok-1', 'gemini-4-neon-medium', '2026-01-01T00:00:00Z');
        INSERT INTO users (id, username, email, assistant_name, created_at)
        VALUES (2, 'karel', 'karel@example.com', 'KarelBot', '2026-01-02T00:00:00Z');

        CREATE TABLE user_mcp_servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
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
        INSERT INTO user_mcp_servers (user_id, name, scope, transport, url, created_at)
        VALUES (1, 'google-petr', 'osobni', 'http', 'https://google.example/mcp', '2026-01-01T00:00:00Z');
        INSERT INTO user_mcp_servers (user_id, name, scope, transport, url, created_at)
        VALUES (2, 'karel-mcp', 'all', 'http', 'https://karel.example/mcp', '2026-01-02T00:00:00Z');

        CREATE TABLE sessions (
            session_key TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL DEFAULT 1,
            conversation_id TEXT,
            context_mode TEXT NOT NULL DEFAULT 'auto',
            updated_at TEXT NOT NULL
        );
        INSERT INTO sessions VALUES ('u:1:tg:999:auto', 1, 'conv-petr', 'auto', '2026-01-01T00:00:00Z');
        INSERT INTO sessions VALUES ('u:1:tg:999:firma', 1, 'conv-firma', 'firma-x', '2026-01-01T00:00:00Z');
        INSERT INTO sessions VALUES ('u:1:tg:999:privat', 1, 'conv-privat', 'soukrome', '2026-01-01T00:00:00Z');
        INSERT INTO sessions VALUES ('u:2:tg:888:auto', 2, 'conv-karel', 'auto', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    # Migrace legacy OAuth z users/1/home/.gemini
    legacy_gemini = tmp_path / "users" / "1" / "home" / ".gemini"
    legacy_gemini.mkdir(parents=True)
    (legacy_gemini / "oauth_creds.json").write_text('{"token":"ok"}', encoding="utf-8")

    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=data_dir)
    assert migrate_legacy_home_if_needed(settings) is True
    assert has_agy_oauth(settings) is True

    db = AssistantDB(db_path)
    owner = db.get_owner()
    assert owner is not None
    assert owner.username == "petr"
    assert owner.email == "petr@example.com"
    assert owner.assistant_name == "JarvisPetr"
    assert owner.telegram_bot_token == "tg-tok-1"
    assert owner.agy_model == "gemini-4-neon-medium"

    mcps = db.list_mcp_servers()
    assert [m.name for m in mcps] == ["google-petr"]

    conv_id, _ = db.get_session("tg:999:auto")
    assert conv_id == "conv-petr"

    # Starší názvy kontextů se převedou na pracovni / osobni
    assert db.get_session("tg:999:firma")[1] == "pracovni"
    assert db.get_session("tg:999:privat")[1] == "osobni"


def test_singleuser_password_and_2fa_flow(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data")
    settings.workspace_dir.mkdir(parents=True)
    db = AssistantDB(settings.db_path)
    runner = AsyncMock()
    runner.check_version = AsyncMock(return_value="1.5.12")

    app = create_app(settings, db, WorkspaceMemory(settings.workspace_dir), runner)
    client = TestClient(app)

    assert client.get("/api/auth/state").json()["needs_initial_setup"] is True

    setup = client.post(
        "/api/auth/initial-setup",
        json={"email": "petr@gmail.com", "username": "petr", "password": "SuperHeslo123!"},
    )
    assert setup.status_code == 200
    tok = setup.json()["session_token"]
    hdr = {"Authorization": f"Bearer {tok}"}

    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers=hdr).status_code == 200

    # Nastavení a zapnutí 2FA
    tfa = client.post("/api/2fa/setup", headers=hdr).json()
    code = generate_totp_code(tfa["secret"])
    en = client.post("/api/2fa/enable", headers=hdr, json={"code": code})
    assert en.status_code == 200
    recovery_codes = en.json()["recovery_codes"]
    assert len(recovery_codes) == 8

    # Přihlášení s 2FA výzvou
    login1 = client.post("/api/login", json={"identifier": "petr@gmail.com", "password": "SuperHeslo123!"})
    assert login1.status_code == 200
    assert login1.json()["requires_2fa"] is True
    challenge = login1.json()["challenge_token"]

    login2 = client.post(
        "/api/login/verify-2fa",
        json={"challenge_token": challenge, "code": recovery_codes[0]},
    )
    assert login2.status_code == 200
    assert login2.json()["session_token"].startswith("sess_")

    # Staré multi-user endpointy již neexistují
    assert client.get("/api/users", headers=hdr).status_code == 404
    assert client.get("/api/invites", headers=hdr).status_code == 404
