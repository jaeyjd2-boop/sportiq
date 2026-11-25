"""IndexNow for billnojay.com (Naver, Bing and the other IndexNow engines).

* Key file: GET /{KEY}.txt returns exactly the key (public by design; ergrow.com
  and zynktv.com use the same key).
* Once a day, after 08:00 KST (European games of the previous KST day are over
  by then), the job sends:
    - yesterday's /livescore/{date} result page, if it is indexable,
    - the team pages of teams that played that day (their results changed).
  Each sent day is logged in the lounge SQLite table seo_indexnow_log (also in
  the PG snapshot list), so restarts and redeploys do not resend.
* Runs only on Render (RENDER env var) or with INDEXNOW_ENABLED=1;
  INDEXNOW_ENABLED=0 turns it off. A failed ping is logged and never raises.

The POST goes to api.indexnow.org, not to SportIQ.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

log = logging.getLogger("sportiq.indexnow")

KEY = os.environ.get("INDEXNOW_KEY", "3d607ed829cf5ec71c53df02918ffde5").strip()
HOST = "billnojay.com"
SITE = f"https://{HOST}"
ENDPOINT = "https://api.indexnow.org/indexnow"
MAX_URLS = 10000
RUN_AFTER_HOUR_KST = 8
TABLE = "seo_indexnow_log"
KST = timezone(timedelta(hours=9))

router = APIRouter(tags=["indexnow"])
_READY = [False]
_STARTED = [False]


@router.api_route(f"/{KEY}.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse)
def indexnow_key_file():
    return PlainTextResponse(KEY, headers={"Cache-Control": "public, max-age=86400"})


# --------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------
def submit(urls: Iterable[str], timeout: float = 15.0) -> List[int]:
    """POST the URLs (deduplicated, own host only) in chunks of MAX_URLS.

    Returns the HTTP status of each request (0 when it failed before an
    answer). 200/202 mean accepted. Never raises."""
    clean = list(dict.fromkeys(u for u in urls if isinstance(u, str) and u.startswith(SITE + "/")))
    statuses: List[int] = []
    for i in range(0, len(clean), MAX_URLS):
        body = json.dumps({
            "host": HOST, "key": KEY, "keyLocation": f"{SITE}/{KEY}.txt", "urlList": clean[i:i + MAX_URLS],
        }).encode("utf-8")
        req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                     headers={"Content-Type": "application/json; charset=utf-8",
                                              "User-Agent": "SportIQ-IndexNow/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                statuses.append(int(resp.status))
        except urllib.error.HTTPError as e:
            statuses.append(int(e.code))
        except Exception as e:      # network, DNS, timeout
            log.warning("indexnow submit failed: %s", e)
            statuses.append(0)
    return statuses


def accepted(statuses: List[int]) -> bool:
    return bool(statuses) and all(s in (200, 202) for s in statuses)


# --------------------------------------------------------------------------
# what to send
# --------------------------------------------------------------------------
def urls_for_day(day: date) -> List[str]:
    """The day's result page (if indexable) and the pages of teams that played."""
    from app import sportiq_results_pages as rp
    from app import sportiq_team_pages as tp

    res = rp.day_results(day) or {}
    urls: List[str] = []
    if int(res.get("finished") or 0) >= rp.MIN_FINISHED:
        urls.append(f"{SITE}/livescore/{day.isoformat()}")
    team_league = {key: tl for key, _p, _s, _n, tl in rp.RESULT_LEAGUES if tl}
    for lg in res.get("leagues") or []:
        league = team_league.get(lg.get("key"))
        if not league:
            continue
        by_id = {t["id"]: t for t in tp.league_teams(league)}
        for g in lg.get("games") or []:
            for side in ("home", "away"):
                t = by_id.get(str((g.get(side) or {}).get("id") or ""))
                if t and tp.standings_indexable(t):
                    urls.append(f"{SITE}/team/{league}/{t['slug']}")
    return list(dict.fromkeys(urls))


# --------------------------------------------------------------------------
# log
# --------------------------------------------------------------------------
def _conn():
    from app import sportiq_auto_analyst as aa

    return aa._get_conn()


def _ensure_table() -> None:
    if _READY[0]:
        return
    with _conn() as c:
        c.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            day TEXT PRIMARY KEY, sent_at REAL NOT NULL, url_count INTEGER NOT NULL, statuses TEXT NOT NULL)""")
    try:
        from app import sportiq_auto_analyst as aa
        tables = aa._PG_SNAPSHOT_TABLES
        if TABLE not in tables:
            aa._PG_SNAPSHOT_TABLES = (list(tables) + [TABLE]) if isinstance(tables, list) else tuple(tables) + (TABLE,)
        aa._restore_snapshots_from_pg()
    except Exception:
        pass
    _READY[0] = True


def already_sent(day: date) -> bool:
    _ensure_table()
    with _conn() as c:
        row = c.execute(f"SELECT statuses FROM {TABLE} WHERE day = ?", (day.isoformat(),)).fetchone()
    if not row:
        return False
    try:
        st = json.loads(row[0])
    except (TypeError, ValueError):
        return False
    # Accepted, or refused for good (4xx other than 429: bad key or URLs): do not
    # retry. Network errors (0), 429 and 5xx are retried on the next check.
    return accepted(st) or any(400 <= s < 500 and s != 429 for s in st)


def _record(day: date, count: int, statuses: List[int]) -> None:
    _ensure_table()
    with _conn() as c:
        c.execute(f"INSERT OR REPLACE INTO {TABLE} (day, sent_at, url_count, statuses) VALUES (?, ?, ?, ?)",
                  (day.isoformat(), time.time(), count, json.dumps(statuses)))
    try:
        from app import sportiq_auto_analyst as aa
        aa._backup_tables_to_pg(TABLE)
    except Exception:
        pass


# --------------------------------------------------------------------------
# daily job
# --------------------------------------------------------------------------
def run_daily(now: Optional[datetime] = None) -> Optional[Tuple[int, List[int]]]:
    """Send yesterday's URLs once. Returns (url count, statuses) when it sent, else None."""
    now = now or datetime.now(KST)
    if now.hour < RUN_AFTER_HOUR_KST:
        return None
    day = now.date() - timedelta(days=1)
    try:
        if already_sent(day):
            return None
        urls = urls_for_day(day)
        if not urls:
            _record(day, 0, [200])        # nothing to send that day; do not retry
            return 0, []
        statuses = submit(urls)
        _record(day, len(urls), statuses)  # a failed day is retried on the next check
        log.info("indexnow %s: %d urls -> %s", day, len(urls), statuses)
        return len(urls), statuses
    except Exception as e:
        log.warning("indexnow daily job failed: %s", e)
        return None


def enabled() -> bool:
    flag = (os.environ.get("INDEXNOW_ENABLED") or "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    return flag in ("1", "true", "yes") or bool(os.environ.get("RENDER"))


def _loop() -> None:
    time.sleep(120)                  # let the web process settle first
    while True:
        run_daily()
        time.sleep(1800)


@router.on_event("startup")
def _start_indexnow_job() -> None:
    if not enabled() or _STARTED[0]:
        return
    _STARTED[0] = True
    threading.Thread(target=_loop, name="indexnow-daily", daemon=True).start()
