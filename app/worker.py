import logging
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import text
from app.config import settings
from app.db import engine
from app.collector import (collect_leagues, collect_fixtures, collect_live, refresh_forecast_results,
                           recheck_unfinished, collect_head_to_head)
from app.forecast_tracking import snapshot_forecasts, evaluate_forecasts
from app import multisport, multisport_backfill

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
    # Start at yesterday (UTC): late kickoffs (e.g. Brazil 19:30Z+) finish
    # after midnight UTC and were never rescanned once "today" moved on.
    # football-data returns a whole date range per competition request, so
    # the extra day costs no additional API calls.
    today = datetime.now(timezone.utc).date()
    end_day = today + timedelta(days=max(1, settings.fixture_lookahead_days) - 1)
    return collect_fixtures(today - timedelta(days=1), end_day)


LIVE_POLL_MIN_SECONDS = 120


def live_poll_interval_seconds(configured=None):
    """LIVE_POLL_SECONDS (render.yaml: 600), never below 120 s.

    The old max(3600, ...) silently turned the 10-minute setting into an
    hourly poll. One live poll costs 1 request (+ up to 3 for matches that
    just left the live feed), against football-data's 10 req/min.
    """
    value = settings.live_poll_seconds if configured is None else configured
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 600
    return max(LIVE_POLL_MIN_SECONDS, value)


def safe_multisport(label, fn):
    """Like safe() but without the football-data token requirement."""
    if not settings.multisport_enabled:
        return
    try:
        result = fn()
        log.info("%s: %s", label, result)
        try:
            record_job(label, "success", result=sum(v for v in (result or {}).values() if isinstance(v, int)))
        except Exception:
            log.exception("%s monitoring write failed", label)
    except Exception as exc:
        log.exception("%s failed", label)
        try:
            record_job(label, "failed", error=exc)
        except Exception:
            log.exception("%s monitoring write failed", label)


scheduler = BlockingScheduler(timezone="UTC")
scheduler.add_job(lambda: safe("forecast_snapshot", snapshot_forecasts), "interval", hours=6, id="forecast_snapshot", max_instances=1, coalesce=True)
scheduler.add_job(lambda: safe("forecast_result_refresh", refresh_forecast_results), "interval", hours=3, id="forecast_result_refresh", max_instances=1, coalesce=True)
scheduler.add_job(lambda: safe("forecast_evaluation", evaluate_forecasts), "interval", hours=1, id="forecast_evaluation", max_instances=1, coalesce=True)
scheduler.add_job(lambda: safe("leagues", collect_leagues), "interval", hours=24, id="leagues", max_instances=1, coalesce=True)
scheduler.add_job(lambda: safe("fixtures", fixture_window), "interval", hours=12, id="fixtures", max_instances=1, coalesce=True)
# First run 1 min after start: every deploy restarts the worker, and an
# interval job otherwise waits a full period before its first run.
scheduler.add_job(lambda: safe("live", collect_live), "interval", seconds=live_poll_interval_seconds(), id="live", max_instances=1, coalesce=True,
                  next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))
# Matches past kickoff+2h that are still NS/LIVE are rechecked until the
# provider reports a final status (bounded to the last 3 days).
scheduler.add_job(lambda: safe("recheck_unfinished", recheck_unfinished), "interval", minutes=30, id="recheck_unfinished", max_instances=1, coalesce=True)
# First run 10 min after start: the worker restarts on every deploy, so a
# plain 24h interval might never fire.
scheduler.add_job(lambda: safe("head_to_head", collect_head_to_head), "interval", hours=24, id="head_to_head", max_instances=1, coalesce=True,
                  next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10))

# misfire_grace_time=None: the startup football scan takes minutes, so the
# first scheduled run is usually "missed"; run it late instead of skipping
# to the next interval (otherwise other sports wait an hour after deploy).
# Non-football sports (ESPN, or API-SPORTS once API_SPORTS_KEY is set).
scheduler.add_job(lambda: safe_multisport("multisport_schedule", multisport.collect_schedule), "interval",
                  minutes=max(30, settings.multisport_schedule_minutes), id="multisport_schedule", max_instances=1, coalesce=True, misfire_grace_time=None,
                  next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))
KOREA_LIVE_ON = settings.korea_live_minutes > 0
scheduler.add_job(lambda: safe_multisport("multisport_live", lambda: multisport.collect_live(include_korea=not KOREA_LIVE_ON)), "interval",
                  minutes=max(5, settings.multisport_live_minutes), id="multisport_live", max_instances=1, coalesce=True, misfire_grace_time=None,
                  next_run_time=datetime.now(timezone.utc) + timedelta(minutes=4))
# Korean live games every 2 min (billnojay scoreboard, 2 requests, only while one is on):
# with the 10-min job above, 경기판 and /v1/matches/live showed K League minutes 10 min late.
if KOREA_LIVE_ON:
    scheduler.add_job(lambda: safe_multisport("multisport_korea_live", multisport.collect_korea_live), "interval",
                      minutes=max(1, settings.korea_live_minutes), id="multisport_korea_live", max_instances=1, coalesce=True,
                      misfire_grace_time=None, next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3))
# History for /analysis: last 30 days of finished matches + team season schedules,
# at most 25 ESPN requests per run; progress is kept in the DB across deploys.
scheduler.add_job(lambda: safe_multisport("multisport_backfill", multisport_backfill.collect_backfill), "interval",
                  minutes=10, id="multisport_backfill", max_instances=1, coalesce=True, misfire_grace_time=None,
                  next_run_time=datetime.now(timezone.utc) + timedelta(minutes=15))

if __name__ == "__main__":
    safe("leagues", collect_leagues)
    safe("fixtures", fixture_window)
    safe("forecast_snapshot", snapshot_forecasts)
    safe("forecast_result_refresh", refresh_forecast_results)
    safe("forecast_evaluation", evaluate_forecasts)
    safe("recheck_unfinished", recheck_unfinished)
    scheduler.start()
