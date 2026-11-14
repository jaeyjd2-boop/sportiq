import logging
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from app.config import settings
from app.collector import collect_leagues, collect_fixtures, collect_live, collect_match_details

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sportiq-worker")


def safe(label, fn):
    try:
        log.info("%s: %s", label, fn())
    except Exception:
        log.exception("%s failed", label)


def fixture_window():
    # API-Football free tier permits yesterday, today and tomorrow only.
    # Use UTC to match the worker scheduler and avoid timezone boundary drift.
    today = datetime.now(timezone.utc).date()
    future_days = min(max(0, settings.fixture_lookahead_days), 1)
    days = [today + timedelta(days=offset) for offset in range(-1, future_days + 1)]
    total = 0
    for day in days:
        try:
            count = collect_fixtures(day)
            total += count
            log.info("fixtures %s: %s", day.isoformat(), count)
        except Exception:
            # A single unavailable date must not prevent other permitted dates.
            log.exception("fixtures %s failed", day.isoformat())
    return total


scheduler = BlockingScheduler(timezone="UTC")
scheduler.add_job(lambda: safe("leagues", collect_leagues), "interval", hours=24)
scheduler.add_job(lambda: safe("fixtures", fixture_window), "interval", hours=12)
scheduler.add_job(lambda: safe("live", collect_live), "interval", seconds=max(60, settings.live_poll_seconds))
# At most 2 live fixtures per hour, 4 external requests per run.
scheduler.add_job(lambda: safe("match details", collect_match_details), "interval", hours=1)

if __name__ == "__main__":
    safe("initial leagues", collect_leagues)
    safe("initial fixtures", fixture_window)
    scheduler.start()
