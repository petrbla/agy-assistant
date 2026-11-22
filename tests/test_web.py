from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.agy_runner import AgyResult
from app.config import Settings
from app.db import AssistantDB
from app.memory import WorkspaceMemory
from app.version_check import AppVersionChecker, fetch_latest_app_version_sync
from app.web import create_app


def _build_client(
    tmp_path: Path,
    auth_token: str = "",
    *,
    app_version_check_url: str = "",
    app_version_checker: AppVersionChecker | None = None,
) -> TestClient:
    settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
        web_auth_token=auth_token,
        app_version_check_url=app_version_check_url,
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
    app = create_app(
        settings,
        db,
        memory,
        runner,
        scheduler=None,
        app_version_checker=app_version_checker,
    )
    return TestClient(app)


def test_health_is_public_and_api_requires_token(tmp_path: Path) -> None:
    import re
    from app import __version__

    root = Path(__file__).resolve().parent.parent
    version_file = root / "VERSION"
    assert version_file.is_file()
    assert version_file.read_text(encoding="utf-8").strip() == __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert "COPY VERSION /app/VERSION" in (root / "Dockerfile").read_text(encoding="utf-8")

    client = _build_client(tmp_path, auth_token="tajne123")
    home_res = client.get("/")
    assert home_res.status_code == 200
    assert "agyOnboardingCard" in home_res.text
    assert "chatAgyAuthBanner" in home_res.text
    assert "appVersionBadge" in home_res.text
    assert "appVerBadge" in home_res.text
    assert "appUpdateBanner" in home_res.text
    assert "bootstrap@5.3.9/dist/css/bootstrap.min.css" in home_res.text
    assert "bootstrap@5.3.9/dist/js/bootstrap.bundle.min.js" in home_res.text
    assert "bootstrap-icons@1.13.2/font/bootstrap-icons.min.css" in home_res.text
    assert "@xterm/xterm@5.5.0" in home_res.text
    assert "pluginConnectTerminal" in home_res.text
    assert "oauthTerminal" in home_res.text
    assert "onboardOauthTerminal" in home_res.text
    assert "memoryModeSeg" in home_res.text
    assert "memoryToolbar" in home_res.text
    assert "memoryPreview" in home_res.text
    assert "perm-seg-btn" in home_res.text
    assert "tabler-icons" not in home_res.text
    assert "no-cache" in (home_res.headers.get("cache-control") or "").lower()
    assert f"/static/styles.css?v={__version__}-" in home_res.text
    assert f"/static/app.js?v={__version__}-" in home_res.text
    assert "/static/favicon.svg" in home_res.text
    fav_svg = client.get("/static/favicon.svg")
    assert fav_svg.status_code == 200 and "<svg" in fav_svg.text
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200

    assert "tab-terminal" in home_res.text
    assert "agyTerminalHost" in home_res.text
    assert "termChannelSeg" in home_res.text
    assert "startAgyTermBtn" in home_res.text

    for spa_path in ("/chat", "/terminal", "/memory", "/ops", "/settings", "/settings/plugins/gmail"):
        spa_res = client.get(spa_path)
        assert spa_res.status_code == 200
        assert "tab-plugin-perms" in spa_res.text
        assert "no-cache" in (spa_res.headers.get("cache-control") or "").lower()
    assert client.get("/api/neexistuje").status_code == 404
    health_res = client.get("/api/health")
    assert health_res.status_code == 200
    assert health_res.json()["version"] == __version__
    auth_st = client.get("/api/auth/state")
    assert auth_st.status_code == 200
    assert auth_st.json()["app_version"] == __version__
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/app/version").status_code == 401

    ok = client.get("/api/status", headers={"Authorization": "Bearer tajne123"})
    assert ok.status_code == 200
    assert ok.json()["agy_available"] is True
    assert ok.json()["app_version"] == __version__


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

    # Úprava e-mailu i uživatelského jména a ověření přihlášení oběma identifikátory
    acct_res = client.post(
        "/api/account/email",
        headers={"Authorization": f"Bearer {new_tok}"},
        json={"email": "novy@example.com", "username": "NovyLogin"},
    )
    assert acct_res.status_code == 200
    assert acct_res.json()["owner"]["email"] == "novy@example.com"
    assert acct_res.json()["owner"]["username"] == "novylogin"
    assert (
        client.post(
            "/api/login",
            json={"identifier": "novylogin", "password": "noveheslo12345"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/login",
            json={"identifier": "novy@example.com", "password": "noveheslo12345"},
        ).status_code
        == 200
    )


async def test_app_version_checker_and_api(tmp_path: Path) -> None:
    from app.version_check import _with_cache_buster

    assert fetch_latest_app_version_sync("") is None
    assert fetch_latest_app_version_sync("file:///etc/passwd") is None
    assert "?_=" in _with_cache_buster(
        "https://raw.githubusercontent.com/petrbla/agy-assistant/main/VERSION"
    )
    assert _with_cache_buster("https://example.invalid/VERSION") == "https://example.invalid/VERSION"

    calls = 0
    remote_value: str | None = "0.0.6"

    def fake_fetch(_url: str) -> str | None:
        nonlocal calls
        calls += 1
        return remote_value

    settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
        app_version_check_url="https://example.invalid/VERSION",
    )
    checker = AppVersionChecker(
        settings,
        current_version="0.0.6",
        fetcher=fake_fetch,
    )

    info1 = await checker.get_version_info(force_refresh=False)
    assert info1 == {
        "app_version": "0.0.6",
        "app_latest_version": "0.0.6",
        "app_up_to_date": True,
        "app_update_available": False,
    }
    assert calls == 1

    # Cache zabrání opakovanému stažení při force_refresh=False
    info_cached = await checker.get_version_info(force_refresh=False)
    assert info_cached["app_up_to_date"] is True
    assert calls == 1

    # Novější verze po vynuceném obnovení
    remote_value = "0.0.7"
    info_update = await checker.get_version_info(force_refresh=True)
    assert info_update == {
        "app_version": "0.0.6",
        "app_latest_version": "0.0.7",
        "app_up_to_date": False,
        "app_update_available": True,
    }
    assert calls == 2

    # Chyba / nedostupný vzdálený zdroj -> graceful fallback bez výjimky
    remote_value = None
    info_err = await checker.get_version_info(force_refresh=True)
    assert info_err == {
        "app_version": "0.0.6",
        "app_latest_version": None,
        "app_up_to_date": None,
        "app_update_available": False,
    }
    assert calls == 3

    # Ověření API endpointů /api/app/version a /api/status s tokenem i bez něj
    remote_value = "0.0.7"
    api_dir = tmp_path / "api"
    api_dir.mkdir(parents=True, exist_ok=True)
    client = _build_client(
        api_dir,
        auth_token="tajne-verze",
        app_version_check_url="https://example.invalid/VERSION",
        app_version_checker=checker,
    )
    assert client.get("/api/app/version").status_code == 401

    ver_res = client.get(
        "/api/app/version?refresh=true",
        headers={"Authorization": "Bearer tajne-verze"},
    )
    assert ver_res.status_code == 200
    assert ver_res.json() == {
        "app_version": "0.0.6",
        "app_latest_version": "0.0.7",
        "app_up_to_date": False,
        "app_update_available": True,
    }

    st_res = client.get(
        "/api/status",
        headers={"Authorization": "Bearer tajne-verze"},
    )
    assert st_res.status_code == 200
    st_json = st_res.json()
    assert st_json["app_version"] == "0.0.6"
    assert st_json["app_latest_version"] == "0.0.7"
    assert st_json["app_up_to_date"] is False
    assert st_json["app_update_available"] is True


