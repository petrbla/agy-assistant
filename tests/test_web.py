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
    assert "updateAppBannerBtn" in home_res.text
    assert "updateAppBtn" in home_res.text
    assert "mcpEditBanner" in home_res.text
    assert "cancelMcpEditBtn" in home_res.text
    assert "mcpClearTokenChk" in home_res.text
    compose_txt = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "updater:" in compose_txt
    assert "nickfedor/watchtower" in compose_txt
    assert "WATCHTOWER_HTTP_API_UPDATE" in compose_txt
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
    assert isinstance(health_res.json().get("boot_id"), str) and health_res.json()["boot_id"]
    auth_st = client.get("/api/auth/state")
    assert auth_st.status_code == 200
    assert auth_st.json()["app_version"] == __version__
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/app/version").status_code == 401
    assert client.post("/api/app/update").status_code == 401

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
        "app_self_update_enabled": False,
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


def test_web_chat_slash_commands(tmp_path: Path) -> None:
    from app.agy_runner import QuotaBucket, QuotaGroup, QuotaSnapshot

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "MEMORY.md").write_text("# Dlouhodobá paměť\n- Projekt Alpha", encoding="utf-8")

    settings = Settings(workspace_dir=ws_dir, data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    db.update_owner(agy_model="gemini-4-neon-medium")
    db.set_conversation_id("web:auto", "conv-web-999888777", "auto")
    db.log_run(
        source="web",
        session_key="web:auto",
        context_mode="auto",
        prompt="Test",
        response="Odpověď",
        conversation_id="conv-web-999888777",
        duration_ms=500,
        success=True,
        input_tokens=900,
        output_tokens=300,
        total_tokens=1200,
    )

    sample_quota = QuotaSnapshot(
        available=True,
        groups=(
            QuotaGroup(
                name="Gemini Models",
                buckets=(
                    QuotaBucket(
                        id="gemini-weekly",
                        name="Weekly Limit Remaining",
                        window="weekly",
                        remaining_fraction=0.71,
                        reset_time="2026-11-28T10:48:15Z",
                    ),
                    QuotaBucket(
                        id="gemini-5h",
                        name="Five Hour Limit Remaining",
                        window="5h",
                        remaining_fraction=0.934,
                        reset_time="2026-11-22T22:32:02Z",
                    ),
                ),
            ),
        ),
        fetched_at="2026-11-22T18:42:52Z",
    )

    runner = AsyncMock()
    runner.check_version = AsyncMock(return_value="1.5.14")
    runner.list_models = AsyncMock(return_value=["gemini-4-neon-medium", "gemini-4-neon-high"])
    runner.get_quota_usage = AsyncMock(return_value=sample_quota)
    runner.run = AsyncMock(
        return_value=AgyResult(
            text="Běžná AI odpověď",
            conversation_id="conv-web-999888777",
            duration_ms=42,
            success=True,
        )
    )
    app = create_app(settings, db, WorkspaceMemory(ws_dir), runner, scheduler=None)
    client = TestClient(app)

    # /usage i /quota vrátí přehled spotřeby včetně 5h a týdenního limitu a nesmí volat runner.run
    for cmd_input, expected_cmd in (("/usage", "usage"), ("/quota", "quota")):
        usage_res = client.post(
            "/api/chat",
            json={"prompt": cmd_input, "session_key": "web:auto", "context_mode": "auto"},
        )
        assert usage_res.status_code == 200
        usage_body = usage_res.json()
        assert usage_body["handled_command"] == expected_cmd
        assert usage_body["total_tokens"] == 0
        assert "gemini-4-neon-medium" in usage_body["text"]
        assert "conv-web-999" in usage_body["text"]
        assert "1 200" in usage_body["text"]
        assert "Gemini Models" in usage_body["text"]
        assert "93.4%" in usage_body["text"]
        assert "71.0%" in usage_body["text"]
    runner.run.assert_not_called()

    # /status, /memory, /help
    st_res = client.post(
        "/api/chat",
        json={"prompt": "/status", "session_key": "web:auto", "context_mode": "auto"},
    )
    assert st_res.status_code == 200
    assert st_res.json()["handled_command"] == "status"
    assert "1.5.14" in st_res.json()["text"]

    mem_res = client.post(
        "/api/chat",
        json={"prompt": "/memory", "session_key": "web:auto", "context_mode": "auto"},
    )
    assert mem_res.status_code == 200
    assert mem_res.json()["handled_command"] == "memory"
    assert "Projekt Alpha" in mem_res.json()["text"]

    help_res = client.post(
        "/api/chat",
        json={"prompt": "/help", "session_key": "web:auto", "context_mode": "auto"},
    )
    assert help_res.status_code == 200
    assert help_res.json()["handled_command"] == "help"
    assert "/usage" in help_res.json()["text"]

    # /pracovni přepne kanál přes switch_channel
    work_res = client.post(
        "/api/chat",
        json={"prompt": "/pracovni", "session_key": "web:auto", "context_mode": "auto"},
    )
    assert work_res.status_code == 200
    assert work_res.json()["handled_command"] == "pracovni"
    assert work_res.json()["switch_channel"] == "pracovni"

    # /model uloží nový model do DB a vrátí active_model
    mdl_res = client.post(
        "/api/chat",
        json={
            "prompt": "/model gemini-4-neon-high",
            "session_key": "web:auto",
            "context_mode": "auto",
        },
    )
    assert mdl_res.status_code == 200
    assert mdl_res.json()["handled_command"] == "model"
    assert mdl_res.json()["active_model"] == "gemini-4-neon-high"
    assert db.get_owner() is not None and db.get_owner().agy_model == "gemini-4-neon-high"

    # /lang cs uloží jazyk do DB a vrátí switch_language
    lang_res = client.post(
        "/api/chat",
        json={"prompt": "/lang cs", "session_key": "web:auto", "context_mode": "auto"},
    )
    assert lang_res.status_code == 200
    assert lang_res.json()["handled_command"] == "lang"
    assert lang_res.json()["switch_language"] == "cs"
    assert db.get_owner() is not None and db.get_owner().language == "cs"

    # /new vyčistí conversation_id v DB
    new_res = client.post(
        "/api/chat",
        json={"prompt": "/new", "session_key": "web:auto", "context_mode": "auto"},
    )
    assert new_res.status_code == 200
    assert new_res.json()["handled_command"] == "new"
    assert new_res.json()["conversation_id"] is None
    assert db.get_session("web:auto")[0] is None

    runner.run.assert_not_called()

    # Cesta začínající lomítkem i neznámý příkaz propadnou normálně do runner.run(...)
    path_res = client.post(
        "/api/chat",
        json={"prompt": "/etc/hosts", "session_key": "web:auto", "context_mode": "auto"},
    )
    assert path_res.status_code == 200
    assert path_res.json()["text"] == "Běžná AI odpověď"

    unk_res = client.post(
        "/api/chat",
        json={"prompt": "/neznamy", "session_key": "web:auto", "context_mode": "auto"},
    )
    assert unk_res.status_code == 200
    assert unk_res.json()["text"] == "Běžná AI odpověď"
    assert runner.run.call_count == 2


def test_agy_quota_api_endpoint(tmp_path: Path) -> None:
    from app.agy_runner import QuotaBucket, QuotaGroup, QuotaSnapshot

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(workspace_dir=ws_dir, data_dir=tmp_path / "data", web_auth_token="tajne-quota")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)

    sample_quota = QuotaSnapshot(
        available=True,
        groups=(
            QuotaGroup(
                name="Gemini Models",
                buckets=(
                    QuotaBucket(
                        id="gemini-5h",
                        name="Five Hour Limit Remaining",
                        window="5h",
                        remaining_fraction=0.85,
                        reset_time="2026-11-22T22:32:02Z",
                    ),
                    QuotaBucket(
                        id="gemini-weekly",
                        name="Weekly Limit Remaining",
                        window="weekly",
                        remaining_fraction=0.62,
                        reset_time="2026-11-28T10:48:15Z",
                    ),
                ),
            ),
        ),
        fetched_at="2026-11-22T18:45:00Z",
    )

    runner = AsyncMock()
    runner.check_version = AsyncMock(return_value="1.5.14")
    runner.list_models = AsyncMock(return_value=[])
    runner.get_quota_usage = AsyncMock(return_value=sample_quota)

    app = create_app(settings, db, WorkspaceMemory(ws_dir), runner, scheduler=None)
    client = TestClient(app)

    assert 'id="agyQuotaCard"' in client.get("/").text
    assert client.get("/api/agy/quota").status_code == 401

    res = client.get("/api/agy/quota?refresh=true", headers={"Authorization": "Bearer tajne-quota"})
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["fetched_at"] == "2026-11-22T18:45:00Z"
    assert body["groups"][0]["name"] == "Gemini Models"
    assert body["groups"][0]["buckets"][0]["remaining_percent"] == 85.0
    assert body["groups"][0]["buckets"][1]["remaining_percent"] == 62.0
    runner.get_quota_usage.assert_awaited_once_with(force_refresh=True)


