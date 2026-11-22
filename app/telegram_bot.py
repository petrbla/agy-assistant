from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import time
from collections import deque
from typing import AsyncIterator

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.agy_runner import AgyRunner, QuotaSnapshot
from app.attachments import (
    build_attachment_prompt,
    build_inbox_path,
    extract_attachment_text,
    is_image_filename,
)
from app.chat_commands import (
    build_usage_report,
    fmt_int,
    format_cleanup_summary as render_cleanup_summary,
    format_status_report,
    format_template,
    format_usage_report as render_usage_report,
)
from app.config import ContextMode, Settings, normalize_chat_channel, normalize_language
from app.db import AssistantDB, OwnerConfig
from app.i18n import (
    channel_label,
    get_language_name,
    list_available_languages,
    resolve_language,
    t,
)
from app.memory import WorkspaceMemory
from app.scheduler import AssistantScheduler
from app.user_env import ensure_workspace, get_memory, prepare_agy_home

logger = logging.getLogger(__name__)

VALID_CONTEXTS: tuple[ContextMode, ...] = ("auto", "pracovni", "osobni")
MODEL_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
EFFORT_BUTTON_LABELS: dict[str, str] = {
    "low": "🟢 Low",
    "medium": "🟡 Medium",
    "high": "🔴 High",
    "xhigh": "🔥 XHigh",
}
FALLBACK_TELEGRAM_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-4-argon-low",
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
    "gemini-4-hydrogen-low",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-high",
    "gemini-4-hydrogen-xhigh",
    "gemini-4-neon-low",
    "gemini-4-neon-medium",
    "gemini-4-neon-high",
    "gemini-4-neon-xhigh",
    "claude-sonnet-5-5-low",
    "claude-sonnet-5-5-medium",
    "claude-sonnet-5-5-high",
    "claude-opus-5-5-low",
    "claude-opus-5-5-medium",
    "claude-opus-5-5-high",
)


def split_model_effort(full_name: str) -> tuple[str, str]:
    """Rozdělí název modelu na `(základ, váha)`, např. `('gemini-4-argon', 'medium')`."""
    cleaned = full_name.strip()
    if not cleaned:
        return "", ""
    for eff in MODEL_EFFORTS:
        suffix = f"-{eff}"
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            return cleaned[: -len(suffix)], eff
    return cleaned, ""


