"""Server-rendered daily results: /livescore/{YYYY-MM-DD}.

Scores and results only (billnojay's role). Schedules, previews and
head-to-head belong to zynktv/ergrow, so:
  * only past KST dates in the last WINDOW_DAYS days get a page,
  * today redirects (301) to /livescore, future and older dates are 404,
  * only finished games are listed (final scores), nothing scheduled.

Data: ESPN's public scoreboards for the team-page leagues plus UCL and
K League 1. A KST day spans two ESPN (US) dates, so both are read and games
are kept by their KST start date. A finished day never changes, so it is
fetched once and stored in the lounge SQLite table seo_daily_results (also
in the PG snapshot list, so it survives redeploys). The table has one row
per date, so widening the window later needs no schema change.

Index rule: at least MIN_FINISHED finished games; otherwise noindex and not
in the sitemap.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

KST = timezone(timedelta(hours=9))
SITE = "https://billnojay.com"
ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports"
WINDOW_DAYS = 60
MIN_FINISHED = 5
TABLE = "seo_daily_results"
WD_KO = ["월", "화", "수", "목", "금", "토", "일"]

# (key, ESPN path, sport, Korean league name, team-page league or None)
RESULT_LEAGUES: List[Tuple[str, str, str, str, Optional[str]]] = [
    ("EPL", "soccer/eng.1", "soccer", "프리미어리그", "epl"),
    ("LALIGA", "soccer/esp.1", "soccer", "라리가", "laliga"),
    ("BUNDESLIGA", "soccer/ger.1", "soccer", "분데스리가", "bundesliga"),
    ("SERIEA", "soccer/ita.1", "soccer", "세리에 A", "seriea"),
    ("LIGUE1", "soccer/fra.1", "soccer", "리그 1", "ligue1"),
    ("UCL", "soccer/uefa.champions", "soccer", "UEFA 챔피언스리그", None),
    ("KLEAGUE", "soccer/kor.1", "soccer", "K리그1", None),
    ("JLEAGUE", "soccer/jpn.1", "soccer", "J1리그", "jleague"),
    ("MLS", "soccer/usa.1", "soccer", "MLS", "mls"),
    ("NBA", "basketball/nba", "basketball", "NBA", "nba"),
    ("WNBA", "basketball/wnba", "basketball", "WNBA", "wnba"),
    ("NHL", "hockey/nhl", "hockey", "NHL", "nhl"),
    ("NFL", "football/nfl", "american_football", "NFL", "nfl"),
    ("MLB", "baseball/mlb", "baseball", "MLB", "mlb"),
]

router = APIRouter(tags=["result-pages"])
_READY = [False]
_WARM_LOCK = threading.Lock()
_WARMING = [False]
_MEM: Dict[str, Tuple[float, Dict[str, Any]]] = {}     # unfinished days (yesterday) cached in memory
_MEM_TTL = 1800.0


def today_kst() -> date:
    return datetime.now(KST).date()


def window(today: Optional[date] = None) -> List[date]:
    """Past dates that get a page, newest first (yesterday .. today-WINDOW_DAYS)."""
    t = today or today_kst()
    return [t - timedelta(days=i) for i in range(1, WINDOW_DAYS + 1)]


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
def _conn():
    from app import sportiq_auto_analyst as aa

    return aa._get_conn()


def _ensure_table() -> None:
    if _READY[0]:
        return
    with _conn() as c:
        c.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            day TEXT PRIMARY KEY, payload TEXT NOT NULL, finished INTEGER NOT NULL, fetched_at REAL NOT NULL)""")
    try:
        from app import sportiq_auto_analyst as aa
        tables = aa._PG_SNAPSHOT_TABLES
        if TABLE not in tables:
            aa._PG_SNAPSHOT_TABLES = (list(tables) + [TABLE]) if isinstance(tables, list) else tuple(tables) + (TABLE,)
        aa._restore_snapshots_from_pg()
    except Exception:
        pass
    _READY[0] = True