def test_app_self_update_api_and_security(tmp_path: Path, monkeypatch) -> None:
    import io
    import urllib.error
    import urllib.request
    from email.message import Message

    from app import __version__
    from app.version_check import (
        AppUpdateTriggerResult,
        AppUpdater,
        is_valid_update_url,
        trigger_app_update_sync,
    )

    # 1. Validace URL
    assert is_valid_update_url("") is False
    assert is_valid_update_url("   ") is False
    assert is_valid_update_url("file:///etc/passwd") is False
    assert is_valid_update_url("ftp://updater:8080/v1/update") is False
    assert is_valid_update_url("http://") is False
    assert is_valid_update_url("http://updater:8080/v1/update?async=true") is True
    assert is_valid_update_url("https://portainer.example.com/api/webhooks/123") is True

    # 2. Subprocesy agy nesmí dědit aktualizační tajemství z prostředí
    monkeypatch.setenv("APP_UPDATE_URL", "http://updater:8080/v1/update?async=true")
    monkeypatch.setenv("APP_UPDATE_TOKEN", "tajny-updater-token")
    monkeypatch.setenv("WATCHTOWER_HTTP_API_TOKEN", "tajny-updater-token")
    env_settings = Settings(workspace_dir=tmp_path / "ws_env", data_dir=tmp_path / "data_env")
    sub_env = env_settings.subprocess_env()
    assert "APP_UPDATE_URL" not in sub_env
    assert "APP_UPDATE_TOKEN" not in sub_env
    assert "WATCHTOWER_HTTP_API_TOKEN" not in sub_env

    # 3. trigger_app_update_sync přes mock urllib.request.urlopen (202, 401, 429, URLError)
    captured_reqs: list[urllib.request.Request] = []

    class _FakeResp:
        def __init__(self, status: int, body: bytes = b"") -> None:
            self.status = status
            self._body = body

        def read(self, _n: int = -1) -> bytes:
            return self._body

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *_args) -> None:
            return None

    def _mock_urlopen_202(req: urllib.request.Request, timeout: float = 15.0):
        captured_reqs.append(req)
        return _FakeResp(202, b'{"accepted":true}')

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen_202)
    res_202 = trigger_app_update_sync(
        "http://updater:8080/v1/update?async=true",
        token="tajny-updater-token",
    )
    assert res_202.triggered is True
    assert res_202.status_code == 202
    assert res_202.async_mode is True
    assert len(captured_reqs) == 1
    assert captured_reqs[0].get_method() == "POST"
    assert captured_reqs[0].get_header("Authorization") == "Bearer tajny-updater-token"

    def _mock_urlopen_401(req: urllib.request.Request, timeout: float = 15.0):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", Message(), io.BytesIO(b"bad token")
        )

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen_401)
    res_401 = trigger_app_update_sync("http://updater:8080/v1/update", token="spatny")
    assert res_401.triggered is False
    assert res_401.status_code == 401
    assert res_401.already_in_progress is False

    def _mock_urlopen_429(req: urllib.request.Request, timeout: float = 15.0):
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", Message(), io.BytesIO(b"busy")
        )

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen_429)
    res_429 = trigger_app_update_sync("http://updater:8080/v1/update", token="ok")
    assert res_429.triggered is False
    assert res_429.status_code == 429
    assert res_429.already_in_progress is True

    def _mock_urlopen_err(_req: urllib.request.Request, timeout: float = 15.0):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen_err)
    res_net = trigger_app_update_sync("http://updater:8080/v1/update", token="ok")
    assert res_net.triggered is False
    assert res_net.status_code is None
    assert "updater" in res_net.message.lower()

    # 4. API chování bez nakonfigurovaného updateru -> 503
    unconf_dir = tmp_path / "unconf"
    unconf_dir.mkdir(parents=True)
    monkeypatch.delenv("APP_UPDATE_URL", raising=False)
    monkeypatch.delenv("APP_UPDATE_TOKEN", raising=False)
    monkeypatch.delenv("WATCHTOWER_HTTP_API_TOKEN", raising=False)
    client_unconf = _build_client(unconf_dir, auth_token="web-tajne")
    hdr_unconf = {"Authorization": "Bearer web-tajne"}
    assert client_unconf.post("/api/app/update").status_code == 401
    assert client_unconf.post("/api/app/update", headers=hdr_unconf).status_code == 503

    # 5. API s nakonfigurovaným updaterem -> 200, 409 cooldown, 502 při chybě, bez úniku tokenu
    conf_ws = tmp_path / "conf_ws"
    conf_ws.mkdir(parents=True)
    (conf_ws / "MEMORY.md").write_text("# Paměť", encoding="utf-8")
    conf_settings = Settings(
        workspace_dir=conf_ws,
        data_dir=tmp_path / "conf_data",
        web_auth_token="web-tajne-2",
        app_version_check_url="",
        app_update_url="http://updater:8080/v1/update?async=true",
        app_update_token="tajny-updater-token",
    )
    conf_db = AssistantDB(conf_settings.db_path)
    conf_mem = WorkspaceMemory(conf_ws)
    conf_runner = AsyncMock()
    conf_runner.check_version = AsyncMock(return_value="1.5.14")

    next_result = AppUpdateTriggerResult(
        triggered=True,
        status_code=202,
        message="Aktualizace spuštěna.",
        async_mode=True,
    )

    def _fake_trigger(url: str, token: str) -> AppUpdateTriggerResult:
        assert url == "http://updater:8080/v1/update?async=true"
        assert token == "tajny-updater-token"
        return next_result

    updater = AppUpdater(conf_settings, trigger_fn=_fake_trigger, cooldown_seconds=60.0)
    conf_app = create_app(conf_settings, conf_db, conf_mem, conf_runner, app_updater=updater)
    conf_client = TestClient(conf_app)
    hdr_conf = {"Authorization": "Bearer web-tajne-2"}

    assert conf_client.post("/api/app/update").status_code == 401

    st_res = conf_client.get("/api/status", headers=hdr_conf)
    assert st_res.status_code == 200
    assert st_res.json()["app_self_update_enabled"] is True
    assert "app_update_token" not in st_res.json()
    assert "tajny-updater-token" not in st_res.text

    ver_res = conf_client.get("/api/app/version?refresh=false", headers=hdr_conf)
    assert ver_res.status_code == 200
    assert ver_res.json()["app_self_update_enabled"] is True
    assert "tajny-updater-token" not in ver_res.text

    upd_ok = conf_client.post("/api/app/update", headers=hdr_conf)
    assert upd_ok.status_code == 200
    upd_body = upd_ok.json()
    assert upd_body["status"] == "ok"
    assert upd_body["triggered"] is True
    assert upd_body["current_version"] == __version__
    assert isinstance(upd_body["boot_id"], str) and upd_body["boot_id"]
    assert upd_body["status_code"] == 202
    assert upd_body["async_mode"] is True

    # Okamžité opakování narazí na cooldown -> 409
    upd_dup = conf_client.post("/api/app/update", headers=hdr_conf)
    assert upd_dup.status_code == 409

    # Chyba updateru po resetu cooldownu -> 502
    updater._last_triggered_at = None
    next_result = AppUpdateTriggerResult(
        triggered=False,
        status_code=None,
        message="Aktualizační sidecar updater není dostupný.",
    )
    upd_fail = conf_client.post("/api/app/update", headers=hdr_conf)
    assert upd_fail.status_code == 502
    assert "updater" in upd_fail.json()["detail"].lower()


