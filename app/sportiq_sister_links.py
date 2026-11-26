"""Links from a billnojay match to the same match on zynktv and ergrow.

Three-site roles (sports-creator-ai docs/match-pages-design.md A-2): billnojay
shows the live score, zynktv the match data page (head-to-head, form, model
estimate), ergrow the preview article. The live-score modal asks
GET /api/sister/match-links and shows "next to read" links for the same match.

* IDs: billnojay live items carry ESPN event ids ("NBA-401909317"); zynktv
  and ergrow use the SportIQ DB match id (matches.id), the same database this
  app reads. ESPN-sourced rows store provider_match_id = "espn:<event id>",
  so those map exactly. Football rows come from football-data (no ESPN id);
  they are matched by kickoff (within KICKOFF_WINDOW) and team name, English
  or Korean, and only when the pick is unambiguous.
* zynktv: https://zynktv.com/match/<id> (zynktv redirects to the full slug).
  Without a DB match, the zynktv day page for that KST date (its range is
  today-30 .. today+7).
* ergrow: the published preview post, found with the public WordPress REST
  API by the slugs sports-creator-ai gives auto drafts (post_slugs.py):
  "<home>-vs-<away>-<sport>-preview-<id>" and the old "sca-auto-<sport>-<id>".
  Drafts are not returned without login, so only published posts link.
* Cached in memory: found 6 h, not found 30 min, errors 5 min. A failure
  never breaks the modal; it just shows fewer links.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

log = logging.getLogger("sportiq.sister")

ZYNKTV = "https://zynktv.com"
ERGROW = "https://ergrow.com"
ZYNKTV_PAST_DAYS = 30
ZYNKTV_FUTURE_DAYS = 7
KICKOFF_WINDOW = timedelta(minutes=20)
FOUND_TTL = 6 * 3600
MISSING_TTL = 30 * 60
ERROR_TTL = 5 * 60
MAX_CACHE = 2000
KST = timezone(timedelta(hours=9))
# billnojay live-score sport -> SportIQ sports.code
SPORT_CODES = {"soccer": "football", "football": "football", "basketball": "basketball", "baseball": "baseball",
               "hockey": "hockey", "american_football": "american_football", "volleyball": "volleyball"}

router = APIRouter(tags=["sister-links"])
_CACHE: Dict[str, Tuple[float, float, Any]] = {}
_LOCK = threading.Lock()


def _cget(key: str):
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < hit[1]:
        return True, hit[2]
    return False, None


def _cput(key: str, ttl: float, value: Any) -> None:
    with _LOCK:
        if len(_CACHE) >= MAX_CACHE:
            # Drop the oldest quarter (dicts keep insertion order), not everything:
            # many unknown keys must not wipe the links already found.
            for k in list(_CACHE)[: MAX_CACHE // 4]:
                _CACHE.pop(k, None)
        _CACHE.pop(key, None)
        _CACHE[key] = (time.monotonic(), ttl, value)


# --------------------------------------------------------------------------
# slugs (same rule as sports-creator-ai services/api/app/services/post_slugs.py)
# --------------------------------------------------------------------------
_DROP = {"fc", "cf", "sc", "ssc", "afc", "ac", "as", "ss", "cd", "ud", "sk", "fk", "bk", "if", "sv", "vfb", "vfl",
         "rc", "rcd", "club", "de", "calcio"}
_TRANSLIT = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "ß": "ss", "đ": "d", "Đ": "d", "ł": "l",
                           "Ł": "l", "ı": "i"})


def slug_part(name: str) -> str:
    text = unicodedata.normalize("NFKD", (name or "").translate(_TRANSLIT)).encode("ascii", "ignore").decode().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w and w not in _DROP]
    return "-".join(words)[:40].strip("-") or "team"


def ergrow_slugs(match_id: int, sport: str, home: str, away: str) -> List[str]:
    sp = re.sub(r"[^a-z0-9]+", "-", (sport or "").lower()).strip("-") or "sport"
    return [f"{slug_part(home)}-vs-{slug_part(away)}-{sp}-preview-{match_id}", f"sca-auto-{sp}-{match_id}"]


# --------------------------------------------------------------------------
# finding the SportIQ match
# --------------------------------------------------------------------------
def parse_kickoff(kst_text: str) -> Optional[datetime]:
    """'2026.11.26 (목) 08:20 KST' -> aware datetime (KST)."""
    m = re.search(r"(\d{4})[.-](\d{1,2})[.-](\d{1,2})\D+(\d{1,2}):(\d{2})", kst_text or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=KST)
    except ValueError:
        return None


def _norm(name: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", name or "").lower())


def _stripped(name: str) -> str:
    """ASCII form without club tokens: 'Arsenal FC' / 'Arsenal' -> 'arsenal'. Empty for Korean."""
    text = unicodedata.normalize("NFKD", (name or "").translate(_TRANSLIT)).encode("ascii", "ignore").decode().lower()
    return "".join(w for w in re.split(r"[^a-z0-9]+", text) if w and w not in _DROP)


def _names(name: str, live_sport: str) -> set:
    """Comparable forms of one team name: as given, without club tokens, and its Korean name.
    Used on both sides (live item and DB row), so English or Korean on either side can match."""
    out = {_norm(name), _stripped(name)}
    try:
        from app import sportiq_team_pages as tp
        ko = tp.korean_name(name, live_sport)
        if ko:
            out.add(_norm(ko))
    except Exception:
        pass
    out.discard("")
    return out


def _rows(sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    from sqlalchemy import text

    from app.db import engine
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), params).mappings().all()]


_SELECT = ("SELECT m.id, s.code AS sport, ht.name AS home, at.name AS away, m.start_time "
           "FROM matches m JOIN sports s ON s.id = m.sport_id "
           "LEFT JOIN teams ht ON ht.id = m.home_team_id LEFT JOIN teams at ON at.id = m.away_team_id ")


def pick_by_names(cands: List[Dict[str, Any]], home: str, away: str, live_sport: str) -> Optional[Dict[str, Any]]:
    """The one candidate whose teams match; both sides when several match one side."""
    h, a = _names(home, live_sport), _names(away, live_sport)
    scored = []
    for c in cands:
        hit_h = bool(h & _names(c.get("home") or "", live_sport))
        hit_a = bool(a & _names(c.get("away") or "", live_sport))
        if hit_h or hit_a:
            scored.append((hit_h and hit_a, c))
    both = [c for ok, c in scored if ok]
    if len(both) == 1:
        return both[0]
    if not both and len(scored) == 1:
        return scored[0][1]
    return None


def find_match(live_id: str, live_sport: str, kickoff: Optional[datetime], home: str, away: str) -> Optional[Dict[str, Any]]:
    code = SPORT_CODES.get((live_sport or "").lower())
    if not code:
        return None
    eid = (live_id or "").rsplit("-", 1)[-1]
    if eid.isdigit() and code != "football":
        rows = _rows(_SELECT + "WHERE s.code = :code AND m.provider_match_id = :pid LIMIT 1",
                     {"code": code, "pid": f"espn:{eid}"})
        if rows:
            return rows[0]
    if kickoff is None or not (home or away):
        return None
    ko = kickoff.astimezone(timezone.utc)
    cands = _rows(_SELECT + "WHERE s.code = :code AND m.start_time BETWEEN :a AND :b ORDER BY m.start_time LIMIT 200",
                  {"code": code, "a": ko - KICKOFF_WINDOW, "b": ko + KICKOFF_WINDOW})
    return pick_by_names(cands, home, away, live_sport)


# --------------------------------------------------------------------------
# ergrow
# --------------------------------------------------------------------------
def _http_json(url: str, timeout: float = 4.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "Mozilla/5.0 (compatible; SportIQ-sister/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(1_000_000).decode("utf-8"))


def ergrow_preview(match_id: int, sport: str, home: str, away: str) -> Optional[str]:
    key = f"ergrow:{match_id}"
    cached, value = _cget(key)
    if cached:
        return value
    slugs = ergrow_slugs(match_id, sport, home, away)
    q = urllib.parse.urlencode({"slug": ",".join(slugs), "_fields": "slug,link", "per_page": len(slugs)})
    try:
        rows = _http_json(f"{ERGROW}/wp-json/wp/v2/posts?{q}")
    except Exception as e:
        log.info("ergrow lookup %s failed: %s", match_id, e)
        _cput(key, ERROR_TTL, None)
        return None
    by_slug = {r.get("slug"): r.get("link") for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}
    found = next((by_slug[s] for s in slugs if isinstance(by_slug.get(s), str) and by_slug[s].startswith(ERGROW + "/")),
                 None)
    _cput(key, FOUND_TTL if found else MISSING_TTL, found)
    return found


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def zynktv_day(kickoff: Optional[datetime], today: Optional[date] = None) -> Optional[str]:
    if kickoff is None:
        return None
    d = kickoff.astimezone(KST).date()
    today = today or datetime.now(KST).date()
    if today - timedelta(days=ZYNKTV_PAST_DAYS) <= d <= today + timedelta(days=ZYNKTV_FUTURE_DAYS):
        return f"{ZYNKTV}/schedule/{d.isoformat()}"
    return None


def links_for(live_id: str, sport: str, kst: str, home: str, away: str) -> Dict[str, Optional[str]]:
    kickoff = parse_kickoff(kst)
    # Football is matched by name, so the names are part of the key: a lookup with other
    # (or made-up) names must not decide the result cached for this match.
    key = f"m:{live_id}:{sport}:{kst}:{_norm(home)}:{_norm(away)}"
    cached, hit = _cget(key)
    if not cached:
        try:
            hit = find_match(live_id, sport, kickoff, home, away)
            _cput(key, FOUND_TTL if hit else MISSING_TTL, hit)
        except Exception as e:
            log.info("sister match lookup %s failed: %s", live_id, e)
            hit = None
            _cput(key, ERROR_TTL, None)
    out: Dict[str, Optional[str]] = {"zynktv": None, "zynktv_day": zynktv_day(kickoff), "ergrow": None}
    if hit:
        out["zynktv"] = f"{ZYNKTV}/match/{int(hit['id'])}"
        out["ergrow"] = ergrow_preview(int(hit["id"]), str(hit.get("sport") or ""), str(hit.get("home") or ""),
                                       str(hit.get("away") or ""))
    return out


@router.get("/api/sister/match-links")
def sister_match_links(
    id: str = Query("", max_length=80),
    sport: str = Query("", max_length=30),
    kst: str = Query("", max_length=60),
    home: str = Query("", max_length=80),
    away: str = Query("", max_length=80),
):
    return JSONResponse(links_for(id, sport, kst, home, away), headers={"Cache-Control": "public, max-age=600"})
