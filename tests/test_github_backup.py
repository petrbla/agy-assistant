from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import AssistantDB
from app.github_backup import (
    collect_agent_backup_files,
    normalize_github_repo_url,
    run_github_backup,
    validate_github_branch,
)
from app.memory import WorkspaceMemory
from app.scheduler import AssistantScheduler
from app.web import create_app


def test_normalize_github_repo_url_and_branch() -> None:
    assert normalize_github_repo_url("alice/my-backup") == "https://github.com/alice/my-backup.git"
    assert (
        normalize_github_repo_url("https://github.com/alice/my-backup")
        == "https://github.com/alice/my-backup.git"
    )
    assert (
        normalize_github_repo_url("https://github.com/alice/my-backup.git")
        == "https://github.com/alice/my-backup.git"
    )

    for bad in (
        "",
        "alice",
        "http://github.com/alice/repo",
        "https://gitlab.com/alice/repo",
        "https://user:secret@github.com/alice/repo",
        "https://github.com/alice/repo?x=1",
        "--upload-pack=evil/repo",
        "alice/../etc",
    ):
        with pytest.raises(ValueError):
            normalize_github_repo_url(bad)

    assert validate_github_branch("") == "main"
    assert validate_github_branch("  ") == "main"
    assert validate_github_branch("backup/prod-1") == "backup/prod-1"

    for bad_branch in ("-flag", "main..other", "feature//sub", "branch/", "ref.lock", "a b"):
        with pytest.raises(ValueError):
            validate_github_branch(bad_branch)


def test_collect_agent_backup_files_includes_data_and_redacts_secrets(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "SOUL.md").write_text("# Jméno: TestBot", encoding="utf-8")
    (ws / "MEMORY.md").write_text("# Dlouhodobá paměť\n- Uživatel má rád čaj.", encoding="utf-8")
    (ws / "memory").mkdir()
    (ws / "memory" / "2026-11-22.md").write_text("Poznámka ze dne.", encoding="utf-8")
    skill_dir = ws / ".agents" / "skills" / "custom-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: custom-skill\n---\nPokyny", encoding="utf-8")

    secret_pwd_hash = "pbkdf2$verysecretpasswordhash999"
    secret_totp = "JBSWY3DPEHPK3PXPSECRET"
    secret_tg = "123456:TG_SECRET_TOKEN_XYZ"
    secret_gemini = "AIzaSyDummySecretGeminiKey987"
    secret_mcp = "Bearer_MCP_SECRET_TOKEN_777"
    secret_pat = "github_pat_DUMMYSECRETTOKEN12345"

    settings = Settings(
        workspace_dir=ws,
        data_dir=tmp_path / "data",
        gemini_api_key=secret_gemini,
        telegram_bot_token=secret_tg,
        github_backup_enabled=True,
        github_backup_repo="alice/agent-backup",
        github_backup_branch="main",
        github_backup_token=secret_pat,
        github_backup_cron="15 3 * * *",
    )
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    db.update_owner(
        password_hash=secret_pwd_hash,
        totp_enabled=True,
        totp_secret=secret_totp,
        telegram_bot_token=secret_tg,
        gemini_api_key=secret_gemini,
        github_backup_token=secret_pat,
    )
    db.upsert_scheduled_task(
        name="Ranní kontrola",
        prompt="Zkontroluj úkoly",
        schedule_type="cron",
        cron_expr="0 9 * * *",
    )
    db.upsert_mcp_server(
        name="firemni-api",
        scope="pracovni",
        transport="http",
        url="https://mcp.example.com/sse",
        auth_token=secret_mcp,
    )
    db.create_plugin_account(
        "gmail",
        label="Osobní",
        scope="osobni",
        custom_rules="Před smazáním se ptej.",
    )
    owner = db.get_owner()
    assert owner is not None

    files = collect_agent_backup_files(settings, db, owner, app_version="0.0.15")

    assert "workspace/SOUL.md" in files
    assert "workspace/MEMORY.md" in files
    assert "workspace/memory/2026-11-22.md" in files
    assert "workspace/.agents/skills/custom-skill/SKILL.md" in files
    assert "config/agent_config.json" in files
    assert "config/scheduler.json" in files
    assert "config/scheduled_tasks.json" in files
    assert "config/mcp_servers.json" in files
    assert "config/plugins.json" in files
    assert "backup_manifest.json" in files
    assert "README.md" in files

    scheduler_cfg = json.loads(files["config/scheduler.json"])
    assert scheduler_cfg["version"] == 1
    assert scheduler_cfg["system"]["heartbeat_interval_minutes"] == owner.heartbeat_interval_minutes
    assert len(scheduler_cfg["tasks"]) == 1
    assert scheduler_cfg["tasks"][0]["name"] == "Ranní kontrola"
    assert scheduler_cfg["tasks"][0]["cron_expr"] == "0 9 * * *"

    agent_cfg = json.loads(files["config/agent_config.json"])
    assert agent_cfg["github_backup_repo"] == "alice/agent-backup"
    assert "github_backup_token" not in agent_cfg
    assert "gemini_api_key" not in agent_cfg
    assert "telegram_bot_token" not in agent_cfg

    mcp_cfg = json.loads(files["config/mcp_servers.json"])
    assert mcp_cfg[0]["has_auth_token"] is True
    assert "auth_token" not in mcp_cfg[0]

    combined_dump = "\n".join(files.values())
    for forbidden in (secret_pwd_hash, secret_totp, secret_tg, secret_gemini, secret_mcp, secret_pat):
        assert forbidden not in combined_dump


