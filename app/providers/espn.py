"""ESPN public scoreboard adapter (no key) returning the collector's fixture shape.

ESPN's site API (site.api.espn.com) is undocumented and unofficial: it can
change shape or block clients at any time. Every request is spaced, has a
short timeout, and any failure or unexpected shape yields an empty list
for that league instead of raising.

The per-sport parsing follows the live-scores parser in
app/sportiq_datacenter.py (team sports, tennis groupings, MMA bouts), but
this module is standalone so the worker does not import the web modules.
"""
import logging
import threading
import time
from datetime import date, timedelta

import httpx

log = logging.getLogger("sportiq-espn")

BASE = "https://site.api.espn.com/apis/site/v2/sports"
MIN_REQUEST_INTERVAL = 1.0
TIMEOUT = 8.0
_LOCK = threading.Lock()
_LAST = [0.0]


class League:
    """One ESPN scoreboard.

    mode "dates": one request per date (dates=YYYYMMDD); ESPN answers 400 to
    ranges for NBA/NFL/MLB/NHL. mode "event": the default scoreboard is the
    current tournament/card (tennis, UFC); items are filtered by date.
    """

    def __init__(self, key, sport, slug, name, country, mode):
        self.key, self.sport, self.slug, self.name, self.country, self.mode = key, sport, slug, name, country, mode


LEAGUES = [
    League("NBA", "basketball", "basketball/nba", "NBA", "USA", "dates"),
    League("MLB", "baseball", "baseball/mlb", "MLB", "USA", "dates"),
    League("NHL", "hockey", "hockey/nhl", "NHL", "USA", "dates"),
    League("NFL", "american_football", "football/nfl", "NFL", "USA", "dates"),
    League("NCAAW_VB", "volleyball", "volleyball/womens-college-volleyball", "NCAA Women's Volleyball", "USA", "dates"),
    League("ATP", "tennis", "tennis/atp", "ATP Tour", "World", "event"),
    League("WTA", "tennis", "tennis/wta", "WTA Tour", "World", "event"),
    League("UFC", "mma", "mma/ufc", "UFC", "World", "event"),
]
SPORTS = sorted({lg.sport for lg in LEAGUES})


def _throttle():
    with _LOCK:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _LAST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.monotonic()


def _int(v):
    if isinstance(v, dict):          # team-schedule payloads: {"value": 27.0, "displayValue": "27"}
        v = v.get("value", v.get("displayValue"))
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ESPN status.type.name -> collector short codes (same vocabulary as football).
_STATUS_BY_NAME = {
    "STATUS_SCHEDULED": "NS", "STATUS_DELAYED": "NS", "STATUS_TBD": "NS",
    "STATUS_HALFTIME": "HT",
    "STATUS_FINAL": "FT", "STATUS_FULL_TIME": "FT", "STATUS_FINAL_PEN": "PEN",
    "STATUS_FINAL_OT": "AET", "STATUS_FINAL_SO": "PEN",
    "STATUS_POSTPONED": "PST", "STATUS_CANCELED": "CANC", "STATUS_CANCELLED": "CANC",
    "STATUS_SUSPENDED": "SUSP", "STATUS_ABANDONED": "ABD", "STATUS_FORFEIT": "AWD",
    "STATUS_RETIRED": "FT", "STATUS_WALKOVER": "AWD",
}


def map_status(status_obj):
    st = (status_obj or {}).get("type") or {}
    name = str(st.get("name") or "").upper()
    state = st.get("state")
    if name in _STATUS_BY_NAME:
        short = _STATUS_BY_NAME[name]
    elif state == "in":
        short = "LIVE"
    elif state == "post":
        short = "FT" if st.get("completed", True) else "SUSP"
    else:
        short = "NS"
    return short, str(st.get("shortDetail") or st.get("description") or name or short)[:100]


