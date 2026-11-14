import logging
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import text
from app.config import settings
from app.db import engine
from app.collector import collect_leagues, collect_fixtures, collect_live
from app.forecast_tracking import snapshot_forecasts, evaluate_forecasts

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sportiq-worker")


def record_job(label, status, result=None, error=None):
    # Persist run outcomes for operational checks, including failed runs.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS collector_runs (
                id BIGSERIAL PRIMARY KEY,
                job_name TEXT NOT NULL,
                status TEXT NOT NULL,
                result_count INTEGER,
                error_message TEXT,
                finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("""
            INSERT INTO collector_runs(job_name,status,result_count,error_message)
            VALUES (:job,:status,:count,:error)
        """), {"job": label, "status": status, "count": result if isinstance(result, int) else None,
                "error": str(error)[:1000] if error else None})
        conn.execute(text("""
            DELETE FROM collector_runs WHERE finished_at < NOW() - INTERVAL '30 days'
        """))


def safe(label, fn):
    if not settings.football_data_token:
        log.warning("%s skipped: FOOTBALL_DATA_TOKEN is not configured", label)
        try:
            record_job(label, "skipped", error="FOOTBALL_DATA_TOKEN missing")
        except Exception:
            log.exception("%s monitoring write failed", label)
        return
    try:
        result = fn()
        log.info("%s: %s", label, result)
        try:
            record_job(label, "success", result=result)
        except Exception:
            log.exception("%s monitoring write failed", label)
    except Exception as exc:
        log.exception("%s failed", label)
        try:
            record_job(label, "failed", error=exc)
        except Exception:
            log.exception("%s monitoring write failed", label)


def fixture_window():
    today = datetime.now(timezone.utc).date()
    end_day = today + timedelta(days=max(1, settings.fixture_lookahead_days) - 1)
    return collect_fixtures(today, end_day)


scheduler = BlockingScheduler(timezone="UTC")
scheduler.add_job(lambda: safe("forecast_snapshot", snapshot_forecasts), "interval", hours=6, id="forecast_snapshot", max_instances=1, coalesce=True)
scheduler.add_job(lambda: safe("forecast_evaluation", evaluate_forecasts), "interval", hours=1, id="forecast_evaluation", max_instances=1, coalesce=True)
scheduler.add_job(lambda: safe("leagues", collect_leagues), "interval", hours=24, id="leagues", max_instances=1, coalesce=True)
scheduler.add_job(lambda: safe("fixtures", fixture_window), "interval", hours=12, id="fixtures", max_instances=1, coalesce=True)
scheduler.add_job(lambda: safe("live", collect_live), "interval", seconds=max(3600, settings.live_poll_seconds), id="live", max_instances=1, coalesce=True)

if __name__ == "__main__":
    safe("leagues", collect_leagues)
    safe("fixtures", fixture_window)
    safe("forecast_snapshot", snapshot_forecasts)
    safe("forecast_evaluation", evaluate_forecasts)
    scheduler.start()
