"""Server-rendered team pages: /team/{league}/{slug}.

billnojay's role is live scores, standings, votes and community; previews,
analysis and fixtures lists belong to ergrow/zynktv. A team page therefore
shows only:
  * the team's current standing (rank, record, goal/point difference),
  * its finished results this season (newest first, final scores),
  * one line with the single next match (date + opponent, no link),
  * links to the league table and the other teams of the same league.

Data comes from ESPN's public standings and team-schedule endpoints (the same
source as the home scoreboard and /standings pages). Nothing is invented: a
page with too little real data is noindex and stays out of the sitemap.

Index rule: the team has a standings row with gamesPlayed >= 3 (that is the
number of finished games this season), and the rendered page shows at least 3
finished results. The sitemap uses the standings part (cheap: one request per
league); a page whose schedule could not be loaded marks itself noindex.

Slugs come from ESPN's English displayName and never change with the Korean
name. The title uses the Korean name when the existing tables have one, and
the English name otherwise.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

KST = timezone(timedelta(hours=9))
SITE = "https://billnojay.com"
ESPN_V2 = "https://site.api.espn.com/apis/v2/sports"
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports"
MIN_GAMES = 3
RECENT_MAX = 10
WD_KO = ["월", "화", "수", "목", "금", "토", "일"]

# slug -> (datacenter code, ESPN path, sport, Korean league name)
# UCL is left out (its clubs already have a domestic-league page) and so is
# K League 1 (ESPN has no standings for it).
TEAM_LEAGUES: Dict[str, Tuple[str, str, str, str]] = {
    "epl": ("EPL", "soccer/eng.1", "soccer", "프리미어리그"),
    "laliga": ("LALIGA", "soccer/esp.1", "soccer", "라리가"),
    "bundesliga": ("BUNDESLIGA", "soccer/ger.1", "soccer", "분데스리가"),
    "seriea": ("SERIEA", "soccer/ita.1", "soccer", "세리에 A"),
    "ligue1": ("LIGUE1", "soccer/fra.1", "soccer", "리그 1"),
    "jleague": ("JLEAGUE", "soccer/jpn.1", "soccer", "J1리그"),
    "mls": ("MLS", "soccer/usa.1", "soccer", "MLS"),
    "mlb": ("MLB", "baseball/mlb", "baseball", "MLB"),
    "nba": ("NBA", "basketball/nba", "basketball", "NBA"),
    "wnba": ("WNBA", "basketball/wnba", "basketball", "WNBA"),
    "nhl": ("NHL", "hockey/nhl", "hockey", "NHL"),
    "nfl": ("NFL", "football/nfl", "american_football", "NFL"),
}
GROUP_KO = {"Eastern Conference": "동부 컨퍼런스", "Western Conference": "서부 컨퍼런스",
            "American League": "아메리칸리그", "National League": "내셔널리그"}

STANDINGS_TTL = 1800.0
SCHEDULE_TTL = 6 * 3600.0
_CACHE: Dict[str, Tuple[float, Any]] = {}
_LOCK = threading.Lock()
_FETCH_SLOTS = threading.BoundedSemaphore(3)   # crawlers must not fan out to ESPN

router = APIRouter(tags=["team-pages"])


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "team"


def _fetch(url: str) -> Optional[Dict[str, Any]]:
    from app import sportiq_datacenter as dc

    with _FETCH_SLOTS:
        data = dc._fetch_json(url, timeout=8)
    return data if isinstance(data, dict) else None


def _cached(key: str, ttl: float, build):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = build()
    if val is not None:
        with _LOCK:
            _CACHE[key] = (now, val)
    elif hit and hit[1] is not None:
        return hit[1]          # keep serving the last good copy when ESPN fails
    else:
        with _LOCK:            # remember the failure for 5 minutes instead of retrying every view
            _CACHE[key] = (now - max(0.0, ttl - 300.0), None)
    return val


def _num(st: Dict[str, Any], key: str, default: float = 0.0) -> float:
    v = st.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def korean_name(name_en: str, sport: str) -> Optional[str]:
    try:
        from app.sportiq_lounge_router import _localize_team_ko_v37
        ko = _localize_team_ko_v37(name_en, sport)
    except Exception:
        return None
    return ko if re.search("[가-힣]", ko or "") else None


# --------------------------------------------------------------------------
# league teams (standings)
# --------------------------------------------------------------------------
def _walk_entries(node: Dict[str, Any], group: str = "") -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    for ch in node.get("children") or []:
        out += _walk_entries(ch, str(ch.get("name") or group))
    for ent in ((node.get("standings") or {}).get("entries") or []):
        out.append((group, ent))
    return out


def _build_league_teams(league: str) -> Optional[List[Dict[str, Any]]]:
    code, path, sport, _lg_ko = TEAM_LEAGUES[league]
    data = _fetch(f"{ESPN_V2}/{path}/standings")
    if not data:
        return None
    teams: List[Dict[str, Any]] = []
    for group, ent in _walk_entries(data):
        tm = ent.get("team") or {}
        tid, name = str(tm.get("id") or ""), str(tm.get("displayName") or "").strip()
        if not tid or not name:
            continue
        st = {x.get("name"): x.get("value") for x in (ent.get("stats") or []) if isinstance(x, dict)}
        w, l, d = int(_num(st, "wins")), int(_num(st, "losses")), int(_num(st, "ties"))
        gp = int(_num(st, "gamesPlayed")) or (w + l + d + int(_num(st, "otLosses", _num(st, "overtimeLosses"))))
        pct = st.get("winPercent")
        teams.append({
            "id": tid, "name_en": name, "short": str(tm.get("shortDisplayName") or name),
            "abbr": str(tm.get("abbreviation") or ""), "group_en": group,
            "gp": gp, "w": w, "d": d, "l": l,
            "gf": int(_num(st, "pointsFor")), "ga": int(_num(st, "pointsAgainst")),
            "pts": int(_num(st, "points")) if sport in ("soccer", "hockey") else None,
            "pct": round(float(pct), 3) if isinstance(pct, (int, float)) else None,
            "espn_rank": int(_num(st, "rank")) or None,
        })
    if not teams:
        return None
    _apply_ranks(league, teams, data)
    seen: Dict[str, int] = {}
    for t in sorted(teams, key=lambda x: x["name_en"]):
        s = slugify(t["name_en"])
        if s in seen:
            s = f"{s}-{slugify(t['abbr'] or t['id'])}"
        seen[s] = 1
        t["slug"] = s
    return teams


def _apply_ranks(league: str, teams: List[Dict[str, Any]], data: Dict[str, Any]) -> None:
    """Same ranks as the /standings pages."""
    from app import sportiq_datacenter as dc

    code, _path, sport, _ = TEAM_LEAGUES[league]
    if code in dc._US_GROUP_LEAGUES:
        rows = dc._us_group_rows(code, data) or []
        by_abbr = {r.get("team_abbr"): r for r in rows}
        by_name = {r.get("team"): r for r in rows}
        for t in teams:
            r = by_abbr.get(t["abbr"]) or by_name.get(t["name_en"])
            if r:
                t["rank"], t["group"] = r.get("rank"), r.get("group")
                if code == "NHL":
                    t["pts"] = r.get("pts")
        return
    groups = sorted({t["group_en"] for t in teams})
    multi = len(groups) > 1
    for g in groups:
        members = [t for t in teams if t["group_en"] == g]
        if sport == "soccer" and not multi:
            members.sort(key=lambda t: (-(t["pts"] or 0), -(t["gf"] - t["ga"]), -t["gf"]))
            for i, t in enumerate(members, start=1):
                t["rank"] = i
        elif all(t["espn_rank"] for t in members):
            for t in members:
                t["rank"] = t["espn_rank"]
        else:
            # MLB has no rank stat: order by win percentage within the league.
            members.sort(key=lambda t: (-(t["pct"] or 0), -t["w"]))
            for i, t in enumerate(members, start=1):
                t["rank"] = i
        for t in members:
            t["group"] = GROUP_KO.get(g, g) if multi else None


def league_teams(league: str) -> List[Dict[str, Any]]:
    if league not in TEAM_LEAGUES:
        return []
    return _cached(f"teams:{league}", STANDINGS_TTL, lambda: _build_league_teams(league)) or []


def find_team(league: str, slug: str) -> Optional[Dict[str, Any]]:
    return next((t for t in league_teams(league) if t.get("slug") == slug), None)


def standings_indexable(team: Dict[str, Any]) -> bool:
    return int(team.get("gp") or 0) >= MIN_GAMES


def team_slug_lookup(league_code: str) -> Dict[str, str]:
    """name/short/abbr -> slug for linking /standings rows (empty if not built)."""
    league = next((k for k, v in TEAM_LEAGUES.items() if v[0] == league_code), None)
    if not league:
        return {}
    out: Dict[str, str] = {}
    for t in league_teams(league):
        for k in (t["name_en"], t["short"], t["abbr"]):
            if k:
                out.setdefault(k, t["slug"])
    return out


# --------------------------------------------------------------------------
# team schedule
# --------------------------------------------------------------------------
def _score(c: Dict[str, Any]) -> Optional[int]:
    s = c.get("score")
    if isinstance(s, dict):
        s = s.get("value", s.get("displayValue"))
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _parse_schedule(data: Optional[Dict[str, Any]], team_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    done: List[Dict[str, Any]] = []
    upcoming: List[Dict[str, Any]] = []
    for ev in (data or {}).get("events") or []:
        try:
            comp = (ev.get("competitions") or [])[0]
            cs = comp.get("competitors") or []
            me = next(c for c in cs if str((c.get("team") or {}).get("id")) == team_id)
            opp = next(c for c in cs if c is not me)
            state = str((((comp.get("status") or ev.get("status") or {}).get("type")) or {}).get("state") or "")
            when = datetime.fromisoformat(str(comp.get("date") or ev.get("date")).replace("Z", "+00:00")).astimezone(KST)
        except (StopIteration, IndexError, ValueError, TypeError, AttributeError):
            continue
        item = {
            "id": str(ev.get("id") or ""), "when": when, "home": me.get("homeAway") == "home",
            "opp_en": str((opp.get("team") or {}).get("displayName") or ""),
        }
        if state == "post":
            us, them = _score(me), _score(opp)
            if us is None or them is None:
                continue
            res = "W" if me.get("winner") is True else ("L" if opp.get("winner") is True else ("D" if us == them else ("W" if us > them else "L")))
            item.update(us=us, them=them, result=res)
            done.append(item)
        elif state == "pre":
            upcoming.append(item)
    done.sort(key=lambda x: x["when"], reverse=True)
    upcoming.sort(key=lambda x: x["when"])
    return done, upcoming


def _build_schedule(league: str, team_id: str) -> Optional[Dict[str, Any]]:
    _code, path, sport, _ = TEAM_LEAGUES[league]
    base = f"{ESPN_SITE}/{path}/teams/{team_id}/schedule"
    data = _fetch(base)
    if data is None:
        return None
    done, upcoming = _parse_schedule(data, team_id)
    if not done:
        # Off-season: ESPN's default is the next season. Ask for the one before.
        yr = ((data.get("requestedSeason") or data.get("season") or {}) or {}).get("year")
        if isinstance(yr, int):
            prev = _fetch(f"{base}?season={yr - 1}")
            done = _parse_schedule(prev, team_id)[0] if prev else []
    if sport == "soccer" and not upcoming:
        fx = _fetch(f"{base}?fixture=true")
        upcoming = _parse_schedule(fx, team_id)[1] if fx else []
    return {"done": done, "next": upcoming[:1]}


def team_schedule(league: str, team_id: str) -> Dict[str, Any]:
    return _cached(f"sched:{league}:{team_id}", SCHEDULE_TTL, lambda: _build_schedule(league, team_id)) or {"done": [], "next": [], "failed": True}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _e(v: Any) -> str:
    import html

    return html.escape(str(v if v is not None else ""), quote=True)


def _date_ko(dt: datetime) -> str:
    return f"{dt.month}월 {dt.day}일 ({WD_KO[dt.weekday()]})"


def _record(team: Dict[str, Any], sport: str) -> str:
    if sport == "soccer":
        return f"{team['gp']}경기 {team['w']}승 {team['d']}무 {team['l']}패 · 승점 {team.get('pts') or 0}"
    if sport == "hockey":
        return f"{team['gp']}경기 {team['w']}승 {team['l']}패 · 승점 {team.get('pts') or 0}"
    rec = f"{team['gp']}경기 {team['w']}승 {team['l']}패"
    if sport == "american_football" and team.get("d"):
        rec += f" {team['d']}무"
    if team.get("pct") is not None:
        rec += f" · 승률 {team['pct']:.3f}".replace(" 0.", " .")
    return rec


def build_team_page(league: str, team: Dict[str, Any]) -> Tuple[str, bool]:
    """(html, indexable)"""
    from app import sportiq_seo as seo

    _code, _path, sport, lg_ko = TEAM_LEAGUES[league]
    ko = korean_name(team["name_en"], sport)
    disp = ko or team["name_en"]
    sched = team_schedule(league, team["id"])
    done, nxt = sched.get("done") or [], sched.get("next") or []
    indexable = standings_indexable(team) and len(done) >= MIN_GAMES

    where = f"{team['group']} " if team.get("group") else ""
    rank_txt = f"{where}{team['rank']}위" if team.get("rank") else ""
    record = _record(team, sport)
    recent = done[:RECENT_MAX]

    def opp_name(en: str) -> str:
        return korean_name(en, sport) or en

    last3 = ", ".join(
        f"{opp_name(r['opp_en'])}전 {r['us']}-{r['them']} {'승' if r['result'] == 'W' else ('무' if r['result'] == 'D' else '패')}"
        for r in recent[:3]
    )
    title = f"{disp} 순위·최근 경기 결과 | {lg_ko} | SportIQ"
    desc = f"{disp}: {lg_ko} {rank_txt + ' · ' if rank_txt else ''}{record}." + (f" 최근 결과: {last3}." if last3 else "")
    h1 = f"{disp} ({team['name_en']}) — {lg_ko}" if ko else f"{disp} — {lg_ko}"

    body = [
        f'<p class="muted">{_e(lg_ko)} {_e(rank_txt)} · {_e(record)} · '
        f'<a href="/standings/{_e(league)}">{_e(lg_ko)} 순위표</a></p>'
    ]
    if nxt:
        n = nxt[0]
        body.append(f"<p>다음 경기: {_e(_date_ko(n['when']))} vs {_e(opp_name(n['opp_en']))} ({'홈' if n['home'] else '원정'})</p>")
    if recent:
        trs = "".join(
            f"<tr><td>{_e(_date_ko(r['when']))}</td><td>{_e(opp_name(r['opp_en']))}</td>"
            f"<td>{'홈' if r['home'] else '원정'}</td><td class=\"num\">{r['us']} : {r['them']}</td>"
            f"<td>{'승' if r['result'] == 'W' else ('무' if r['result'] == 'D' else '패')}</td></tr>"
            for r in recent
        )
        body.append(
            f"<h2>최근 경기 결과</h2><table><thead><tr><th>날짜</th><th>상대</th><th>홈/원정</th>"
            f"<th class=\"num\">스코어</th><th>결과</th></tr></thead><tbody>{trs}</tbody></table>"
        )
    else:
        body.append('<p class="note">지금은 이 팀의 경기 결과를 불러오지 못했습니다. 잠시 후 다시 확인해 주세요.</p>')
    others = [t for t in league_teams(league) if t["slug"] != team["slug"]]
    if others:
        links = "".join(
            f'<li><a href="/team/{_e(league)}/{_e(t["slug"])}">{_e(korean_name(t["name_en"], sport) or t["name_en"])}</a></li>'
            for t in sorted(others, key=lambda t: (t.get("group") or "", t.get("rank") or 99))
        )
        body.append(f'<h2>{_e(lg_ko)} 다른 팀</h2><ul class="grid">{links}</ul>')

    ld = {
        "@context": "https://schema.org", "@type": "SportsTeam", "name": disp,
        "alternateName": team["name_en"] if ko else None, "sport": sport,
        "memberOf": {"@type": "SportsOrganization", "name": lg_ko},
        "url": f"{SITE}/team/{league}/{team['slug']}",
    }
    ld = {k: v for k, v in ld.items() if v is not None}
    page = seo._page(f"/team/{league}/{team['slug']}", title, desc, h1, "".join(body), ld)
    if not indexable:
        page = seo.set_robots(page, seo.NOINDEX)
    return page, indexable


_PAGE_CACHE: Dict[str, Tuple[float, str, bool]] = {}
_PAGE_TTL = 300.0


@router.api_route("/team/{league}/{slug}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def team_page(league: str, slug: str, request: Request):
    from app import sportiq_seo as seo

    league, slug = (league or "").lower(), (slug or "").lower()
    team = find_team(league, slug) if league in TEAM_LEAGUES else None
    if not team:
        page = seo._page(request.url.path, "팀을 찾을 수 없습니다 | SportIQ", seo.SHORT_DESC, "팀을 찾을 수 없습니다",
                         '<p><a href="/standings">리그 순위표</a>에서 팀을 찾아 주세요.</p>')
        return HTMLResponse(seo.set_robots(page, seo.NOINDEX), status_code=404, headers={"X-Robots-Tag": seo.NOINDEX})
    key = f"{league}/{slug}"
    now = time.monotonic()
    hit = _PAGE_CACHE.get(key)
    if hit and now - hit[0] < _PAGE_TTL:
        html_text, indexable = hit[1], hit[2]
    else:
        html_text, indexable = build_team_page(league, team)
        _PAGE_CACHE[key] = (now, html_text, indexable)
    headers = {"Cache-Control": "public, max-age=300"}
    if not indexable:
        headers["X-Robots-Tag"] = seo.NOINDEX
    return HTMLResponse(html_text, headers=headers)


def sitemap_team_urls() -> List[Tuple[str, str]]:
    """(path, league) for every team that passes the standings part of the rule."""
    out: List[Tuple[str, str]] = []
    for league in TEAM_LEAGUES:
        for t in league_teams(league):
            if standings_indexable(t):
                out.append((f"/team/{league}/{t['slug']}", league))
    return out
