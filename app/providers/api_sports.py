"""API-SPORTS (api-sports.io) adapter for non-football sports. Off without a key.

Enabled only when API_SPORTS_KEY is set; without it no client is built and
no request is made. Each sport API has its own daily request quota on the
free plan (100/day per API; the user should confirm this on their
api-sports dashboard). Two guards keep us under it:

* a per-day, per-sport budget stored in the database (api_request_budget),
  so worker restarts on deploy do not reset the count;
* the x-ratelimit-requests-remaining response header: we stop when the
  key's remaining quota for that API falls to API_SPORTS_RESERVE. That also
  covers the lounge web service, which can use the same key.

Endpoint and field names follow the lounge's existing API-SPORTS bridge
(/games?date=YYYY-MM-DD, response[].teams/scores/status). The public docs
could not be fetched from this environment, so parsing is defensive: an
unexpected shape yields [] rather than an error.
"""
import logging
import threading
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from app.config import settings

log = logging.getLogger("sportiq-api-sports")

HOSTS = {
    "basketball": "https://v1.basketball.api-sports.io",
    "baseball": "https://v1.baseball.api-sports.io",
    "hockey": "https://v1.hockey.api-sports.io",
    "volleyball": "https://v1.volleyball.api-sports.io",
    "american_football": "https://v1.american-football.api-sports.io",
}
# Tennis has no API-SPORTS API; MMA's fight payload could not be verified,
# so both stay on ESPN.
SUPPORTED_SPORTS = tuple(HOSTS)
MIN_REQUEST_INTERVAL = 6.5   # ~9 req/min per process, under a 10 req/min cap
_LOCK = threading.Lock()
_LAST = [0.0]

_LIVE = {"Q1", "Q2", "Q3", "Q4", "OT", "LIVE", "IN", "P1", "P2", "P3", "PT", "BT",
         "S1", "S2", "S3", "S4", "S5", "IN1", "IN2", "IN3", "IN4", "IN5", "IN6", "IN7", "IN8", "IN9"}
_MAP = {"NS": "NS", "TBD": "NS", "FT": "FT", "AOT": "AET", "AET": "AET", "AP": "PEN", "PEN": "PEN",
        "HT": "HT", "POST": "PST", "CANC": "CANC", "SUSP": "SUSP", "INTR": "SUSP", "ABD": "ABD",
        "AWD": "AWD", "WO": "AWD"}


def enabled():
    return bool((settings.api_sports_key or "").strip())


def active_sports():
    """Sports served by API-SPORTS (key set and sport listed in API_SPORTS_SPORTS)."""
    if not enabled():
        return set()
    wanted = {s.strip() for s in (settings.api_sports_sports or "").split(",") if s.strip()}
    if wanted == {"none"}:      # key used only for Korean games (see korea_sports)
        return set()
    return {s for s in SUPPORTED_SPORTS if not wanted or s in wanted}


KOREA_COUNTRIES = {"south korea", "korea", "korea republic", "republic of korea"}


def korea_sports():
    """Sports fetched from API-SPORTS for their Korean games only (ESPN keeps the rest).

    Needs the key; a sport already fully on API-SPORTS is left out because its
    Korean games come with the full feed.
    """
    if not enabled():
        return set()
    wanted = {s.strip() for s in (settings.api_sports_korea or "").split(",") if s.strip()}
    return {s for s in SUPPORTED_SPORTS if s in wanted} - active_sports()


def is_korean(item):
    """True for a normalized game whose API-SPORTS country is South Korea."""
    name = str(((item or {}).get("country") or {}).get("name") or "").strip().lower()
    return name in KOREA_COUNTRIES


def map_status(short):
    s = str(short or "").upper()
    if s in _MAP:
        return _MAP[s]
    if s in _LIVE:
        return "HT" if s == "BT" else "LIVE"
    return "NS" if not s else s[:40]


def _score(v):
    if isinstance(v, dict):
        v = v.get("total")
    try:
        return None if v is None else int(float(v))
    except (TypeError, ValueError):
        return None


