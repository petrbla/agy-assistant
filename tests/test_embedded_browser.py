from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.agy_runner import AgyResult
from app.config import Settings
from app.db import AssistantDB
from app.embedded_browser import EmbeddedBrowserManager, EmbeddedBrowserState
from app.memory import WorkspaceMemory
from app.plugins import PluginConnectState
from app.web import create_app


def _build_browser_test_client(
    tmp_path: Path,
    *,
    plugin_manager: MagicMock | None = None,
    browser_manager: MagicMock | None = None,
) -> TestClient:
    settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
        web_auth_token="tajne123",
    )
    (tmp_path / "MEMORY.md").write_text("# Paměť", encoding="utf-8")
    db = AssistantDB(settings.db_path)
    memory = WorkspaceMemory(tmp_path)
    runner = AsyncMock()
    runner.close_all = AsyncMock()
    runner.run = AsyncMock(
        return_value=AgyResult(text="OK", conversation_id="c1", duration_ms=10, success=True)
    )
    app = create_app(
        settings,
        db,
        memory,
        runner,
        plugin_manager=plugin_manager,
        browser_manager=browser_manager,
    )
    return TestClient(app)


def test_embedded_browser_callback_matching() -> None:
    mgr = EmbeddedBrowserManager()
    mgr._callback_port = 43210
    mgr._callback_path = "/callback"

    assert mgr.get_last_frame() == ""
    mgr._last_frame = "abc123"
    assert mgr.get_last_frame() == "abc123"

    assert mgr._is_matching_callback_url("http://127.0.0.1:43210/callback?state=s1&code=abc")
    assert mgr._is_matching_callback_url("http://localhost:43210/callback?error=access_denied")
    assert not mgr._is_matching_callback_url("http://127.0.0.1:99999/callback?code=abc")
    assert not mgr._is_matching_callback_url("https://accounts.google.com/o/oauth2/auth?code=abc")
    assert not mgr._is_matching_callback_url("http://127.0.0.1:43210/other?code=abc")
    assert not mgr._is_matching_callback_url("http://127.0.0.1:43210/callback?state=s1")


async def test_embedded_browser_enter_key_sends_cr() -> None:
    mgr = EmbeddedBrowserManager()
    mgr._state.running = True
    mgr._cdp_ws = object()
    mgr._send_cdp = AsyncMock()  # type: ignore[method-assign]

    await mgr.handle_client_event(
        {
            "type": "key",
            "action": "keyDown",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
        }
    )

    mgr._send_cdp.assert_awaited_once_with(
        "Input.dispatchKeyEvent",
        {
            "type": "keyDown",
            "key": "Enter",
            "code": "Enter",
            "text": "\r",
            "unmodifiedText": "\r",
            "windowsVirtualKeyCode": 13,
        },
    )


async def test_embedded_browser_rejects_invalid_url() -> None:
    mgr = EmbeddedBrowserManager()
    with pytest.raises(ValueError):
        await mgr.start("file:///etc/passwd")
    with pytest.raises(ValueError):
        await mgr.start("javascript:alert(1)")


def test_plugin_browser_endpoints_flow(tmp_path: Path) -> None:
    pm = MagicMock()
    pm.get_connect_state.return_value = PluginConnectState(
        running=True,
        plugin_name="gmail",
        flow_type="localhost_callback",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth?client_id=test",
        callback_port=51234,
        callback_path="/callback",
        expected_state="st123",
    )
    pm.finalize_browser_oauth = AsyncMock(
        return_value={"status": "ok", "plugin": "gmail", "connected": True}
    )
    pm.stop_connect = AsyncMock()
    pm.get_enabled_plugin_names.return_value = []
    pm.list_active_accounts_by_context.return_value = {}

    bm = MagicMock()
    bm.is_available.return_value = True
    bm.get_state.return_value = EmbeddedBrowserState(
        available=True,
        running=False,
        callback_reached=False,
    )
    bm.start = AsyncMock(
        return_value=EmbeddedBrowserState(
            available=True,
            running=True,
            current_url="https://accounts.google.com/o/oauth2/v2/auth?client_id=test",
        )
    )
    bm.stop = AsyncMock()

    client = _build_browser_test_client(tmp_path, plugin_manager=pm, browser_manager=bm)
    headers = {"Authorization": "Bearer tajne123"}

    st_res = client.get("/api/plugins/connect/status", headers=headers)
    assert st_res.status_code == 200
    assert st_res.json()["browser_available"] is True

    start_res = client.post("/api/plugins/connect/browser/start", headers=headers)
    assert start_res.status_code == 200
    assert start_res.json()["running"] is True
    bm.start.assert_awaited_once()

    # Dokud callback nedorazil, complete vrátí 400
    comp_fail = client.post("/api/plugins/connect/browser/complete", headers=headers)
    assert comp_fail.status_code == 400

    # Po dosažení callbacku complete dokončí relaci a zavře prohlížeč
    bm.get_state.return_value = EmbeddedBrowserState(
        available=True,
        running=True,
        callback_reached=True,
        callback_url="http://127.0.0.1:51234/callback?state=st123&code=xyz",
    )
    comp_ok = client.post("/api/plugins/connect/browser/complete", headers=headers)
    assert comp_ok.status_code == 200
    assert comp_ok.json()["connected"] is True
    bm.stop.assert_awaited()
    pm.finalize_browser_oauth.assert_awaited_once()


def test_plugin_browser_start_requires_active_oauth(tmp_path: Path) -> None:
    pm = MagicMock()
    pm.get_connect_state.return_value = PluginConnectState(running=False, auth_url="")
    bm = MagicMock()
    bm.is_available.return_value = True
    client = _build_browser_test_client(tmp_path, plugin_manager=pm, browser_manager=bm)
    res = client.post(
        "/api/plugins/connect/browser/start",
        headers={"Authorization": "Bearer tajne123"},
    )
    assert res.status_code == 400