def test_mcp_edit_preserves_token_headers_and_validates_duplicate_name(tmp_path: Path) -> None:
    ws_dir = tmp_path / "ws_mcp"
    ws_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(workspace_dir=ws_dir, data_dir=tmp_path / "data_mcp", web_auth_token="mcp-tajne")
    db = AssistantDB(settings.db_path)
    runner = AsyncMock()
    runner.check_version = AsyncMock(return_value="1.5.14")
    runner.close_all = AsyncMock()

    app = create_app(settings, db, WorkspaceMemory(ws_dir), runner)
    client = TestClient(app)
    hdr = {"Authorization": "Bearer mcp-tajne"}

    # 1. Vytvoření MCP serveru s Bearer tokenem a vlastními hlavičkami
    res1 = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "nocodb-test",
            "scope": "all",
            "transport": "http",
            "url": "https://example.invalid/mcp/1",
            "auth_token": "tajny-mcp-token",
            "headers": {"X-Custom": "abc"},
            "enabled": True,
        },
    )
    assert res1.status_code == 200
    srv1 = res1.json()
    srv1_id = srv1["id"]
    assert srv1["has_auth_token"] is True
    assert srv1["headers"] == {"X-Custom": "abc"}

    # 2. Úprava podle id bez předání auth_token a headers -> zachová token i hlavičky
    res2 = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "id": srv1_id,
            "name": "nocodb-prejmenovany",
            "scope": "pracovni",
            "transport": "http",
            "url": "https://example.invalid/mcp/2",
            "enabled": False,
        },
    )
    assert res2.status_code == 200
    srv2 = res2.json()
    assert srv2["id"] == srv1_id
    assert srv2["name"] == "nocodb-prejmenovany"
    assert srv2["scope"] == "pracovni"
    assert srv2["url"] == "https://example.invalid/mcp/2"
    assert srv2["enabled"] is False
    assert srv2["has_auth_token"] is True
    assert srv2["headers"] == {"X-Custom": "abc"}
    stored = db.get_mcp_server(srv1_id)
    assert stored is not None and stored.auth_token == "tajny-mcp-token"

    # 3. Smazání tokenu pomocí clear_auth_token=True
    res3 = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "id": srv1_id,
            "name": "nocodb-prejmenovany",
            "scope": "pracovni",
            "transport": "http",
            "url": "https://example.invalid/mcp/2",
            "clear_auth_token": True,
            "enabled": True,
        },
    )
    assert res3.status_code == 200
    assert res3.json()["has_auth_token"] is False
    stored_after = db.get_mcp_server(srv1_id)
    assert stored_after is not None and stored_after.auth_token == ""

    # 4. Kolize názvu při přejmenování na jiný existující MCP server -> 400
    res_other = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "druhy-server",
            "scope": "osobni",
            "transport": "http",
            "url": "https://example.invalid/mcp/3",
            "enabled": True,
        },
    )
    assert res_other.status_code == 200

    res_dup = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "id": srv1_id,
            "name": "druhy-server",
            "scope": "pracovni",
            "transport": "http",
            "url": "https://example.invalid/mcp/2",
            "enabled": True,
        },
    )
    assert res_dup.status_code == 400


