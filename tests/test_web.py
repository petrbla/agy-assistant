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
    home_res = client.get("/")
    assert home_res.status_code == 200
    assert "agyOnboardingCard" in home_res.text
    assert "chatAgyAuthBanner" in home_res.text
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
    import re

    root = Path(__file__).resolve().parent.parent
    base_tpl = root / "workspace_template"
    base_rel_files = {p.relative_to(base_tpl).as_posix() for p in base_tpl.rglob("*") if p.is_file()}
    assert len(base_rel_files) == 9

    dockerfile_txt = (root / "Dockerfile").read_text(encoding="utf-8")
    for tpl_name in (
        "workspace_template",
        "workspace_template_cs",
        "workspace_template_de",
        "workspace_template_sk",
        "workspace_template_es",
        "workspace_template_pl",
    ):
        tpl_dir = root / tpl_name
        rel_files = {p.relative_to(tpl_dir).as_posix() for p in tpl_dir.rglob("*") if p.is_file()}
        assert rel_files == base_rel_files, f"Rozdíl souborů v {tpl_name}"

        parsed_mcp = json.loads((tpl_dir / ".agents" / "mcp_config.example.json").read_text(encoding="utf-8"))
        assert isinstance(parsed_mcp.get("mcpServers"), dict)
        expected_server = "n8n-prace" if tpl_name == "workspace_template_cs" else "n8n-work"
        assert expected_server in parsed_mcp["mcpServers"], f"Chybí {expected_server} v {tpl_name}"

        assert "HEARTBEAT_OK" in (tpl_dir / "HEARTBEAT.md").read_text(encoding="utf-8")
        assert "HEARTBEAT_OK" in (tpl_dir / "AGENTS.md").read_text(encoding="utf-8")
        assert "name: morning-briefing" in (
            tpl_dir / ".agents" / "skills" / "morning-briefing" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "name: memory-maintenance" in (
            tpl_dir / ".agents" / "skills" / "memory-maintenance" / "SKILL.md"
        ).read_text(encoding="utf-8")
        if tpl_name != "workspace_template":
            assert f"COPY {tpl_name}" in dockerfile_txt, f"{tpl_name} chybí v Dockerfile"

    # Parita všech klíčů a placeholderů napříč všemi jazyky v app/locales/*.json
    locales_dir = root / "app" / "locales"
    locale_files = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(locales_dir.glob("*.json"))}
    assert set(locale_files.keys()) == {"en", "cs", "de", "es", "pl", "sk"}
    en_locale = locale_files["en"]
    for lang_code, loc_data in locale_files.items():
        assert set(loc_data.keys()) == {"_meta", "ui", "backend", "prompts"}, f"Chybějící/nadbytečná sekce v {lang_code}"
        for sec in ("_meta", "ui", "backend", "prompts"):
            assert set(loc_data[sec].keys()) == set(en_locale[sec].keys()), f"Rozdíl klíčů v {lang_code}.{sec}"
            if sec in ("ui", "backend", "prompts"):
                for k, val in loc_data[sec].items():
                    assert isinstance(val, str) and val.strip(), f"Prázdná hodnota {lang_code}.{sec}.{k}"
                    en_phs = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", en_locale[sec][k]))
                    loc_phs = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", val))
                    assert loc_phs == en_phs, f"Neshoda placeholderů v {lang_code}.{sec}.{k}: {loc_phs} != {en_phs}"
        assert "HEARTBEAT_OK" in loc_data["prompts"]["heartbeat"], f"Chybí HEARTBEAT_OK v {lang_code}"
        assert "MEMORY_FLUSH_OK" in loc_data["prompts"]["nightly_memory_flush"], f"Chybí MEMORY_FLUSH_OK v {lang_code}"
        assert "schedule_request.json" in loc_data["prompts"]["schedule_hint"], f"Chybí schedule_request.json v {lang_code}"

    client = _build_client(tmp_path)
    i18n_list = client.get("/api/i18n")
    assert i18n_list.status_code == 200
    assert i18n_list.json()["default_language"] == "en"
    assert [x["code"] for x in i18n_list.json()["available_languages"]] == ["en", "cs", "de", "es", "pl", "sk"]

    expected_nav_settings = {
        "cs": "Nastavení",
        "de": "Einstellungen",
        "sk": "Nastavenia",
        "es": "Configuración",
        "pl": "Ustawienia",
    }
    for code, expected_label in expected_nav_settings.items():
        bundle = client.get(f"/api/i18n/{code}")
        assert bundle.status_code == 200
        assert bundle.json()["language"] == code
        assert bundle.json()["ui"]["nav_settings"] == expected_label

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
    lang_upd_de = client.post(
        "/api/settings",
        headers={"Authorization": f"Bearer {tok1}"},
        json={"language": "de"},
    )
    assert lang_upd_de.status_code == 200
    assert lang_upd_de.json()["language"] == "de"

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

