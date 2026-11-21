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

    template_mcp = (
        Path(__file__).resolve().parent.parent
        / "workspace_template"
        / ".agents"
        / "mcp_config.example.json"
    )
    parsed = json.loads(template_mcp.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    assert isinstance(parsed.get("mcpServers"), dict)
    assert "n8n-prace" in parsed["mcpServers"]

    client = _build_client(tmp_path)
    setup_res = client.post(
        "/api/auth/initial-setup",
        json={"email": "uzivatel@example.com", "username": "admin", "password": "heslo12345"},
    )
    assert setup_res.status_code == 200
    tok1 = setup_res.json()["session_token"]

    login_res = client.post(
        "/api/login",
        json={"identifier": "admin", "password": "heslo12345"},
    )
    assert login_res.status_code == 200
    tok2 = login_res.json()["session_token"]

    assert client.get("/api/status", headers={"Authorization": f"Bearer {tok2}"}).status_code == 200

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

