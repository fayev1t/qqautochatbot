"""Task scheduler for background jobs."""

import logging
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler = AsyncIOScheduler()


def get_scheduler() -> AsyncIOScheduler:
    """Get the global scheduler instance."""
    return scheduler


async def init_scheduler() -> None:
    """Initialize scheduler and start it."""
    if not scheduler.running:
        logger.info("[scheduler] Starting AsyncIOScheduler...")
        try:
            # Get current event loop
            loop = asyncio.get_running_loop()
            logger.debug(f"[scheduler] Current event loop: {loop}")

            # Ensure scheduler uses the current event loop
            scheduler.configure(timezone="UTC")
            scheduler.start()

            logger.info(f"[scheduler] ✅ Scheduler started. Running: {scheduler.running}")
        except Exception as e:
            logger.error(f"[scheduler] ❌ Failed to start scheduler: {e}", exc_info=True)
            raise
    else:
        logger.debug("[scheduler] Scheduler already running")


async def shutdown_scheduler() -> None:
    """Shutdown scheduler gracefully."""
    if scheduler.running:
        logger.info("[scheduler] Shutting down scheduler...")
        try:
            scheduler.shutdown()
            logger.info("[scheduler] ✅ Scheduler shut down")
        except Exception as e:
            logger.error(f"[scheduler] ❌ Failed to shutdown scheduler: {e}", exc_info=True)


