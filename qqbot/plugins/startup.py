"""Startup plugin - Initialize database, scheduler and sync tasks.

This plugin is loaded by NoneBot and handles all startup initialization.
"""

import logging
import asyncio
import datetime
from nonebot import get_driver, get_bot

logger = logging.getLogger(__name__)

driver = get_driver()


@driver.on_startup
async def on_startup() -> None:
    """Initialize database and scheduler on startup."""
    print("[startup] 🚀 *** STARTUP PLUGIN TRIGGERED ***")
    logger.info("=" * 50)
    logger.info("🚀 Initializing core services...")
    logger.info("=" * 50)

    try:
        from qqbot.core import init_db, init_scheduler

        # Initialize database
        logger.info("[startup] 📦 Initializing database...")
        await init_db()
        logger.info("[startup] ✅ Database initialized")

        # Initialize scheduler
        logger.info("[startup] ⏱️  Initializing scheduler...")
        await init_scheduler()
        logger.info("[startup] ✅ Scheduler initialized")

        logger.info("=" * 50)
        logger.info("[startup] ✨ Core services ready!")
        logger.info("=" * 50)

        # Schedule sync tasks to start 40 seconds after startup
        logger.info("[startup] ⏰ Scheduling sync tasks to start in 40 seconds...")
        asyncio.create_task(_schedule_sync_tasks_delayed())

    except Exception as e:
        logger.error(f"[startup] ❌ Initialization failed: {e}", exc_info=True)
        raise


async def _schedule_sync_tasks_delayed() -> None:
    """Schedule sync tasks after a delay to allow bot connection."""
    print("[startup] 🚀 *** _schedule_sync_tasks_delayed STARTED ***")

    try:
        # Wait 40 seconds for bot to be fully connected
        print("[startup] ⏳ Starting 40 second wait...")
        await asyncio.sleep(40)
        print("[startup] ⏳ 40 second wait completed!")

        print("[startup] 📝 Registering sync tasks now...")

        from qqbot.core.scheduler import get_scheduler

        try:
            bot = get_bot()
            print(f"[startup] ✅ Got bot: {bot}")
        except ValueError as e:
            print(f"[startup] ❌ No bot connected yet: {e}")
            return

        scheduler = get_scheduler()
        print(f"[startup] scheduler.running = {scheduler.running}")

        if not scheduler.running:
            print("[startup] ❌ Scheduler not running")
            return

        from qqbot.plugins.sync_nicknames import sync_all_group_nicknames
        print("[startup] ✅ Imported sync_all_group_nicknames")

        # 直接执行一次同步（不通过scheduler）
        print("[startup] 🔄 Running sync_all_group_nicknames DIRECTLY (not via scheduler)...")
        try:
            await sync_all_group_nicknames(bot)
            print("[startup] ✅ Direct sync completed!")
        except Exception as e:
            print(f"[startup] ❌ Direct sync failed: {e}")
            import traceback
            traceback.print_exc()

        # Register initial sync (run immediately)
        scheduler.add_job(
            sync_all_group_nicknames,
            "date",
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=1),
            args=[bot],
            id="sync_nicknames_initial",
            misfire_grace_time=60,
        )
        print("[startup] ✅ Registered initial sync (will run in 1 second)")

        # Register periodic sync (every 30 minutes)
        scheduler.add_job(
            sync_all_group_nicknames,
            "interval",
            minutes=30,
            args=[bot],
            id="sync_nicknames_periodic",
            replace_existing=True,
        )
        print("[startup] ✅ Registered periodic sync (every 30 minutes)")

    except Exception as e:
        print(f"[startup] ❌ Failed to schedule sync tasks: {e}")
        import traceback
        traceback.print_exc()


@driver.on_shutdown
async def on_shutdown() -> None:
    """Cleanup on shutdown."""
    logger.info("[shutdown] 🛑 Shutting down...")
    try:
        from qqbot.core import shutdown_scheduler, close_db
        await shutdown_scheduler()
        await close_db()
        logger.info("[shutdown] ✅ Cleanup complete")
    except Exception as e:
        logger.error(f"[shutdown] ❌ Error during cleanup: {e}", exc_info=True)
