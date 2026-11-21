from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.auth_session import AgyAuthManager, AuthSessionState, clean_terminal_output, extract_urls
from app.config import Settings
from app.db import AssistantDB
from app.memory import WorkspaceMemory
from app.runtime_config import apply_runtime_overrides, save_runtime_overrides, sync_agy_settings_file
from app.web import create_app


def test_polivka_env_aliases(monkeypatch, tmp_path: Path) -> None:
    from app.config import normalize_chat_channel, normalize_language

    monkeypatch.setenv("JMENO", "Jarvis")
    monkeypatch.setenv("TG_TOKEN", "bot-token-xyz")
    monkeypatch.setenv("TG_ID", "123456789")
    monkeypatch.setenv("JAZYK", "cs")
    s = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    assert s.assistant_name == "Jarvis"
    assert s.telegram_bot_token == "bot-token-xyz"
    assert s.allowed_telegram_user_ids == {123456789}
    assert s.language == "cs"
    assert normalize_chat_channel("work") == "pracovni"
    assert normalize_chat_channel("personal") == "osobni"

    assert normalize_language("deutsch") == "de"
    assert normalize_language("německy") == "de"
    assert normalize_language("slovenčina") == "sk"
    assert normalize_language("español") == "es"
    assert normalize_language("polski") == "pl"
    assert normalize_language("cz") == "cs"
    assert normalize_language("english") == "en"
    assert s.template_dir_for_language("de") == s.template_dir


def test_clean_terminal_output_and_extract_urls() -> None:
    raw = "\x1b[32mOtevřete:\x1b[0m https://accounts.google.com/o/oauth2/auth?code=123.\r\n"
    cleaned = clean_terminal_output(raw)
    assert "\x1b" not in cleaned
    assert extract_urls(cleaned) == ["https://accounts.google.com/o/oauth2/auth?code=123"]


def test_runtime_overrides_persist_and_sync_settings(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    save_runtime_overrides(
        settings,
        {"gemini_api_key": "gk-test", "telegram_allowed_users": "111, 222"},
    )
    assert settings.gemini_api_key == "gk-test"
    assert settings.allowed_telegram_user_ids == {111, 222}

    reloaded = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    apply_runtime_overrides(reloaded)
    assert reloaded.gemini_api_key == "gk-test"

    fake_home = tmp_path / "home"
    sync_agy_settings_file(reloaded, home_dir=fake_home)
    agy_cfg = json.loads((fake_home / ".gemini" / "antigravity-cli" / "settings.json").read_text())
    assert agy_cfg["modelProvider"] == "gemini"


async def test_settings_and_auth_endpoints(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    memory = WorkspaceMemory(tmp_path)
    runner = AsyncMock()
    runner.check_version = AsyncMock(return_value="1.2.0")

    auth_mgr = AgyAuthManager(settings)
    auth_mgr.start = AsyncMock(
        return_value=AuthSessionState(
            running=True,
            command="agy auth login",
            output="Navštivte https://example.com/login",
            urls=["https://example.com/login"],
        )
    )
    callback = AsyncMock()

    app = create_app(
        settings,
        db,
        memory,
        runner,
        auth_manager=auth_mgr,
        on_settings_updated=callback,
    )
    client = TestClient(app)

    res = client.post(
        "/api/settings",
        json={"telegram_bot_token": "tg-123", "telegram_allowed_users": "42"},
    )
    assert res.status_code == 200
    assert res.json()["has_telegram_bot_token"] is True
    callback.assert_awaited_once()

    auth_res = client.post("/api/auth/start", json={"mode": "auth_login"})
    assert auth_res.status_code == 200
    assert auth_res.json()["urls"] == ["https://example.com/login"]
