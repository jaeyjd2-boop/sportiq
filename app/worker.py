import logging
from datetime import date,timedelta
from apscheduler.schedulers.blocking import BlockingScheduler
from app.config import settings
from app.collector import collect_leagues,collect_fixtures,collect_live
logging.basicConfig(level=logging.INFO)
log=logging.getLogger("sportiq-worker")
def safe(label,fn):
    try: log.info("%s: %s",label,fn())
    except Exception: log.exception("%s failed",label)
def fixture_window():
    return sum(collect_fixtures(date.today()+timedelta(days=n)) for n in range(settings.fixture_lookahead_days+1))
scheduler=BlockingScheduler(timezone="UTC")
scheduler.add_job(lambda:safe("leagues",collect_leagues),"interval",hours=24)
scheduler.add_job(lambda:safe("fixtures",fixture_window),"interval",hours=12)
scheduler.add_job(lambda:safe("live",collect_live),"interval",seconds=max(60,settings.live_poll_seconds))
if __name__=="__main__":
    safe("initial leagues",collect_leagues); safe("initial fixtures",fixture_window); scheduler.start()