def test_mcp_token_reveal_and_connection_status_checks(tmp_path: Path, monkeypatch) -> None:
    import sys
    import app.plugins as plugins_mod

    ws_dir = tmp_path / "ws_mcp_status"
    ws_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        workspace_dir=ws_dir,
        data_dir=tmp_path / "data_mcp_status",
        web_auth_token="mcp-status-tajne",
    )
    db = AssistantDB(settings.db_path)
    runner = AsyncMock()
    runner.check_version = AsyncMock(return_value="1.5.14")
    runner.close_all = AsyncMock()

    app = create_app(settings, db, WorkspaceMemory(ws_dir), runner)
    client = TestClient(app)
    hdr = {"Authorization": "Bearer mcp-status-tajne"}

    # Web UI musí obsahovat tlačítko zobrazení tokenu, hlavičky, JSON import a ověření připojení MCP serverů
    html_res = client.get("/")
    assert html_res.status_code == 200
    assert 'id="toggleMcpTokenBtn"' in html_res.text
    assert 'id="checkMcpStatusBtn"' in html_res.text
    assert 'id="mcpHeaders"' in html_res.text
    assert 'id="toggleMcpJsonImportBtn"' in html_res.text
    assert 'id="mcpJsonImportInput"' in html_res.text
    assert 'id="applyMcpJsonImportBtn"' in html_res.text

    # 1. Vytvoření testovacích MCP serverů (HTTP aktivní, HTTP s chybou autorizace, HTTP vypnutý, stdio OK, stdio neexistující)
    s_http_ok = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "http-ok",
            "scope": "all",
            "transport": "http",
            "url": "https://ok.example.invalid/mcp",
            "auth_token": "secret-tok-123",
            "enabled": True,
        },
    ).json()["id"]
    s_http_err = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "http-err",
            "scope": "all",
            "transport": "http",
            "url": "https://fail.example.invalid/mcp",
            "auth_token": "secret-tok-456",
            "enabled": True,
        },
    ).json()["id"]
    s_http_off = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "http-off",
            "scope": "all",
            "transport": "http",
            "url": "https://off.example.invalid/mcp",
            "enabled": False,
        },
    ).json()["id"]
    s_stdio_ok = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "stdio-ok",
            "scope": "all",
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-V"],
            "enabled": True,
        },
    ).json()["id"]
    s_stdio_bad = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "stdio-bad",
            "scope": "all",
            "transport": "stdio",
            "command": "neexistujici-mcp-binarka-xyz",
            "args": [],
            "enabled": True,
        },
    ).json()["id"]

    # 2. Endpoint pro zobrazení Bearer tokenu vyžaduje auth, veřejný výpis token nevrací
    list_res = client.get("/api/mcp", headers=hdr)
    assert list_res.status_code == 200
    assert "secret-tok-123" not in list_res.text
    assert next(i for i in list_res.json() if i["id"] == s_http_ok)["has_auth_token"] is True

    assert client.get(f"/api/mcp/{s_http_ok}/token").status_code == 401
    assert client.get("/api/mcp/999999/token", headers=hdr).status_code == 404
    tok_res = client.get(f"/api/mcp/{s_http_ok}/token", headers=hdr)
    assert tok_res.status_code == 200
    assert tok_res.json() == {
        "id": s_http_ok,
        "has_auth_token": True,
        "auth_token": "secret-tok-123",
    }

    # 3. Endpointy stavu připojení vyžadují auth a kontrolují HTTP / stdio / vypnuto (mock JSON-RPC)
    assert client.get("/api/mcp/status").status_code == 401
    assert client.post(f"/api/mcp/{s_http_ok}/check").status_code == 401
    assert client.post("/api/mcp/999999/check", headers=hdr).status_code == 404

    def _fake_post_jsonrpc(url: str, payload: dict, **kwargs):
        method = payload.get("method")
        if "fail.example.invalid" in url:
            raise RuntimeError("MCP server odmítl autorizaci (HTTP 401) pro secret-tok-456")
        if method == "initialize":
            return 200, "sid-1", {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}
        if method == "notifications/initialized":
            return 202, "sid-1", {}
        if method == "tools/list":
            return 200, "sid-1", {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "t1"}, {"name": "t2"}]},
            }
        raise AssertionError(f"Neočekávaná MCP metoda: {method}")

    monkeypatch.setattr(plugins_mod, "_post_mcp_jsonrpc_sync", _fake_post_jsonrpc)

    status_res = client.get("/api/mcp/status", headers=hdr)
    assert status_res.status_code == 200
    by_id = {item["id"]: item for item in status_res.json()["items"]}

    assert by_id[s_http_ok]["status"] == "connected"
    assert by_id[s_http_ok]["connected"] is True
    assert by_id[s_http_ok]["tools_count"] == 2
    assert isinstance(by_id[s_http_ok]["latency_ms"], int)

    assert by_id[s_http_err]["status"] == "error"
    assert by_id[s_http_err]["connected"] is False
    assert "401" in by_id[s_http_err]["error"]
    assert "secret-tok-456" not in by_id[s_http_err]["error"]

    assert by_id[s_http_off]["status"] == "disabled"
    assert by_id[s_http_off]["connected"] is False

    assert by_id[s_stdio_ok]["status"] == "connected"
    assert by_id[s_stdio_ok]["connected"] is True

    assert by_id[s_stdio_bad]["status"] == "error"
    assert by_id[s_stdio_bad]["connected"] is False

    single_res = client.post(f"/api/mcp/{s_http_ok}/check", headers=hdr)
    assert single_res.status_code == 200
    assert single_res.json()["connected"] is True
    assert single_res.json()["tools_count"] == 2

    # 4. Vlastní HTTP hlavičky (např. x-api-key): předání do probe, sanitizace chyby, validace a vymazání
    captured_headers: list[dict[str, str]] = []

    def _fake_post_with_headers(url: str, payload: dict, **kwargs):
        captured_headers.append(dict(kwargs.get("headers") or {}))
        if "reject-hdr.example.invalid" in url:
            raise RuntimeError("Chyba autorizace pro tajny-x-api-key-999")
        method = payload.get("method")
        if method == "initialize":
            return 200, "sid-h", {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}
        if method == "notifications/initialized":
            return 202, "sid-h", {}
        if method == "tools/list":
            return 200, "sid-h", {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "list_tables"}]}}
        raise AssertionError(f"Neočekávaná MCP metoda: {method}")

    monkeypatch.setattr(plugins_mod, "_post_mcp_jsonrpc_sync", _fake_post_with_headers)

    s_hdr_ok = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "nocodb-headers",
            "scope": "all",
            "transport": "http",
            "url": "https://hdr.example.invalid/mcp",
            "headers": {"x-api-key": "tajny-x-api-key-123"},
            "enabled": True,
        },
    ).json()["id"]

    hdr_check = client.post(f"/api/mcp/{s_hdr_ok}/check", headers=hdr)
    assert hdr_check.status_code == 200
    assert hdr_check.json()["connected"] is True
    assert hdr_check.json()["tools_count"] == 1
    assert captured_headers and captured_headers[0].get("x-api-key") == "tajny-x-api-key-123"

    s_hdr_err = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "nocodb-headers-err",
            "scope": "all",
            "transport": "http",
            "url": "https://reject-hdr.example.invalid/mcp",
            "headers": {"x-api-key": "tajny-x-api-key-999"},
            "enabled": True,
        },
    ).json()["id"]

    err_check = client.post(f"/api/mcp/{s_hdr_err}/check", headers=hdr)
    assert err_check.status_code == 200
    assert err_check.json()["connected"] is False
    assert "tajny-x-api-key-999" not in err_check.json()["error"]
    assert "***" in err_check.json()["error"]

    # Neplatný název hlavičky s dvojtečkou nebo novým řádkem musí vrátit 400
    bad_hdr = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "bad-hdr-srv",
            "scope": "all",
            "transport": "http",
            "url": "https://hdr.example.invalid/mcp",
            "headers": {"x-bad:key": "val"},
            "enabled": True,
        },
    )
    assert bad_hdr.status_code == 400

    # Vymazání hlaviček přes headers={}
    cleared_hdr = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "id": s_hdr_ok,
            "name": "nocodb-headers",
            "scope": "all",
            "transport": "http",
            "url": "https://hdr.example.invalid/mcp",
            "headers": {},
            "enabled": True,
        },
    )
    assert cleared_hdr.status_code == 200
    assert cleared_hdr.json()["headers"] == {}


