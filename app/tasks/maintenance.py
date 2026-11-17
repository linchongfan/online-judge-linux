"""Periodic judge maintenance: Outbox redispatch + expired-task sweeping.

Runs as a standalone long-lived process next to the result consumer so that
transient Redis/database failures cannot leave submissions stuck forever.
"""

import logging
import time

from app.db import SessionLocal
from app.services.auth import cleanup_expired_sessions
from app.tasks.dispatcher import process_outbox
from app.tasks.sweeper import sweep_expired_tasks

logger = logging.getLogger(__name__)

SESSION_CLEANUP_INTERVAL_SECONDS = 3600


def run_maintenance(interval_seconds: int = 5) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.info("Judge maintenance started (interval=%ss)", interval_seconds)
    last_session_cleanup = 0.0
    while True:
        try:
            stats = process_outbox()
            if stats["failed"]:
                logger.warning("Outbox sweep: %s", stats)
        except Exception:
            logger.exception("Outbox sweep failed")
        try:
            stats = sweep_expired_tasks()
            if stats["recovered"] or stats["failed"]:
                logger.info("Expired-task sweep: %s", stats)
        except Exception:
            logger.exception("Expired-task sweep failed")
        now = time.monotonic()
        if now - last_session_cleanup >= SESSION_CLEANUP_INTERVAL_SECONDS:
            last_session_cleanup = now
            db = SessionLocal()
            try:
                removed = cleanup_expired_sessions(db)
                if removed:
                    logger.info("Cleaned up %s expired auth sessions", removed)
            except Exception:
                logger.exception("Auth session cleanup failed")
            finally:
                db.close()
        time.sleep(interval_seconds)


if __name__ == "__main__":
    run_maintenance()
