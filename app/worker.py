import logging
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from app.config import settings
from app.collector import collect_leagues, collect_fixtures, collect_live, collect_match_details

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sportiq-worker")


def safe(label, fn):
    if not settings.football_data_token:
        log.warning("%s skipped: FOOTBALL_DATA_TOKEN is not configured", label)
        return
    try:
        log.info("%s: %s", label, fn())
    except Exception:
        log.exception("%s failed", label)


def fixture_window():
    # Six competition requests per run; 7-day coverage without daily requests.
    today = datetime.now(timezone.utc).date()
    end_day = today + timedelta(days=6)
    return collect_fixtures(today, end_day)


scheduler = BlockingScheduler(timezone="UTC")
scheduler.add_job(lambda: safe("leagues", collect_leagues), "interval", hours=24)
scheduler.add_job(lambda: safe("fixtures", fixture_window), "interval", hours=12)
scheduler.add_job(lambda: safe("live", collect_live), "interval", seconds=max(3600, settings.live_poll_seconds))
# At most 2 live fixtures per hour, 4 external requests per run.
 # Detailed statistics/events are unavailable on football-data.org free tier.

if __name__ == "__main__":
    safe("initial leagues", collect_leagues)
    safe("initial fixtures", fixture_window)
    scheduler.start()
