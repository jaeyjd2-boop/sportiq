"""Links from a billnojay match to the same match on zynktv and ergrow.

Three-site roles (sports-creator-ai docs/match-pages-design.md A-2): billnojay
shows the live score, zynktv the match data page (head-to-head, form, model
estimate), ergrow the preview article. The live-score modal asks
GET /api/sister/match-links and shows "next to read" links for the same match.

* IDs: billnojay live items carry ESPN event ids ("NBA-401909317"); zynktv
  and ergrow use the SportIQ DB match id (matches.id), the same database this
  app reads. ESPN-sourced rows store provider_match_id = "espn:<event id>",
  so those map exactly. Football rows come from football-data (no ESPN id);
  they are matched by kickoff (within KICKOFF_WINDOW) and both team names,
  English or Korean, and only when the pick is unambiguous.
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
GYEONGGIPAN = "https://gyeonggipan.com"
GYEONGGIPAN_PAST_DAYS = 1     # 경기판 build.js DAYS_BEFORE / DAYS_AFTER
GYEONGGIPAN_FUTURE_DAYS = 6
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
            _CACHE[key] = _CACHE.pop(key)   # recently used -> newest, so eviction drops unused keys first
            return True, hit[2]
    return False, None


def _cput(key: str, ttl: float, value: Any) -> None:
    with _LOCK:
        if len(_CACHE) >= MAX_CACHE:
            # Drop the least recently used quarter (_cget moves hits to the end), not
            # everything: many unknown keys must not wipe the links already found.
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
    """The one candidate whose teams match.

    With both names given, both sides must match: Korean names are looked up loosely
    ("Arsenal U21" -> 아스널), so one matching side could be another match in the same
    kickoff window (Arsenal U21 vs Chelsea U21 -> Arsenal vs Brentford). Fewer links
    is better than a wrong one. With only one name given, that side must match one row."""
    h, a = _names(home, live_sport), _names(away, live_sport)
    if not (h or a):
        return None
    hits = []
    for c in cands:
        hit_h = not h or bool(h & _names(c.get("home") or "", live_sport))
        hit_a = not a or bool(a & _names(c.get("away") or "", live_sport))
        if hit_h and hit_a:
            hits.append(c)
    return hits[0] if len(hits) == 1 else None


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


def gyeonggipan_link(hit: Optional[Dict[str, Any]], kickoff: Optional[datetime],
                     now: Optional[datetime] = None) -> Optional[str]:
    """경기판 (watch guide): its page for this match while the match is in its build window
    (KST yesterday .. +6 days; same SportIQ match id), else its "this week" page for games
    still to come. Finished games outside the window get no link (경기판 is about what to watch)."""
    if kickoff is None:
        return None
    now = now or datetime.now(timezone.utc)
    d, today = kickoff.astimezone(KST).date(), now.astimezone(KST).date()
    if hit and today - timedelta(days=GYEONGGIPAN_PAST_DAYS) <= d <= today + timedelta(days=GYEONGGIPAN_FUTURE_DAYS):
        return f"{GYEONGGIPAN}/match/{int(hit['id'])}/"
    if today <= d <= today + timedelta(days=GYEONGGIPAN_FUTURE_DAYS):
        return f"{GYEONGGIPAN}/this-week/"
    return None


def links_for(live_id: str, sport: str, kst: str, home: str, away: str) -> Dict[str, Optional[str]]:
    kickoff = parse_kickoff(kst)
    # Football is matched by name, so the names are part of its key: a lookup with other
    # (or made-up) names must not decide the result cached for this match. ESPN sports map
    # by event id, so their key leaves the names out (made-up names cannot add DB lookups).
    names = f":{_norm(home)}:{_norm(away)}" if SPORT_CODES.get((sport or "").lower()) == "football" else ""
    key = f"m:{live_id}:{sport}:{kst}{names}"
    cached, hit = _cget(key)
    if not cached:
        try:
            hit = find_match(live_id, sport, kickoff, home, away)
            _cput(key, FOUND_TTL if hit else MISSING_TTL, hit)
        except Exception as e:
            log.info("sister match lookup %s failed: %s", live_id, e)
            hit = None
            _cput(key, ERROR_TTL, None)
    out: Dict[str, Optional[str]] = {"zynktv": None, "zynktv_day": zynktv_day(kickoff), "ergrow": None,
                                     "gyeonggipan": gyeonggipan_link(hit, kickoff)}
    if hit:
        out["zynktv"] = f"{ZYNKTV}/match/{int(hit['id'])}"
        out["ergrow"] = ergrow_preview(int(hit["id"]), str(hit.get("sport") or ""), str(hit.get("home") or ""),
                                       str(hit.get("away") or ""))
    return out


# --------------------------------------------------------------------------
# /picks: upcoming matches that have an ergrow preview, and links for a pick
# --------------------------------------------------------------------------
# ergrow auto preview slugs end in "-<sport>-preview-<SportIQ match id>" (ergrow_slugs).
PREVIEW_SLUG = re.compile(r"-(football|basketball|baseball|hockey|american-football|volleyball)-preview-(\d+)/?$")
CARD_TTL = 15 * 60
CARD_PAST = timedelta(hours=2)        # a match that just started still gets its card
CARD_FUTURE = timedelta(hours=48)
# /picks league codes (sportiq_quick_picks.ALLOWED_LEAGUES) -> SportIQ sports.code
PICK_LEAGUE_SPORTS = {"EPL": "football", "KLEAGUE": "football", "LALIGA": "football", "BUNDESLIGA": "football",
                      "SERIEA": "football", "LIGUE1": "football", "UCL": "football", "NBA": "basketball",
                      "KBL": "basketball", "MLB": "baseball", "NHL": "hockey", "NFL": "american_football",
                      "VLEAGUE": "volleyball"}
_FOOTBALL_LEAGUES = (("champions league", "UCL"), ("premier league", "EPL"), ("k league 1", "KLEAGUE"),
                     ("k리그1", "KLEAGUE"), ("primera", "LALIGA"), ("la liga", "LALIGA"), ("laliga", "LALIGA"),
                     ("bundesliga", "BUNDESLIGA"), ("serie a", "SERIEA"), ("ligue 1", "LIGUE1"))
_SPORT_LEAGUE = {"basketball": "NBA", "baseball": "MLB", "hockey": "NHL", "american_football": "NFL",
                 "volleyball": "VLEAGUE"}
_VS = re.compile(r"\s+(?:vs\.?|v\.?|대)\s+", re.I)


def pick_league_code(sport: str, league_name: str) -> Optional[str]:
    """The /picks league code for a DB match, or None when /picks has no such league."""
    name = (league_name or "").lower()
    if sport == "football":
        if any(x in name for x in ("2", "women", "u21", "u18", "u19")):   # Premier League 2, 2. Bundesliga, Serie A Women
            return None
        return next((code for key, code in _FOOTBALL_LEAGUES if key in name), None)
    if sport == "basketball" and ("kbl" in name or "korea" in name):
        return "KBL"
    return _SPORT_LEAGUE.get(sport)


_LEAGUE_KO = {"EPL": "프리미어리그", "KLEAGUE": "K리그1", "LALIGA": "라리가", "BUNDESLIGA": "분데스리가",
              "SERIEA": "세리에 A", "LIGUE1": "리그 1", "UCL": "챔피언스리그", "NBA": "NBA", "KBL": "KBL",
              "MLB": "MLB", "NHL": "NHL", "NFL": "NFL", "VLEAGUE": "V-리그"}
# Football leagues /picks has no code for (checked in order: "europa conference" before "europa").
_LEAGUE_KO_EXTRA = (("conference league", "컨퍼런스리그"), ("europa league", "유로파리그"), ("championship", "챔피언십"),
                    ("eredivisie", "에레디비시"), ("primeira liga", "프리메이라리가"), ("k league 2", "K리그2"),
                    ("fa cup", "FA컵"), ("copa del rey", "코파 델 레이"), ("dfb", "DFB 포칼"), ("coppa italia", "코파 이탈리아"),
                    ("major league soccer", "MLS"), ("mls", "MLS"), ("j1", "J1리그"), ("saudi", "사우디 프로리그"))


def league_ko(sport: str, league_name: str) -> str:
    """Korean league label for a match card: 'Primera Division' -> 라리가. Unknown names stay as they are."""
    name = (league_name or "").strip()
    if sport == "basketball" and "wkbl" in name.lower():
        return "WKBL"
    code = pick_league_code(sport, name)
    # Football codes come from the league name; other sports' codes from the sport alone,
    # so there only a name that is the code itself (NBA, NHL...) is replaced.
    if code and (sport == "football" or code == "KBL" or name.upper() == code):
        return _LEAGUE_KO[code]
    low = name.lower()
    return next((ko for key, ko in _LEAGUE_KO_EXTRA if key in low), name)


def _display_name(name: str, sport: str) -> str:
    """Korean name when known. sport is the DB code; the Korean tables use the live-score
    name "soccer" for football (only then do they match "Real Betis Balompié" -> 레알 베티스).
    Accented football-data names are tried again without accents ("Deportivo Alavés")."""
    try:
        from app import sportiq_team_pages as tp
        sp = "soccer" if sport == "football" else sport
        ascii_name = unicodedata.normalize("NFKD", (name or "").translate(_TRANSLIT)).encode("ascii", "ignore").decode()
        return tp.korean_name(name, sp) or (ascii_name != name and tp.korean_name(ascii_name, sp)) or name
    except Exception:
        return name


def ergrow_recent_previews() -> Dict[int, str]:
    """{SportIQ match id: ergrow preview URL} for the latest published previews."""
    cached, value = _cget("ergrow:recent")
    if cached:
        return value
    q = urllib.parse.urlencode({"per_page": 60, "_fields": "slug,link", "orderby": "date", "order": "desc"})
    try:
        rows = _http_json(f"{ERGROW}/wp-json/wp/v2/posts?{q}")
    except Exception as e:
        log.info("ergrow recent previews failed: %s", e)
        _cput("ergrow:recent", ERROR_TTL, {})
        return {}
    out: Dict[int, str] = {}
    for r in rows if isinstance(rows, list) else []:
        link, m = (r.get("link") or "") if isinstance(r, dict) else "", PREVIEW_SLUG.search(str((r or {}).get("slug") or ""))
        if m and isinstance(link, str) and link.startswith(ERGROW + "/"):
            out.setdefault(int(m.group(2)), link)
    _cput("ergrow:recent", CARD_TTL, out)
    return out


_CARD_SELECT = ("SELECT m.id, s.code AS sport, ht.name AS home, at.name AS away, m.start_time, l.name AS league "
                "FROM matches m JOIN sports s ON s.id = m.sport_id LEFT JOIN leagues l ON l.id = m.league_id "
                "LEFT JOIN teams ht ON ht.id = m.home_team_id LEFT JOIN teams at ON at.id = m.away_team_id ")


def _aware(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, datetime):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def upcoming_match_cards(limit: int = 8, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Matches from now-2h to now+48h that have an ergrow preview, soonest first, each with
    its ergrow / zynktv / 경기판 links and the /picks league + match label for "write a pick".
    A failure gives an empty list (the /picks page then just has no cards)."""
    now = now or datetime.now(timezone.utc)
    cached, value = _cget("cards")
    if cached:
        return value[:limit]
    previews = ergrow_recent_previews()
    if not previews:
        return []
    try:
        rows = _rows(_CARD_SELECT + "WHERE m.id = ANY(:ids) AND m.start_time BETWEEN :a AND :b ORDER BY m.start_time",
                     {"ids": list(previews), "a": now - CARD_PAST, "b": now + CARD_FUTURE})
    except Exception as e:
        log.info("pick cards lookup failed: %s", e)
        _cput("cards", ERROR_TTL, [])
        return []
    cards: List[Dict[str, Any]] = []
    for r in rows:
        ko = _aware(r.get("start_time"))
        sport, mid = str(r.get("sport") or ""), int(r["id"])
        if ko is None or not (r.get("home") and r.get("away")):
            continue
        home, away = _display_name(str(r["home"]), sport), _display_name(str(r["away"]), sport)
        cards.append({
            "id": mid, "sport": sport, "league": league_ko(sport, str(r.get("league") or "")), "home": home, "away": away,
            "kickoff": ko.astimezone(KST).isoformat(), "pick_league": pick_league_code(sport, str(r.get("league") or "")),
            "match_label": f"{home} vs {away}",
            "links": {"ergrow": previews[mid], "zynktv": f"{ZYNKTV}/match/{mid}",
                      "gyeonggipan": gyeonggipan_link({"id": mid}, ko, now)},
        })
    _cput("cards", CARD_TTL, cards)
    return cards[:limit]


