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
from app.user_env import clear_agy_oauth, has_agy_oauth, migrate_legacy_home_if_needed
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
        VALUES (1, 'alice', 'alice@example.com', 'AriaBot', 'tg-tok-1', 'gemini-4-neon-medium', '2026-01-01T00:00:00Z');
        INSERT INTO users (id, username, email, assistant_name, created_at)
        VALUES (2, 'bob', 'bob@example.com', 'BobBot', '2026-01-02T00:00:00Z');

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
        VALUES (1, 'google-personal', 'osobni', 'http', 'https://google.example/mcp', '2026-01-01T00:00:00Z');
        INSERT INTO user_mcp_servers (user_id, name, scope, transport, url, created_at)
        VALUES (2, 'bob-mcp', 'all', 'http', 'https://bob.example/mcp', '2026-01-02T00:00:00Z');

        CREATE TABLE sessions (
            session_key TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL DEFAULT 1,
            conversation_id TEXT,
            context_mode TEXT NOT NULL DEFAULT 'auto',
            updated_at TEXT NOT NULL
        );
        INSERT INTO sessions VALUES ('u:1:tg:999:auto', 1, 'conv-alice', 'auto', '2026-01-01T00:00:00Z');
        INSERT INTO sessions VALUES ('u:1:tg:999:firma', 1, 'conv-firma', 'firma-x', '2026-01-01T00:00:00Z');
        INSERT INTO sessions VALUES ('u:1:tg:999:privat', 1, 'conv-privat', 'soukrome', '2026-01-01T00:00:00Z');
        INSERT INTO sessions VALUES ('u:2:tg:888:auto', 2, 'conv-bob', 'auto', '2026-01-01T00:00:00Z');
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
    assert owner.username == "alice"
    assert owner.email == "alice@example.com"
    assert owner.assistant_name == "AriaBot"
    assert owner.language == "en"
    assert owner.telegram_bot_token == "tg-tok-1"
    assert owner.agy_model == "gemini-4-neon-medium"

    mcps = db.list_mcp_servers()
    assert [m.name for m in mcps] == ["google-personal"]

    conv_id, _ = db.get_session("tg:999:auto")
    assert conv_id == "conv-alice"

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
        json={"email": "alice@example.com", "username": "alice", "password": "SuperHeslo123!"},
    )
    assert setup.status_code == 200
    tok = setup.json()["session_token"]
    hdr = {"Authorization": f"Bearer {tok}"}

    assert client.get("/api/status").status_code == 401
    st_res = client.get("/api/status", headers=hdr)
    assert st_res.status_code == 200
    assert st_res.json()["has_agy_oauth"] is False
    assert st_res.json()["agy_authenticated"] is False

    # Nastavení a zapnutí 2FA
    tfa = client.post("/api/2fa/setup", headers=hdr).json()
    code = generate_totp_code(tfa["secret"])
    en = client.post("/api/2fa/enable", headers=hdr, json={"code": code})
    assert en.status_code == 200
    recovery_codes = en.json()["recovery_codes"]
    assert len(recovery_codes) == 8

    # Přihlášení s 2FA výzvou
    login1 = client.post("/api/login", json={"identifier": "alice@example.com", "password": "SuperHeslo123!"})
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


def test_has_agy_oauth_ignores_cli_runtime_files_and_detects_tokens(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data")
    gemini_dir = settings.effective_home_dir / ".gemini"
    cli_dir = gemini_dir / "antigravity-cli"
    for sub in ("brain", "builtin", "cache", "conversations", "crashes", "implicit", "log"):
        (cli_dir / sub).mkdir(parents=True, exist_ok=True)
    (gemini_dir / "config").mkdir(parents=True, exist_ok=True)

    # Typické runtime soubory nepřihlášeného `agy 1.5.14`
    (cli_dir / "builtin" / "skill.md").write_text("# skill", encoding="utf-8")
    (cli_dir / "log" / "cli-20261121.log").write_text("started\n", encoding="utf-8")
    (cli_dir / "conversation_summaries.db").write_bytes(b"sqlite")
    (cli_dir / "conversation_summaries.db-shm").write_bytes(b"shm")
    (cli_dir / "conversation_summaries.db-wal").write_bytes(b"wal")
    (cli_dir / "installation_id").write_text("00000000-0000-0000-0000-000000000000", encoding="utf-8")
    (cli_dir / "jetski_state.pbtxt").write_text(
        'installation_uuid: "00000000-0000-0000-0000-000000000000"\n',
        encoding="utf-8",
    )
    (cli_dir / "settings.json").write_text("{}", encoding="utf-8")
    (gemini_dir / "config" / "mcp_config.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    (gemini_dir / "google_accounts.json").write_text('{"active": ""}', encoding="utf-8")

    assert has_agy_oauth(settings) is False

    clear_agy_oauth(settings)
    assert (cli_dir / "settings.json").is_file()
    assert (gemini_dir / "config" / "mcp_config.json").is_file()
    assert (cli_dir / "conversation_summaries.db").is_file()
    assert (cli_dir / "builtin" / "skill.md").is_file()

    # Reálný OAuth token soubor
    token_file = cli_dir / "antigravity-oauth-token"
    token_file.write_text('{"refresh_token": "tok-abc"}', encoding="utf-8")
    assert has_agy_oauth(settings) is True
    clear_agy_oauth(settings)
    assert has_agy_oauth(settings) is False
    assert not token_file.exists()

    # Autentizovaný jetski_state.pbtxt s refresh_tokenem
    (cli_dir / "jetski_state.pbtxt").write_text(
        'refresh_token: "1//example-token-123"\n',
        encoding="utf-8",
    )
    assert has_agy_oauth(settings) is True
    clear_agy_oauth(settings)
    assert has_agy_oauth(settings) is False

    # google_accounts.json s aktivním účtem
    (gemini_dir / "google_accounts.json").write_text('{"active": "user@example.com"}', encoding="utf-8")
    assert has_agy_oauth(settings) is True
    clear_agy_oauth(settings)
    assert has_agy_oauth(settings) is False


def test_mcp_server_name_sanitization_and_legacy_db_migration(tmp_path: Path) -> None:
    import pytest
    from app.db import sanitize_mcp_server_name
    from app.user_env import build_mcp_config_dict

    assert sanitize_mcp_server_name("noco db fedia") == "noco-db-fedia"
    assert sanitize_mcp_server_name("  NocoDB__nocopetr  ") == "NocoDB__nocopetr"
    assert sanitize_mcp_server_name("Můj   Server (2)!") == "Muj-Server-2"
    assert sanitize_mcp_server_name("n8n-prace") == "n8n-prace"
    with pytest.raises(ValueError):
        sanitize_mcp_server_name("   ---   ")

    # Starší DB se záznamy obsahujícími mezery + kolizní název se při startu bezpečně přejmenují
    db_path = tmp_path / "data" / "assistant.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE mcp_servers (
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
        INSERT INTO mcp_servers (name, url, created_at)
        VALUES ('noco db', 'https://a.example.invalid/mcp', '2026-01-01T00:00:00Z');
        INSERT INTO mcp_servers (name, url, created_at)
        VALUES ('noco-db', 'https://b.example.invalid/mcp', '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    db = AssistantDB(db_path)
    servers = {s.id: s for s in db.list_mcp_servers()}
    assert servers[1].name == "noco-db-1"
    assert servers[2].name == "noco-db"

    cfg = build_mcp_config_dict(list(servers.values()), context_mode="auto")
    assert set(cfg["mcpServers"].keys()) == {"noco-db-1", "noco-db"}
    assert all(" " not in key for key in cfg["mcpServers"])