def test_pty_websockets_auth_and_streaming(tmp_path: Path) -> None:
    import pytest
    from starlette.websockets import WebSocketDisconnect

    from app.auth_session import AgyAuthManager
    from app.plugins import PluginManager

    settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
        home_dir=tmp_path / "home",
        web_auth_token="ws-secret",
    )
    (tmp_path / "MEMORY.md").write_text("# Paměť", encoding="utf-8")
    db = AssistantDB(settings.db_path)
    memory = WorkspaceMemory(tmp_path)
    runner = AsyncMock()
    auth_mgr = AgyAuthManager(settings)
    plugin_mgr = PluginManager(settings, db)

    plugin_inputs: list[str] = []
    plugin_resizes: list[tuple[int, int]] = []
    auth_inputs: list[str] = []
    auth_resizes: list[tuple[int, int]] = []

    plugin_mgr.write_connect_raw = lambda data: plugin_inputs.append(data)  # type: ignore[method-assign]
    plugin_mgr.resize_connect = lambda c, r: plugin_resizes.append((c, r))  # type: ignore[method-assign]
    auth_mgr.write_raw = lambda data: auth_inputs.append(data)  # type: ignore[method-assign]
    auth_mgr.resize = lambda c, r: auth_resizes.append((c, r))  # type: ignore[method-assign]

    app = create_app(
        settings,
        db,
        memory,
        runner,
        auth_manager=auth_mgr,
        plugin_manager=plugin_mgr,
    )
    client = TestClient(app)

    # 1. Odmítnutí neplatného tokenu na obou WS endpointech
    for ws_path in ("/ws/pty/plugin", "/ws/pty/auth"):
        with client.websocket_connect(ws_path) as ws:
            ws.send_json({"type": "auth", "token": "spatny-token"})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4401

    # 2. Plugin PTY WebSocket: snapshot, resize, input, live chunk i exit
    plugin_mgr._connect_state.running = True
    plugin_mgr._connect_state.exit_code = None
    plugin_mgr._raw_output = "\x1b[32mHello TUI\x1b[0m"
    with client.websocket_connect("/ws/pty/plugin") as ws:
        ws.send_json({"type": "auth", "token": "ws-secret"})
        assert ws.receive_json() == {"type": "ready", "running": True}
        assert ws.receive_json() == {"type": "output", "data": "\x1b[32mHello TUI\x1b[0m"}

        ws.send_json({"type": "resize", "cols": 92, "rows": 28})
        ws.send_json({"type": "input", "data": "\x1b[B"})

        plugin_mgr._broadcast_connect("next-chunk")
        assert ws.receive_json() == {"type": "output", "data": "next-chunk"}

        plugin_mgr._connect_state.exit_code = 0
        plugin_mgr._broadcast_connect(None)
        assert ws.receive_json() == {"type": "exit", "exit_code": 0}

    assert plugin_resizes == [(92, 28)]
    assert plugin_inputs == ["\x1b[B"]

    # 3. Auth PTY WebSocket: snapshot, resize, input i exit
    auth_mgr._state.running = True
    auth_mgr._state.exit_code = None
    auth_mgr._raw_output = "Prompt>"
    with client.websocket_connect("/ws/pty/auth") as ws:
        ws.send_json({"type": "auth", "token": "ws-secret"})
        assert ws.receive_json() == {"type": "ready", "running": True}
        assert ws.receive_json() == {"type": "output", "data": "Prompt>"}

        ws.send_json({"type": "resize", "cols": 88, "rows": 24})
        ws.send_json({"type": "input", "data": "\r"})

        auth_mgr._state.exit_code = 0
        auth_mgr._broadcast(None)
        assert ws.receive_json() == {"type": "exit", "exit_code": 0}

    assert auth_resizes == [(88, 24)]
    assert auth_inputs == ["\r"]


