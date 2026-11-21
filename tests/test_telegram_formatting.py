from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from app.config import Settings
from app.db import AssistantDB
from app.memory import WorkspaceMemory
from app.telegram_bot import (
    TelegramAssistantBot,
    build_model_selection_keyboard,
    markdown_to_telegram_html,
    split_model_effort,
)


def test_markdown_to_telegram_html_formatting_and_escaping() -> None:
    md = (
        "## Přehled úkolů\n"
        "- **Tučný bod** a *kurzíva* a ~~škrtnuté~~\n"
        "- Proměnná `web_auth_token` s podtržítky `a < b & c`\n"
        "- Odkaz: [Web](https://example.com/?a=1&b=2)\n"
        "- Surové znaky: 5 < 10 & > 2\n"
        "```python\nx = a < b and '**not bold**'\n```"
    )
    out = markdown_to_telegram_html(md)
    assert "<b>Přehled úkolů</b>" in out
    assert "• <b>Tučný bod</b> a <i>kurzíva</i> a <s>škrtnuté</s>" in out
    assert "<code>web_auth_token</code>" in out
    assert "<code>a &lt; b &amp; c</code>" in out
    assert '<a href="https://example.com/?a=1&amp;b=2">Web</a>' in out
    assert "5 &lt; 10 &amp; &gt; 2" in out
    assert '<pre><code class="language-python">x = a &lt; b and \'**not bold**\'</code></pre>' in out
    assert "<i>auth</i>" not in out

    # Osamocená dvojice zpětných uvozovek nesmí sežrat zbytek odstavce jako <code>
    tricky = "Odesílám uvozovky (``), ale **tučný text** a parametr `parse_mode`."
    tricky_out = markdown_to_telegram_html(tricky)
    assert "(``), ale <b>tučný text</b> a parametr <code>parse_mode</code>." in tricky_out


async def test_telegram_send_long_uses_html_and_falls_back_on_bad_request(tmp_path: Path) -> None:
    settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
        telegram_bot_token="123456:ABCDEF_valid_looking_token",
    )
    db = AssistantDB(settings.db_path)
    mem = WorkspaceMemory(tmp_path)
    bot = TelegramAssistantBot(settings, db, mem, AsyncMock())
    try:
        bot.bot.send_message = AsyncMock()
        await bot._send_long(42, "**Ahoj** světe")
        bot.bot.send_message.assert_awaited_once_with(
            chat_id=42,
            text="<b>Ahoj</b> světe",
            parse_mode=ParseMode.HTML,
        )

        # Fallback na čistý text při chybě Telegram API
        err = TelegramBadRequest(method=SendMessage(chat_id=42, text="x"), message="can't parse entities")
        bot.bot.send_message = AsyncMock(side_effect=[err, None])
        await bot._send_long(42, "**Text**")
        assert bot.bot.send_message.await_count == 2
        assert bot.bot.send_message.await_args_list[0].kwargs["parse_mode"] == ParseMode.HTML
        assert "parse_mode" not in bot.bot.send_message.await_args_list[1].kwargs
        assert bot.bot.send_message.await_args_list[1].kwargs["text"] == "**Text**"
    finally:
        await bot.bot.session.close()


def test_split_model_effort_and_build_model_selection_keyboard() -> None:
    assert split_model_effort("gemini-3.8-flash-medium") == ("gemini-3.8-flash", "medium")
    assert split_model_effort("gemini-4-neon-xhigh") == ("gemini-4-neon", "xhigh")
    assert split_model_effort("gemini-pro-orchestrator-neon") == ("gemini-pro-orchestrator-neon", "")

    models = [
        "gemini-3.8-flash-low",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-high",
        "gemini-4-argon-low",
        "gemini-4-argon-medium",
        "gemini-4-argon-high",
        "gemini-4-neon-xhigh",
    ]
    kb = build_model_selection_keyboard(models, "gemini-3.8-flash-medium")
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    texts = [b.text for b in all_buttons]
    callbacks = [b.callback_data for b in all_buttons]

    assert "✅ gemini-3.8-flash" in texts
    assert "gemini-4-argon" in texts
    assert "mdl:gemini-4-argon-medium" in callbacks
    assert any("🟡 Medium" in t and "✅" in t for t in texts)
    assert "mdl:gemini-3.8-flash-high" in callbacks
    assert "mdl:default" in callbacks


async def test_telegram_with_progress_sends_updates_and_deletes_status(tmp_path: Path) -> None:
    import asyncio
    from types import SimpleNamespace

    settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
        telegram_bot_token="123456:ABCDEF_valid_looking_token",
    )
    db = AssistantDB(settings.db_path)
    mem = WorkspaceMemory(tmp_path)
    bot = TelegramAssistantBot(settings, db, mem, AsyncMock())
    try:
        bot.bot.send_chat_action = AsyncMock()
        bot.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=999))
        bot.bot.edit_message_text = AsyncMock()
        bot.bot.delete_message = AsyncMock()

        async with bot._with_progress(42, initial_delay=0.02, update_interval=0.02):
            await asyncio.sleep(0.07)

        bot.bot.send_message.assert_awaited_once()
        assert "⏳" in bot.bot.send_message.await_args.kwargs["text"]
        assert bot.bot.edit_message_text.await_count >= 1
        bot.bot.delete_message.assert_awaited_once_with(chat_id=42, message_id=999)
    finally:
        await bot.bot.session.close()

