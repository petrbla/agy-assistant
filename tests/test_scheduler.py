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

    assert summary == {"flushed_sessions": 0, "reset_sessions": 0, "errors": []}
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