def _load(day: date) -> Optional[Dict[str, Any]]:
    _ensure_table()
    with _conn() as c:
        row = c.execute(f"SELECT payload FROM {TABLE} WHERE day = ?", (day.isoformat(),)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _save(day: date, payload: Dict[str, Any]) -> None:
    _ensure_table()
    with _conn() as c:
        c.execute(f"INSERT OR REPLACE INTO {TABLE} (day, payload, finished, fetched_at) VALUES (?, ?, ?, ?)",
                  (day.isoformat(), json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   int(payload.get("finished") or 0), time.time()))
    try:
        from app import sportiq_auto_analyst as aa
        aa._backup_tables_to_pg(TABLE)
    except Exception:
        pass


def stored_counts() -> Dict[str, int]:
    """day -> finished games for every stored day."""
    _ensure_table()
    with _conn() as c:
        return {r[0]: int(r[1]) for r in c.execute(f"SELECT day, finished FROM {TABLE}").fetchall()}


# --------------------------------------------------------------------------
# ESPN
# --------------------------------------------------------------------------
def _fetch(url: str) -> Optional[Dict[str, Any]]:
    from app import sportiq_team_pages as tp

    return tp._fetch(url)


def _score(c: Dict[str, Any]) -> Optional[int]:
    s = c.get("score")
    if isinstance(s, dict):
        s = s.get("value", s.get("displayValue"))
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _parse(data: Optional[Dict[str, Any]], day: date) -> List[Dict[str, Any]]:
    out = []
    for ev in (data or {}).get("events") or []:
        try:
            comp = (ev.get("competitions") or [])[0]
            st = (((comp.get("status") or ev.get("status") or {}).get("type")) or {})
            if str(st.get("state")) != "post" or st.get("completed") is False:
                continue
            when = datetime.fromisoformat(str(comp.get("date") or ev.get("date")).replace("Z", "+00:00")).astimezone(KST)
            if when.date() != day:
                continue
            cs = comp.get("competitors") or []
            home = next(c for c in cs if c.get("homeAway") == "home")
            away = next(c for c in cs if c.get("homeAway") == "away")
            hs, as_ = _score(home), _score(away)
            if hs is None or as_ is None:
                continue
            out.append({
                "id": str(ev.get("id") or ""), "start": when.isoformat(),
                "home": {"id": str((home.get("team") or {}).get("id") or ""), "name": str((home.get("team") or {}).get("displayName") or "")},
                "away": {"id": str((away.get("team") or {}).get("id") or ""), "name": str((away.get("team") or {}).get("displayName") or "")},
                "hs": hs, "as": as_, "detail": str(st.get("shortDetail") or st.get("detail") or ""),
            })
        except (StopIteration, IndexError, ValueError, TypeError, AttributeError):
            continue
    return out


def _collect(day: date) -> Optional[Dict[str, Any]]:
    """All finished games of one KST day, or None when every request failed."""
    leagues: List[Dict[str, Any]] = []
    ok = 0
    for key, path, _sport, _name, _tl in RESULT_LEAGUES:
        games: Dict[str, Dict[str, Any]] = {}
        for d in (day - timedelta(days=1), day):
            data = _fetch(f"{ESPN_SITE}/{path}/scoreboard?dates={d.strftime('%Y%m%d')}&limit=200")
            if data is not None:
                ok += 1
            for g in _parse(data, day):
                games[g["id"]] = g
        if games:
            leagues.append({"key": key, "games": sorted(games.values(), key=lambda g: g["start"])})
    if ok == 0:
        return None
    return {"day": day.isoformat(), "leagues": leagues, "finished": sum(len(l["games"]) for l in leagues)}


def day_results(day: date) -> Optional[Dict[str, Any]]:
    """Stored results; days that are not final yet (yesterday) come from a short memory cache."""
    final = day <= today_kst() - timedelta(days=2)
    if final:
        hit = _load(day)
        if hit is not None:
            return hit
    else:
        mem = _MEM.get(day.isoformat())
        if mem and time.time() - mem[0] < _MEM_TTL:
            return mem[1]
    res = _collect(day)
    if res is None:
        return _load(day)
    if final:
        _save(day, res)
    else:
        _MEM[day.isoformat()] = (time.time(), res)
        _save(day, res)          # replaced once the day is final
    return res


def warm_window_async() -> None:
    """Fill missing stored days in the background (one at a time).
    SPORTIQ_RESULTS_WARM=0 turns it off (tests)."""
    import os

    if os.environ.get("SPORTIQ_RESULTS_WARM") == "0":
        return
    with _WARM_LOCK:
        if _WARMING[0]:
            return
        _WARMING[0] = True

    def run():
        try:
            have = stored_counts()
            for d in window():
                if d.isoformat() not in have and d <= today_kst() - timedelta(days=2):
                    day_results(d)
                    time.sleep(1.0)      # about 28 ESPN requests per day: keep the pace gentle
        except Exception:
            pass
        finally:
            _WARMING[0] = False

    threading.Thread(target=run, name="seo-results-warm", daemon=True).start()


def sitemap_result_days() -> List[str]:
    """Stored days in the window with enough finished games (newest first)."""
    try:
        have = stored_counts()
    except Exception:
        return []
    days = [d.isoformat() for d in window()]
    return [d for d in days if have.get(d, 0) >= MIN_FINISHED]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _e(v: Any) -> str:
    import html

    return html.escape(str(v if v is not None else ""), quote=True)


def date_ko(d: date) -> str:
    return f"{d.year}년 {d.month}월 {d.day}일"


def _team_link(league: Optional[str], team: Dict[str, Any], sport: str) -> str:
    from app import sportiq_team_pages as tp

    name = tp.korean_name(team["name"], sport) or team["name"]
    if league:
        slug = next((t["slug"] for t in tp.league_teams(league) if t["id"] == team["id"]), None)
        if slug:
            return f'<a href="/team/{_e(league)}/{_e(slug)}">{_e(name)}</a>'
    return _e(name)


def build_page(day: date, res: Dict[str, Any]) -> Tuple[str, bool]:
    from app import sportiq_seo as seo
    from app import sportiq_team_pages as tp

    meta = {k: (path, sport, name, tl) for k, path, sport, name, tl in RESULT_LEAGUES}
    finished = int(res.get("finished") or 0)
    indexable = finished >= MIN_FINISHED
    blocks, samples = [], []
    for lg in res.get("leagues") or []:
        if lg["key"] not in meta:
            continue
        _path, sport, lg_name, tl = meta[lg["key"]]
        trs = []
        for g in lg["games"]:
            trs.append(
                f"<tr><td>{_team_link(tl, g['home'], sport)}</td><td class=\"num\">{g['hs']} : {g['as']}</td>"
                f"<td>{_team_link(tl, g['away'], sport)}</td><td>{_e(g.get('detail') or '종료')}</td></tr>"
            )
            if len(samples) < 3:
                h = tp.korean_name(g["home"]["name"], sport) or g["home"]["name"]
                a = tp.korean_name(g["away"]["name"], sport) or g["away"]["name"]
                samples.append(f"{lg_name} {h} {g['hs']}-{g['as']} {a}")
        blocks.append(
            f"<h2>{_e(lg_name)} ({len(lg['games'])}경기)</h2><table><thead><tr><th>홈</th>"
            f"<th class=\"num\">스코어</th><th>원정</th><th>상태</th></tr></thead><tbody>{''.join(trs)}</tbody></table>"
        )
    if not blocks:
        blocks.append('<p class="note">이 날짜에 끝난 주요 리그 경기가 없습니다.</p>')
    nav = []
    prev_d, next_d = day - timedelta(days=1), day + timedelta(days=1)
    if prev_d in window():
        nav.append(f'<a href="/livescore/{prev_d.isoformat()}">← {_e(date_ko(prev_d))} 결과</a>')
    if next_d < today_kst():
        nav.append(f'<a href="/livescore/{next_d.isoformat()}">{_e(date_ko(next_d))} 결과 →</a>')
    nav.append('<a href="/livescore">오늘 라이브스코어</a>')
    wd = WD_KO[day.weekday()]
    title = f"{date_ko(day)} 스포츠 경기 결과·최종 스코어 | SportIQ"
    desc = f"{date_ko(day)}({wd}) 끝난 {finished}경기 결과" + (f": {', '.join(samples)} 등." if samples else ".")
    intro = (f'<p class="muted">{_e(date_ko(day))}({wd}) 한국 시간 기준 · 끝난 경기 {finished}개 · '
             "축구·농구·야구·하키·미식축구 주요 리그 최종 스코어</p>")
    body = intro + f'<p>{" · ".join(nav)}</p>' + "".join(blocks) + f'<p>{" · ".join(nav)}</p>'
    page = seo._page(f"/livescore/{day.isoformat()}", title, desc, f"{date_ko(day)} 경기 결과", body)
    if not indexable:
        page = seo.set_robots(page, seo.NOINDEX)
    return page, indexable


@router.api_route("/livescore/{day}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def result_page(day: str, request: Request):
    from app import sportiq_seo as seo

    try:
        d = date.fromisoformat(day)
        if len(day) != 10:
            raise ValueError
    except ValueError:
        d = None
    today = today_kst()
    if d == today:
        return RedirectResponse("/livescore", status_code=301)
    if d is None or d > today or d not in window(today):
        page = seo._page(request.url.path, "결과 페이지가 없습니다 | SportIQ", seo.SHORT_DESC, "결과 페이지가 없습니다",
                         f'<p>최근 {WINDOW_DAYS}일의 지난 경기 결과만 볼 수 있습니다. '
                         '<a href="/livescore">오늘 라이브스코어</a>로 이동해 주세요.</p>')
        return HTMLResponse(seo.set_robots(page, seo.NOINDEX), status_code=404, headers={"X-Robots-Tag": seo.NOINDEX})
    res = day_results(d)
    if res is None:
        res = {"day": d.isoformat(), "leagues": [], "finished": 0}
    html_text, indexable = build_page(d, res)
    headers = {"Cache-Control": "public, max-age=600"}
    if not indexable:
        headers["X-Robots-Tag"] = seo.NOINDEX
    return HTMLResponse(html_text, headers=headers)


def recent_links_html(n: int = 7) -> str:
    days = window()[:n]
    return " · ".join(f'<a href="/livescore/{d.isoformat()}">{d.month}월 {d.day}일</a>' for d in days)
