"""History for non-football sports: past 30 days, then one new day per day.

The schedule job covers yesterday..lookahead only, so /analysis had no
recent form or head-to-head for ESPN sports. This job fills finished
matches in small steps:

* "day" tasks: one ESPN date per league, newest first, back to 30 days.
  Event leagues (ATP, WTA, UFC) take one 30-day range request first
  ("range" task), then single days.
* "team" tasks (NFL, NBA, NHL, MLB): each team's season schedule, so
  head-to-head covers the whole current season (MLB: the 2026 season that
  just ended). One request per team plus one for the team list.

Every task is recorded in espn_backfill_progress once its request
succeeded, so redeploys resume where they stopped and finished days are
never fetched again. Each run makes at most MAX_REQUESTS_PER_RUN requests
(spaced by the ESPN client). Only finished matches (FT/AET/PEN) are
stored, with the provider's status and score. Sports switched to
API-SPORTS are skipped (one source per sport).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app import multisport
from app.db import engine
from app.providers import espn

log = logging.getLogger("sportiq-multisport-backfill")

DAYS_BACK = 30
MAX_REQUESTS_PER_RUN = 25
FINISHED = ("FT", "AET", "PEN")
TEAM_SCHEDULE_LEAGUES = ("NFL", "NBA", "NHL", "MLB")
RECENT_FIRST_DAYS = 7


def _ensure_table():
    with engine.begin() as c:
        c.execute(text("""CREATE TABLE IF NOT EXISTS espn_backfill_progress(
            task VARCHAR(160) PRIMARY KEY, done_at VARCHAR(40) NOT NULL,
            stored INTEGER NOT NULL DEFAULT 0, detail TEXT)"""))


def _done_tasks():
    with engine.connect() as c:
        return {r[0]: r[1] for r in c.execute(text("SELECT task, detail FROM espn_backfill_progress")).all()}


def _mark(task, stored, detail=None):
    with engine.begin() as c:
        c.execute(text("""INSERT INTO espn_backfill_progress(task, done_at, stored, detail)
                          VALUES(:t, :at, :n, :d)
                          ON CONFLICT(task) DO UPDATE SET done_at=EXCLUDED.done_at, stored=EXCLUDED.stored, detail=EXCLUDED.detail"""),
                  {"t": task, "at": datetime.now(timezone.utc).isoformat(), "n": int(stored), "d": detail})


def season_label(league_key, today):
    """Season the team-schedule task refers to (and the ?season= sent for MLB)."""
    if league_key == "MLB":            # March..October; ESPN's default in winter is next season
        return str(today.year if today.month >= 3 else today.year - 1)
    return str(today.year if today.month >= 7 else today.year - 1)


def plan(today, done):
    """Ordered (task_key, kind, league, arg) still to do.

    Order: the last week of days, event-league ranges, team schedules
    (NFL first: 30 days hold too few NFL meetings), then older days.
    """
    leagues = {lg.key: lg for lg in espn.LEAGUES if multisport.source_for(lg.sport) == "espn"}
    first, last = today - timedelta(days=DAYS_BACK), today - timedelta(days=2)

    def day_tasks(offsets):
        out = []
        for off in offsets:
            d = today - timedelta(days=off)
            for lg in leagues.values():
                if lg.mode == "event":
                    rng = done.get(f"range:{lg.key}")
                    if rng is None or d.isoformat() <= rng:
                        continue           # covered by the (pending or finished) range request
                key = f"day:{lg.key}:{d.isoformat()}"
                if key not in done:
                    out.append((key, "day", lg, d))
        return out

    tasks = day_tasks(range(2, RECENT_FIRST_DAYS + 1))
    for lg in leagues.values():
        if lg.mode == "event" and f"range:{lg.key}" not in done:
            tasks.append((f"range:{lg.key}", "range", lg, (first, last)))
    for key in TEAM_SCHEDULE_LEAGUES:
        lg = leagues.get(key)
        if lg is None:
            continue
        season = season_label(key, today)
        list_key = f"teamlist:{key}:{season}"
        if list_key not in done:
            tasks.append((list_key, "teamlist", lg, season))
            continue
        for tid in [t for t in (done.get(list_key) or "").split(",") if t]:
            if f"team:{key}:{season}:{tid}" not in done:
                tasks.append((f"team:{key}:{season}:{tid}", "team", lg, (tid, season)))
    tasks += day_tasks(range(RECENT_FIRST_DAYS + 1, DAYS_BACK + 1))
    return tasks


def _finished(items):
    return [x for x in items if x["fixture"]["status"]["short"] in FINISHED]


def collect_backfill(espn_client=None, now=None, max_requests=MAX_REQUESTS_PER_RUN):
    today = (now or datetime.now(timezone.utc)).date()
    _ensure_table()
    done = _done_tasks()
    todo = plan(today, done)
    if not todo:
        return {"stored": 0, "progress": {"tasks_done": 0, "requests": 0, "tasks_left": 0}}
    own = espn_client is None
    client = espn_client or espn.EspnClient()
    start_requests = client.requests
    stored = completed = 0
    try:
        for key, kind, lg, arg in todo:
            if client.requests - start_requests >= max_requests:
                break
            if kind == "teamlist":
                ids = client.team_ids(lg)
                if client.last_ok and ids:
                    _mark(key, 0, ",".join(ids))
                    done[key] = ",".join(ids)
                    completed += 1
                continue
            if kind == "day":
                items = client.day(lg, arg)
            elif kind == "range":
                items = client.date_range(lg, *arg)
            else:
                tid, season = arg
                items = client.team_schedule(lg, tid, season if lg.key == "MLB" else None)
            if not client.last_ok:
                continue                   # retried on a later run
            n = multisport._store(_finished(items), lg.sport, "espn:")
            detail = arg[1].isoformat() if kind == "range" else None
            _mark(key, n, detail)
            done[key] = detail
            stored += n
            completed += 1
    finally:
        if own:
            client.close()
    left = len(plan(today, done))
    # Only "stored" is an int at top level: safe_multisport records the sum of int values.
    return {"stored": stored, "progress": {"tasks_done": completed, "requests": client.requests - start_requests,
                                           "tasks_left": left}}
