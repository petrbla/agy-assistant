from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import __version__
from app.agy_runner import AgyResult, AgyRunner
from app.config import ContextMode, Settings
from app.db import AssistantDB
from app.github_backup import GitHubBackupResult, run_github_backup as execute_github_backup
from app.i18n import t
from app.memory import WorkspaceMemory
from app.user_env import ensure_workspace

logger = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]

HEARTBEAT_PROMPT = t("prompts.heartbeat", "en")
MORNING_BRIEFING_PROMPT = t("prompts.morning_briefing", "en")
NIGHTLY_MEMORY_FLUSH_PROMPT = t("prompts.nightly_memory_flush", "en")

IDLE_RESET_MIN_RUNS = 6
IDLE_RESET_MIN_TOKENS = 20_000
IDLE_RESET_CHECK_INTERVAL_MINUTES = 10


def is_heartbeat_ok(text: str) -> bool:
    """Rozpozná tichou odpověď Heartbeatu bez upozornění."""
    cleaned = text.strip().strip("`*_").strip()
    return cleaned.upper() == "HEARTBEAT_OK"


@dataclass(frozen=True)
class ScheduledJobInfo:
    """Informace o naplánované úloze pro Web UI / API."""

    id: str
    name: str
    next_run_time: str | None


class AssistantScheduler:
    """Single-user plánovač (Heartbeat, Ranní briefing, Noční úklid a vlastní úlohy)."""

    def __init__(
        self,
        settings: Settings,
        runner: AgyRunner,
        notifier: Notifier | None = None,
        db: AssistantDB | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.notifier = notifier
        self.db = db
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.tz))

    def _safe_zone(self, tz_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return ZoneInfo(self.settings.tz)

    def start(self) -> None:
        """Zaregistruje povolené úlohy a spustí plánovač."""
        self.sync_jobs()
        self.scheduler.start()
        logger.info("Plánovač spuštěn (%s úloh).", len(self.scheduler.get_jobs()))

    def sync_jobs(self) -> None:
        """Synchronizuje všechny úlohy plánovače podle aktuálního nastavení vlastníka v DB."""
        for job in list(self.scheduler.get_jobs()):
            with contextlib.suppress(Exception):
                self.scheduler.remove_job(job.id)

        owner = self.db.get_owner() if self.db is not None else None
        tz_name = owner.tz if owner is not None else self.settings.tz
        owner_lang = owner.language if owner is not None else self.settings.language
        tz = self._safe_zone(tz_name)

        if self.db is not None:
            removed = self.db.cleanup_expired_once_tasks(tz_name=tz_name)
            if removed:
                logger.info("Plánovač uklidil %s proběhlých jednorázových úloh.", removed)

        hb_enabled = owner.heartbeat_enabled if owner is not None else self.settings.heartbeat_enabled
        hb_interval = (
            owner.heartbeat_interval_minutes
            if owner is not None
            else self.settings.heartbeat_interval_minutes
        )
        brief_enabled = (
            owner.morning_briefing_enabled
            if owner is not None
            else self.settings.morning_briefing_enabled
        )
        brief_cron = (
            owner.morning_briefing_cron if owner is not None else self.settings.morning_briefing_cron
        )
        nightly_enabled = (
            owner.nightly_reset_enabled
            if owner is not None
            else self.settings.nightly_reset_enabled
        )
        nightly_cron = (
            owner.nightly_reset_cron if owner is not None else self.settings.nightly_reset_cron
        )
        idle_enabled = (
            owner.idle_reset_enabled
            if owner is not None
            else self.settings.idle_reset_enabled
        )
        gh_enabled = (
            owner.github_backup_enabled
            if owner is not None
            else self.settings.github_backup_enabled
        )
        gh_repo = (
            owner.github_backup_repo if owner is not None else self.settings.github_backup_repo
        ).strip()
        gh_token = (
            owner.github_backup_token if owner is not None else self.settings.github_backup_token
        ).strip()
        gh_cron = (
            (owner.github_backup_cron if owner is not None else self.settings.github_backup_cron).strip()
            or "30 2 * * *"
        )

        if hb_enabled:
            self.scheduler.add_job(
                self.run_heartbeat,
                trigger=IntervalTrigger(minutes=max(1, hb_interval)),
                kwargs={"notify": True},
                id="heartbeat",
                name=t("ui.job_name_heartbeat", owner_lang),
                replace_existing=True,
            )

        if brief_enabled:
            try:
                self.scheduler.add_job(
                    self.run_morning_briefing,
                    trigger=CronTrigger.from_crontab(brief_cron, timezone=tz),
                    kwargs={"notify": True},
                    id="briefing",
                    name=t("ui.job_name_briefing", owner_lang),
                    replace_existing=True,
                )
            except ValueError as exc:
                logger.warning("Neplatný cron výraz briefingu '%s': %s", brief_cron, exc)

        if nightly_enabled:
            try:
                self.scheduler.add_job(
                    self.run_nightly_reset,
                    trigger=CronTrigger.from_crontab(nightly_cron, timezone=tz),
                    id="nightly_reset",
                    name=t("ui.job_name_nightly", owner_lang),
                    replace_existing=True,
                )
            except ValueError as exc:
                logger.warning("Neplatný cron výraz nočního resetu '%s': %s", nightly_cron, exc)

        if idle_enabled:
            self.scheduler.add_job(
                self.run_idle_reset,
                trigger=IntervalTrigger(minutes=IDLE_RESET_CHECK_INTERVAL_MINUTES),
                id="idle_reset",
                name=t("ui.job_name_idle_reset", owner_lang),
                replace_existing=True,
            )

        if gh_enabled and gh_repo and gh_token:
            try:
                self.scheduler.add_job(
                    self.run_github_backup,
                    trigger=CronTrigger.from_crontab(gh_cron, timezone=tz),
                    kwargs={"notify": True},
                    id="github_backup",
                    name=t("ui.job_name_github_backup", owner_lang),
                    replace_existing=True,
                )
            except ValueError as exc:
                logger.warning("Neplatný cron výraz GitHub zálohy '%s': %s", gh_cron, exc)

        if self.db is not None:
            for task in self.db.list_scheduled_tasks(enabled_only=True):
                job_id = f"task:{task.id}"
                if task.schedule_type == "once":
                    try:
                        dt = datetime.fromisoformat(task.run_at)
                    except ValueError:
                        logger.warning("Neplatné datum run_at '%s' u úlohy %s", task.run_at, task.id)
                        continue
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=tz)
                    if dt <= datetime.now(tz):
                        continue
                    task_trigger = DateTrigger(run_date=dt, timezone=tz)
                else:
                    try:
                        task_trigger = CronTrigger.from_crontab(task.cron_expr, timezone=tz)
                    except ValueError as exc:
                        logger.warning("Neplatný cron '%s' u úlohy %s: %s", task.cron_expr, task.id, exc)
                        continue
                self.scheduler.add_job(
                    self.run_custom_task,
                    trigger=task_trigger,
                    kwargs={"task_id": task.id, "notify": True},
                    id=job_id,
                    name=task.name,
                    replace_existing=True,
                )

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def list_jobs(self) -> list[ScheduledJobInfo]:
        return [
            ScheduledJobInfo(
                id=job.id,
                name=job.name,
                next_run_time=(
                    nrt.isoformat()
                    if (nrt := getattr(job, "next_run_time", None)) is not None
                    else None
                ),
            )
            for job in self.scheduler.get_jobs()
        ]

    async def _send_notification(self, message: str) -> None:
        if self.notifier is not None:
            await self.notifier(message)

    def _lang(self) -> str:
        if self.db is not None:
            owner = self.db.get_owner()
            if owner is not None:
                return owner.language
        return self.settings.language

    async def _run_isolated(self, prompt: str, *, source: str, session_key: str) -> AgyResult:
        if self.db is not None:
            owner = self.db.get_owner()
            ensure_workspace(self.settings, owner)
        return await self.runner.run(
            prompt,
            session_key=session_key,
            source=source,
            use_conversation=False,
        )

    async def run_heartbeat(self, notify: bool = True) -> AgyResult:
        lang = self._lang()
        result = await self._run_isolated(
            t("prompts.heartbeat", lang), source="heartbeat", session_key="cron:heartbeat"
        )
        if result.success and not is_heartbeat_ok(result.text) and notify:
            await self._send_notification(t("backend.heartbeat_alert", lang, text=result.text))
        return result

    async def run_morning_briefing(self, notify: bool = True) -> AgyResult:
        lang = self._lang()
        result = await self._run_isolated(
            t("prompts.morning_briefing", lang), source="briefing", session_key="cron:briefing"
        )
        if result.success and notify:
            await self._send_notification(
                t("backend.morning_briefing_title", lang, text=result.text)
            )
        return result

    async def _flush_session_memory(
        self,
        session_key: str,
        raw_ctx: str,
        *,
        source: str,
        flush_prompt: str,
    ) -> tuple[bool, str | None]:
        ctx: ContextMode = (
            raw_ctx if raw_ctx in ("auto", "pracovni", "osobni") else "auto"
        )  # type: ignore[assignment]
        owner = self.db.get_owner() if self.db is not None else None
        try:
            ensure_workspace(self.settings, owner)
            res = await self.runner.run(
                flush_prompt,
                session_key=session_key,
                source=source,
                context_mode=ctx,
                use_conversation=True,
            )
            if res.success:
                return True, None
            return False, f"{session_key}: {res.error or res.text}"
        except Exception as exc:
            logger.exception("Úklid paměti (%s) pro %s selhal: %s", source, session_key, exc)
            return False, f"{session_key}: {exc}"

    async def run_nightly_reset(self) -> dict[str, object]:
        """Uloží důležité poznatky z aktivních konverzací do paměti, promaže staré denní logy a resetuje conversation_id."""
        if self.db is None:
            return {
                "flushed_sessions": 0,
                "reset_sessions": 0,
                "pruned_daily_files": 0,
                "errors": [],
            }

        owner = self.db.get_owner()
        lang = owner.language if owner is not None else self.settings.language
        tz_name = owner.tz if owner is not None else self.settings.tz
        retention_days = (
            owner.daily_memory_retention_days
            if owner is not None
            else self.settings.daily_memory_retention_days
        )

        active_sessions = self.db.list_active_sessions()
        flushed = 0
        errors: list[str] = []
        if not active_sessions:
            logger.info("Noční reset konverzací přeskočen – žádné aktivní konverzace.")
        else:
            flush_prompt = t("prompts.nightly_memory_flush", lang)
            try:
                for session_key, _conv_id, raw_ctx in active_sessions:
                    ok, err = await self._flush_session_memory(
                        session_key,
                        raw_ctx,
                        source="nightly_reset",
                        flush_prompt=flush_prompt,
                    )
                    if ok:
                        flushed += 1
                    elif err:
                        errors.append(err)
            finally:
                self.db.reset_all_sessions()
                if isinstance(self.runner, AgyRunner):
                    await self.runner.close_all()

        pruned = WorkspaceMemory(
            self.settings.workspace_dir, timezone_name=tz_name
        ).prune_old_daily_memory(retention_days)
        if pruned:
            logger.info("Promazáno %s starých denních poznámek z memory/.", len(pruned))

        if active_sessions:
            logger.info(
                "Noční úklid dokončen (uloženo %s/%s relací, vše resetováno, smazáno %s starých dnů).",
                flushed,
                len(active_sessions),
                len(pruned),
            )
        return {
            "flushed_sessions": flushed,
            "reset_sessions": len(active_sessions),
            "pruned_daily_files": len(pruned),
            "errors": errors,
        }

    async def run_idle_reset(self, *, now: datetime | None = None) -> dict[str, object]:
        """Po uplynutí doby nečinnosti uloží paměť a resetuje delší konverzace."""
        if self.db is None:
            return {"checked_sessions": 0, "flushed_sessions": 0, "reset_sessions": 0, "errors": []}

        owner = self.db.get_owner()
        idle_enabled = (
            owner.idle_reset_enabled if owner is not None else self.settings.idle_reset_enabled
        )
        if not idle_enabled:
            return {"checked_sessions": 0, "flushed_sessions": 0, "reset_sessions": 0, "errors": []}

        idle_minutes = (
            owner.idle_reset_minutes if owner is not None else self.settings.idle_reset_minutes
        )
        lang = owner.language if owner is not None else self.settings.language
        ref_now = now or datetime.now(timezone.utc)
        cutoff = ref_now - timedelta(minutes=max(15, int(idle_minutes)))

        sessions = self.db.list_active_sessions_with_updated_at()
        if not sessions:
            return {"checked_sessions": 0, "flushed_sessions": 0, "reset_sessions": 0, "errors": []}

        flush_prompt = t("prompts.nightly_memory_flush", lang)
        flushed = 0
        reset_count = 0
        errors: list[str] = []

        for session_key, conv_id, raw_ctx, updated_at_raw in sessions:
            try:
                updated_dt = datetime.fromisoformat(updated_at_raw)
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if updated_dt > cutoff:
                continue

            runs, tokens = self.db.get_conversation_stats(conv_id)
            if runs < IDLE_RESET_MIN_RUNS and tokens < IDLE_RESET_MIN_TOKENS:
                continue

            ctx: ContextMode = (
                raw_ctx if raw_ctx in ("auto", "pracovni", "osobni") else "auto"
            )  # type: ignore[assignment]
            try:
                ok, err = await self._flush_session_memory(
                    session_key,
                    ctx,
                    source="idle_reset",
                    flush_prompt=flush_prompt,
                )
                if ok:
                    flushed += 1
                elif err:
                    errors.append(err)
            finally:
                self.db.reset_conversation(session_key, default_context=ctx)
                if isinstance(self.runner, AgyRunner):
                    await self.runner.close_session(session_key)
                reset_count += 1

        if reset_count:
            logger.info(
                "Úklid po nečinnosti dokončen (zkontrolováno %s, uloženo %s, resetováno %s).",
                len(sessions),
                flushed,
                reset_count,
            )
        return {
            "checked_sessions": len(sessions),
            "flushed_sessions": flushed,
            "reset_sessions": reset_count,
            "errors": errors,
        }

    async def run_custom_task(self, *, task_id: int, notify: bool = True) -> AgyResult:
        if self.db is None:
            return AgyResult(
                text="Plánovač nemá databázi.",
                conversation_id=None,
                duration_ms=0,
                success=False,
                error="Chybí DB",
            )
        task = self.db.get_scheduled_task(task_id)
        if task is None:
            return AgyResult(
                text=f"Úloha {task_id} nebyla nalezena.",
                conversation_id=None,
                duration_ms=0,
                success=False,
                error="Úloha neexistuje",
            )

        if task.action_type == "message":
            if notify:
                await self._send_notification(f"🔔 {task.name}\n\n{task.prompt}")
            self.db.log_run(
                source="task",
                session_key=f"task:{task.id}",
                context_mode="auto",
                prompt=task.name,
                response=task.prompt,
                conversation_id=None,
                duration_ms=0,
                success=True,
            )
            result = AgyResult(
                text=task.prompt, conversation_id=None, duration_ms=0, success=True
            )
        else:
            result = await self._run_isolated(
                task.prompt, source="task", session_key=f"task:{task.id}"
            )
            if result.success and notify:
                await self._send_notification(f"📌 {task.name}\n\n{result.text}")

        if task.schedule_type == "once":
            self.db.delete_scheduled_task(task.id)
        else:
            self.db.update_scheduled_task(
                task.id, last_run_at=datetime.now(timezone.utc).isoformat()
            )
        return result

    async def run_github_backup(self, notify: bool = True) -> GitHubBackupResult:
        """Spustí zálohu celého agenta na GitHub a při chybě upozorní vlastníka."""
        if self.db is None:
            return GitHubBackupResult(
                success=False,
                changed=False,
                commit_sha="",
                file_count=0,
                repo_url="",
                branch="main",
                message="Databáze není dostupná.",
                ran_at=datetime.now(timezone.utc).isoformat(),
            )
        result = await execute_github_backup(self.settings, self.db, app_version=__version__)
        if not result.success and notify:
            await self._send_notification(
                t("backend.github_backup_failed_alert", self._lang(), error=result.message)
            )
        return result
