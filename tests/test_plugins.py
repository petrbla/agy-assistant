from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.agy_runner import AgyRunner
from app.config import Settings
from app.db import AssistantDB
from app.memory import WorkspaceMemory
from app.plugins import (
    MANAGED_PLUGIN_DENY_PREFIXES,
    PLUGIN_CATALOG,
    PluginConnectState,
    PluginManager,
    build_plugin_permissions_prompt_hint,
    compute_plugin_deny_rules,
    detect_installed_plugins_from_disk,
    extract_oauth_callback_info,
    extract_signed_in_account,
    forward_localhost_callback_sync,
    should_set_agy_unattended,
    validate_localhost_callback_url,
    validate_plugin_name,
    validate_plugin_target,
)
from app.runtime_config import sync_agy_settings_file
from app.user_env import prepare_agy_home
from app.web import create_app


def test_validate_plugin_name_and_target() -> None:
    assert validate_plugin_name("gmail") == "gmail"
    assert validate_plugin_name("atlassian-twg-cli") == "atlassian-twg-cli"
    assert validate_plugin_target("gmail") == "gmail"
    assert validate_plugin_target("notion@antigravity-plugins-partners") == "notion@antigravity-plugins-partners"
    assert validate_plugin_target("https://github.com/example-org/sample-plugin") == (
        "https://github.com/example-org/sample-plugin"
    )

    for bad_name in ("", "-rf", "--help", "../etc", "a/b", "a b", "gmail;ls"):
        with pytest.raises(ValueError):
            validate_plugin_name(bad_name)

    for bad_target in (
        "",
        "--flag",
        "../local",
        "http://github.com/org/repo",
        "https://evil.example/org/repo",
        "https://github.com/org/repo?x=1",
        "a@b@c",
        "gmail --help",
    ):
        with pytest.raises(ValueError):
            validate_plugin_target(bad_target)


def test_extract_oauth_callback_info_and_ssrf_validation() -> None:
    local_oauth = (
        "https://accounts.google.com/o/oauth2/v2/auth?access_type=offline"
        "&client_id=test-id.apps.googleusercontent.com"
        "&redirect_uri=http%3A%2F%2F127.0.0.1%3A44535%2Fcallback"
        "&response_type=code&scope=email&state=ABCSTATE999"
    )
    info = extract_oauth_callback_info([local_oauth])
    assert info["flow_type"] == "localhost_callback"
    assert info["callback_port"] == 44535
    assert info["callback_path"] == "/callback"
    assert info["expected_state"] == "ABCSTATE999"

    code_oauth = (
        "https://accounts.google.com/o/oauth2/auth?access_type=offline"
        "&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
        "&response_type=code&state=XYZ"
    )
    info2 = extract_oauth_callback_info([code_oauth])
    assert info2["flow_type"] == "code_paste"
    assert info2["callback_port"] is None

    ok_cb = (
        "http://localhost:44535/callback?state=ABCSTATE999&code=sample-auth-code&scope=email"
    )
    validated = validate_localhost_callback_url(
        ok_cb,
        expected_port=44535,
        expected_path="/callback",
        expected_state="ABCSTATE999",
    )
    assert validated == (
        "http://127.0.0.1:44535/callback?state=ABCSTATE999&code=sample-auth-code&scope=email"
    )

    for bad_cb in (
        "https://127.0.0.1:44535/callback?state=ABCSTATE999&code=123",
        "http://169.254.169.254:44535/callback?state=ABCSTATE999&code=123",
        "http://127.0.0.1:9999/callback?state=ABCSTATE999&code=123",
        "http://127.0.0.1:44535/other?state=ABCSTATE999&code=123",
        "http://127.0.0.1:44535/callback?state=ABCSTATE999",
        "http://127.0.0.1:44535/callback?state=WRONG&code=123",
        "http://127.0.0.1:44535/callback?state=ABCSTATE999&error=access_denied",
    ):
        with pytest.raises(ValueError):
            validate_localhost_callback_url(
                bad_cb,
                expected_port=44535,
                expected_path="/callback",
                expected_state="ABCSTATE999",
            )


def test_forward_localhost_callback_does_not_follow_redirect() -> None:
    received: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            received.append(self.path)
            self.send_response(302)
            self.send_header("Location", "https://antigravity.google/auth-success?app=antigravity")
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        validated = validate_localhost_callback_url(
            f"http://127.0.0.1:{port}/callback?state=S1&code=C1",
            expected_port=port,
            expected_state="S1",
        )
        status, location, _ = forward_localhost_callback_sync(validated, timeout=5.0)
        assert status == 302
        assert location == "https://antigravity.google/auth-success?app=antigravity"
        assert received == ["/callback?state=S1&code=C1"]
    finally:
        server.server_close()