def _competitor_ref(c):
    """(provider_team_id, name, logo) for a team, an athlete, or a doubles pair."""
    team, ath, roster = c.get("team") or {}, c.get("athlete") or {}, c.get("roster") or {}
    if team.get("id"):
        return "espn:" + str(team["id"]), team.get("displayName") or team.get("shortDisplayName"), team.get("logo")
    if ath.get("id") or ath.get("displayName"):
        ident = ath.get("id") or c.get("id")
        return ("espn:athlete:" + str(ident)) if ident else None, ath.get("displayName") or ath.get("shortName"), \
            ((ath.get("flag") or {}).get("href"))
    if roster:
        ident = c.get("id")
        name = roster.get("displayName") or " / ".join(
            str((a.get("athlete") or a).get("displayName") or "") for a in (roster.get("athletes") or []) if isinstance(a, dict))
        return ("espn:pair:" + str(ident)) if ident else None, name, None
    return None, None, None


def _competitions(ev, sport):
    comps = [c for c in (ev.get("competitions") or []) if isinstance(c, dict)]
    if sport == "tennis":
        out = [c for g in (ev.get("groupings") or []) if isinstance(g, dict)
               for c in (g.get("competitions") or []) if isinstance(c, dict)]
        return out + comps
    if sport == "mma":
        return comps
    return comps[:1]


def _tennis_sets(me, opp):
    won = 0
    for a, b in zip(me.get("linescores") or [], opp.get("linescores") or []):
        if a.get("winner") is True:
            won += 1
        elif a.get("winner") is None:
            av, bv = _int(a.get("value")), _int(b.get("value"))
            if av is not None and bv is not None and av > bv and av >= 6:
                won += 1
    return won


def normalize_competition(ev, comp, league):
    """Collector fixture dict, or None when ids/names are missing.

    Home/away: team sports use ESPN's homeAway. Individual sports (tennis,
    MMA) have no home side, so the competitor with ESPN order 1 is stored as
    "home" and order 2 as "away"; each athlete (or doubles pair) is a row in
    the teams table under its own provider id (espn:athlete:<id> /
    espn:pair:<id>). Tennis scores are sets won; MMA bouts store no score
    (the winner is kept in raw_payload).
    """
    comps = [c for c in (comp.get("competitors") or []) if isinstance(c, dict)]
    if len(comps) < 2:
        return None
    home = next((c for c in comps if c.get("homeAway") == "home"), None)
    away = next((c for c in comps if c.get("homeAway") == "away"), None)
    if home is None or away is None or league.sport in ("tennis", "mma"):
        ordered = sorted(comps, key=lambda c: _int(c.get("order")) or 99)
        home, away = ordered[0], ordered[1]
    h_id, h_name, h_logo = _competitor_ref(home)
    a_id, a_name, a_logo = _competitor_ref(away)
    raw_id = comp.get("id") if league.sport in ("tennis", "mma") else (ev.get("id") or comp.get("id"))
    when = comp.get("date") or comp.get("startDate") or ev.get("date")
    if not (h_id and a_id and h_name and a_name and raw_id and when) or h_id == a_id:
        return None
    short, detail = map_status(comp.get("status") or ev.get("status"))
    winner = "home" if home.get("winner") is True else ("away" if away.get("winner") is True else None)
    if league.sport == "tennis":
        hs, as_ = (_tennis_sets(home, away), _tennis_sets(away, home)) if short != "NS" else (None, None)
    elif league.sport == "mma":
        hs = as_ = None
    else:
        hs, as_ = (_int(home.get("score")), _int(away.get("score"))) if short != "NS" else (None, None)
    return {
        "fixture": {"id": "espn:" + str(raw_id), "date": when,
                    "status": {"short": short, "long": detail},
                    "venue": {"name": ((comp.get("venue") or {}).get("fullName"))}, "referee": None},
        "league": {"id": "espn:" + league.key, "name": league.name},
        "country": {"name": league.country},
        "teams": {"home": {"id": h_id, "name": h_name, "logo": h_logo},
                  "away": {"id": a_id, "name": a_name, "logo": a_logo}},
        "goals": {"home": hs, "away": as_},
        "winner": winner,
        "event": {"id": str(ev.get("id") or ""), "name": ev.get("name")},
        "source": "ESPN",
    }