def build_model_selection_keyboard(
    models: list[str], active_model: str, *, lang: str = "en"
) -> InlineKeyboardMarkup:
    """Sestaví klikací inline klávesnici pro výběr rodiny modelu a jeho váhy v Telegramu."""
    effective_list = list(models) if models else list(FALLBACK_TELEGRAM_MODELS)
    if active_model and active_model not in effective_list:
        effective_list.insert(0, active_model)

    groups: dict[str, list[str]] = {}
    for item in effective_list:
        base, eff = split_model_effort(item)
        if not base:
            continue
        bucket = groups.setdefault(base, [])
        if eff not in bucket:
            bucket.append(eff)

    active_base, active_eff = split_model_effort(active_model)
    rows: list[list[InlineKeyboardButton]] = []
    pending_pair: list[InlineKeyboardButton] = []

    for base, efforts in groups.items():
        if active_eff and active_eff in efforts:
            target_full = f"{base}-{active_eff}"
        elif "medium" in efforts:
            target_full = f"{base}-medium"
        elif "" in efforts:
            target_full = base
        else:
            first_eff = efforts[0]
            target_full = f"{base}-{first_eff}" if first_eff else base

        cb_data = f"mdl:{target_full}"
        if len(cb_data.encode("utf-8")) > 64:
            continue
        is_selected = bool(active_model and base == active_base)
        label = f"✅ {base}" if is_selected else base
        btn = InlineKeyboardButton(text=label, callback_data=cb_data)

        if len(base) <= 22:
            pending_pair.append(btn)
            if len(pending_pair) == 2:
                rows.append(pending_pair)
                pending_pair = []
        else:
            if pending_pair:
                rows.append(pending_pair)
                pending_pair = []
            rows.append([btn])

    if pending_pair:
        rows.append(pending_pair)

    active_efforts = [e for e in groups.get(active_base, []) if e]
    if active_base and active_efforts:
        ordered_effs = [e for e in MODEL_EFFORTS if e in active_efforts]
        eff_row: list[InlineKeyboardButton] = []
        for eff in ordered_effs:
            full = f"{active_base}-{eff}"
            cb_data = f"mdl:{full}"
            if len(cb_data.encode("utf-8")) > 64:
                continue
            eff_label = EFFORT_BUTTON_LABELS.get(eff, eff)
            if eff == active_eff:
                eff_label = f"✅ {eff_label}"
            eff_row.append(InlineKeyboardButton(text=eff_label, callback_data=cb_data))
        if eff_row:
            rows.append(eff_row)

    default_label = (
        t("backend.model_default_btn_active", lang)
        if not active_model
        else t("backend.model_default_btn", lang)
    )
    rows.append([InlineKeyboardButton(text=default_label, callback_data="mdl:default")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_DOUBLE_CODE_RE = re.compile(r"(?<!`)``([^`\r\n]+)``(?!`)")
_LONE_DOUBLE_BACKTICK_RE = re.compile(r"(?<!`)``(?!`)")
_INLINE_CODE_RE = re.compile(r"`([^`\r\n]+)`")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_BULLET_RE = re.compile(r"(?m)^[ \t]*[-*]\s+")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(.+?)\*\*\*", re.DOTALL)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_ITALIC_UNDER_RE = re.compile(r"(?:^|(?<=[\s(]))_([^_\n]+?)_(?=$|[\s).,!?:;])")


def markdown_to_telegram_html(text: str) -> str:
    """Bezpečně převede běžný Markdown z odpovědi AI na Telegram HTML."""
    placeholders: list[str] = []

    def _stash(html_snippet: str) -> str:
        token = f"\x00TGPH{len(placeholders)}\x00"
        placeholders.append(html_snippet)
        return token

    def _replace_fence(match: re.Match[str]) -> str:
        lang = match.group(1).strip()
        code = html.escape(match.group(2).rstrip("\r\n"), quote=False)
        if lang:
            return _stash(
                f'<pre><code class="language-{html.escape(lang, quote=True)}">{code}</code></pre>'
            )
        return _stash(f"<pre>{code}</pre>")

    def _replace_inline_code(match: re.Match[str]) -> str:
        return _stash(f"<code>{html.escape(match.group(1), quote=False)}</code>")

    def _replace_link(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2).replace('"', "&quot;")
        return _stash(f'<a href="{url}">{label}</a>')

    work = _FENCE_RE.sub(_replace_fence, text)
    work = _DOUBLE_CODE_RE.sub(_replace_inline_code, work)
    work = _LONE_DOUBLE_BACKTICK_RE.sub(lambda _: _stash("``"), work)
    work = _INLINE_CODE_RE.sub(_replace_inline_code, work)
    work = html.escape(work, quote=False)

    work = _HEADING_RE.sub(r"<b>\1</b>", work)
    work = _BULLET_RE.sub("• ", work)
    work = _LINK_RE.sub(_replace_link, work)
    work = _BOLD_ITALIC_RE.sub(r"<b><i>\1</i></b>", work)
    work = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", work)
    work = _STRIKE_RE.sub(r"<s>\1</s>", work)
    work = _ITALIC_STAR_RE.sub(r"<i>\1</i>", work)
    work = _ITALIC_UNDER_RE.sub(r"<i>\1</i>", work)

    for idx, snippet in enumerate(placeholders):
        work = work.replace(f"\x00TGPH{idx}\x00", snippet)
    return work


def format_elapsed_seconds(seconds: int) -> str:
    """Naformátuje uplynulý čas čitelně (např. `42 s`, `2 min`, `2 min 13 s`)."""
    total = max(1, int(seconds))
    if total < 60:
        return f"{total} s"
    minutes, rem = divmod(total, 60)
    if rem == 0:
        return f"{minutes} min"
    return f"{minutes} min {rem} s"


def split_telegram_message(text: str, max_len: int = 4000) -> list[str]:
    """Rozdělí dlouhý text na části pod limitem Telegramu (4096 znaků)."""
    cleaned = text.strip()
    if not cleaned:
        return ["(Prázdná odpověď)"]
    if len(cleaned) <= max_len:
        return [cleaned]

    chunks: list[str] = []
    remaining = cleaned
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramAssistantBot:
    """Single-user Telegram rozhraní se samoopravným pollingem a deduplikací zpráv."""

    def __init__(
        self,
        settings: Settings,
        db: AssistantDB,
        memory: WorkspaceMemory,
        runner: AgyRunner,
        scheduler: AssistantScheduler | None = None,
        token: str | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.memory = memory
        self.runner = runner
        self.scheduler = scheduler
        owner = db.get_owner()
        effective_token = (
            token
            if token is not None
            else (owner.telegram_bot_token if owner is not None else settings.telegram_bot_token)
        )
        self.token = effective_token.strip()
        self.bot = Bot(token=self.token)
        self.dp = Dispatcher()
        self._polling_task: asyncio.Task[None] | None = None
        self._stopping: bool = False
        self._seen_messages: deque[tuple[int, int]] = deque(maxlen=256)
        self._register_handlers()

    def is_alive(self) -> bool:
        return (
            not self._stopping
            and self._polling_task is not None
            and not self._polling_task.done()
        )

    def _is_duplicate_message(self, message: Message) -> bool:
        msg_id = getattr(message, "message_id", None)
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if not isinstance(msg_id, int) or not isinstance(chat_id, int):
            return False
        key = (chat_id, msg_id)
        if key in self._seen_messages:
            return True
        if not self.db.claim_telegram_message(chat_id, msg_id):
            self._seen_messages.append(key)
            return True
        self._seen_messages.append(key)
        return False

    def _owner(self) -> OwnerConfig | None:
        return self.db.get_owner()

    def _lang(self) -> str:
        owner = self._owner()
        return normalize_language(owner.language if owner is not None else self.settings.language)

    def _allowed_ids(self) -> set[int]:
        owner = self._owner()
        if owner is not None:
            return owner.allowed_telegram_user_ids
        return self.settings.allowed_telegram_user_ids

    def _notify_chat_id(self) -> int | None:
        owner = self._owner()
        if owner is not None:
            return owner.notify_chat_id
        return self.settings.notify_chat_id

    def _default_context(self) -> ContextMode:
        owner = self._owner()
        raw = owner.default_context_mode if owner is not None else self.settings.default_context_mode
        return raw if raw in VALID_CONTEXTS else "auto"  # type: ignore[return-value]

    def _assistant_name(self) -> str:
        owner = self._owner()
        return owner.assistant_name if owner is not None else self.settings.assistant_name

    def _is_authorized_user_id(self, tg_user_id: int | None) -> bool:
        if tg_user_id is None:
            return False
        allowed = self._allowed_ids()
        return bool(allowed) and tg_user_id in allowed

    def _is_authorized(self, message: Message) -> bool:
        user = message.from_user
        return self._is_authorized_user_id(user.id if user is not None else None)

    async def _guard(self, message: Message) -> bool:
        if self._is_authorized(message):
            if self._is_duplicate_message(message):
                logger.info(
                    "Ignoruji duplicitní Telegram zprávu (chat_id=%s, message_id=%s).",
                    getattr(getattr(message, "chat", None), "id", None),
                    getattr(message, "message_id", None),
                )
                return False
            return True
        lang = self._lang()
        uid = message.from_user.id if message.from_user else t("backend.tg_unknown_user", lang)
        logger.warning("Neautorizovaný přístup k Telegram botovi od tg_user_id=%s", uid)
        await message.answer(t("backend.tg_access_denied", lang, uid=uid))
        return False

    def _session_key(self, message: Message) -> str:
        return f"tg:{message.chat.id}"

    def _active_context(self, message: Message) -> ContextMode:
        _, ctx = self.db.get_session(
            self._session_key(message), default_context=self._default_context()
        )
        return ctx if ctx in VALID_CONTEXTS else "auto"  # type: ignore[return-value]

    def _chat_session_key(self, message: Message, context_mode: str) -> str:
        channel = normalize_chat_channel(context_mode)
        return f"{self._session_key(message)}:{channel}"

    @staticmethod
    def _fmt_int(value: int) -> str:
        return fmt_int(value)

    @staticmethod
    def _format_cleanup_summary(summary: dict[str, object], *, lang: str = "en") -> str:
        return render_cleanup_summary(summary, lang=lang)

    def _format_usage_report(
        self,
        message: Message,
        *,
        lang: str | None = None,
        quota: QuotaSnapshot | None = None,
    ) -> str:
        active_ctx = self._active_context(message)
        return render_usage_report(
            self.db,
            self.settings,
            session_key=self._chat_session_key(message, active_ctx),
            context_mode=active_ctx,
            owner=self._owner(),
            lang=lang if lang is not None else self._lang(),
            quota=quota,
        )

    async def _send_long(self, chat_id: int, text: str) -> None:
        for chunk in split_telegram_message(text, max_len=3400):
            html_chunk = markdown_to_telegram_html(chunk)
            if len(html_chunk) <= 4096:
                try:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=html_chunk,
                        parse_mode=ParseMode.HTML,
                    )
                    continue
                except TelegramBadRequest as exc:
                    logger.warning("Telegram odmítl HTML formátování, posílám prostý text: %s", exc)
            await self.bot.send_message(chat_id=chat_id, text=chunk)

    async def send_notification(self, text: str) -> None:
        chat_id = self._notify_chat_id()
        if chat_id is None:
            logger.info("Notifikace přeskočena – není nastaveno cílové chat ID.")
            return
        await self._send_long(chat_id, text)

    async def _typing_loop(self, chat_id: int) -> None:
        try:
            while True:
                await self.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    @contextlib.asynccontextmanager
    async def _with_progress(
        self,
        chat_id: int,
        *,
        label: str | None = None,
        initial_delay: float = 12.0,
        update_interval: float = 15.0,
    ) -> AsyncIterator[None]:
        started = time.monotonic()
        status_msg_id: int | None = None
        eff_label = label or t("backend.tg_progress_default", self._lang())

        async def _progress_loop() -> None:
            nonlocal status_msg_id
            try:
                await asyncio.sleep(initial_delay)
                elapsed = format_elapsed_seconds(int(time.monotonic() - started))
                sent = await self.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏳ {eff_label}… ({elapsed})",
                )
                status_msg_id = sent.message_id
                while True:
                    await asyncio.sleep(update_interval)
                    elapsed = format_elapsed_seconds(int(time.monotonic() - started))
                    with contextlib.suppress(Exception):
                        await self.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_msg_id,
                            text=f"⏳ {eff_label}… ({elapsed})",
                        )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("Aktualizace průběhu v Telegramu selhala: %s", exc)

        typing = asyncio.create_task(self._typing_loop(chat_id))
        progress = asyncio.create_task(_progress_loop())
        try:
            yield
        finally:
            typing.cancel()
            progress.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing
            with contextlib.suppress(asyncio.CancelledError):
                await progress
            if status_msg_id is not None:
                with contextlib.suppress(Exception):
                    await self.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)

    def _register_handlers(self) -> None:
        @self.dp.message(Command("start", "help"))
        async def cmd_start(message: Message) -> None:
            if not await self._guard(message):
                return
            lang = self._lang()
            ctx = self._active_context(message)
            ch_lbl = channel_label(normalize_chat_channel(ctx), lang)
            await message.answer(
                format_template(
                    "backend.tg_start",
                    lang,
                    name=self._assistant_name(),
                    channel=ch_lbl,
                    ctx=ctx,
                    lang=f"{get_language_name(lang)} ({lang})",
                )
            )

        @self.dp.message(Command("new"))
        async def cmd_new(message: Message) -> None:
            if not await self._guard(message):
                return
            lang = self._lang()
            ctx = self._active_context(message)
            channel = normalize_chat_channel(ctx)
            chat_session = self._chat_session_key(message, ctx)
            self.db.reset_conversation(chat_session, default_context=ctx)
            if isinstance(self.runner, AgyRunner):
                await self.runner.close_session(chat_session)
            await message.answer(
                t("backend.tg_new_done", lang, channel=channel_label(channel, lang))
            )

        async def _switch_context(message: Message, target: str) -> None:
            lang = self._lang()
            cleaned = target.strip().lower()
            valid_inputs = {
                "auto",
                "pracovni",
                "prace",
                "firemni",
                "work",
                "business",
                "company",
                "osobni",
                "soukrome",
                "personal",
                "private",
            }
            if cleaned not in valid_inputs:
                await message.answer(t("backend.tg_invalid_context", lang))
                return
            canonical = normalize_chat_channel(cleaned)
            self.db.set_context_mode(self._session_key(message), canonical)
            await message.answer(
                t(
                    "backend.tg_switched_context",
                    lang,
                    channel=channel_label(canonical, lang),
                    ctx=canonical,
                )
            )

        @self.dp.message(Command("auto"))
        async def cmd_auto(message: Message) -> None:
            if await self._guard(message):
                await _switch_context(message, "auto")

        @self.dp.message(Command("pracovni", "work"))
        async def cmd_pracovni(message: Message) -> None:
            if await self._guard(message):
                await _switch_context(message, "pracovni")

        @self.dp.message(Command("osobni", "personal"))
        async def cmd_osobni(message: Message) -> None:
            if await self._guard(message):
                await _switch_context(message, "osobni")

        @self.dp.message(Command("kontext", "context"))
        async def cmd_context(message: Message) -> None:
            if not await self._guard(message):
                return
            lang = self._lang()
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) < 2:
                current = self._active_context(message)
                await message.answer(
                    t(
                        "backend.tg_context_status",
                        lang,
                        channel=channel_label(normalize_chat_channel(current), lang),
                        ctx=current,
                    )
                )
                return
            await _switch_context(message, parts[1].strip().lower())

        @self.dp.message(Command("lang", "language"))
        async def cmd_lang(message: Message) -> None:
            if not await self._guard(message):
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) < 2:
                cur = self._lang()
                avail = ", ".join(item["code"] for item in list_available_languages())
                await message.answer(
                    format_template(
                        "backend.tg_lang_status",
                        cur,
                        lang=cur,
                        lang_name=get_language_name(cur),
                        available=avail,
                    )
                )
                return
            new_lang = resolve_language(parts[1])
            if self._owner() is not None:
                self.db.update_owner(language=new_lang)
            else:
                self.settings.language = new_lang
            if isinstance(self.runner, AgyRunner):
                await self.runner.close_all()
            await message.answer(
                format_template(
                    "backend.tg_lang_switched",
                    new_lang,
                    lang=new_lang,
                    lang_name=get_language_name(new_lang),
                )
            )

        async def _apply_model(new_model: str) -> None:
            if self._owner() is not None:
                self.db.update_owner(agy_model=new_model)
            else:
                self.settings.agy_model = new_model
            prepare_agy_home(self.settings, self.db)
            if isinstance(self.runner, AgyRunner):
                await self.runner.close_all()

        def _format_model_menu_text(active_model: str, lang: str) -> str:
            _, eff = split_model_effort(active_model)
            eff_part = t("backend.tg_model_effort_part", lang, eff=eff) if eff else ""
            return t(
                "backend.tg_model_menu",
                lang,
                model=active_model or t("backend.model_default", lang),
                eff_part=eff_part,
            )

        @self.dp.message(Command("model"))
        async def cmd_model(message: Message) -> None:
            if not await self._guard(message):
                return
            lang = self._lang()
            parts = (message.text or "").split(maxsplit=1)
            owner = self._owner()
            active_model = owner.agy_model if owner is not None else self.settings.agy_model
            if len(parts) >= 2:
                raw_model = parts[1].strip()
                active_model = "" if raw_model.lower() in {"default", "auto", "reset", "-"} else raw_model
                await _apply_model(active_model)
            avail = await self.runner.list_models()
            keyboard = build_model_selection_keyboard(avail, active_model, lang=lang)
            await message.answer(_format_model_menu_text(active_model, lang), reply_markup=keyboard)

        @self.dp.callback_query(F.data.startswith("mdl:"))
        async def cb_select_model(callback: CallbackQuery) -> None:
            lang = self._lang()
            uid = callback.from_user.id if callback.from_user else None
            if not self._is_authorized_user_id(uid):
                await callback.answer(t("backend.tg_access_denied_short", lang), show_alert=True)
                return
            raw_model = (callback.data or "")[4:].strip()
            new_model = "" if raw_model.lower() in {"default", "auto", "reset", "-"} else raw_model
            await _apply_model(new_model)
            avail = await self.runner.list_models()
            keyboard = build_model_selection_keyboard(avail, new_model, lang=lang)
            if isinstance(callback.message, Message):
                with contextlib.suppress(TelegramBadRequest):
                    await callback.message.edit_text(
                        _format_model_menu_text(new_model, lang), reply_markup=keyboard
                    )
            await callback.answer(
                t(
                    "backend.tg_model_set_toast",
                    lang,
                    model=new_model or t("backend.model_default", lang),
                )
            )

        @self.dp.message(Command("memory"))
        async def cmd_memory(message: Message) -> None:
            if not await self._guard(message):
                return
            lang = self._lang()
            try:
                content = self.memory.read_file("MEMORY.md")
            except FileNotFoundError:
                content = t("backend.tg_memory_missing", lang)
            await self._send_long(message.chat.id, content)

        @self.dp.message(Command("usage", "quota"))
        async def cmd_usage(message: Message) -> None:
            if not await self._guard(message):
                return
            active_ctx = self._active_context(message)
            report = await build_usage_report(
                self.db,
                self.settings,
                self.runner,
                session_key=self._chat_session_key(message, active_ctx),
                context_mode=active_ctx,
                owner=self._owner(),
                lang=self._lang(),
                force_refresh=True,
            )
            await self._send_long(message.chat.id, report)

        @self.dp.message(Command("uklid", "cleanup"))
        async def cmd_cleanup(message: Message) -> None:
            if not await self._guard(message):
                return
            lang = self._lang()
            if not self.scheduler:
                await message.answer(t("backend.tg_scheduler_missing", lang))
                return
            async with self._with_progress(
                message.chat.id, label=t("backend.tg_progress_cleanup", lang)
            ):
                summary = await self.scheduler.run_nightly_reset()
            await self._send_long(
                message.chat.id, self._format_cleanup_summary(summary, lang=lang)
            )

        @self.dp.message(Command("heartbeat"))
        async def cmd_heartbeat(message: Message) -> None:
            if not await self._guard(message):
                return
            lang = self._lang()
            if not self.scheduler:
                await message.answer(t("backend.tg_scheduler_missing", lang))
                return
            async with self._with_progress(
                message.chat.id, label=t("backend.tg_progress_heartbeat", lang)
            ):
                result = await self.scheduler.run_heartbeat(notify=False)
            await self._send_long(message.chat.id, result.text)

        @self.dp.message(Command("briefing"))
        async def cmd_briefing(message: Message) -> None:
            if not await self._guard(message):
                return
            lang = self._lang()
            if not self.scheduler:
                await message.answer(t("backend.tg_scheduler_missing", lang))
                return
            async with self._with_progress(
                message.chat.id, label=t("backend.tg_progress_briefing", lang)
            ):
                result = await self.scheduler.run_morning_briefing(notify=False)
            await self._send_long(message.chat.id, result.text)

        @self.dp.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            if not await self._guard(message):
                return
            _, ctx = self.db.get_session(
                self._session_key(message), default_context=self._default_context()
            )
            text = await format_status_report(
                self.settings,
                self.db,
                self.memory,
                self.runner,
                self.scheduler,
                context_mode=ctx,
                owner=self._owner(),
                lang=self._lang(),
            )
            await message.answer(text)

        @self.dp.message(F.photo | F.document | F.voice | F.audio)
        async def handle_attachment(message: Message) -> None:
            if not await self._guard(message):
                return
            await self._download_and_handle_attachment(message)

        @self.dp.message(F.text)
        async def handle_text(message: Message) -> None:
            if not await self._guard(message):
                return
            prompt = (message.text or "").strip()
            if not prompt:
                return
            await self._run_prompt_and_reply(message, prompt)

    async def _run_prompt_and_reply(self, message: Message, prompt: str) -> None:
        async with self._with_progress(message.chat.id):
            active_ctx = self._active_context(message)
            chat_session = self._chat_session_key(message, active_ctx)
            owner = self._owner()
            ensure_workspace(self.settings, owner)
            result = await self.runner.run(
                prompt,
                session_key=chat_session,
                source="telegram",
                context_mode=active_ctx,
            )
        await self._send_long(message.chat.id, result.text)

    @staticmethod
    def _extract_attachment_meta(
        message: Message,
        *,
        lang: str = "en",
    ) -> tuple[str, str, str, str, int, str] | None:
        if message.photo:
            best = message.photo[-1]
            return best.file_id, "telegram_obrazek.jpg", ".jpg", "image/jpeg", best.file_size or 0, t("prompts.att_kind_image", lang)
        if message.document:
            doc = message.document
            fname = doc.file_name or "document"
            mime = doc.mime_type or ""
            kind = (
                t("prompts.att_kind_image", lang)
                if is_image_filename(fname, mime)
                else t("prompts.att_kind_document", lang)
            )
            return doc.file_id, fname, "", mime, doc.file_size or 0, kind
        if message.voice:
            v = message.voice
            return v.file_id, "voice_message.ogg", ".ogg", v.mime_type or "audio/ogg", v.file_size or 0, t("prompts.att_kind_voice", lang)
        if message.audio:
            a = message.audio
            return a.file_id, a.file_name or "audio.mp3", ".mp3", a.mime_type or "audio/mpeg", a.file_size or 0, t("prompts.att_kind_audio", lang)
        return None

    async def _download_and_handle_attachment(self, message: Message) -> None:
        lang = self._lang()
        meta = self._extract_attachment_meta(message, lang=lang)
        if meta is None:
            return
        file_id, raw_name, default_ext, mime_type, file_size, kind = meta
        if file_size and file_size > 20 * 1024 * 1024:
            await message.answer(t("backend.tg_file_too_large", lang))
            return

        ws = ensure_workspace(self.settings, self._owner())
        dest_path, rel_path = build_inbox_path(ws, raw_name, default_ext=default_ext)
        try:
            await self.bot.download(file_id, destination=dest_path)
        except Exception as exc:
            logger.exception("Stažení přílohy z Telegramu selhalo (%s): %s", raw_name, exc)
            await message.answer(t("backend.tg_file_download_failed", lang))
            return

        actual_size = dest_path.stat().st_size if dest_path.is_file() else file_size
        extracted_text, truncated = await asyncio.to_thread(extract_attachment_text, dest_path)
        prompt = build_attachment_prompt(
            rel_path=rel_path,
            kind=kind,
            caption=message.caption or "",
            mime_type=mime_type,
            size_bytes=actual_size,
            extracted_text=extracted_text,
            truncated=truncated,
            lang=lang,
        )
        await self._run_prompt_and_reply(message, prompt)

    async def _polling_loop(self) -> None:
        backoff = 5
        while not self._stopping:
            try:
                with contextlib.suppress(Exception):
                    await self.bot.delete_webhook(drop_pending_updates=True)
                await self.dp.start_polling(
                    self.bot, handle_signals=False, close_bot_session=False
                )
                if self._stopping:
                    break
                logger.warning("Telegram polling skončil neočekávaně – restartuji za %s s.", backoff)
            except asyncio.CancelledError:
                raise
            except TelegramConflictError as exc:
                logger.warning(
                    "Telegram polling konflikt (%s) – čekám %s s na uvolnění staré instance.",
                    exc,
                    backoff,
                )
            except Exception as exc:
                logger.exception("Telegram polling spadl (%s) – restartuji za %s s.", exc, backoff)
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(30, backoff + 5)

    async def start(self) -> None:
        if not self._allowed_ids():
            logger.warning("Telegram bot nemá nastavená povolená ID – odmítne všechny zprávy.")
        self._stopping = False
        self._polling_task = asyncio.create_task(self._polling_loop())
        logger.info("Telegram bot spuštěn.")

    async def stop(self) -> None:
        self._stopping = True
        with contextlib.suppress(Exception):
            await self.dp.stop_polling()
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task
        await self.bot.session.close()


