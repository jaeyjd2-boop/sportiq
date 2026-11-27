"""billnojay.com search: shared titles, server-rendered public pages, noindex.

billnojay.com covers live scores, league tables, match-prediction votes and
the community. Analysis and preview articles belong to ergrow, so nothing
here uses "분석" as a search term.

Pages (GET, plain HTML, no JS needed to read them):
  /livescore              today's real (non-sample) matches by league
  /standings              list of league tables
  /standings/<league>     one table, e.g. /standings/epl

Example data is never shown as real: sample matches are skipped, and a sample
or unavailable table renders a notice instead of rows.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from urllib.parse import parse_qs
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

SITE = "https://billnojay.com"

HOME_TITLE = "SportIQ 라이브스코어·실시간 스코어 | 리그 순위표·승부예측 투표 커뮤니티"
HOME_H1 = "SportIQ 라이브스코어 · 리그 순위표 · 승부예측 커뮤니티"
TAGLINE = "라이브스코어·순위표·승부예측 커뮤니티"
HOME_DESC = (
    "축구·야구·농구·배구·하키 실시간 라이브스코어와 EPL·K리그·NBA·MLB 리그 순위표, "
    "경기 승부예측 투표와 응원톡 커뮤니티를 가입 없이 이용하세요."
)
SHORT_DESC = "실시간 라이브스코어, 리그 순위표, 승부예측 투표와 응원톡을 가입 없이 보는 스포츠 커뮤니티"
NOINDEX = "noindex, follow"
PUBLIC_REPORT_TITLES: Dict[str, str] = {}

def public_report(post_id: str) -> Optional[Dict[str, str]]:
    title_prefix = PUBLIC_REPORT_TITLES.get(post_id)
    if not title_prefix:
        return None
    from app.sportiq_lounge_router import resolve_post_og_meta_v29

    report = resolve_post_og_meta_v29(post_id)
    if (
        report
        and report["author"] == "SportIQ AI 분석팀"
        and report["title"].startswith(title_prefix)
        and len(report["full_body"].strip()) >= 200
    ):
        return report
    return None


def public_report_response(post_id: str, report: Dict[str, str]) -> HTMLResponse:
    paragraphs = "".join(
        f"<p>{_e(paragraph.strip())}</p>"
        for paragraph in report["full_body"].splitlines()
        if paragraph.strip()
    )
    page = _page(
        f"/?post={post_id}", f'{report["title"]} | SportIQ', report["desc"], report["title"],
        f'<article><p class="muted">{_e(report["author"])}</p>{paragraphs}</article>',
    )
    page = set_robots(page, NOINDEX)
    return HTMLResponse(page, headers={"Cache-Control": "private, no-store", "X-Robots-Tag": NOINDEX})
  
# URL slug -> datacenter league code. ESPN-backed tables only: leagues whose
# table is always an example (KBO, NPB, LCK, V-리그) get no public page.
STANDINGS_LEAGUES: Dict[str, Tuple[str, str]] = {
    "epl": ("EPL", "프리미어리그(EPL)"),
    "laliga": ("LALIGA", "라리가"),
    "bundesliga": ("BUNDESLIGA", "분데스리가"),
    "seriea": ("SERIEA", "세리에 A"),
    "ligue1": ("LIGUE1", "리그 1"),
    "ucl": ("UCL", "UEFA 챔피언스리그"),
    "kleague": ("KLEAGUE", "K리그1"),
    "jleague": ("JLEAGUE", "J1리그"),
    "mls": ("MLS", "MLS"),
    "mlb": ("MLB", "MLB"),
    "nba": ("NBA", "NBA"),
    "wnba": ("WNBA", "WNBA"),
    "nhl": ("NHL", "NHL"),
    "nfl": ("NFL", "NFL"),
}

PUBLIC_PATHS: List[Tuple[str, str, str]] = (
    [("/livescore", "always", "0.9"), ("/standings", "daily", "0.8")]
    + [(f"/standings/{slug}", "daily", "0.7") for slug in STANDINGS_LEAGUES]
)

SPORT_KO = {
    "soccer": "축구", "baseball": "야구", "basketball": "농구", "hockey": "아이스하키",
    "american_football": "미식축구", "volleyball": "배구", "tennis": "테니스",
    "esports": "e스포츠", "ufc": "UFC", "f1": "F1",
}
STATE_KO = {"in": "진행 중", "pre": "예정", "post": "종료"}

router = APIRouter(tags=["seo-pages"])

_PAGE_CACHE: Dict[str, Tuple[float, str]] = {}
_PAGE_LOCK = threading.Lock()
_PAGE_TTL = 60.0

_e = lambda v: html.escape(str(v if v is not None else ""), quote=True)  # noqa: E731


# --------------------------------------------------------------------------
# Head helpers used by the home page and ?post= pages
# --------------------------------------------------------------------------
def set_robots(html_text: str, content: str) -> str:
    """Replace (or add) <meta name="robots">."""
    if not html_text:
        return html_text
    tag = f'<meta name="robots" content="{_e(content)}" />'
    pat = re.compile(r'<meta\s+name=["\']robots["\'][^>]*>', re.I)
    if pat.search(html_text):
        out = pat.sub(lambda _m: tag, html_text)
    elif "</head>" in html_text:
        out = html_text.replace("</head>", f"  {tag}\n</head>", 1)
    else:
        out = html_text
    if content == NOINDEX and "?post=" in out:
        out = re.sub(
            r'<link\s+rel=["\']canonical["\']\s+href=["\']https://billnojay\.com/\?post=[^"\']*["\']\s*/?>',
            '<link rel="canonical" href="https://billnojay.com/" />',
            out,
            flags=re.I,
        )
    out = out.replace("[00:00 글로벌 TOP 20 종합 리포트]", "[데일리 글로벌 TOP 20 종합 리포트]")
    out = out.replace("[00:00 커뮤니티 자동분석]", "[AI 데일리 자동분석]")
    return out


# --------------------------------------------------------------------------
# Page shell
# --------------------------------------------------------------------------
_CSS = (
    "body{margin:0;background:#1a1d23;color:#f3f5f7;font-family:Pretendard,system-ui,sans-serif;line-height:1.5}"
    "a{color:#ff6372}header,main,footer{max-width:960px;margin:0 auto;padding:16px}"
    "nav a{margin-right:14px;font-weight:600}h1{font-size:1.6rem;margin:.4em 0}"
    "h2{font-size:1.15rem;margin:1.4em 0 .4em;color:#ffd166}table{width:100%;border-collapse:collapse;font-size:.92rem}"
    "th,td{padding:6px 8px;border-bottom:1px solid #2c3038;text-align:left}th{color:#c4c9d1;font-weight:600}"
    ".num{text-align:right;font-variant-numeric:tabular-nums}.muted{color:#c4c9d1;font-size:.88rem}"
    ".live{color:#ff4d5e;font-weight:700}.note{background:#22262e;padding:12px;border-radius:8px}"
    "ul.grid{display:flex;flex-wrap:wrap;gap:8px;padding:0;list-style:none}"
    "ul.grid a{display:block;padding:8px 12px;background:#22262e;border-radius:8px}"
    "@media(max-width:600px){th,td{padding:5px 4px;font-size:.85rem}}"
)


def _page(path: str, title: str, desc: str, h1: str, body: str, ld: Optional[Dict[str, Any]] = None) -> str:
    import json

    url = SITE + path
    crumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [{"@type": "ListItem", "position": 1, "name": "SportIQ", "item": SITE + "/"}],
    }
    parts = [p for p in path.split("?", 1)[0].strip("/").split("/") if p]
    acc = ""
    for i, p in enumerate(parts, start=2):
        acc += "/" + p
        name = {"livescore": "라이브스코어", "standings": "리그 순위표"}.get(p) or STANDINGS_LEAGUES.get(p, (None, p))[1]
        crumbs["itemListElement"].append({"@type": "ListItem", "position": i, "name": name, "item": SITE + acc})
    lds = [crumbs] + ([ld] if ld else [])
    ld_html = "".join(
        f'<script type="application/ld+json">{json.dumps(x, ensure_ascii=False, separators=(",", ":"))}</script>'
        for x in lds
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"ko\">\n<head>\n"
        '<meta charset="UTF-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"<title>{_e(title)}</title>\n"
        f'<meta name="description" content="{_e(desc)}" />\n'
        '<meta name="robots" content="index, follow, max-snippet:-1" />\n'
        f'<link rel="canonical" href="{_e(url)}" />\n'
        '<meta property="og:type" content="website" />\n'
        '<meta property="og:site_name" content="SportIQ" />\n'
        '<meta property="og:locale" content="ko_KR" />\n'
        f'<meta property="og:title" content="{_e(title)}" />\n'
        f'<meta property="og:description" content="{_e(desc)}" />\n'
        f'<meta property="og:url" content="{_e(url)}" />\n'
        f'<meta property="og:image" content="{SITE}/og-cover.png?v=28" />\n'
        '<meta name="theme-color" content="#ff4d5e" />\n'
        '<link rel="icon" href="/favicon.svg" type="image/svg+xml" />\n'
        '<link rel="icon" href="/favicon-32.png" sizes="32x32" type="image/png" />\n'
        '<meta name="twitter:card" content="summary_large_image" />\n'
        f'<meta name="twitter:title" content="{_e(title)}" />\n'
        f'<meta name="twitter:description" content="{_e(desc)}" />\n'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png" />\n'
        f"<style>{_CSS}</style>\n{ld_html}\n</head>\n<body>\n"
        '<header><nav><a href="/">SportIQ 홈</a><a href="/livescore">라이브스코어</a>'
        '<a href="/standings">리그 순위표</a><a href="/">승부예측 투표·커뮤니티</a></nav></header>\n'
        f"<main>\n<h1>{_e(h1)}</h1>\n{body}\n</main>\n"
        '<footer class="muted"><p>데이터: ESPN 등 공개 경기 정보. 실시간 화면과 승부예측 투표, 응원톡은 '
        '<a href="/">SportIQ 홈</a>에서 이용할 수 있습니다.</p></footer>\n'
        "</body>\n</html>\n"
    )


def _cached(key: str, build) -> str:
    now = time.monotonic()
    with _PAGE_LOCK:
        hit = _PAGE_CACHE.get(key)
        if hit and now - hit[0] < _PAGE_TTL:
            return hit[1]
    out = build()
    with _PAGE_LOCK:
        _PAGE_CACHE[key] = (now, out)
    return out


def _response(text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        text,
        status_code=status,
        headers={"Cache-Control": "public, max-age=60", "CDN-Cache-Control": "max-age=60"},
    )


# --------------------------------------------------------------------------
# /livescore
# --------------------------------------------------------------------------
def _live_matches() -> Tuple[List[Dict[str, Any]], str]:
    from app import sportiq_datacenter as dc

    res = dc.get_live_scores(sport="all", league="all", date_offset=0) or {}
    rows = [m for m in (res.get("matches") or []) if isinstance(m, dict) and not m.get("is_sample")]
    return rows, str(res.get("server_now_kst") or "")


def build_livescore_html() -> str:
    try:
        matches, now_kst = _live_matches()
    except Exception:
        matches, now_kst = [], ""
    order = {"in": 0, "pre": 1, "post": 2}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for m in matches:
        sport = SPORT_KO.get(str(m.get("sport") or ""), str(m.get("sport") or "기타"))
        lg = str(m.get("league_full") or m.get("league") or "")
        groups.setdefault(f"{sport} · {lg}", []).append(m)
    live_n = sum(1 for m in matches if m.get("state") == "in")

    blocks: List[str] = []
    if not matches:
        blocks.append('<p class="note">지금 불러온 오늘 경기가 없습니다. 잠시 후 다시 확인해 주세요.</p>')
    for name in sorted(groups, key=lambda g: (min(order.get(m.get("state"), 3) for m in groups[g]), g)):
        rows = sorted(groups[name], key=lambda m: (order.get(m.get("state"), 3), str(m.get("match_dt_full_kst") or "")))
        trs = []
        for m in rows:
            st = str(m.get("state") or "")
            score = f'{_e(m.get("home_score"))} : {_e(m.get("away_score"))}' if st in ("in", "post") else "vs"
            badge = _e(m.get("status_badge") or STATE_KO.get(st, ""))
            cls = ' class="live"' if st == "in" else ""
            trs.append(
                f"<tr><td>{_e(m.get('match_dt_kst'))}</td><td>{_e(m.get('home'))}</td>"
                f'<td class="num">{score}</td><td>{_e(m.get("away"))}</td><td{cls}>{badge}</td></tr>'
            )
        blocks.append(
            f"<h2>{_e(name)}</h2><table><thead><tr><th>시간(KST)</th><th>홈</th><th class=\"num\">스코어</th>"
            f"<th>원정</th><th>상태</th></tr></thead><tbody>{''.join(trs)}</tbody></table>"
        )
    try:
        from app import sportiq_results_pages as rp
        past = f'<p>지난 경기 결과: {rp.recent_links_html(7)}</p>'
    except Exception:
        past = ""
    intro = past + (
        f'<p class="muted">{_e(now_kst)} 기준 · 오늘 경기 {len(matches)}개, 진행 중 {live_n}개. '
        "축구·농구·야구·배구·하키·테니스 실시간 스코어를 리그별로 모았습니다.</p>"
    )
    return _page(
        "/livescore",
        "라이브스코어 · 오늘 실시간 스포츠 스코어 | SportIQ",
        "오늘 축구·농구·야구·배구·하키·테니스 경기의 실시간 스코어와 경기 상태를 리그별로 확인하세요. 진행 중인 경기를 먼저 보여 줍니다.",
        "오늘 라이브스코어 · 실시간 스포츠 스코어",
        intro + "".join(blocks),
        ld={"@context": "https://schema.org", "@type": "CollectionPage", "name": "라이브스코어 · 오늘 실시간 스포츠 스코어 | SportIQ", "url": f"{SITE}/livescore", "inLanguage": "ko-KR"},
    )


# --------------------------------------------------------------------------
# /standings and /standings/<league>
# --------------------------------------------------------------------------
def _standings(code: str) -> Dict[str, Any]:
    from app import sportiq_datacenter as dc

    return dc.get_standings(code) or {}


def _ko_team(name: Any, sport: str) -> str:
    """Korean name from the shared tables (home cards, team pages); English when there is none."""
    en = str(name or "")
    try:
        from app import sportiq_team_pages as tp
        return tp.korean_name(en, sport) or en
    except Exception:
        return en


def _row_team_name(r: Dict[str, Any], sport: str) -> str:
    """Korean name by ESPN's full name first (short names like "Atlético" or
    "Miami" miss or fit several clubs), then by the short name, else English."""
    full = str(r.get("team_full") or "")
    if full:
        ko = _ko_team(full, sport)
        if ko != full:
            return ko
    return _ko_team(r.get("team"), sport)


def _table_html(rows: List[Dict[str, Any]], sport: str, links: Optional[Dict[str, str]] = None) -> str:
    # soccer: W-D-L, goal difference, points. NHL: W-L, points (no OTL in the
    # feed). Other US leagues: W-L and win percentage.
    soccer, hockey = sport == "soccer", sport == "hockey"
    cols = ["순위", "팀", "경기", "승"] + (["무"] if soccer else []) + ["패"]
    cols += ["득실", "승점"] if soccer else (["승점"] if hockey else ["승률"])
    head = "".join((f"<th>{c}</th>" if c in ("순위", "팀") else f'<th class="num">{c}</th>') for c in cols)
    trs = []
    for r in rows:
        vals = [r.get("gp"), r.get("w")] + ([r.get("d")] if soccer else []) + [r.get("l")]
        vals += [r.get("gd"), r.get("_pts")] if soccer else ([r.get("_pts")] if hockey else [r.get("pts_label")])
        tds = "".join(f'<td class="num">{_e(v)}</td>' for v in vals)
        team_cell = _e(_row_team_name(r, sport))
        href = ((links or {}).get(str(r.get("team_full") or "")) or (links or {}).get(str(r.get("team") or ""))
                or (links or {}).get(str(r.get("team_abbr") or "")))
        if href:
            team_cell = f'<a href="{_e(href)}">{team_cell}</a>'
        trs.append(f'<tr><td>{_e(r.get("rank"))}</td><td>{team_cell}</td>{tds}</tr>')
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(trs)}</tbody></table>"


def _stream_note(slug: str, name: str) -> str:
    """One nofollow line to jjangdoltv: the league page once it exists, else its home page."""
    try:
        from app import sportiq_jjangdol_links as jl
        href, rel = jl.url_for(slug), jl.REL
    except Exception:
        return ""
    return (
        f'<p class="muted">{_e(name)} 경기 실시간 중계는 '
        f'<a href="{_e(href)}" target="_blank" rel="{_e(rel)}">짱돌TV</a>에서 볼 수 있습니다.</p>'
    )


def build_standings_league_html(slug: str) -> Optional[str]:
    if slug not in STANDINGS_LEAGUES:
        return None
    code, name = STANDINGS_LEAGUES[slug]
    try:
        res = _standings(code)
    except Exception:
        res = {"unavailable": True}
    rows = [r for r in (res.get("standings") or []) if isinstance(r, dict)]
    sport = str(res.get("sport") or "")
    real = bool(rows) and res.get("is_sample") is False and not res.get("unavailable")
    links: Dict[str, str] = {}
    if real:
        try:
            from app import sportiq_team_pages as tp
            if slug in tp.TEAM_LEAGUES:
                links = {k: f"/team/{slug}/{v}" for k, v in tp.team_slug_lookup(code).items()}
        except Exception:
            links = {}
    if real:
        groups = list(dict.fromkeys(r.get("group") for r in rows if r.get("group")))
        if groups:
            body = "".join(
                f"<h2>{_e(g)}</h2>" + _table_html([r for r in rows if r.get("group") == g], sport, links)
                for g in groups
            )
            basis = "컨퍼런스 안 승점 순" if code == "NHL" else "컨퍼런스 안 승률 순"
        else:
            body = _table_html(rows, sport, links)
            basis = "승점 순" if sport == "soccer" else "승률 순"
        leader = _row_team_name(rows[0], sport) if not groups else " · ".join(
            f"{g} 1위 {_row_team_name(next((r for r in rows if r.get('group') == g), {}), sport)}" for g in groups
        )
        intro = (
            f'<p class="muted">{_e(res.get("updated_at_kst"))} 기준 · {_e(basis)} · 출처 {_e(res.get("source") or "ESPN")}. '
            f"현재 1위: {_e(leader)}.</p>"
        )
    else:
        body = '<p class="note">지금은 이 리그의 실제 순위를 불러오지 못했습니다. 잠시 후 다시 확인해 주세요.</p>'
        intro = ""
    others = "".join(
        f'<li><a href="/standings/{s}">{_e(n)}</a></li>' for s, (_c, n) in STANDINGS_LEAGUES.items() if s != slug
    )
    title = f"{name} 순위표 · 실시간 리그 순위 | SportIQ"
    desc = f"{name} 현재 순위표: 팀별 경기 수, 승·무·패, 득실차, 승점(승률)을 실시간으로 확인하세요."
    ld = None
    if real:
        ld = {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": f"{name} 순위표",
            "url": f"{SITE}/standings/{slug}",
            "inLanguage": "ko-KR",
            "dateModified": time.strftime("%Y-%m-%d"),
        }
    page = _page(
        f"/standings/{slug}",
        title,
        desc,
        f"{name} 순위표",
        intro + body + _stream_note(slug, name) + f"<h2>다른 리그 순위표</h2><ul class=\"grid\">{others}</ul>",
        ld,
    )
    # No real table (source failed, example data, or no source at all, as for
    # K League 1, which ESPN does not cover): keep it out of the index. The
    # page is rebuilt every minute, so it is indexable again once data returns.
    return page if real else set_robots(page, NOINDEX)


def build_standings_index_html() -> str:
    items = "".join(f'<li><a href="/standings/{s}">{_e(n)} 순위표</a></li>' for s, (_c, n) in STANDINGS_LEAGUES.items())
    return _page(
        "/standings",
        "리그 순위표 · EPL·K리그·NBA·MLB 실시간 순위 | SportIQ",
        "프리미어리그, 라리가, 분데스리가, K리그, 챔피언스리그, NBA, MLB, NHL, NFL 등 주요 리그의 현재 순위표를 한곳에서 보세요.",
        "리그 순위표",
        '<p class="muted">리그를 고르면 현재 순위표를 볼 수 있습니다.</p>'
        f'<h2>축구·농구·야구·하키·미식축구</h2><ul class="grid">{items}</ul>',
        ld={"@context": "https://schema.org", "@type": "CollectionPage", "name": "리그 순위표 · EPL·K리그·NBA·MLB 실시간 순위 | SportIQ", "url": f"{SITE}/standings", "inLanguage": "ko-KR"},
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def _param_guarded_response(page: str, request: Request) -> HTMLResponse:
    if request and request.query_params:
        return HTMLResponse(
            set_robots(page, NOINDEX),
            status_code=200,
            headers={"Cache-Control": "private, no-store", "X-Robots-Tag": NOINDEX},
        )
    return _response(page)


@router.api_route("/robots.txt", methods=["GET", "HEAD"])
def seo_robots_txt() -> Response:
    txt = (
        "User-agent: *\nAllow: /\nAllow: /og-cover.png\nAllow: /rss.xml\n"
        "Allow: /sitemap.xml\nAllow: /sitemap-pages.xml\nAllow: /sitemap-teams.xml\n"
        "Allow: /pwa-icon-192.png\nAllow: /pwa-icon-512.png\nAllow: /apple-touch-icon.png\n"
        "Allow: /manifest.webmanifest\nAllow: /api/member/p0p1-upgrade-bundle\nAllow: /api/member/p0p1-upgrade.js\n"
        "Disallow: /api/admin/\nDisallow: /api/presence/\nDisallow: /api/datacenter/\n"
        "Disallow: /api/pro-lab/\nDisallow: /api/auto-analyst/\nDisallow: /api/member/scoreboard\n"
        "Disallow: /api/member/auto-shorts\nDisallow: /api/member/match-h2h\n"
        "Disallow: /api/member/live-sources-diag\nDisallow: /api/wp/member/verify\n"
        "Disallow: /api/wp/member/reward\nDisallow: /api/member/sync\n"
        f"Sitemap: {SITE}/sitemap.xml\n"
    )
    return Response(content=txt, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})


          
@router.api_route("/livescore", methods=["GET", "HEAD"], response_class=HTMLResponse)
def seo_livescore_page(request: Request):
    return _param_guarded_response(_cached("livescore", build_livescore_html), request)


@router.api_route("/standings", methods=["GET", "HEAD"], response_class=HTMLResponse)
def seo_standings_index_page(request: Request):
    return _param_guarded_response(_cached("standings", build_standings_index_html), request)


@router.api_route("/standings/{league}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def seo_standings_league_page(league: str, request: Request):
    slug = (league or "").strip().lower()
    if slug not in STANDINGS_LEAGUES:
        page = _page(
            request.url.path,
            "순위표를 찾을 수 없습니다 | SportIQ",
            SHORT_DESC,
            "순위표를 찾을 수 없습니다",
            '<p><a href="/standings">리그 순위표 목록</a>으로 이동해 주세요.</p>',
        )
        return HTMLResponse(set_robots(page, NOINDEX), status_code=404, headers={"X-Robots-Tag": NOINDEX})
    page = _cached(f"standings:{slug}", lambda: build_standings_league_html(slug) or "")
    if request and request.query_params:
        return _param_guarded_response(page, request)
    resp = _response(page)
    if f'content="{NOINDEX}"' in page:
        resp.headers["X-Robots-Tag"] = NOINDEX
    return resp


def standings_has_data(slug: str) -> bool:
    """True when /standings/<slug> shows a real table (same test as the page)."""
    if slug not in STANDINGS_LEAGUES:
        return False
    try:
        res = _standings(STANDINGS_LEAGUES[slug][0])
    except Exception:
        return False
    rows = [r for r in (res.get("standings") or []) if isinstance(r, dict)]
    return bool(rows) and res.get("is_sample") is False and not res.get("unavailable")


def _sitemap_entries(kind: str) -> List[Tuple[str, str, str, str]]:
    """(path, lastmod date, changefreq, priority) for one sub-sitemap."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    now_kst = _dt.now(_tz(_td(hours=9)))
    today = now_kst.date().isoformat()
    hour_iso = now_kst.strftime("%Y-%m-%dT%H:00:00+09:00")
    if kind == "pages":
        out = [("/", hour_iso, "hourly", "1.0")]
        for p, f, pr in PUBLIC_PATHS:
            if p.startswith("/standings/") and not standings_has_data(p.rsplit("/", 1)[1]):
                continue          # noindex page: not in the sitemap
            out.append((p, hour_iso if p == "/livescore" else today, f, pr))
        for post_id in PUBLIC_REPORT_TITLES:
            if public_report(post_id):
                out.append((f"/?post={post_id}", "", "monthly", "0.5"))
        return out
    if kind == "teams":
        try:
            from app import sportiq_team_pages as tp
            return [(p, today, "daily", "0.6") for p, _lg in tp.sitemap_team_urls()]
        except Exception:
            return []
    if kind == "results":
        try:
            from app import sportiq_results_pages as rp
            rp.warm_window_async()          # fills missing past days in the background
            out = []
            for d in rp.sitemap_result_days():
                nxt = (_dt.fromisoformat(d) + _td(days=1)).date().isoformat()   # final the day after
                out.append((f"/livescore/{d}", nxt, "monthly", "0.5"))
            return out
        except Exception:
            return []
    return []