def test_memory_export_and_import_api_endpoints(tmp_path: Path) -> None:
    import io
    import json
    import zipfile
    from app.scheduler import AssistantScheduler

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "SOUL.md").write_text("# Původní duše", encoding="utf-8")
    (ws / "MEMORY.md").write_text("# Původní paměť", encoding="utf-8")
    (ws / "memory").mkdir()
    (ws / "memory" / "2026-11-20.md").write_text("Záznam", encoding="utf-8")

    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data", web_auth_token="mem-secret")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    db.update_owner(morning_briefing_cron="30 7 * * *", heartbeat_interval_minutes=45)
    db.upsert_scheduled_task(
        name="Ranní test",
        prompt="Připomeň vodu",
        schedule_type="cron",
        cron_expr="0 10 * * *",
    )

    memory = WorkspaceMemory(ws)
    runner = AsyncMock()
    runner.close_all = AsyncMock()
    scheduler = AssistantScheduler(settings, runner, db=db)
    app = create_app(settings, db, memory, runner, scheduler=scheduler)
    client = TestClient(app)

    assert client.get("/api/memory/export").status_code == 401
    assert client.post("/api/memory/import", content=b"123").status_code == 401

    headers = {"Authorization": "Bearer mem-secret"}
    exp_res = client.get("/api/memory/export", headers=headers)
    assert exp_res.status_code == 200
    assert exp_res.headers["content-type"].startswith("application/zip")
    assert exp_res.headers.get("x-exported-tasks") == "1"
    with zipfile.ZipFile(io.BytesIO(exp_res.content), "r") as zf:
        assert "SOUL.md" in zf.namelist()
        assert "memory/2026-11-20.md" in zf.namelist()
        assert "scheduler.json" in zf.namelist()
        sched_json = json.loads(zf.read("scheduler.json").decode("utf-8"))
        assert sched_json["system"]["morning_briefing_cron"] == "30 7 * * *"
        assert sched_json["system"]["heartbeat_interval_minutes"] == 45
        assert [t_item["name"] for t_item in sched_json["tasks"]] == ["Ranní test"]

    # Přidání dočasných souborů/úloh a změna nastavení plánovače před čistou obnovou
    (ws / "memory" / "docasny.md").write_text("smazat", encoding="utf-8")
    (ws / "SOUL.md").write_text("# Změněná duše", encoding="utf-8")
    db.update_owner(morning_briefing_cron="0 11 * * *", heartbeat_interval_minutes=15)
    db.upsert_scheduled_task(
        name="Dočasná úloha",
        prompt="Smazat při replace",
        schedule_type="cron",
        cron_expr="0 12 * * *",
    )

    imp_res = client.post(
        "/api/memory/import?mode=replace",
        headers={**headers, "Content-Type": "application/zip"},
        content=exp_res.content,
    )
    assert imp_res.status_code == 200
    payload = imp_res.json()
    assert payload["status"] == "ok"
    assert payload["mode"] == "replace"
    assert payload["imported_count"] == 3
    assert payload["scheduler_restored"] is True
    assert payload["restored_system_schedule"] is True
    assert payload["imported_tasks_count"] == 1
    assert (ws / "SOUL.md").read_text(encoding="utf-8") == "# Původní duše"
    assert not (ws / "memory" / "docasny.md").exists()

    restored_owner = db.get_owner()
    assert restored_owner is not None
    assert restored_owner.morning_briefing_cron == "30 7 * * *"
    assert restored_owner.heartbeat_interval_minutes == 45
    assert [t_item.name for t_item in db.list_scheduled_tasks()] == ["Ranní test"]
    job_names = {j.name for j in scheduler.list_jobs()}
    assert "Ranní test" in job_names
    assert "Dočasná úloha" not in job_names

    bad_imp = client.post(
        "/api/memory/import",
        headers={**headers, "Content-Type": "application/zip"},
        content=b"not-a-zip",
    )
    assert bad_imp.status_code == 400