def test_plugin_permissions_rules_settings_sync_and_db_crud(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    s = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data", home_dir=home_dir)
    db = AssistantDB(s.db_path)

    # DB CRUD
    rec = db.upsert_plugin_config(
        "gmail",
        permissions_json=json.dumps({"read": True, "draft": True, "send": False, "organize": False}),
        connected=True,
    )
    assert rec.plugin_name == "gmail"
    assert rec.connected is True
    assert rec.connected_at
    assert "gmail" in db.list_plugin_configs()

    # Zachování cizích deny pravidel + aktualizace spravovaných
    agy_cfg_file = home_dir / ".gemini" / "antigravity-cli" / "settings.json"
    agy_cfg_file.parent.mkdir(parents=True, exist_ok=True)
    agy_cfg_file.write_text(
        json.dumps(
            {
                "permissions": {
                    "deny": ["command(rm -rf /)", "command(gmail mutate send)"],
                    "allow": ["command(git)"],
                }
            }
        ),
        encoding="utf-8",
    )

    # Na disku aktivujeme gmail a gcalendar
    for pname in ("gmail", "gcalendar"):
        pdir = home_dir / ".gemini" / "config" / "plugins" / pname
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "plugin.json").write_text(json.dumps({"name": pname}), encoding="utf-8")

    db.upsert_plugin_config(
        "gcalendar",
        permissions_json=json.dumps({"read": True, "write": True, "delete": False}),
    )

    prepare_agy_home(s, db)
    written = json.loads(agy_cfg_file.read_text(encoding="utf-8"))
    deny_list = written["permissions"]["deny"]
    assert "command(rm -rf /)" in deny_list
    assert "command(gmail mutate send)" in deny_list
    assert "command(gmail mutate trash)" in deny_list
    assert "command(gcalendar mutate delete)" in deny_list
    assert "command(gmail readonly)" not in deny_list
    assert written["permissions"]["allow"] == ["command(git)"]

    cfgs = db.list_plugin_configs()
    assert should_set_agy_unattended({"gmail", "gcalendar"}, cfgs) is True
    hint = build_plugin_permissions_prompt_hint({"gmail", "gcalendar"}, cfgs)
    assert "gmail: ALLOWED(read, draft), DENIED(send, organize)" in hint
    assert "gcalendar: ALLOWED(read, write), DENIED(delete)" in hint

    # Povolení všech akcí odebere managed deny pravidla, ale cizí zachová
    db.upsert_plugin_config(
        "gmail",
        permissions_json=json.dumps({"read": True, "draft": True, "send": True, "organize": True}),
    )
    db.upsert_plugin_config(
        "gcalendar",
        permissions_json=json.dumps({"read": True, "write": True, "delete": True}),
    )
    cfgs2 = db.list_plugin_configs()
    assert should_set_agy_unattended({"gmail", "gcalendar"}, cfgs2) is False
    assert compute_plugin_deny_rules({"gmail", "gcalendar"}, cfgs2) == []
    sync_agy_settings_file(
        s,
        home_dir=home_dir,
        managed_deny_rules=[],
        managed_deny_prefixes=MANAGED_PLUGIN_DENY_PREFIXES,
    )
    cleaned = json.loads(agy_cfg_file.read_text(encoding="utf-8"))
    assert cleaned["permissions"]["deny"] == ["command(rm -rf /)"]

    assert db.delete_plugin_config("gmail") is True
    assert db.get_plugin_config("gmail") is None


