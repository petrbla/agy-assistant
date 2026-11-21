from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import Settings
from app.db import AssistantDB
from app.memory import WorkspaceMemory
from app.telegram_bot import TelegramAssistantBot, format_elapsed_seconds


def test_format_usage_report_with_active_conversation(tmp_path: Path) -> None:
    settings = Settings(
        workspace_dir=tmp_path / "ws",
        data_dir=tmp_path / "data",
        telegram_bot_token="123456:TESTTOKEN_FOR_UNIT_TESTS_ONLY",
        telegram_allowed_users="111",
    )
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    db.update_owner(agy_model="gemini-4-neon-medium", nightly_reset_cron="15 0 * * *")
    db.set_conversation_id("tg:111:auto", "conv-999999999999", "auto")
    db.log_run(
        source="telegram",
        session_key="tg:111:auto",
        context_mode="auto",
        prompt="Ahoj",
        response="Nazdar",
        conversation_id="conv-999999999999",
        duration_ms=1200,
        success=True,
        input_tokens=1500,
        output_tokens=300,
        total_tokens=1800,
    )

    mem = WorkspaceMemory(tmp_path / "ws")
    bot = TelegramAssistantBot(settings, db, mem, AsyncMock())
    fake_msg = SimpleNamespace(chat=SimpleNamespace(id=111))

    report_en = bot._format_usage_report(fake_msg)  # type: ignore[arg-type]
    assert "gemini-4-neon-medium" in report_en
    assert "conv-9999999" in report_en
    assert "1 800 tokens" in report_en
    assert "15 0 * * *" in report_en
    assert "/cleanup" in report_en

    report_cs = bot._format_usage_report(fake_msg, lang="cs")  # type: ignore[arg-type]
    assert "1 800 tokenů" in report_cs
    assert "/uklid" in report_cs


def test_format_cleanup_summary() -> None:
    empty = TelegramAssistantBot._format_cleanup_summary(
        {"flushed_sessions": 0, "reset_sessions": 0, "errors": []}
    )
    assert "already clean" in empty

    ok = TelegramAssistantBot._format_cleanup_summary(
        {"flushed_sessions": 2, "reset_sessions": 2, "errors": []}
    )
    assert "2/2" in ok
    assert "reset" in ok

    partial = TelegramAssistantBot._format_cleanup_summary(
        {"flushed_sessions": 1, "reset_sessions": 2, "errors": ["err"]}
    )
    assert "1/2" in partial
    assert "Note:" in partial

    ok_cs = TelegramAssistantBot._format_cleanup_summary(
        {"flushed_sessions": 2, "reset_sessions": 2, "errors": []},
        lang="cs",
    )
    assert "2/2" in ok_cs
    assert "resetován" in ok_cs


def test_format_elapsed_seconds() -> None:
    assert format_elapsed_seconds(0) == "1 s"
    assert format_elapsed_seconds(12) == "12 s"
    assert format_elapsed_seconds(60) == "1 min"
    assert format_elapsed_seconds(120) == "2 min"
    assert format_elapsed_seconds(133) == "2 min 13 s"


def test_duplicate_message_guard(tmp_path: Path) -> None:
    settings = Settings(
        workspace_dir=tmp_path / "ws",
        data_dir=tmp_path / "data",
        telegram_bot_token="123456:TESTTOKEN_FOR_UNIT_TESTS_ONLY",
        telegram_allowed_users="111",
    )
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    bot = TelegramAssistantBot(settings, db, WorkspaceMemory(tmp_path / "ws"), AsyncMock())
    m1 = SimpleNamespace(chat=SimpleNamespace(id=111), message_id=42)
    m2 = SimpleNamespace(chat=SimpleNamespace(id=111), message_id=43)

    assert bot._is_duplicate_message(m1) is False  # type: ignore[arg-type]
    assert bot._is_duplicate_message(m1) is True  # type: ignore[arg-type]
    assert bot._is_duplicate_message(m2) is False  # type: ignore[arg-type]
    assert bot.is_alive() is False
