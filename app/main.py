from __future__ import annotations

import asyncio
import logging

import uvicorn

from app.agy_runner import AgyRunner
from app.auth_session import AgyAuthManager
from app.config import Settings
from app.db import AssistantDB
from app.i18n import t  # noqa: F401 - zachování importní konzistence
from app.memory import WorkspaceMemory
from app.plugins import PluginManager
from app.runtime_config import apply_runtime_overrides
from app.scheduler import AssistantScheduler
from app.telegram_bot import TelegramBotController
from app.user_env import (
    ensure_workspace,
    import_global_mcp_if_needed,
    migrate_legacy_home_if_needed,
    prepare_agy_home,
)
from app.web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def serve() -> None:
    """Hlavní vstupní bod single-user orchestrátoru – Web UI, plánovač a Telegram bot."""
    settings = Settings()
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.effective_home_dir.mkdir(parents=True, exist_ok=True)
    apply_runtime_overrides(settings)

    migrate_legacy_home_if_needed(settings)

    db = AssistantDB(settings.db_path)
    owner = db.ensure_owner(settings)
    db.cleanup_expired_auth_sessions()
    import_global_mcp_if_needed(db, home_dir=settings.effective_home_dir)
    ensure_workspace(settings, owner)
    prepare_agy_home(settings, db)

    memory = WorkspaceMemory(settings.workspace_dir, timezone_name=owner.tz)
    runner = AgyRunner(settings, db)
    auth_manager = AgyAuthManager(settings)
    plugin_manager = PluginManager(settings, db)
    scheduler = AssistantScheduler(settings, runner, db=db)
    runner.on_schedule_updated = scheduler.sync_jobs
    bot_controller = TelegramBotController(settings, db, runner, scheduler)
    scheduler.notifier = bot_controller.send_notification

    async def on_settings_updated() -> None:
        scheduler.sync_jobs()
        await bot_controller.reload()

    app = create_app(
        settings,
        db,
        memory,
        runner,
        scheduler=scheduler,
        auth_manager=auth_manager,
        plugin_manager=plugin_manager,
        on_settings_updated=on_settings_updated,
    )

    scheduler.start()
    await bot_controller.reload()

    config = uvicorn.Config(
        app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await runner.close_all()
        await auth_manager.stop()
        await plugin_manager.stop_connect()
        scheduler.shutdown()
        await bot_controller.stop()


if __name__ == "__main__":
    asyncio.run(serve())