def test_standalone_mcp_card_tool_discovery_and_permissions(tmp_path: Path, monkeypatch) -> None:
    import app.plugins as plugins_mod
    from app.plugins import build_plugin_permissions_prompt_hint

    ws_dir = tmp_path / "ws_standalone_mcp"
    ws_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        workspace_dir=ws_dir,
        data_dir=tmp_path / "data_standalone_mcp",
        web_auth_token="standalone-tajne",
    )
    db = AssistantDB(settings.db_path)
    runner = AsyncMock()
    runner.check_version = AsyncMock(return_value="1.5.14")
    runner.close_all = AsyncMock()

    app = create_app(settings, db, WorkspaceMemory(ws_dir), runner)
    client = TestClient(app)
    hdr = {"Authorization": "Bearer standalone-tajne"}

    # 1. Přidání samostatného MCP serveru s mezerami v názvu a vlastní hlavičkou
    created = client.post(
        "/api/mcp",
        headers=hdr,
        json={
            "name": "noco db demo",
            "scope": "all",
            "transport": "http",
            "url": "https://nocodb-demo.example.invalid/mcp",
            "headers": {"x-api-key": "demo-noco-key-123"},
            "enabled": True,
        },
    )
    assert created.status_code == 200
    srv_id = created.json()["id"]

    # Server musí být hned vidět v /api/plugins jako samostatná karta `mcp-{id}`
    plugins_res = client.get("/api/plugins", headers=hdr)
    assert plugins_res.status_code == 200
    standalone_card = next(p for p in plugins_res.json()["plugins"] if p["name"] == f"mcp-{srv_id}")
    assert standalone_card["display_name"] == "noco db demo"
    assert standalone_card["standalone_mcp"] is True
    assert standalone_card["connected"] is True
    assert standalone_card["custom_mcp"]["has_headers"] is True

    # 2. Mock JSON-RPC tools/list -> kontrola stavu rovnou uloží nástroje do cache
    def _fake_post_jsonrpc(url: str, payload: dict, **kwargs):
        method = payload.get("method")
        if method == "initialize":
            return 200, "sid-demo", {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}
        if method == "notifications/initialized":
            return 202, "sid-demo", {}
        if method == "tools/list":
            return 200, "sid-demo", {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {"name": "list_records", "title": "List Records", "description": "Read rows"},
                        {"name": "delete_record", "title": "Delete Record", "description": "Delete a row"},
                    ]
                },
            }
        raise AssertionError(f"Neočekávaná MCP metoda: {method}")

    monkeypatch.setattr(plugins_mod, "_post_mcp_jsonrpc_sync", _fake_post_jsonrpc)

    status_res = client.get("/api/mcp/status", headers=hdr)
    assert status_res.status_code == 200
    st_item = next(i for i in status_res.json()["items"] if i["id"] == srv_id)
    assert st_item["connected"] is True
    assert st_item["tools_count"] == 2
    assert st_item["tools_cache_updated"] is True
    assert "_discovered_tools" not in st_item

    # Karta v /api/plugins i výpis /api/mcp teď ukazují 2 nástroje a výchozí oprávnění
    plugins_after = client.get("/api/plugins", headers=hdr).json()["plugins"]
    card_after = next(p for p in plugins_after if p["name"] == f"mcp-{srv_id}")
    assert card_after["tools_count"] == 2
    tools_by_key = {t["key"]: t for t in card_after["custom_mcp"]["tools"]}
    assert tools_by_key["list_records"]["mode"] == "allow"
    assert tools_by_key["delete_record"]["mode"] == "ask"

    # 3. Uložení 3-stavových oprávnění pro `mcp-{srv_id}` a kontrola prompt hintu
    upd_perms = client.put(
        f"/api/plugins/mcp-{srv_id}/tool-permissions",
        headers=hdr,
        json={"tool_permissions": {"list_records": "allow", "delete_record": "block"}},
    )
    assert upd_perms.status_code == 200
    upd_tools = {t["key"]: t for t in upd_perms.json()["custom_mcp"]["tools"]}
    assert upd_tools["delete_record"]["mode"] == "block"

    hint = build_plugin_permissions_prompt_hint(
        set(), {}, mcp_servers=db.list_mcp_servers(), context_mode="auto"
    )
    assert "mcp:noco db demo" in hint
    assert "BLOCKED_TOOLS(delete_record)" in hint

    # 4. Ruční discover i smazání přes `mcp-{srv_id}` endpointy fungují
    disc_res = client.post(f"/api/plugins/mcp-{srv_id}/tools/discover", headers=hdr)
    assert disc_res.status_code == 200
    assert disc_res.json()["count"] == 2

    del_res = client.delete(f"/api/plugins/mcp-{srv_id}/custom-url", headers=hdr)
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] is True
    assert all(item["id"] != srv_id for item in client.get("/api/mcp", headers=hdr).json())