def test_2fa_enable_and_disable_validation_returns_400_without_logout(tmp_path: Path) -> None:
    from app.security import generate_totp_code

    client = _build_client(tmp_path)
    home_res = client.get("/")
    assert home_res.status_code == 200
    assert "unit_minutes_short" in home_res.text
    assert "disable2faFeedback" in home_res.text
    assert 'id="cfgModelSelect"' in home_res.text
    assert 'id="cfgEffortRange"' in home_res.text
    assert '<select id="cfgTz"' in home_res.text

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
    tok = setup_res.json()["session_token"]
    headers = {"Authorization": f"Bearer {tok}"}

    setup_2fa = client.post("/api/2fa/setup", headers=headers)
    assert setup_2fa.status_code == 200
    secret = setup_2fa.json()["secret"]
    valid_code = generate_totp_code(secret)

    enable_res = client.post("/api/2fa/enable", headers=headers, json={"code": valid_code})
    assert enable_res.status_code == 200
    assert enable_res.json()["owner"]["totp_enabled"] is True

    # Špatné heslo ani špatný kód nesmí vrátit 401 (nesmí odhlásit relaci v Web UI)
    bad_pwd = client.post(
        "/api/2fa/disable",
        headers=headers,
        json={"password": "spatne-heslo", "code": valid_code},
    )
    assert bad_pwd.status_code == 400
    assert client.get("/api/status", headers=headers).status_code == 200

    wrong_code = f"{(int(valid_code) + 500000) % 1000000:06d}"
    bad_code = client.post(
        "/api/2fa/disable",
        headers=headers,
        json={"password": "heslo12345", "code": wrong_code},
    )
    assert bad_code.status_code == 400
    assert client.get("/api/status", headers=headers).status_code == 200

    ok_dis = client.post(
        "/api/2fa/disable",
        headers=headers,
        json={"password": "heslo12345", "code": generate_totp_code(secret)},
    )
    assert ok_dis.status_code == 200
    assert ok_dis.json()["owner"]["totp_enabled"] is False


