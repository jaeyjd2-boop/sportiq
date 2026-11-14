"""Opt-in web-process backfill scheduler. Disabled unless explicitly enabled."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from app.backfill import run_backfill
from app.config import settings
from app.db import engine

log = logging.getLogger("sportiq-web-scheduler")
# Session-level PostgreSQL advisory lock: held by a dedicated connection
# for the entire backfill, including API requests and between-window pauses.
LOCK_ID = 836240119
scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
_local_lock = asyncio.Lock()


def _backfill_locked(days, window_days, competition):
    # No SQL transaction is held during HTTP calls. Advisory lock is
    # session-scoped and released explicitly before connection returns to pool.
    with engine.connect() as conn:
        acquired = conn.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": LOCK_ID}
        ).scalar_one()
        conn.commit()
        if not acquired:
            return {"status": "skipped", "reason": "another_process_running"}
        try:
            result = run_backfill(days, window_days, competition, write=True)
            if result["failed_windows"]:
                return {"status": "failed", **result}
            return {"status": "completed", **result}
        finally:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": LOCK_ID})
                conn.commit()
            except Exception:
                # Invalidate to avoid returning a connection with a held session lock.
                conn.invalidate()
                log.exception("Could not release backfill advisory lock")


def _record_outcome(result, elapsed):
    # Monitoring failures must not hide the backfill result.
    from app.worker import record_job
    status = result["status"]
    count = result.get("saved")
    error = result.get("reason") if status == "skipped" else (
        f"failed_windows={result.get('failed_windows', 0)}" if status == "failed" else None
    )
    record_job("web_backfill", "success" if status == "completed" else status,
               result=count, error=error)


async def scheduled_backfill_job(days=7, window_days=7, competition=None, source="scheduler"):
    if not settings.web_backfill_enabled:
        return {"status": "skipped", "reason": "disabled"}
    if _local_lock.locked():
        return {"status": "skipped", "reason": "already_running"}
    async with _local_lock:
        started = datetime.now(timezone.utc)
        try:
            result = await asyncio.to_thread(
                _backfill_locked, days, window_days,
                competition or settings.web_backfill_competition
            )
        except Exception:
            log.exception("Web backfill failed source=%s", source)
            result = {"status": "failed", "reason": "backfill_exception"}
        elapsed = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
        try:
            await asyncio.to_thread(_record_outcome, result, elapsed)
        except Exception:
            log.exception("Web backfill monitoring failed")
        log.info("Web backfill source=%s result=%s elapsed_sec=%s", source, result, elapsed)
        return {**result, "elapsed_sec": elapsed}


@asynccontextmanager
async def web_backfill_lifespan(app):
    if settings.web_backfill_enabled:
        if not settings.football_data_token:
            raise RuntimeError("WEB_BACKFILL_ENABLED requires FOOTBALL_DATA_TOKEN")
        if engine.dialect.name != "postgresql":
            raise RuntimeError("Web backfill requires PostgreSQL advisory locking")
        scheduler.add_job(
            scheduled_backfill_job, IntervalTrigger(hours=6),
            kwargs={"days": 7, "window_days": 7, "source": "interval_6h"},
            id="web_backfill_recent", max_instances=1, coalesce=True,
            replace_existing=True
        )
        scheduler.add_job(
            scheduled_backfill_job,
            CronTrigger(hour=4, minute=0, timezone="Asia/Seoul"),
            kwargs={"days": 30, "window_days": 7, "source": "daily_4am"},
            id="web_backfill_daily", max_instances=1, coalesce=True,
            replace_existing=True
        )
        scheduler.start()
        log.info("Opt-in web backfill scheduler enabled")
    else:
        log.info("Web backfill scheduler disabled (default)")
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