def normalize_game(g, sport):
    if not isinstance(g, dict) or g.get("id") is None:
        return None
    teams = g.get("teams") if isinstance(g.get("teams"), dict) else {}
    home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
    away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
    league = g.get("league") if isinstance(g.get("league"), dict) else {}
    when = g.get("date")
    if home.get("id") is None or away.get("id") is None or league.get("id") is None or not when:
        return None
    if isinstance(when, dict):          # some APIs nest {"date","time","timestamp"}
        ts = when.get("timestamp")
        when = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else None
        if not when:
            return None
    status = g.get("status") if isinstance(g.get("status"), dict) else {}
    short = map_status(status.get("short"))
    scores = g.get("scores") if isinstance(g.get("scores"), dict) else {}
    hs, as_ = (_score(scores.get("home")), _score(scores.get("away"))) if short != "NS" else (None, None)
    country = g.get("country") if isinstance(g.get("country"), dict) else {}
    return {
        "fixture": {"id": f"as:{g['id']}", "date": when,
                    "status": {"short": short, "long": str(status.get("long") or short)[:100]},
                    "venue": {"name": None}, "referee": None},
        "league": {"id": f"as:{league['id']}", "name": league.get("name") or "Unknown", "logo": league.get("logo")},
        "country": {"name": country.get("name")},
        "teams": {"home": {"id": f"as:{home['id']}", "name": home.get("name") or "Unknown", "logo": home.get("logo")},
                  "away": {"id": f"as:{away['id']}", "name": away.get("name") or "Unknown", "logo": away.get("logo")}},
        "goals": {"home": hs, "away": as_},
        "source": "API-SPORTS",
    }


class BudgetExhausted(RuntimeError):
    pass


class ApiSportsClient:
    def __init__(self, engine, transport=None):
        if not enabled():
            raise RuntimeError("API_SPORTS_KEY is not configured")
        self.engine = engine
        self.client = httpx.Client(timeout=15, transport=transport,
                                   headers={"x-apisports-key": settings.api_sports_key.strip()})
        self.requests = 0
        self.remote_remaining = {}
        with engine.begin() as c:
            c.execute(text("""CREATE TABLE IF NOT EXISTS api_request_budget(
                day VARCHAR(10) NOT NULL, api VARCHAR(40) NOT NULL, used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(day, api))"""))

    def used_today(self, sport):
        day = datetime.now(timezone.utc).date().isoformat()
        with self.engine.connect() as c:
            v = c.execute(text("SELECT used FROM api_request_budget WHERE day=:d AND api=:a"),
                          {"d": day, "a": sport}).scalar()
        return int(v or 0)

    def _spend(self, sport):
        day = datetime.now(timezone.utc).date().isoformat()
        budget = max(0, int(settings.api_sports_daily_budget))
        with self.engine.begin() as c:
            used = c.execute(text("SELECT used FROM api_request_budget WHERE day=:d AND api=:a"),
                             {"d": day, "a": sport}).scalar()
            if int(used or 0) >= budget:
                raise BudgetExhausted(f"{sport}: daily budget {budget} used")
            c.execute(text("""INSERT INTO api_request_budget(day, api, used) VALUES(:d, :a, 1)
                              ON CONFLICT(day, api) DO UPDATE SET used = api_request_budget.used + 1"""),
                      {"d": day, "a": sport})

    def games(self, sport, day, korea_only=False):
        """Games on one UTC date. Raises BudgetExhausted when out of budget.

        korea_only keeps South Korean games only; it costs the same single
        request, since /games?date= returns every league of the sport.
        """
        if sport not in HOSTS:
            return []
        rem = self.remote_remaining.get(sport)
        if rem is not None and rem <= int(settings.api_sports_reserve):
            raise BudgetExhausted(f"{sport}: provider reports {rem} requests left")
        self._spend(sport)
        with _LOCK:
            wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _LAST[0])
            if wait > 0:
                time.sleep(wait)
            _LAST[0] = time.monotonic()
        self.requests += 1
        try:
            r = self.client.get(f"{HOSTS[sport]}/games", params={"date": day, "timezone": "UTC"})
        except httpx.HTTPError as exc:
            log.warning("api-sports %s failed: %s", sport, exc)
            return []
        try:
            self.remote_remaining[sport] = int(r.headers.get("x-ratelimit-requests-remaining"))
        except (TypeError, ValueError):
            pass
        if r.status_code != 200:
            log.warning("api-sports %s HTTP %s", sport, r.status_code)
            return []
        try:
            data = r.json()
        except ValueError:
            return []
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            # API-SPORTS reports quota/plan problems in "errors" with HTTP 200.
            log.warning("api-sports %s errors: %s", sport, str(errors)[:200])
            return []
        rows = data.get("response") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        out = [x for x in (normalize_game(g, sport) for g in rows) if x]
        return [x for x in out if is_korean(x)] if korea_only else out

    def close(self):
        self.client.close()