def split_match_label(label: str) -> Tuple[str, str]:
    """'아스널 vs 첼시' -> ('아스널', '첼시'); ('', '') when the label has no 'vs'."""
    parts = _VS.split((label or "").strip(), maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", "")


def links_for_pick(league: str, match_label: str, created_at: Optional[datetime]) -> Dict[str, Optional[str]]:
    """Sister links for a user's pick: the DB match with both team names, kicking off from 12 h
    before the post to 7 days after it. Nothing when the label is free text or ambiguous."""
    out: Dict[str, Optional[str]] = {"zynktv": None, "ergrow": None, "gyeonggipan": None}
    code = PICK_LEAGUE_SPORTS.get((league or "").upper())
    home, away = split_match_label(match_label)
    created = _aware(created_at)
    if not (code and home and away and created):
        return out
    key = f"pick:{code}:{_norm(home)}:{_norm(away)}:{created.date().isoformat()}"
    cached, hit = _cget(key)
    if not cached:
        try:
            cands = _rows(_SELECT + "WHERE s.code = :code AND m.start_time BETWEEN :a AND :b ORDER BY m.start_time LIMIT 800",
                          {"code": code, "a": created - timedelta(hours=12), "b": created + timedelta(days=7)})
            hit = pick_by_names(cands, home, away, "soccer" if code == "football" else code)
            _cput(key, FOUND_TTL if hit else MISSING_TTL, hit)
        except Exception as e:
            log.info("pick sister lookup failed: %s", e)
            hit = None
            _cput(key, ERROR_TTL, None)
    if not hit:
        return out
    mid, ko = int(hit["id"]), _aware(hit.get("start_time"))
    out["zynktv"] = f"{ZYNKTV}/match/{mid}"
    out["ergrow"] = ergrow_preview(mid, str(hit.get("sport") or code), str(hit.get("home") or ""), str(hit.get("away") or ""))
    out["gyeonggipan"] = gyeonggipan_link(hit, ko)
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
