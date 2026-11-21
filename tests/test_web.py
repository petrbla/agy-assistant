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