async def test_agy_runner_enforces_unattended_and_prompt_hint(monkeypatch, tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    ws_dir = tmp_path / "ws"
    s = Settings(
        workspace_dir=ws_dir,
        data_dir=tmp_path / "data",
        home_dir=home_dir,
        agy_persistent_session=False,
    )
    db = AssistantDB(s.db_path)
    pdir = home_dir / ".gemini" / "config" / "plugins" / "gmail"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(json.dumps({"name": "gmail"}), encoding="utf-8")
    db.upsert_plugin_config(
        "gmail",
        permissions_json=json.dumps({"read": True, "draft": True, "send": False, "organize": False}),
    )

    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (json.dumps({"response": "OK", "conversationId": "c1"}).encode("utf-8"), b"")

    async def _fake_exec(*args: object, **kwargs: object) -> _FakeProc:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    runner = AgyRunner(s, db)
    res = await runner.run("Zkontroluj poštu", session_key="web:auto", source="web")
    assert res.success is True
    assert captured["env"]["AGY_UNATTENDED"] == "1"  # type: ignore[index]
    cmd_args = captured["args"]
    assert any("DENIED(send, organize)" in str(a) for a in cmd_args)  # type: ignore[union-attr]


def test_plugins_rest_api_endpoints(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "MEMORY.md").write_text("# Mem", encoding="utf-8")

    s = Settings(
        workspace_dir=ws_dir,
        data_dir=tmp_path / "data",
        home_dir=home_dir,
        web_auth_token="tajnytoken",
    )
    db = AssistantDB(s.db_path)
    memory = WorkspaceMemory(ws_dir)
    runner = AsyncMock()
    pm = PluginManager(s, db)

    async def _fake_cmd(*cli_args: str, timeout: float = 45.0) -> tuple[int, str]:
        if cli_args == ("list", "--json"):
            disk = sorted((home_dir / ".gemini" / "config" / "plugins").glob("*")) if (home_dir / ".gemini" / "config" / "plugins").is_dir() else []
            out = []
            for d in disk:
                meta = json.loads((d / "plugin.json").read_text(encoding="utf-8"))
                out.append(
                    {
                        "name": meta["name"],
                        "enabled": not meta.get("disabled", False),
                        "version": "0.0.7",
                        "source": "antigravity-plugins-official",
                        "path": str(d),
                    }
                )
            return 0, json.dumps(out)
        if cli_args[0] == "install":
            name = cli_args[1].split("@")[0]
            pdir = home_dir / ".gemini" / "config" / "plugins" / name
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "plugin.json").write_text(json.dumps({"name": name, "version": "0.0.7"}), encoding="utf-8")
            return 0, f"Installed {name}"
        if cli_args[0] in ("enable", "disable"):
            name = cli_args[1]
            pdir = home_dir / ".gemini" / "config" / "plugins" / name / "plugin.json"
            meta = json.loads(pdir.read_text(encoding="utf-8"))
            meta["disabled"] = cli_args[0] == "disable"
            pdir.write_text(json.dumps(meta), encoding="utf-8")
            return 0, "ok"
        if cli_args[0] == "uninstall":
            import shutil

            shutil.rmtree(home_dir / ".gemini" / "config" / "plugins" / cli_args[1], ignore_errors=True)
            return 0, "ok"
        return 1, "unknown"

    pm._run_agy_plugin_cmd = _fake_cmd  # type: ignore[method-assign]

    app = create_app(s, db, memory, runner, plugin_manager=pm)
    client = TestClient(app)
    hdr = {"Authorization": "Bearer tajnytoken"}

    # Auth guard
    assert client.get("/api/plugins").status_code == 401

    # Katalog
    res = client.get("/api/plugins", headers=hdr)
    assert res.status_code == 200
    names = {p["name"] for p in res.json()["plugins"]}
    assert {"gmail", "gcalendar", "gdrive", "notion"}.issubset(names)

    # Instalace
    inst = client.post("/api/plugins/install", headers=hdr, json={"target": "gmail"})
    assert inst.status_code == 200
    assert inst.json()["status"] == "ok"

    # Vypnutí / zapnutí
    tog = client.post("/api/plugins/gmail/toggle", headers=hdr, json={"enabled": False})
    assert tog.status_code == 200
    assert tog.json()["enabled"] is False
    client.post("/api/plugins/gmail/toggle", headers=hdr, json={"enabled": True})

    # Přepínače oprávnění + zápis do settings.json
    perms_res = client.put(
        "/api/plugins/gmail/permissions",
        headers=hdr,
        json={"permissions": {"read": True, "draft": False, "send": False, "organize": False}},
    )
    assert perms_res.status_code == 200
    assert perms_res.json()["permissions"]["draft"] is False
    agy_cfg = json.loads(
        (home_dir / ".gemini" / "antigravity-cli" / "settings.json").read_text(encoding="utf-8")
    )
    assert "command(gmail mutate)" in agy_cfg["permissions"]["deny"]

    # Callback happy path + SSRF ochrana
    class _CbHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "https://antigravity.google/auth-success?app=antigravity")
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            return

    cb_server = HTTPServer(("127.0.0.1", 0), _CbHandler)
    cb_port = cb_server.server_address[1]
    t = threading.Thread(target=cb_server.handle_request, daemon=True)
    t.start()
    try:
        pm._connect_state = PluginConnectState(
            running=True,
            plugin_name="gmail",
            flow_type="localhost_callback",
            callback_port=cb_port,
            callback_path="/callback",
            expected_state="STATE1",
        )
        # Špatný port -> 400 (SSRF blokace)
        bad = client.post(
            "/api/plugins/connect/callback",
            headers=hdr,
            json={"callback_url": f"http://127.0.0.1:{cb_port + 1}/callback?state=STATE1&code=OK"},
        )
        assert bad.status_code == 400

        # Platný callback
        ok_cb = client.post(
            "/api/plugins/connect/callback",
            headers=hdr,
            json={"callback_url": f"http://127.0.0.1:{cb_port}/callback?state=STATE1&code=OK"},
        )
        assert ok_cb.status_code == 200
        assert ok_cb.json()["connected"] is True

        cat2 = client.get("/api/plugins", headers=hdr).json()["plugins"]
        gmail_row = next(p for p in cat2 if p["name"] == "gmail")
        assert gmail_row["connected"] is True
    finally:
        cb_server.server_close()

    # Odinstalace
    uninst = client.delete("/api/plugins/gmail", headers=hdr)
    assert uninst.status_code == 200
    assert db.get_plugin_config("gmail") is None