class TelegramBotController:
    """Spravuje jedinou instanci Telegram bota včetně 30s watchdogu a hot-reloadu."""

    def __init__(
        self,
        settings: Settings,
        db: AssistantDB,
        runner: AgyRunner,
        scheduler: AssistantScheduler | None = None,
        watchdog_interval_seconds: float = 30.0,
    ) -> None:
        self.settings = settings
        self.db = db
        self.runner = runner
        self.scheduler = scheduler
        self._watchdog_interval = max(5.0, float(watchdog_interval_seconds))
        self._bot: TelegramAssistantBot | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._reload_lock = asyncio.Lock()
        self._stopping = False

    async def _watchdog_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self._watchdog_interval)
                if self._stopping:
                    break
                if self._bot is not None and not self._bot.is_alive():
                    logger.warning("Watchdog: Telegram bot neběží – provádím automatický restart.")
                    await self.reload()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Chyba v Telegram watchdogu: %s", exc)

    def _ensure_watchdog(self) -> None:
        if self._stopping:
            return
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def reload(self) -> None:
        async with self._reload_lock:
            if self._stopping:
                return
            owner = self.db.get_owner()
            token = (
                owner.telegram_bot_token if owner is not None else self.settings.telegram_bot_token
            ).strip()
            if not token:
                if self._bot is not None:
                    await self._bot.stop()
                    self._bot = None
                return

            if (
                self._bot is not None
                and self._bot.is_alive()
                and self._bot.token == token
            ):
                ensure_workspace(self.settings, owner)
                self._bot.memory = get_memory(self.settings, owner)
                self._ensure_watchdog()
                return

            if self._bot is not None:
                await self._bot.stop()
                self._bot = None

            ensure_workspace(self.settings, owner)
            memory = get_memory(self.settings, owner)
            bot = TelegramAssistantBot(
                self.settings,
                self.db,
                memory,
                self.runner,
                scheduler=self.scheduler,
                token=token,
            )
            await bot.start()
            self._bot = bot
            self._ensure_watchdog()

    async def send_notification(self, text: str) -> None:
        if self._bot is None:
            logger.info("Telegram bot není aktivní – notifikace přeskočena.")
            return
        await self._bot.send_notification(text)

    async def stop(self) -> None:
        self._stopping = True
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
        async with self._reload_lock:
            if self._bot is not None:
                await self._bot.stop()
                self._bot = None
