import nonebot
from nonebot import get_driver
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

nonebot.init()

# Load all plugins explicitly
nonebot.load_plugins("qqbot.plugins")

# Print loaded plugins
import nonebot.plugin
print(f"[startup] Loaded plugins: {nonebot.plugin.get_loaded_plugins()}")

driver = get_driver()


@driver.on_startup
async def on_startup() -> None:
    """Initialize database and scheduler on startup."""
    print("[startup] 🚀 *** on_startup HOOK TRIGGERED ***")
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
        import asyncio
        asyncio.create_task(_schedule_sync_tasks_delayed())

    except Exception as e:
        logger.error(f"[startup] ❌ Initialization failed: {e}", exc_info=True)
        raise


async def _schedule_sync_tasks_delayed() -> None:
    """Schedule sync tasks after a delay to allow bot connection."""
    print("[startup] 🚀 *** _schedule_sync_tasks_delayed STARTED ***")
    import asyncio
    import datetime

    try:
        # Wait 40 seconds for bot to be fully connected
        logger.info("[startup] ⏳ Waiting 40 seconds for bot to connect...")
        await asyncio.sleep(40)

        logger.info("[startup] 📝 Registering sync tasks now...")

        from qqbot.core.scheduler import get_scheduler

        # Get the bot instance (assume first connected bot)
        from nonebot import get_bot

        try:
            bot = get_bot()
        except ValueError:
            logger.error("[startup] ❌ No bot connected yet, cannot register tasks")
            return

        scheduler = get_scheduler()

        if not scheduler.running:
            logger.error("[startup] ❌ Scheduler not running")
            return

        from qqbot.plugins.sync_nicknames import sync_all_group_nicknames

        # Register initial sync (run immediately)
        scheduler.add_job(
            sync_all_group_nicknames,
            "date",
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=1),
            args=[bot],
            id="sync_nicknames_initial",
            misfire_grace_time=60,
        )
        logger.info("[startup] ✅ Registered initial sync (will run in 1 second)")

        # Register periodic sync (every 30 minutes)
        scheduler.add_job(
            sync_all_group_nicknames,
            "interval",
            minutes=30,
            args=[bot],
            id="sync_nicknames_periodic",
            replace_existing=True,
        )
        logger.info("[startup] ✅ Registered periodic sync (every 30 minutes)")

    except Exception as e:
        logger.error(f"[startup] ❌ Failed to schedule sync tasks: {e}", exc_info=True)


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


if __name__ == "__main__":
    nonebot.run()