def test_scheduled_script_task_api_validation_and_run(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch
    from app.scheduler import AssistantScheduler

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data", web_auth_token="task-secret")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    memory = WorkspaceMemory(ws)
    runner = AsyncMock()
    scheduler = AssistantScheduler(settings, runner, db=db)

    app = create_app(settings, db, memory, runner, scheduler=scheduler)
    client = TestClient(app)
    hdr = {"Authorization": "Bearer task-secret"}

    bad_res = client.post(
        "/api/tasks",
        headers=hdr,
        json={
            "name": "Neplatný skript",
            "prompt": 'python3 "neuzavrene',
            "schedule_type": "cron",
            "cron_expr": "0 * * * *",
            "action_type": "script",
        },
    )
    assert bad_res.status_code == 400

    ok_res = client.post(
        "/api/tasks",
        headers=hdr,
        json={
            "name": "Hodinový skript",
            "prompt": "python3 scripts/demo.py",
            "schedule_type": "cron",
            "cron_expr": "0 * * * *",
            "action_type": "script",
        },
    )
    assert ok_res.status_code == 200
    created = ok_res.json()
    assert created["action_type"] == "script"
    assert created["prompt"] == "python3 scripts/demo.py"

    async def _fake_exec(*_cmd, **_kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"hotovo\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        run_res = client.post(f"/api/tasks/{created['id']}/run", headers=hdr)
    assert run_res.status_code == 200
    run_data = run_res.json()
    assert run_data["success"] is True
    assert run_data["text"] == "hotovo"