# "results" (/livescore/{date}) is left out while those pages are noindex
# (sportiq_results_pages.INDEX_DATE_PAGES); /sitemap-results.xml stays as an empty set.
SITEMAP_KINDS = ("pages", "teams")


def sitemap_url_blocks(now_iso: str) -> List[str]:
    """All URLs except home in one list (kept for the old single sitemap builder)."""
    return [
        "  <url>\n"
        f"    <loc>{SITE}{p}</loc>\n"
        + (f"    <lastmod>{lm}</lastmod>\n" if lm else "")
        + f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{prio}</priority>\n"
        "  </url>"
        for kind in SITEMAP_KINDS
        for p, lm, freq, prio in _sitemap_entries(kind)
        if p != "/"
    ]


def _xml(body: str) -> Response:
    return Response(content='<?xml version="1.0" encoding="UTF-8"?>\n' + body, media_type="application/xml; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=600"})


@router.api_route("/sitemap.xml", methods=["GET", "HEAD"])
def sitemap_index():
    """Sitemap index: pages (home, live scores, standings) and teams."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now_kst = _dt.now(_tz(_td(hours=9)))
    today = now_kst.date().isoformat()
    hour_iso = now_kst.strftime("%Y-%m-%dT%H:00:00+09:00")
    items = "".join(
        f"  <sitemap>\n    <loc>{SITE}/sitemap-{k}.xml</loc>\n    <lastmod>{hour_iso if k == "pages" else today}</lastmod>\n  </sitemap>\n"
        for k in SITEMAP_KINDS
    )
    return _xml(f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{items}</sitemapindex>\n')


def _urlset(kind: str) -> Response:
    rows = []
    for p, lm, freq, prio in _sitemap_entries(kind):
        img = ""
        if p == "/":
            img = ("    <image:image>\n      <image:loc>" + SITE + "/og-cover.png?v=28</image:loc>\n"
                   "      <image:title>SportIQ 라이브스코어·리그 순위표·승부예측 커뮤니티</image:title>\n    </image:image>\n")
        modified = f"    <lastmod>{lm}</lastmod>\n" if lm else ""
        rows.append(f"  <url>\n    <loc>{SITE}{p}</loc>\n{modified}"
                    f"    <changefreq>{freq}</changefreq>\n    <priority>{prio}</priority>\n{img}  </url>\n")
    return _xml('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">\n' + "".join(rows) + "</urlset>\n")


@router.api_route("/sitemap-pages.xml", methods=["GET", "HEAD"])
def sitemap_pages():
    return _urlset("pages")


@router.api_route("/sitemap-teams.xml", methods=["GET", "HEAD"])
def sitemap_teams():
    return _urlset("teams")


@router.api_route("/sitemap-results.xml", methods=["GET", "HEAD"])
def sitemap_results():
    return _urlset("results")


HOME_LINKS_ID = "sqSeoSiteLinks"


def home_links_html() -> str:
    """Plain links for the home page's initial HTML.

    The home screen is drawn by JS, so crawlers saw almost no links. This is a
    visible footer block with public reports, live scores, league tables and
    links to the last 7 days of results on ZynkTV."""
    reports = _cached(
        "home:public-reports",
        lambda: "".join(
            f'<li><a href="/?post={post_id}">{_e(title.partition("] ")[2] or title.strip("[]").split(" ", 1)[-1])}</a></li>'
            for post_id, title in PUBLIC_REPORT_TITLES.items()
            if public_report(post_id)
        ),
    )
    leagues = "".join(f'<li><a href="/standings/{s}">{_e(n)} 순위</a></li>' for s, (_c, n) in STANDINGS_LEAGUES.items())
    try:
        from app import sportiq_results_pages as rp
        # Date search belongs to zynktv (our /livescore/{date} pages are noindex),
        # so the home footer sends crawlers and people to zynktv's page for that day.
        days = "".join(
            f'<li><a href="{rp.ZYNKTV}/schedule/{d.isoformat()}">{d.month}월 {d.day}일 결과</a></li>'
            for d in rp.window()[:7]
        )
    except Exception:
        days = ""
    css = (
        f"#{HOME_LINKS_ID}{{max-width:80rem;margin:24px auto 0;padding:16px;border-top:1px solid #2c3038;"
        "font-size:13px;color:#c4c9d1}"
        f"#{HOME_LINKS_ID} h2{{font-size:13px;font-weight:700;color:#f3f5f7;margin:10px 0 6px}}"
        f"#{HOME_LINKS_ID} ul{{display:flex;flex-wrap:wrap;gap:6px 14px;list-style:none;margin:0;padding:0}}"
        f"#{HOME_LINKS_ID} a{{color:#c4c9d1;text-decoration:none}}#{HOME_LINKS_ID} a:hover{{color:#ff6372}}"
    )
    return (
        f'<style>{css}</style><nav id="{HOME_LINKS_ID}" aria-label="SportIQ 바로가기">'
        + (f"<h2>공개 경기 분석과 리포트</h2><ul>{reports}</ul>" if reports else "")
        + '<h2>라이브스코어·순위표</h2><ul>'
        '<li><a href="/livescore">오늘 라이브스코어</a></li><li><a href="/standings">리그 순위표 전체</a></li>'
        f"{leagues}</ul>"
        + (f"<h2>지난 경기 결과</h2><ul>{days}</ul>" if days else "")
        + "</nav>"
    )

def _clean_home_head(html_text: str) -> str:
    if not html_text or "</head>" not in html_text:
        return html_text
    head_end = html_text.find("</head>")
    head = html_text[:head_end]
    tail = html_text[head_end:]
    robots_pat = re.compile(r'[ \t]*<meta\s+name=["\']robots["\'][^>]*>\s*\n?', re.I)
    m_rob = robots_pat.search(head)
    m_desc = re.search(r'<meta\s+name=["\']description["\'][^>]*>', head, re.I)
    if m_rob and m_desc:
        rob_tag = m_rob.group(0).strip()
        head = robots_pat.sub("", head)
        head = re.sub(
            r'(<meta\s+name=["\']description["\'][^>]*>)',
            lambda m: f"{m.group(1)}\n  {rob_tag}",
            head,
            count=1,
            flags=re.I,
        )

    def _strip_search_action(m: re.Match) -> str:
        prefix, raw_json, suffix = m.group(1), m.group(2), m.group(3)
        try:
            data = json.loads(raw_json)
        except Exception:
            return m.group(0)
        changed = False
        if isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                for node in graph:
                    if isinstance(node, dict) and node.get("@type") == "WebSite" and "potentialAction" in node:
                        node.pop("potentialAction", None)
                        changed = True
            elif data.get("@type") == "WebSite" and "potentialAction" in data:
                data.pop("potentialAction", None)
                changed = True
        if not changed:
            return m.group(0)
        return f"{prefix}{json.dumps(data, ensure_ascii=False)}{suffix}"

    out = re.sub(
        r'(<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>)(.*?)(</script>)',
        _strip_search_action,
        head + tail,
        flags=re.I | re.S,
    )
    return out



def inject_home_links(html_text: str) -> str:
    if not html_text or HOME_LINKS_ID in html_text or "</body>" not in html_text:
        return html_text
    html_text = _clean_home_head(html_text)
    i = html_text.rfind("</body>")
    return html_text[:i] + home_links_html() + "\n" + html_text[i:]


def rss_items(pub_rfc_xml: str) -> List[str]:
    """Feed items for the public pages (pub_rfc_xml is already XML-escaped)."""
    pages = [("/livescore", "오늘 라이브스코어 · 실시간 스포츠 스코어", "오늘 경기의 실시간 스코어와 경기 상태를 리그별로 모았습니다.")]
    pages += [
        (f"/standings/{slug}", f"{name} 순위표", f"{name} 현재 순위표: 경기 수, 승·무·패, 득실차, 승점(승률).")
        for slug, (_c, name) in STANDINGS_LEAGUES.items()
        if standings_has_data(slug)          # no noindex pages in the feed
    ]
    return [
        "    <item>\n"
        f"      <title>{_e(t)}</title>\n"
        f"      <link>{SITE}{p}</link>\n"
        f'      <guid isPermaLink="true">{SITE}{p}</guid>\n'
        f"      <description>{_e(d)}</description>\n"
        f"      <pubDate>{pub_rfc_xml}</pubDate>\n"
        "    </item>"
        for p, t, d in pages
    ]


class ForwardedProtoMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            for k, v in scope.get("headers") or []:
                if k == b"x-forwarded-proto":
                    proto = v.decode("latin-1").split(",")[0].strip().lower()
                    if proto in ("http", "https"):
                        ws = scope["type"] == "websocket"
                        scope = dict(scope, scheme=("wss" if proto == "https" else "ws") if ws else proto)
                    break

        is_http = scope.get("type") == "http"
        is_https = is_http and scope.get("scheme") == "https"
        qs_bytes = scope.get("query_string") or b""
        need_root_no_idx = False
        if is_http and scope.get("method") in ("GET", "HEAD") and scope.get("path") == "/" and qs_bytes:
            qs_map = parse_qs(qs_bytes.decode("latin-1", errors="ignore"), keep_blank_values=True)
            if not (qs_map.get("post") and any(v.strip() for v in qs_map["post"])):
                need_root_no_idx = True

        if is_https or need_root_no_idx:
            start_msg: Dict[str, Any] = {}
            chunks: List[bytes] = []
            buf_html = False

            async def _send(message):
                nonlocal buf_html
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers") or [])
                    if is_https and not any(k.lower() == b"strict-transport-security" for k, _v in headers):
                        headers.append((b"strict-transport-security", HSTS_VALUE.encode("latin-1")))
                    if need_root_no_idx:
                        ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"").lower()
                        enc = next((v for k, v in headers if k.lower() == b"content-encoding"), b"")
                        if not any(k.lower() == b"x-robots-tag" for k, _v in headers):
                            headers.append((b"x-robots-tag", NOINDEX.encode("latin-1")))
                        if ctype.startswith(b"text/html") and not enc and scope.get("method") == "GET":
                            buf_html = True
                            start_msg.update(message)
                            start_msg["headers"] = headers
                            return
                    message = dict(message, headers=headers)
                elif message["type"] == "http.response.body" and buf_html:
                    chunks.append(message.get("body", b""))
                    if message.get("more_body"):
                        return
                    raw_body = b"".join(chunks)
                    try:
                        patched = set_robots(raw_body.decode("utf-8"), NOINDEX).encode("utf-8")
                    except UnicodeDecodeError:
                        patched = raw_body
                    hdrs = [(k, v) for k, v in (start_msg.get("headers") or []) if k.lower() != b"content-length"]
                    hdrs.append((b"content-length", str(len(patched)).encode("latin-1")))
                    await send(dict(start_msg, headers=hdrs))
                    await send({"type": "http.response.body", "body": patched, "more_body": False})
                    return
                await send(message)

            await self.app(scope, receive, _send)
            return
        await self.app(scope, receive, send)


HSTS_VALUE = "max-age=31536000"
