from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from app.agy_runner import AgyResult
from app.config import Settings
from app.db import AssistantDB
from app.scheduler import AssistantScheduler, NIGHTLY_MEMORY_FLUSH_PROMPT, is_heartbeat_ok


def test_is_heartbeat_ok() -> None:
    assert is_heartbeat_ok("HEARTBEAT_OK") is True
    assert is_heartbeat_ok("  `heartbeat_ok` \n") is True
    assert is_heartbeat_ok("Máš za 30 minut schůzku.") is False


async def test_scheduler_skips_notification_on_heartbeat_ok(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    runner = AsyncMock()
    runner.run = AsyncMock(
        return_value=AgyResult(text="HEARTBEAT_OK", conversation_id=None, duration_ms=10, success=True)
    )
    notifier = AsyncMock()

    scheduler = AssistantScheduler(settings, runner, notifier=notifier)
    await scheduler.run_heartbeat(notify=True)
    notifier.assert_not_awaited()


async def test_scheduler_sends_notification_on_alert(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    runner = AsyncMock()
    runner.run = AsyncMock(
        return_value=AgyResult(
            text="Přišla urgentní faktura.", conversation_id=None, duration_ms=10, success=True
        )
    )
    notifier = AsyncMock()

    scheduler = AssistantScheduler(settings, runner, notifier=notifier)
    await scheduler.run_heartbeat(notify=True)
    notifier.assert_awaited_once()


async def test_nightly_reset_skips_when_no_active_sessions(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    runner = AsyncMock()

    scheduler = AssistantScheduler(settings, runner, db=db)
    summary = await scheduler.run_nightly_reset()

    assert summary == {
        "flushed_sessions": 0,
        "reset_sessions": 0,
        "pruned_daily_files": 0,
        "errors": [],
    }
    runner.run.assert_not_awaited()


async def test_nightly_reset_flushes_active_sessions_and_resets_ids(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    db.set_conversation_id("tg:12345:auto", "conv-abc-123", "auto")

    runner = AsyncMock()
    runner.run = AsyncMock(
        return_value=AgyResult(
            text="MEMORY_FLUSH_OK", conversation_id="conv-abc-123", duration_ms=50, success=True
        )
    )

    scheduler = AssistantScheduler(settings, runner, db=db)
    summary = await scheduler.run_nightly_reset()

    assert summary["flushed_sessions"] == 1
    assert summary["reset_sessions"] == 1
    assert summary["errors"] == []
    runner.run.assert_awaited_once()
    call_args, call_kwargs = runner.run.await_args
    assert call_args[0] == NIGHTLY_MEMORY_FLUSH_PROMPT
    assert call_kwargs["session_key"] == "tg:12345:auto"
    assert call_kwargs["source"] == "nightly_reset"
    assert call_kwargs["use_conversation"] is True

    conv_id, ctx = db.get_session("tg:12345:auto")
    assert conv_id is None
    assert ctx == "auto"


async def test_nightly_reset_still_resets_on_flush_error(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    db.set_conversation_id("tg:12345:pracovni", "conv-err-1", "pracovni")

    runner = AsyncMock()
    runner.run = AsyncMock(side_effect=RuntimeError("CLI timeout"))

    scheduler = AssistantScheduler(settings, runner, db=db)
    summary = await scheduler.run_nightly_reset()

    assert summary["flushed_sessions"] == 0
    assert summary["reset_sessions"] == 1
    assert len(summary["errors"]) == 1
    conv_id, _ = db.get_session("tg:12345:pracovni")
    assert conv_id is None


async def test_sync_jobs_registers_and_removes_nightly_reset(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    runner = AsyncMock()

    scheduler = AssistantScheduler(settings, runner, db=db)
    scheduler.start()
    try:
        job_ids = {j.id for j in scheduler.list_jobs()}
        assert "nightly_reset" in job_ids

        db.update_owner(nightly_reset_enabled=False)
        scheduler.sync_jobs()
        job_ids_after = {j.id for j in scheduler.list_jobs()}
        assert "nightly_reset" not in job_ids_after
    finally:
        scheduler.shutdown()


async def test_once_task_deleted_after_run_and_expired_cleaned_up(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)

    past_task = db.upsert_scheduled_task(
        name="Stara jednorazovka",
        prompt="Uz probehlo",
        schedule_type="once",
        run_at="2020-01-01T08:00:00",
        action_type="message",
        enabled=True,
    )
    future_task = db.upsert_scheduled_task(
        name="Budouci jednorazovka",
        prompt="Pripominka",
        schedule_type="once",
        run_at="2099-01-01T08:00:00",
        action_type="message",
        enabled=True,
    )
    cron_task = db.upsert_scheduled_task(
        name="Obed",
        prompt="Cas na obed",
        schedule_type="cron",
        cron_expr="0 12 * * *",
        action_type="message",
        enabled=True,
    )

    notifier = AsyncMock()
    scheduler = AssistantScheduler(settings, AsyncMock(), db=db, notifier=notifier)
    scheduler.sync_jobs()

    assert db.get_scheduled_task(past_task.id) is None
    assert db.get_scheduled_task(future_task.id) is not None
    assert db.get_scheduled_task(cron_task.id) is not None

    await scheduler.run_custom_task(task_id=future_task.id, notify=True)
    notifier.assert_awaited_once()
    assert db.get_scheduled_task(future_task.id) is None
    assert db.get_scheduled_task(cron_task.id) is not None


async def test_scheduler_uses_czech_prompts_when_language_cs(tmp_path: Path) -> None:
    from app.i18n import t

    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data", language="cs")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    db.set_conversation_id("tg:1:auto", "conv-cs", "auto")

    runner = AsyncMock()
    runner.run = AsyncMock(
        return_value=AgyResult(text="Pozor na schůzku.", conversation_id=None, duration_ms=10, success=True)
    )
    notifier = AsyncMock()

    scheduler = AssistantScheduler(settings, runner, notifier=notifier, db=db)
    await scheduler.run_heartbeat(notify=True)
    notifier.assert_awaited_once()
    assert "Heartbeat upozornění" in notifier.await_args.args[0]

    await scheduler.run_nightly_reset()
    assert runner.run.await_args.args[0] == t("prompts.nightly_memory_flush", "cs")


async def test_sync_jobs_registers_and_removes_idle_reset(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    runner = AsyncMock()

    scheduler = AssistantScheduler(settings, runner, db=db)
    scheduler.start()
    try:
        assert "idle_reset" in {j.id for j in scheduler.list_jobs()}

        db.update_owner(idle_reset_enabled=False)
        scheduler.sync_jobs()
        assert "idle_reset" not in {j.id for j in scheduler.list_jobs()}
    finally:
        scheduler.shutdown()


async def test_idle_reset_resets_only_stale_non_trivial_sessions(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    settings = Settings(workspace_dir=tmp_path / "ws", data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    db.update_owner(idle_reset_enabled=True, idle_reset_minutes=120)

    now = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
    stale_iso = (now - timedelta(hours=3)).isoformat()
    fresh_iso = (now - timedelta(minutes=10)).isoformat()

    # 1. Stale & 6 běhů -> musí se uklidit a resetovat
    db.set_conversation_id("tg:1:auto", "conv-stale-runs", "auto")
    for _ in range(6):
        db.log_run(
            source="telegram",
            session_key="tg:1:auto",
            context_mode="auto",
            prompt="p",
            response="r",
            conversation_id="conv-stale-runs",
            duration_ms=10,
            success=True,
            total_tokens=250,
        )

    # 2. Stale, ale jen 2 běhy a málo tokenů -> přeskočit (nevyhazovat krátký dotaz)
    db.set_conversation_id("tg:2:pracovni", "conv-stale-short", "pracovni")
    for _ in range(2):
        db.log_run(
            source="telegram",
            session_key="tg:2:pracovni",
            context_mode="pracovni",
            prompt="p",
            response="r",
            conversation_id="conv-stale-short",
            duration_ms=10,
            success=True,
            total_tokens=200,
        )

    # 3. Velká konverzace, ale čerstvá (10 min zpět) -> přeskočit
    db.set_conversation_id("tg:3:osobni", "conv-fresh-big", "osobni")
    for _ in range(8):
        db.log_run(
            source="telegram",
            session_key="tg:3:osobni",
            context_mode="osobni",
            prompt="p",
            response="r",
            conversation_id="conv-fresh-big",
            duration_ms=10,
            success=True,
            total_tokens=4000,
        )

    # 4. Stale, jen 2 běhy, ale >= 20 000 tokenů -> musí se uklidit a resetovat
    db.set_conversation_id("web:auto", "conv-stale-tokens", "auto")
    db.log_run(
        source="web",
        session_key="web:auto",
        context_mode="auto",
        prompt="p",
        response="r",
        conversation_id="conv-stale-tokens",
        duration_ms=10,
        success=True,
        total_tokens=22000,
    )

    with db._connect() as conn:
        for key in ("tg:1:auto", "tg:2:pracovni", "web:auto"):
            conn.execute("UPDATE sessions SET updated_at = ? WHERE session_key = ?", (stale_iso, key))
        conn.execute("UPDATE sessions SET updated_at = ? WHERE session_key = ?", (fresh_iso, "tg:3:osobni"))

    runner = AsyncMock()
    runner.run = AsyncMock(
        return_value=AgyResult(
            text="MEMORY_FLUSH_OK", conversation_id="conv-x", duration_ms=10, success=True
        )
    )
    scheduler = AssistantScheduler(settings, runner, db=db)

    summary = await scheduler.run_idle_reset(now=now)
    assert summary == {
        "checked_sessions": 4,
        "flushed_sessions": 2,
        "reset_sessions": 2,
        "errors": [],
    }
    assert runner.run.await_count == 2
    sources = {c.kwargs["source"] for c in runner.run.await_args_list}
    assert sources == {"idle_reset"}

    assert db.get_session("tg:1:auto")[0] is None
    assert db.get_session("web:auto")[0] is None
    assert db.get_session("tg:2:pracovni")[0] == "conv-stale-short"
    assert db.get_session("tg:3:osobni")[0] == "conv-fresh-big"


async def test_nightly_reset_prunes_old_daily_memory_files(tmp_path: Path) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ws = tmp_path / "ws"
    mem_dir = ws / "memory"
    mem_dir.mkdir(parents=True)
    today_name = f"{datetime.now(ZoneInfo('Europe/Prague')).strftime('%Y-%m-%d')}.md"
    (ws / "MEMORY.md").write_text("# Paměť", encoding="utf-8")
    (mem_dir / "rozdelane.md").write_text("# Rozdělané", encoding="utf-8")
    (mem_dir / "2020-01-01.md").write_text("Velmi starý den", encoding="utf-8")
    (mem_dir / today_name).write_text("Dnešní den", encoding="utf-8")

    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    scheduler = AssistantScheduler(settings, AsyncMock(), db=db)

    summary = await scheduler.run_nightly_reset()
    assert summary["pruned_daily_files"] == 1
    assert not (mem_dir / "2020-01-01.md").exists()
    assert (mem_dir / today_name).is_file()
    assert (mem_dir / "rozdelane.md").is_file()
    assert (ws / "MEMORY.md").is_file()


def test_parse_script_command_valid_and_invalid() -> None:
    import sys
    import pytest
    from app.db import parse_script_command

    argv = parse_script_command('python3 "/home/asistent/workspace/scripts/my job.py" --flag 1')
    assert len(argv) == 4
    assert argv[1:] == ["/home/asistent/workspace/scripts/my job.py", "--flag", "1"]

    py_only = parse_script_command("scripts/job.py --check")
    assert py_only == [sys.executable, "scripts/job.py", "--check"]

    with pytest.raises(ValueError):
        parse_script_command("   ")
    with pytest.raises(ValueError):
        parse_script_command('python3 "unterminated')


async def test_script_task_success_silent_and_logged(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import patch

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    monkeypatch.setenv("APP_UPDATE_TOKEN", "secret-update-token")
    monkeypatch.setenv("APP_UPDATE_URL", "http://updater:8080/v1/update")

    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data", language="cs")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    task = db.upsert_scheduled_task(
        name="Hodinový skript",
        prompt="python3 scripts/demo_job.py --mode fast",
        schedule_type="cron",
        cron_expr="0 * * * *",
        action_type="script",
    )

    runner = AsyncMock()
    notifier = AsyncMock()
    scheduler = AssistantScheduler(settings, runner, notifier=notifier, db=db)

    captured: dict[str, object] = {}

    async def _fake_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"Log zapsan\n", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        res = await scheduler.run_custom_task(task_id=task.id, notify=True)

    assert res.success is True
    assert res.text == "Log zapsan"
    notifier.assert_not_awaited()
    runner.run.assert_not_awaited()

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["cwd"] == str(ws)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["HOME"] == str(settings.effective_home_dir)
    assert env["TZ"] == "Europe/Prague"
    assert "APP_UPDATE_TOKEN" not in env
    assert "APP_UPDATE_URL" not in env

    runs = db.list_runs(limit=5)
    assert len(runs) == 1
    assert runs[0].source == "script"
    assert runs[0].success is True
    assert runs[0].total_tokens == 0
    assert runs[0].response == "Log zapsan"
    assert db.get_activity_stats().cron_runs == 1


async def test_script_task_failure_notifies_and_logs(tmp_path: Path) -> None:
    from unittest.mock import patch

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data", language="cs")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    task = db.upsert_scheduled_task(
        name="Selhávající skript",
        prompt="python3 scripts/fail_job.py",
        schedule_type="cron",
        cron_expr="0 * * * *",
        action_type="script",
    )

    notifier = AsyncMock()
    scheduler = AssistantScheduler(settings, AsyncMock(), notifier=notifier, db=db)

    async def _fake_exec(*_cmd, **_kwargs):
        proc = AsyncMock()
        proc.returncode = 2
        proc.communicate = AsyncMock(return_value=(b"stdout info", b"Traceback: boom"))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        res = await scheduler.run_custom_task(task_id=task.id, notify=True)

    assert res.success is False
    assert "Exit code 2" in (res.error or "")
    notifier.assert_awaited_once()
    alert_msg = notifier.await_args.args[0]
    assert "Selhávající skript" in alert_msg
    assert "Traceback: boom" in alert_msg

    runs = db.list_runs(limit=5)
    assert len(runs) == 1
    assert runs[0].source == "script"
    assert runs[0].success is False
    assert "Traceback: boom" in (runs[0].error or "")


async def test_script_task_timeout_and_missing_binary(tmp_path: Path) -> None:
    import asyncio
    from unittest.mock import patch

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data", language="en")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    t_timeout = db.upsert_scheduled_task(
        name="Timeout skript",
        prompt="python3 scripts/slow.py",
        schedule_type="cron",
        cron_expr="0 * * * *",
        action_type="script",
    )
    t_missing = db.upsert_scheduled_task(
        name="Chybějící binárka",
        prompt="/nonexistent/bin/tool --check",
        schedule_type="cron",
        cron_expr="0 * * * *",
        action_type="script",
    )

    notifier = AsyncMock()
    scheduler = AssistantScheduler(settings, AsyncMock(), notifier=notifier, db=db)

    timeout_proc = AsyncMock()
    timeout_proc.returncode = None
    timeout_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    timeout_proc.kill = lambda: None
    timeout_proc.wait = AsyncMock(return_value=-9)

    with patch("asyncio.create_subprocess_exec", return_value=timeout_proc):
        res_to = await scheduler.run_custom_task(task_id=t_timeout.id, notify=True)

    assert res_to.success is False
    assert "časový limit" in (res_to.error or "")
    timeout_proc.wait.assert_awaited_once()

    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no such file")):
        res_nf = await scheduler.run_custom_task(task_id=t_missing.id, notify=True)

    assert res_nf.success is False
    assert "nebyl nalezen" in (res_nf.error or "")
    assert notifier.await_count == 2
    assert len(db.list_runs(limit=10)) == 2
