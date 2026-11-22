from __future__ import annotations

import re
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from app.agy_runner import AgyRunner, is_version_up_to_date
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
from app.user_env import prepare_agy_home

if TYPE_CHECKING:
    from app.scheduler import AssistantScheduler

VALID_CONTEXT_ALIASES: frozenset[str] = frozenset(
    {
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
)

_SLASH_CMD_RE = re.compile(r"^/([a-zA-Z0-9_-]+)$")


def format_template(key: str, locale: str, **kwargs: Any) -> str:
    """Naformátuje lokalizační šablonu bezpečně i při placeholderu `{lang}`."""
    template = t(key, locale)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except Exception:
        return template


def fmt_int(value: int) -> str:
    """Naformátuje celé číslo s mezerou jako oddělovačem tisíců."""
    return f"{int(value):,}".replace(",", " ")


def format_cleanup_summary(summary: dict[str, object], *, lang: str = "en") -> str:
    """Naformátuje souhrn úklidu paměti a resetu aktivních relací."""
    flushed = int(summary.get("flushed_sessions", 0) or 0)
    reset = int(summary.get("reset_sessions", 0) or 0)
    errors = summary.get("errors") or []
    if reset == 0:
        return t("backend.tg_cleanup_empty", lang)
    text = t("backend.tg_cleanup_done", lang, flushed=flushed, reset=reset)
    if isinstance(errors, list) and errors:
        text += t("backend.tg_cleanup_partial", lang, failed=len(errors))
    return text


def format_usage_report(
    db: AssistantDB,
    settings: Settings,
    *,
    session_key: str,
    context_mode: str,
    owner: OwnerConfig | None = None,
    model_override: str | None = None,
    lang: str | None = None,
) -> str:
    """Sestaví lokalizovaný přehled spotřeby tokenů a délky aktuální konverzace."""
    eff_lang = (
        normalize_language(lang)
        if lang is not None
        else normalize_language(owner.language if owner is not None else settings.language)
    )
    if model_override is not None and model_override.strip():
        active_model = model_override.strip()
    else:
        active_model = (
            owner.agy_model if owner is not None else settings.agy_model
        ) or t("backend.model_default", eff_lang)
    channel = normalize_chat_channel(context_mode)
    ch_lbl = channel_label(channel, eff_lang)
    conv_id, _ = db.get_session(session_key, default_context=context_mode)

    if conv_id:
        conv_runs, conv_tokens = db.get_conversation_stats(conv_id)
        short_id = conv_id if len(conv_id) <= 12 else f"{conv_id[:12]}…"
        conv_line = t(
            "backend.tg_usage_conv_active",
            eff_lang,
            channel=ch_lbl,
            short_id=short_id,
            runs=fmt_int(conv_runs),
            tokens=fmt_int(conv_tokens),
        )
    else:
        conv_line = t("backend.tg_usage_conv_clean", eff_lang, channel=ch_lbl)

    def _period_line(label: str, days: int | None) -> str:
        st = db.get_activity_stats(days=days)
        if st.total_runs == 0:
            return t("backend.tg_usage_period_empty", eff_lang, label=label)
        tilde = "~" if st.estimated_runs > 0 and st.exact_runs == 0 else ""
        return t(
            "backend.tg_usage_period",
            eff_lang,
            label=label,
            tilde=tilde,
            tokens=fmt_int(st.total_tokens),
            in_tok=fmt_int(st.input_tokens),
            out_tok=fmt_int(st.output_tokens),
            runs=fmt_int(st.total_runs),
        )

    nightly_enabled = (
        owner.nightly_reset_enabled
        if owner is not None
        else settings.nightly_reset_enabled
    )
    nightly_cron = (
        owner.nightly_reset_cron if owner is not None else settings.nightly_reset_cron
    )
    nightly_line = (
        t("backend.tg_usage_nightly_on", eff_lang, cron=nightly_cron)
        if nightly_enabled
        else t("backend.tg_usage_nightly_off", eff_lang)
    )

    return t(
        "backend.tg_usage_report",
        eff_lang,
        model=active_model,
        conv_line=conv_line,
        nightly_line=nightly_line,
        p24=_period_line(t("backend.tg_usage_24h", eff_lang), 1),
        p7=_period_line(t("backend.tg_usage_7d", eff_lang), 7),
        pall=_period_line(t("backend.tg_usage_all", eff_lang), None),
    )


async def format_status_report(
    settings: Settings,
    db: AssistantDB,
    memory: WorkspaceMemory,
    runner: Any,
    scheduler: AssistantScheduler | None,
    *,
    context_mode: str,
    owner: OwnerConfig | None = None,
    lang: str | None = None,
) -> str:
    """Sestaví lokalizovaný souhrn stavu `agy`, aktivního modelu, skillů a plánovače."""
    del db
    eff_lang = (
        normalize_language(lang)
        if lang is not None
        else normalize_language(owner.language if owner is not None else settings.language)
    )
    unavail = t("backend.tg_status_unavailable", eff_lang)
    raw_ver = await runner.check_version() if hasattr(runner, "check_version") else None
    version = raw_ver.strip() if isinstance(raw_ver, str) and raw_ver.strip() else unavail
    latest = await runner.check_latest_version() if isinstance(runner, AgyRunner) else None
    up_to_date = is_version_up_to_date(None if version == unavail else version, latest)
    if up_to_date is True:
        ver_status = t("backend.tg_status_up_to_date", eff_lang, ver=version)
    elif up_to_date is False and latest:
        ver_status = t("backend.tg_status_update_avail", eff_lang, ver=version, latest=latest)
    else:
        ver_status = version
    active_model = (
        owner.agy_model if owner is not None else settings.agy_model
    ) or t("backend.model_default", eff_lang)
    none_lbl = t("backend.tg_none", eff_lang)
    skills = ", ".join(memory.list_skills()) or none_lbl
    jobs_txt = none_lbl
    if scheduler is not None:
        jobs = scheduler.list_jobs()
        if jobs:
            jobs_txt = "\n".join(f"- {j.id}: {j.next_run_time}" for j in jobs)
    return format_template(
        "backend.tg_status_msg",
        eff_lang,
        ver_status=ver_status,
        model=active_model,
        lang=f"{get_language_name(eff_lang)} ({eff_lang})",
        ctx=f"{channel_label(normalize_chat_channel(context_mode), eff_lang)} ({context_mode})",
        skills=skills,
        jobs=jobs_txt,
    )


def parse_slash_command(prompt: str) -> tuple[str, str] | None:
    """Rozpozná úvodní lomítkový příkaz; cesty typu `/etc/hosts` vrací `None`."""
    stripped = (prompt or "").strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(maxsplit=1)
    match = _SLASH_CMD_RE.fullmatch(parts[0])
    if match is None:
        return None
    arg = parts[1].strip() if len(parts) > 1 else ""
    return match.group(1).lower(), arg


async def execute_web_slash_command(
    *,
    prompt: str,
    settings: Settings,
    db: AssistantDB,
    memory: WorkspaceMemory,
    runner: Any,
    scheduler: AssistantScheduler | None,
    owner: OwnerConfig,
    session_key: str,
    context_mode: ContextMode,
    model: str = "",
) -> dict[str, object] | None:
    """Vyřídí vestavěný lomítkový příkaz z webového chatu bez volání `agy` (0 tokenů)."""
    parsed = parse_slash_command(prompt)
    if parsed is None:
        return None

    started = time.monotonic()
    cmd, arg = parsed
    eff_lang = normalize_language(owner.language)
    channel = normalize_chat_channel(context_mode)
    extra: dict[str, object] = {}
    reply: str | None = None

    if not arg:
        if cmd == "usage":
            reply = format_usage_report(
                db,
                settings,
                session_key=session_key,
                context_mode=context_mode,
                owner=owner,
                model_override=model or None,
                lang=eff_lang,
            )
        elif cmd == "status":
            reply = await format_status_report(
                settings,
                db,
                memory,
                runner,
                scheduler,
                context_mode=context_mode,
                owner=owner,
                lang=eff_lang,
            )
        elif cmd in ("start", "help"):
            reply = format_template(
                "backend.tg_start",
                eff_lang,
                name=owner.assistant_name,
                channel=channel_label(channel, eff_lang),
                ctx=context_mode,
                lang=f"{get_language_name(eff_lang)} ({eff_lang})",
            )
        elif cmd == "memory":
            try:
                reply = memory.read_file("MEMORY.md")
            except FileNotFoundError:
                reply = t("backend.tg_memory_missing", eff_lang)
        elif cmd == "new":
            db.reset_conversation(session_key, default_context=context_mode)
            if isinstance(runner, AgyRunner):
                await runner.close_session(session_key)
            reply = t(
                "backend.tg_new_done",
                eff_lang,
                channel=channel_label(channel, eff_lang),
            )
        elif cmd in ("uklid", "cleanup"):
            if scheduler is None:
                reply = t("backend.tg_scheduler_missing", eff_lang)
            else:
                summary = await scheduler.run_nightly_reset()
                reply = format_cleanup_summary(summary, lang=eff_lang)
        elif cmd == "heartbeat":
            if scheduler is None:
                reply = t("backend.tg_scheduler_missing", eff_lang)
            else:
                reply = (await scheduler.run_heartbeat(notify=False)).text
        elif cmd == "briefing":
            if scheduler is None:
                reply = t("backend.tg_scheduler_missing", eff_lang)
            else:
                reply = (await scheduler.run_morning_briefing(notify=False)).text
        elif cmd in ("auto", "pracovni", "work", "osobni", "personal"):
            canonical = normalize_chat_channel(cmd)
            reply = t(
                "backend.tg_switched_context",
                eff_lang,
                channel=channel_label(canonical, eff_lang),
                ctx=canonical,
            )
            extra["switch_channel"] = canonical

    if reply is None:
        if cmd in ("kontext", "context"):
            if not arg:
                reply = t(
                    "backend.tg_context_status",
                    eff_lang,
                    channel=channel_label(channel, eff_lang),
                    ctx=context_mode,
                )
            elif arg.lower() in VALID_CONTEXT_ALIASES:
                canonical = normalize_chat_channel(arg.lower())
                reply = t(
                    "backend.tg_switched_context",
                    eff_lang,
                    channel=channel_label(canonical, eff_lang),
                    ctx=canonical,
                )
                extra["switch_channel"] = canonical
            else:
                reply = t("backend.tg_invalid_context", eff_lang)
        elif cmd in ("lang", "language"):
            if not arg:
                avail = ", ".join(item["code"] for item in list_available_languages())
                reply = format_template(
                    "backend.tg_lang_status",
                    eff_lang,
                    lang=eff_lang,
                    lang_name=get_language_name(eff_lang),
                    available=avail,
                )
            else:
                new_lang = resolve_language(arg)
                db.update_owner(language=new_lang)
                if isinstance(runner, AgyRunner):
                    await runner.close_all()
                reply = format_template(
                    "backend.tg_lang_switched",
                    new_lang,
                    lang=new_lang,
                    lang_name=get_language_name(new_lang),
                )
                extra["switch_language"] = new_lang
        elif cmd == "model":
            if not arg:
                eff_model = (model or owner.agy_model).strip()
                cur_display = eff_model or t("backend.model_default", eff_lang)
                avail_raw = await runner.list_models() if hasattr(runner, "list_models") else []
                avail = (
                    [m.strip() for m in avail_raw if isinstance(m, str) and m.strip()]
                    if isinstance(avail_raw, list)
                    else []
                )
                if avail:
                    reply = f"🤖 `{cur_display}`\n\n" + "\n".join(f"- `{m}`" for m in avail)
                else:
                    reply = f"🤖 `{cur_display}`"
            else:
                new_model = (
                    "" if arg.lower() in {"default", "auto", "reset", "-"} else arg
                )
                db.update_owner(agy_model=new_model)
                prepare_agy_home(settings, db)
                if isinstance(runner, AgyRunner):
                    await runner.close_all()
                reply = t(
                    "backend.tg_model_set_toast",
                    eff_lang,
                    model=new_model or t("backend.model_default", eff_lang),
                )
                extra["active_model"] = new_model

    if reply is None:
        return None

    conv_id, _ = db.get_session(session_key, default_context=context_mode)
    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "text": reply,
        "conversation_id": conv_id,
        "duration_ms": duration_ms,
        "success": True,
        "error": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "tokens_estimated": False,
        "assistant_name": owner.assistant_name,
        "handled_command": cmd,
        **extra,
    }