def parse_scoreboard(data, league):
    """All competitions in one ESPN scoreboard payload; bad shapes give []."""
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return []
    out = []
    for ev in data["events"]:
        if not isinstance(ev, dict):
            continue
        try:
            comps = _competitions(ev, league.sport)
        except Exception:
            continue
        for comp in comps:
            try:
                item = normalize_competition(ev, comp, league)
            except Exception:
                item = None
            if item:
                out.append(item)
    return out


class EspnClient:
    def __init__(self, transport=None):
        self.client = httpx.Client(base_url=BASE, timeout=TIMEOUT, transport=transport,
                                   headers={"User-Agent": "SportIQ-Collector/1.0"})
        self.requests = 0
        self.last_ok = False

    def _get(self, path, params=None):
        _throttle()
        self.requests += 1
        self.last_ok = False
        try:
            r = self.client.get(path, params=params)
            if r.status_code != 200:
                log.warning("espn %s HTTP %s", path, r.status_code)
                return None
            data = r.json()
            self.last_ok = isinstance(data, dict)
            return data
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("espn %s failed: %s", path, exc)
            return None

    def day(self, league, d: date):
        """One request for one ESPN (US) date; check last_ok to tell failure from an empty day."""
        if league.mode == "dates":
            return parse_scoreboard(self._get(f"/{league.slug}/scoreboard", {"dates": d.strftime("%Y%m%d"), "limit": 500}), league)
        return self.date_range(league, d, d)

    def league_window(self, league, first: date, last: date):
        """Fixtures for first..last (UTC dates). 'dates' leagues: one request per day."""
        items = []
        if league.mode == "dates":
            # ESPN's dates are US-local: a UTC day overlaps the previous US day.
            d = first - timedelta(days=1)
            while d <= last:
                data = self._get(f"/{league.slug}/scoreboard", {"dates": d.strftime("%Y%m%d"), "limit": 200})
                items.extend(parse_scoreboard(data, league))
                d += timedelta(days=1)
        else:
            items = parse_scoreboard(self._get(f"/{league.slug}/scoreboard"), league)
            lo, hi = first.isoformat(), (last + timedelta(days=1)).isoformat()
            items = [x for x in items if lo <= str(x["fixture"]["date"])[:10] <= hi
                     or x["fixture"]["status"]["short"] in ("LIVE", "HT")]
        seen, out = set(), []
        for x in items:
            if x["fixture"]["id"] not in seen:
                seen.add(x["fixture"]["id"])
                out.append(x)
        return out

    def date_range(self, league, first: date, last: date):
        """One request for first..last; ESPN accepts ranges for tennis/MMA scoreboards."""
        rng = f"{first.strftime('%Y%m%d')}-{last.strftime('%Y%m%d')}"
        return parse_scoreboard(self._get(f"/{league.slug}/scoreboard", {"dates": rng, "limit": 500}), league)

    def team_ids(self, league):
        """ESPN team ids for a team league (one request); [] on failure."""
        data = self._get(f"/{league.slug}/teams", {"limit": 100})
        try:
            teams = data["sports"][0]["leagues"][0]["teams"]
            return [str(t["team"]["id"]) for t in teams if (t.get("team") or {}).get("id")]
        except (TypeError, KeyError, IndexError):
            return []

    def team_schedule(self, league, team_id, season=None):
        """A team's season games (team-schedule endpoint), same fixture shape."""
        params = {"season": season} if season else None
        return parse_scoreboard(self._get(f"/{league.slug}/teams/{team_id}/schedule", params), league)

    def close(self):
        self.client.close()