def test_extract_signed_in_account_and_account_label_persistence(tmp_path: Path) -> None:
    tui_signed_in = (
        "Description: Search, read, draft, and send emails in Gmail.\n"
        "Skills:      gmail\n"
        "Sign-in:     ✓ signed in to Google as user@example.com\n"
        "Marketplace: antigravity-plugins-official\n"
    )
    assert extract_signed_in_account(tui_signed_in) == (True, "user@example.com")
    assert extract_signed_in_account("Sign-in: ✓ signed in") == (True, "")

    for negative in (
        "",
        "Sign in...",
        "Not signed in",
        "Sign out...",
        "Sign-in: Not signed in yet",
    ):
        assert extract_signed_in_account(negative) == (False, "")

    home_dir = tmp_path / "home"
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "MEMORY.md").write_text("# Mem", encoding="utf-8")
    s = Settings(workspace_dir=ws_dir, data_dir=tmp_path / "data", home_dir=home_dir)
    db = AssistantDB(s.db_path)

    saved = db.upsert_plugin_config("gmail", connected=True, account_label="user@example.com")
    assert saved.account_label == "user@example.com"

    pdir = home_dir / ".gemini" / "config" / "plugins" / "gmail"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(json.dumps({"name": "gmail"}), encoding="utf-8")

    pm = PluginManager(s, db)

    async def _list_only(*cli_args: str, timeout: float = 45.0) -> tuple[int, str]:
        if cli_args == ("list", "--json"):
            return 0, json.dumps([{"name": "gmail", "enabled": True, "version": "0.0.7"}])
        return 1, ""

    pm._run_agy_plugin_cmd = _list_only  # type: ignore[method-assign]
    client = TestClient(create_app(s, db, WorkspaceMemory(ws_dir), AsyncMock(), plugin_manager=pm))
    res = client.get("/api/plugins")
    assert res.status_code == 200
    gmail_item = next(p for p in res.json()["plugins"] if p["name"] == "gmail")
    assert gmail_item["connected"] is True
    assert gmail_item["account_label"] == "user@example.com"


def test_plugin_catalog_and_manifest_logos(tmp_path: Path) -> None:
    assert len(PLUGIN_CATALOG) == 27
    for item in PLUGIN_CATALOG:
        assert item.logo_url.startswith("https://"), f"Chybí HTTPS logo_url pro {item.name}"

    home_dir = tmp_path / "home"
    plugins_root = home_dir / ".gemini" / "config" / "plugins"

    # 1. Bezpečné načtení lokálního assets/logo.svg jako data URI
    custom_dir = plugins_root / "custom-local"
    (custom_dir / "assets").mkdir(parents=True, exist_ok=True)
    (custom_dir / "assets" / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>',
        encoding="utf-8",
    )
    (custom_dir / "plugin.json").write_text(
        json.dumps({"name": "custom-local", "logo": "assets/logo.svg"}),
        encoding="utf-8",
    )

    # 2. Path traversal v manifestu nesmí přečíst soubor mimo složku pluginu
    (plugins_root / "secret.svg").write_text("<svg/>", encoding="utf-8")
    evil_dir = plugins_root / "custom-evil"
    evil_dir.mkdir(parents=True, exist_ok=True)
    (evil_dir / "plugin.json").write_text(
        json.dumps({"name": "custom-evil", "logo": "../secret.svg"}),
        encoding="utf-8",
    )

    detected = detect_installed_plugins_from_disk(home_dir)
    assert detected["custom-local"]["logo_url"].startswith("data:image/svg+xml;base64,")
    assert detected["custom-evil"]["logo_url"] == ""
