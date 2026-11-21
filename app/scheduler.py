from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.agy_runner import AgyResult, AgyRunner
from app.config import ContextMode, Settings
from app.db import AssistantDB
from app.user_env import ensure_workspace

logger = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]

HEARTBEAT_PROMPT = (
    "Proveď pravidelnou kontrolu podle HEARTBEAT.md. "
    "Přečti si HEARTBEAT.md, MEMORY.md a dnešní poznámku v memory/. "
    "Pokud není nic urgentního ani důvod uživatele kontaktovat, "
    "odpověz přesně a pouze: HEARTBEAT_OK"
)

MORNING_BRIEFING_PROMPT = (
    "Aktivuj skill morning-briefing (viz .agents/skills/morning-briefing/SKILL.md) "
    "a připrav stručný český ranní briefing pro dnešní den."
)

NIGHTLY_MEMORY_FLUSH_PROMPT = (
    "Provádíš automatický noční úklid před resetem této konverzace. "
    "Projdi dnešní konverzaci a ulož důležité trvalé informace, rozhodnutí, "
    "rozdělané úkoly, termíny a kontakty do dnešního souboru memory/YYYY-MM-DD.md "
    "(případně do příslušného tematického souboru v memory/, např. memory/rozdelane.md nebo memory/projekty.md) "
    "a aktualizuj dlouhodobou paměť MEMORY.md podle skillu memory-maintenance. "
    "Drž MEMORY.md kompaktní a stručný (cílově do ~150 řádků – jen aktivní věci, preference a odkazy); "
    "uzavřené nebo starší podrobnosti přesouvej do memory/archiv.md nebo tematických souborů v memory/. "
    "Nikdy neukládej hesla, tokeny ani tajné klíče. "
    "Pokud v konverzaci není nic podstatného k uložení, nic nezapisuj. "
    "Odpověz velmi stručně jednou větou, co bylo uloženo, nebo přesně: MEMORY_FLUSH_OK"
)


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

        if hb_enabled:
            self.scheduler.add_job(
                self.run_heartbeat,
                trigger=IntervalTrigger(minutes=max(1, hb_interval)),
                kwargs={"notify": True},
                id="heartbeat",
                name="Heartbeat",
                replace_existing=True,
            )

        if brief_enabled:
            try:
                self.scheduler.add_job(
                    self.run_morning_briefing,
                    trigger=CronTrigger.from_crontab(brief_cron, timezone=tz),
                    kwargs={"notify": True},
                    id="briefing",
                    name="Ranní briefing",
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
                    name="Noční úklid a reset",
                    replace_existing=True,
                )
            except ValueError as exc:
                logger.warning("Neplatný cron výraz nočního resetu '%s': %s", nightly_cron, exc)

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
        result = await self._run_isolated(
            HEARTBEAT_PROMPT, source="heartbeat", session_key="cron:heartbeat"
        )
        if result.success and not is_heartbeat_ok(result.text) and notify:
            await self._send_notification(f"🔔 Heartbeat upozornění:\n\n{result.text}")
        return result

    async def run_morning_briefing(self, notify: bool = True) -> AgyResult:
        result = await self._run_isolated(
            MORNING_BRIEFING_PROMPT, source="briefing", session_key="cron:briefing"
        )
        if result.success and notify:
            await self._send_notification(f"☀️ Ranní briefing:\n\n{result.text}")
        return result

    async def run_nightly_reset(self) -> dict[str, object]:
        """Uloží důležité poznatky z aktivních konverzací do paměti a poté resetuje jejich conversation_id."""
        if self.db is None:
            return {"flushed_sessions": 0, "reset_sessions": 0, "errors": []}

        active_sessions = self.db.list_active_sessions()
        if not active_sessions:
            logger.info("Noční reset přeskočen – žádné aktivní konverzace.")
            return {"flushed_sessions": 0, "reset_sessions": 0, "errors": []}

        owner = self.db.get_owner()
        flushed = 0
        errors: list[str] = []
        try:
            for session_key, _conv_id, raw_ctx in active_sessions:
                ctx: ContextMode = (
                    raw_ctx
                    if raw_ctx in ("auto", "pracovni", "osobni")
                    else "auto"
                )  # type: ignore[assignment]
                try:
                    ensure_workspace(self.settings, owner)
                    res = await self.runner.run(
                        NIGHTLY_MEMORY_FLUSH_PROMPT,
                        session_key=session_key,
                        source="nightly_reset",
                        context_mode=ctx,
                        use_conversation=True,
                    )
                    if res.success:
                        flushed += 1
                    else:
                        errors.append(f"{session_key}: {res.error or res.text}")
                except Exception as exc:
                    logger.exception("Noční uložení paměti pro %s selhalo: %s", session_key, exc)
                    errors.append(f"{session_key}: {exc}")
        finally:
            self.db.reset_all_sessions()
            if isinstance(self.runner, AgyRunner):
                await self.runner.close_all()

        logger.info(
            "Noční úklid dokončen (uloženo %s/%s relací, vše resetováno).",
            flushed,
            len(active_sessions),
        )
        return {
            "flushed_sessions": flushed,
            "reset_sessions": len(active_sessions),
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