async def test_run_github_backup_end_to_end_against_local_bare_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare_repo = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_repo)], check=True, capture_output=True)

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "MEMORY.md").write_text("# Paměť v1", encoding="utf-8")

    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    db.update_owner(
        github_backup_enabled=True,
        github_backup_repo="alice/agent-backup",
        github_backup_branch="main",
        github_backup_token="pat_test_secret_abc123",
    )

    monkeypatch.setattr("app.github_backup.normalize_github_repo_url", lambda _raw: str(bare_repo))

    # 1. běh -> vytvoří úvodní commit a uloží stav "ok"
    res1 = await run_github_backup(settings, db, app_version="0.0.15")
    assert res1.success is True
    assert res1.changed is True
    assert res1.commit_sha
    assert res1.file_count > 0
    owner1 = db.get_owner()
    assert owner1 is not None
    assert owner1.github_backup_last_status == "ok"

    # 2. běh beze změn -> reportuje changed=False a nevytvoří prázdný commit
    res2 = await run_github_backup(settings, db, app_version="0.0.15")
    assert res2.success is True
    assert res2.changed is False
    owner2 = db.get_owner()
    assert owner2 is not None
    assert owner2.github_backup_last_status == "no_changes"

    log_after_2 = subprocess.run(
        ["git", "--git-dir", str(bare_repo), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert int(log_after_2.stdout.strip()) == 1

    # 3. běh po úpravě paměti -> nový commit
    (ws / "MEMORY.md").write_text("# Paměť v2 – doplněno", encoding="utf-8")
    res3 = await run_github_backup(settings, db, app_version="0.0.15")
    assert res3.success is True
    assert res3.changed is True
    assert res3.commit_sha != res1.commit_sha

    log_after_3 = subprocess.run(
        ["git", "--git-dir", str(bare_repo), "rev-list", "--count", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert int(log_after_3.stdout.strip()) == 2


async def test_github_backup_settings_api_scheduler_and_run_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare_repo = tmp_path / "remote-api.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_repo)], check=True, capture_output=True)

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "MEMORY.md").write_text("# Paměť", encoding="utf-8")

    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data", web_auth_token="gh-api-secret")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    memory = WorkspaceMemory(ws)
    runner = AsyncMock()
    runner.close_all = AsyncMock()
    scheduler = AssistantScheduler(settings, runner, db=db)

    app = create_app(settings, db, memory, runner, scheduler=scheduler)
    client = TestClient(app)
    headers = {"Authorization": "Bearer gh-api-secret"}

    # Neautorizovaný přístup odmítnut
    assert client.post("/api/backup/github/run").status_code == 401

    # Neplatná konfigurace před uložením -> 400
    fail_res = client.post("/api/backup/github/run", headers=headers)
    assert fail_res.status_code == 400

    # Uložení nastavení přes /api/settings (nesmí nikdy vrátit raw PAT!)
    save_res = client.post(
        "/api/settings",
        headers=headers,
        json={
            "github_backup_enabled": True,
            "github_backup_repo": "alice/agent-backup",
            "github_backup_branch": "main",
            "github_backup_token": "github_pat_topsecret_xyz",
            "github_backup_cron": "45 3 * * *",
        },
    )
    assert save_res.status_code == 200
    saved_json = save_res.json()
    assert saved_json["github_backup_enabled"] is True
    assert saved_json["github_backup_repo"] == "https://github.com/alice/agent-backup.git"
    assert saved_json["has_github_backup_token"] is True
    assert "github_backup_token" not in saved_json

    get_res = client.get("/api/settings", headers=headers)
    assert get_res.status_code == 200
    assert get_res.json()["has_github_backup_token"] is True
    assert "github_backup_token" not in get_res.json()

    # Plánovač má registrovaný job "github_backup"
    scheduler.start()
    try:
        assert any(job.id == "github_backup" for job in scheduler.list_jobs())
    finally:
        scheduler.shutdown()

    # Manuální spuštění zálohy přes REST API
    monkeypatch.setattr("app.github_backup.normalize_github_repo_url", lambda _raw: str(bare_repo))
    run_res = client.post("/api/backup/github/run", headers=headers)
    assert run_res.status_code == 200
    run_data = run_res.json()
    assert run_data["success"] is True
    assert run_data["changed"] is True

    # Smazání tokenu
    clear_res = client.post(
        "/api/settings",
        headers=headers,
        json={"clear_github_backup_token": True},
    )
    assert clear_res.status_code == 200
    assert clear_res.json()["has_github_backup_token"] is False


def test_plugin_cards_layout_and_connection_state_styles() -> None:
    root = Path(__file__).resolve().parent.parent
    styles = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    app_js = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")

    assert ".min-w-0" in styles
    assert "backdrop-filter" in styles
    assert "align-items: start;" in styles
    assert ".plugin-card.plugin-card-connected" in styles
    assert ".plugin-card.plugin-card-unconnected" in styles
    assert "plugin-card-connected" in app_js
    assert "plugin-card-unconnected" in app_js
    assert "plugin_status_needs_connect" in app_js
    assert "switchSettingsTab" in app_js
    assert "skipAuthRedirect" in app_js
    assert "mb-auto" not in app_js
    assert "settingsSubnav" in index_html
    assert "tgAdvancedBox" in index_html
    assert "toggleGhBackupBtn" in index_html
    assert "ghBackupGuideBox" in index_html
    assert "toggleMcpCardBtn" in index_html
    assert "accUsername" in index_html
    assert "accountProfileFeedback" in index_html
    assert "accountPwdFeedback" in index_html


def test_scheduler_backup_restore_and_zip_validation(tmp_path: Path) -> None:
    import io
    import zipfile

    from app.github_backup import build_scheduler_backup_payload, restore_scheduler_backup_payload

    ws = tmp_path / "ws"
    ws.mkdir()
    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    owner = db.ensure_owner(settings)
    db.upsert_scheduled_task(
        name="Úloha A",
        prompt="Text A",
        schedule_type="cron",
        cron_expr="15 8 * * *",
    )
    payload = build_scheduler_backup_payload(db, owner)
    assert payload["version"] == 1
    assert len(payload["tasks"]) == 1

    # Neplatný cron výraz musí selhat ještě před úpravou DB
    bad_payload = {
        "system": {"heartbeat_interval_minutes": 30},
        "tasks": [
            {
                "name": "Špatná úloha",
                "prompt": "Test",
                "schedule_type": "cron",
                "cron_expr": "neplatny cron",
            }
        ],
    }
    with pytest.raises(ValueError):
        restore_scheduler_backup_payload(db, bad_payload, mode="replace")
    assert len(db.list_scheduled_tasks()) == 1
    assert db.get_owner().heartbeat_interval_minutes == owner.heartbeat_interval_minutes

    # ZIP s config/scheduler.json (např. ze zálohy GitHub) i neplatný traversal ../scheduler.json
    mem = WorkspaceMemory(ws)
    ok_buf = io.BytesIO()
    with zipfile.ZipFile(ok_buf, "w") as zf:
        zf.writestr("config/scheduler.json", json.dumps(payload))
    imp_res = mem.import_memory_zip(ok_buf.getvalue(), mode="merge")
    assert imp_res["imported_count"] == 0
    assert imp_res["scheduler_data"] == payload

    bad_zip = io.BytesIO()
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../scheduler.json", "{}")
    with pytest.raises(ValueError):
        mem.import_memory_zip(bad_zip.getvalue(), mode="merge")

    bad_json_zip = io.BytesIO()
    with zipfile.ZipFile(bad_json_zip, "w") as zf:
        zf.writestr("scheduler.json", "{neplatny-json")
    with pytest.raises(ValueError):
        mem.import_memory_zip(bad_json_zip.getvalue(), mode="merge")
