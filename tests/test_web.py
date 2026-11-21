from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.agy_runner import AgyResult
from app.config import Settings
from app.db import AssistantDB
from app.memory import WorkspaceMemory
from app.web import create_app


def _build_client(tmp_path: Path, auth_token: str = "") -> TestClient:
    settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
        web_auth_token=auth_token,
    )
    (tmp_path / "MEMORY.md").write_text("# Paměť", encoding="utf-8")
    db = AssistantDB(settings.db_path)
    memory = WorkspaceMemory(tmp_path)
    runner = AsyncMock()
    runner.check_version = AsyncMock(return_value="1.2.0")
    runner.run = AsyncMock(
        return_value=AgyResult(
            text="Odpověď z agy",
            conversation_id="conv-1",
            duration_ms=42,
            success=True,
        )
    )
    app = create_app(settings, db, memory, runner, scheduler=None)
    return TestClient(app)


def test_health_is_public_and_api_requires_token(tmp_path: Path) -> None:
    client = _build_client(tmp_path, auth_token="tajne123")
    assert client.get("/").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/status").status_code == 401

    ok = client.get("/api/status", headers={"Authorization": "Bearer tajne123"})
    assert ok.status_code == 200
    assert ok.json()["agy_available"] is True


def test_chat_and_file_endpoints(tmp_path: Path) -> None:
    client = _build_client(tmp_path)

    chat_res = client.post("/api/chat", json={"prompt": "Ahoj", "context_mode": "pracovni"})
    assert chat_res.status_code == 200
    assert chat_res.json()["text"] == "Odpověď z agy"

    put_res = client.put("/api/files/MEMORY.md", json={"content": "# Nová paměť"})
    assert put_res.status_code == 200
    get_res = client.get("/api/files/MEMORY.md")
    assert get_res.json()["content"] == "# Nová paměť"

    ver_res = client.get("/api/agy/version")
    assert ver_res.status_code == 200
    assert ver_res.json()["agy_version"] == "1.2.0"


def test_password_change_revokes_previous_sessions_and_template_json_valid(tmp_path: Path) -> None:
    import json

    root = Path(__file__).resolve().parent.parent
    template_mcp_en = root / "workspace_template" / ".agents" / "mcp_config.example.json"
    template_mcp_cs = root / "workspace_template_cs" / ".agents" / "mcp_config.example.json"
    parsed_en = json.loads(template_mcp_en.read_text(encoding="utf-8"))
    parsed_cs = json.loads(template_mcp_cs.read_text(encoding="utf-8"))
    assert isinstance(parsed_en.get("mcpServers"), dict) and "n8n-work" in parsed_en["mcpServers"]
    assert isinstance(parsed_cs.get("mcpServers"), dict) and "n8n-prace" in parsed_cs["mcpServers"]

    # Parita všech klíčů v app/locales/en.json a app/locales/cs.json
    en_locale = json.loads((root / "app" / "locales" / "en.json").read_text(encoding="utf-8"))
    cs_locale = json.loads((root / "app" / "locales" / "cs.json").read_text(encoding="utf-8"))
    assert set(en_locale.keys()) == set(cs_locale.keys()) == {"_meta", "ui", "backend", "prompts"}
    for sec in ("_meta", "ui", "backend", "prompts"):
        assert set(en_locale[sec].keys()) == set(cs_locale[sec].keys()), f"Rozdíl v sekci {sec}"

    client = _build_client(tmp_path)
    i18n_list = client.get("/api/i18n")
    assert i18n_list.status_code == 200
    assert i18n_list.json()["default_language"] == "en"
    assert [x["code"] for x in i18n_list.json()["available_languages"]] == ["en", "cs"]

    bundle_cs = client.get("/api/i18n/cs")
    assert bundle_cs.status_code == 200
    assert bundle_cs.json()["language"] == "cs"
    assert bundle_cs.json()["ui"]["nav_settings"] == "Nastavení"

    # Path-traversal / neznámý kód na /api/i18n nikam mimo locales nesáhne
    bundle_slash = client.get("/api/i18n/..%2F..%2Fetc%2Fpasswd")
    assert bundle_slash.status_code in (200, 404)
    bundle_trav = client.get("/api/i18n/..%5C..%5Cetc%5Cpasswd")
    assert bundle_trav.status_code == 200
    assert bundle_trav.json()["language"] == "en"
    bundle_unk = client.get("/api/i18n/xx-unknown")
    assert bundle_unk.status_code == 200
    assert bundle_unk.json()["language"] == "en"

    auth_state = client.get("/api/auth/state").json()
    assert auth_state["language"] == "en"

    setup_res = client.post(
        "/api/auth/initial-setup",
        json={
            "email": "uzivatel@example.com",
            "username": "admin",
            "language": "cs",
            "password": "heslo12345",
        },
    )
    assert setup_res.status_code == 200
    assert setup_res.json()["owner"]["language"] == "cs"
    assert "Profil uživatele" in (tmp_path / "USER.md").read_text(encoding="utf-8")
    tok1 = setup_res.json()["session_token"]

    login_res = client.post(
        "/api/login",
        json={"identifier": "admin", "password": "heslo12345"},
    )
    assert login_res.status_code == 200
    tok2 = login_res.json()["session_token"]

    assert client.get("/api/status", headers={"Authorization": f"Bearer {tok2}"}).status_code == 200

    # Přepnutí jazyka přes /api/settings
    lang_upd = client.post(
        "/api/settings",
        headers={"Authorization": f"Bearer {tok1}"},
        json={"language": "en"},
    )
    assert lang_upd.status_code == 200
    assert lang_upd.json()["language"] == "en"

    pwd_res = client.post(
        "/api/account/password",
        headers={"Authorization": f"Bearer {tok1}"},
        json={"current_password": "heslo12345", "new_password": "noveheslo12345"},
    )
    assert pwd_res.status_code == 200
    new_tok = pwd_res.json()["session_token"]
    assert new_tok

    assert client.get("/api/status", headers={"Authorization": f"Bearer {tok1}"}).status_code == 401
    assert client.get("/api/status", headers={"Authorization": f"Bearer {tok2}"}).status_code == 401
    assert client.get("/api/status", headers={"Authorization": f"Bearer {new_tok}"}).status_code == 200

