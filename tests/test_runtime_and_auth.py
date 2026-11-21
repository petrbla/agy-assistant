from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.auth_session import (
    AgyAuthManager,
    AuthSessionState,
    _merge_unique_urls,
    clean_terminal_output,
    extract_osc8_urls,
    extract_urls,
)
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
    assert s.template_dir_for_language("en").name == "workspace_template"
    for lang_code in ("cs", "de", "sk", "es", "pl"):
        tpl = s.template_dir_for_language(lang_code)
        assert tpl.name == f"workspace_template_{lang_code}"
        assert tpl.is_dir()


def test_all_language_templates_support_onboarding_and_name_sync(tmp_path: Path) -> None:
    from app.agy_runner import (
        extract_assistant_name_from_soul,
        has_pending_assistant_name,
        has_schedule_instructions,
    )
    from app.user_env import ensure_workspace, sync_soul_assistant_name

    for lang_code in ("en", "cs", "de", "sk", "es", "pl"):
        ws = tmp_path / f"ws_{lang_code}"
        s = Settings(workspace_dir=ws, data_dir=tmp_path / f"data_{lang_code}", language=lang_code)
        ensure_workspace(s)
        assert has_pending_assistant_name(ws) is True, f"SOUL.md {lang_code} nebyl detekován jako nevyplněný"
        assert has_schedule_instructions(ws) is True, f"AGENTS.md {lang_code} postrádá add_tasks"

        sync_soul_assistant_name(ws, "Nova")
        assert extract_assistant_name_from_soul(ws) == "Nova", f"Nepodařilo se přečíst jméno ze SOUL.md ({lang_code})"
        assert has_pending_assistant_name(ws) is False, f"SOUL.md {lang_code} zůstal označený jako nevyplněný"


def test_clean_terminal_output_and_extract_urls() -> None:
    raw = "\x1b[32mOtevřete:\x1b[0m https://accounts.google.com/o/oauth2/auth?code=123.\r\n"
    cleaned = clean_terminal_output(raw)
    assert "\x1b" not in cleaned
    assert extract_urls(cleaned) == ["https://accounts.google.com/o/oauth2/auth?code=123"]


def test_extract_urls_reassembles_wrapped_oauth_and_osc8() -> None:
    # 1. Přesný případ uříznutí za `%` na okraji řádku
    wrapped_percent = (
        "Please visit the URL to log in:\n"
        "https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=1071006060591-abc.apps.googleusercontent.com"
        "&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform+https%\n"
        "3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email&state=xyz123.\n"
        "Enter authorization code:\n"
    )
    assert extract_urls(wrapped_percent) == [
        "https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=1071006060591-abc.apps.googleusercontent.com"
        "&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email&state=xyz123"
    ]

    # 2. Víceřádkové 80-znakové zalomení i uvnitř TUI rámečku `│ ... │`
    boxed = (
        "│ https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=1071 │\n"
        "│ 006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com&code_c │\n"
        "│ hallenge=kU8V4Jpwtf1OIIPDod794eP2IZvwb5rh1T_8w5bBlUo&redirect_uri=https%3A │\n"
        "│ %2F%2Fantigravity.google%2Foauth-callback&response_type=code                 │\n"
        "│ Enter code:                                                                  │\n"
    )
    assert extract_urls(boxed) == [
        "https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=1071"
        "006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com&code_c"
        "hallenge=kU8V4Jpwtf1OIIPDod794eP2IZvwb5rh1T_8w5bBlUo&redirect_uri=https%3A"
        "%2F%2Fantigravity.google%2Foauth-callback&response_type=code"
    ]

    # 3. OSC-8 hyperlink v raw ANSI výstupu před očištěním
    raw_osc8 = "\x1b]8;;https://accounts.google.com/o/oauth2/auth?a=1&b=2\x07Sign in\x1b]8;;\x07\n"
    assert extract_osc8_urls(raw_osc8) == ["https://accounts.google.com/o/oauth2/auth?a=1&b=2"]

    # 4. Kurzorové skoky TUI nesmí slepit následující text (`Click here...`) na konec URL
    raw_cursor = (
        "\x1b[10;1Hhttps://accounts.google.com/o/oauth2/auth?a=1"
        "\x1b[12;1HClick here to authenticate\n"
    )
    assert extract_urls(clean_terminal_output(raw_cursor)) == [
        "https://accounts.google.com/o/oauth2/auth?a=1"
    ]

    # 5. Sloučení uříznuté Google OAuth URL a kompletní OSC-8 URL do jediné URL
    short_broken = (
        "https://accounts.google.com/o/oauth2/auth?access_type=offline"
        "&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform+https%"
    )
    full_oauth = (
        "https://accounts.google.com/o/oauth2/auth?access_type=offline"
        "&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform"
        "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email&state=ok"
    )
    assert _merge_unique_urls([short_broken, full_oauth]) == [full_oauth]

    # 6. Řídicí znak BEL (\x07) ani šipka (→) z konce OSC-8 sekvence se nesmí dostat do URL
    bel_arrow_osc8 = (
        "\x1b]8;;https://accounts.google.com/o/oauth2/auth?access_type=offline&state=2FrnI5aRQYA_gbMeTRoU_g"
        "\x07→ Click here to authenticate\x1b]8;;\x07\n"
    )
    clean_bel = clean_terminal_output(bel_arrow_osc8)
    assert "\x07" not in clean_bel
    assert extract_osc8_urls(bel_arrow_osc8) == [
        "https://accounts.google.com/o/oauth2/auth?access_type=offline&state=2FrnI5aRQYA_gbMeTRoU_g"
    ]
    raw_bel_arrow_text = (
        "https://accounts.google.com/o/oauth2/auth?access_type=offline&state=2FrnI5aRQYA_gbMeTRoU_g\x07→\n"
    )
    assert extract_urls(raw_bel_arrow_text) == [
        "https://accounts.google.com/o/oauth2/auth?access_type=offline&state=2FrnI5aRQYA_gbMeTRoU_g"
    ]
    assert extract_urls(clean_terminal_output(raw_bel_arrow_text)) == [
        "https://accounts.google.com/o/oauth2/auth?access_type=offline&state=2FrnI5aRQYA_gbMeTRoU_g"
    ]


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