def test_idle_reset_and_daily_retention_settings_api(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    html_res = client.get("/")
    assert html_res.status_code == 200
    assert 'id="cfgIdleResetEnabled"' in html_res.text
    assert 'id="cfgIdleResetMinutes"' in html_res.text
    assert 'id="cfgDailyRetentionDays"' in html_res.text

    cur = client.get("/api/settings")
    assert cur.status_code == 200
    assert cur.json()["idle_reset_enabled"] is True
    assert cur.json()["idle_reset_minutes"] == 120
    assert cur.json()["daily_memory_retention_days"] == 30

    upd = client.post(
        "/api/settings",
        json={
            "idle_reset_enabled": False,
            "idle_reset_minutes": 90,
            "daily_memory_retention_days": 14,
        },
    )
    assert upd.status_code == 200
    body = upd.json()
    assert body["idle_reset_enabled"] is False
    assert body["idle_reset_minutes"] == 90
    assert body["daily_memory_retention_days"] == 14

    bad_idle = client.post("/api/settings", json={"idle_reset_minutes": 5})
    assert bad_idle.status_code == 422
    bad_ret = client.post("/api/settings", json={"daily_memory_retention_days": -1})
    assert bad_ret.status_code == 422


def test_terminal_endpoints_and_ws_auth(tmp_path: Path, monkeypatch) -> None:
    import pytest
    from starlette.websockets import WebSocketDisconnect

    from app.auth_session import AgyTerminalManager, TerminalSessionState

    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data", web_auth_token="tajne123")
    (tmp_path / "MEMORY.md").write_text("# Paměť", encoding="utf-8")
    db = AssistantDB(settings.db_path)
    memory = WorkspaceMemory(tmp_path)
    runner = AsyncMock()
    runner.check_version = AsyncMock(return_value="1.2.0")
    term_mgr = AgyTerminalManager(settings)
    started_calls: list[dict[str, object]] = []

    async def fake_start(**kwargs):
        started_calls.append(kwargs)
        term_mgr._state = TerminalSessionState(
            running=True,
            context_mode=str(kwargs.get("context_mode") or "auto"),
            command="agy",
            output="",
        )
        return term_mgr.get_state()

    async def fake_stop():
        term_mgr._state.running = False
        term_mgr._broadcast(None)

    monkeypatch.setattr(term_mgr, "start", fake_start)
    monkeypatch.setattr(term_mgr, "stop", fake_stop)

    app = create_app(
        settings,
        db,
        memory,
        runner,
        scheduler=None,
        terminal_manager=term_mgr,
    )
    client = TestClient(app)

    assert client.get("/api/terminal/status").status_code == 401
    assert client.post("/api/terminal/start", json={"context_mode": "auto"}).status_code == 401
    assert client.post("/api/terminal/stop").status_code == 401

    headers = {"Authorization": "Bearer tajne123"}
    st0 = client.get("/api/terminal/status", headers=headers)
    assert st0.status_code == 200
    assert st0.json()["running"] is False

    st1 = client.post(
        "/api/terminal/start",
        headers=headers,
        json={"context_mode": "pracovni", "cols": 110, "rows": 30},
    )
    assert st1.status_code == 200
    assert st1.json()["running"] is True
    assert st1.json()["context_mode"] == "pracovni"
    assert len(started_calls) == 1
    assert started_calls[0]["context_mode"] == "pracovni"
    assert started_calls[0]["cols"] == 110
    assert started_calls[0]["rows"] == 30

    with pytest.raises(WebSocketDisconnect) as bad_ws_exc:
        with client.websocket_connect("/ws/pty/terminal") as ws:
            ws.send_json({"type": "auth", "token": "spatny"})
            ws.receive_json()
    assert bad_ws_exc.value.code == 4401

    written: list[str] = []
    resized: list[tuple[int, int]] = []
    monkeypatch.setattr(term_mgr, "write_raw", lambda d: written.append(d))
    monkeypatch.setattr(term_mgr, "resize", lambda c, r: resized.append((c, r)))

    with client.websocket_connect("/ws/pty/terminal") as ws:
        ws.send_json({"type": "auth", "token": "tajne123"})
        ready = ws.receive_json()
        assert ready == {"type": "ready", "running": True}
        ws.send_json({"type": "input", "data": "ls\r"})
        ws.send_json({"type": "resize", "cols": 120, "rows": 35})

    assert written == ["ls\r"]
    assert resized == [(120, 35)]

    stop_res = client.post("/api/terminal/stop", headers=headers)
    assert stop_res.status_code == 200
    assert client.get("/api/terminal/status", headers=headers).json()["running"] is False




