"""BillNoJay (billnojay.com) 3줄 퀵 픽 SSR 라우터 및 DiscussionForumPosting JSON-LD 모듈."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

SITE = "https://billnojay.com"
KST = timezone(timedelta(hours=9))
NOINDEX = "noindex, follow"
INDEX_ROBOTS = "index, follow, max-snippet:-1, max-image-preview:large"
MIN_INDEXABLE_CHARS = 45
GA4_ID = "G-WL9F5N7VC6"

PICK_CHOICES: Dict[str, str] = {"HOME": "홈승", "DRAW": "무승부", "AWAY": "원정승", "OVER": "오버", "UNDER": "언더"}
ALLOWED_LEAGUES: Dict[str, str] = {
    "EPL": "프리미어리그(EPL)", "KLEAGUE": "K리그1", "LALIGA": "라리가", "BUNDESLIGA": "분데스리가",
    "SERIEA": "세리에 A", "LIGUE1": "리그 1", "UCL": "UEFA 챔피언스리그", "NBA": "NBA",
    "MLB": "MLB", "NHL": "NHL", "NFL": "NFL", "VLEAGUE": "V-리그", "KBL": "KBL·WKBL",
}
FALLBACK_MATCH_CHIPS: List[Tuple[str, str]] = [
    ("KLEAGUE", "FC 서울 vs 울산 HD"), ("EPL", "아스널 vs 첼시"),
    ("LALIGA", "레알 마드리드 vs 바르셀로나"), ("NBA", "보스턴 셀틱스 vs LA 레이커스"),
]

router = APIRouter(tags=["quick-picks-seo"])
_DB_LOCK = threading.Lock()
_DB_READY = False
_RATE_LOCK = threading.Lock()
_WRITE_BUCKETS: Dict[str, deque] = defaultdict(deque)
_e = lambda v: html.escape(str(v if v is not None else ""), quote=True)  # noqa: E731


def _pg_engine():
    if os.environ.get("SPORTIQ_FORCE_SQLITE_PICKS") == "1":
        return None
    try:
        from app.db import engine as _eng  # type: ignore
        if _eng is not None and str(getattr(_eng, "url", "")).startswith("postgresql"):
            return _eng
    except Exception:
        pass
    return None


@contextmanager
def _db_tx():
    eng = _pg_engine()
    if eng is not None:
        from sqlalchemy import text
        with eng.begin() as conn:
            yield ("pg", conn, text)
    else:
        conn = sqlite3.connect(os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db"), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield ("sq", conn, None)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _q_all(ctx, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    mode, conn, text_fn = ctx
    p = params or {}
    if mode == "pg":
        return [dict(r) for r in conn.execute(text_fn(sql), p).mappings().all()]
    return [dict(r) for r in conn.execute(sql, p).fetchall()]


def _q_one(ctx, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    rows = _q_all(ctx, sql, params)
    return rows[0] if rows else None


def _q_exec(ctx, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
    mode, conn, text_fn = ctx
    p = params or {}
    if mode == "pg":
        conn.execute(text_fn(sql), p)
    else:
        conn.execute(sql, p)


def _ensure_tables() -> None:
    global _DB_READY
    if _DB_READY:
        return
    with _DB_LOCK:
        if _DB_READY:
            return
        with _db_tx() as ctx:
            if ctx[0] == "pg":
                _q_exec(ctx, """CREATE TABLE IF NOT EXISTS quick_picks (
                    id BIGSERIAL PRIMARY KEY, league VARCHAR(24) NOT NULL, match_label VARCHAR(160) NOT NULL,
                    pick_code VARCHAR(16) NOT NULL, confidence SMALLINT NOT NULL DEFAULT 4, headline VARCHAR(200) NOT NULL,
                    line1_reason VARCHAR(240) NOT NULL, line2_factor VARCHAR(240) NOT NULL, line3_pick VARCHAR(240) NOT NULL,
                    author_name VARCHAR(48) NOT NULL, author_slug VARCHAR(96) NOT NULL, pin_hash VARCHAR(128) NOT NULL,
                    likes_count INTEGER NOT NULL DEFAULT 0, comments_count INTEGER NOT NULL DEFAULT 0,
                    views_count INTEGER NOT NULL DEFAULT 0, status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ)""")
                _q_exec(ctx, """CREATE TABLE IF NOT EXISTS quick_pick_comments (
                    id BIGSERIAL PRIMARY KEY, pick_id BIGINT NOT NULL REFERENCES quick_picks(id) ON DELETE CASCADE,
                    author_name VARCHAR(48) NOT NULL, author_slug VARCHAR(96) NOT NULL, body VARCHAR(500) NOT NULL,
                    pin_hash VARCHAR(128) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
                _q_exec(ctx, """CREATE TABLE IF NOT EXISTS quick_pick_likes (
                    pick_id BIGINT NOT NULL REFERENCES quick_picks(id) ON DELETE CASCADE,
                    voter_hash VARCHAR(64) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (pick_id, voter_hash))""")
            else:
                ctx[1].executescript("""
                    CREATE TABLE IF NOT EXISTS quick_picks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, league TEXT NOT NULL, match_label TEXT NOT NULL,
                        pick_code TEXT NOT NULL, confidence INTEGER NOT NULL DEFAULT 4, headline TEXT NOT NULL,
                        line1_reason TEXT NOT NULL, line2_factor TEXT NOT NULL, line3_pick TEXT NOT NULL,
                        author_name TEXT NOT NULL, author_slug TEXT NOT NULL, pin_hash TEXT NOT NULL,
                        likes_count INTEGER NOT NULL DEFAULT 0, comments_count INTEGER NOT NULL DEFAULT 0,
                        views_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL, updated_at TEXT);
                    CREATE TABLE IF NOT EXISTS quick_pick_comments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, pick_id INTEGER NOT NULL, author_name TEXT NOT NULL,
                        author_slug TEXT NOT NULL, body TEXT NOT NULL, pin_hash TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS quick_pick_likes (
                        pick_id INTEGER NOT NULL, voter_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                        PRIMARY KEY (pick_id, voter_hash));
                """)
            _q_exec(ctx, "CREATE INDEX IF NOT EXISTS idx_quick_picks_status_id ON quick_picks(status, id DESC)")
            _q_exec(ctx, "CREATE INDEX IF NOT EXISTS idx_quick_picks_author_slug ON quick_picks(author_slug)")
            _q_exec(ctx, "CREATE INDEX IF NOT EXISTS idx_quick_pick_comments_pick ON quick_pick_comments(pick_id, id ASC)")
        _DB_READY = True


def _now_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _to_kst_iso(val: Any) -> str:
    if isinstance(val, datetime):
        return (val.astimezone(KST) if val.tzinfo else val.replace(tzinfo=timezone.utc).astimezone(KST)).isoformat(timespec="seconds")
    s = str(val or "").strip()
    if not s:
        return _now_kst_iso()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt.astimezone(KST) if dt.tzinfo else dt.replace(tzinfo=KST)).isoformat(timespec="seconds")
    except Exception:
        return s


def _fmt_kst_human(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(KST)
        return f"{dt.year}.{dt.month:02d}.{dt.day:02d} ({['월','화','수','목','금','토','일'][dt.weekday()]}) {dt.hour:02d}:{dt.minute:02d} KST"
    except Exception:
        return iso_str


def _hash_pin(pin: str) -> str:
    salt = os.environ.get("SPORTIQ_PIN_SALT", "billnojay-quick-pick-salt-v1").encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", pin.strip().encode("utf-8"), salt, 100_000).hex()


def _verify_pin(pin: str, expected_hex: str) -> bool:
    return bool(pin and expected_hex) and hmac.compare_digest(_hash_pin(pin), expected_hex)


def _author_slug(nickname: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z가-힣_-]", "", re.sub(r"\s+", "-", nickname.strip()))[:32].strip("-")
    return (cleaned or ("member-" + hashlib.sha1(nickname.encode("utf-8")).hexdigest()[:8])).lower()


def _client_ip(request: Request) -> str:
    try:
        from app.sportiq_client_ip import client_ip as _cip  # type: ignore
        return str(_cip(request))
    except Exception:
        xff = request.headers.get("x-forwarded-for", "")
        return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "0.0.0.0")


def _check_rate_limit(request: Request, action: str) -> None:
    now_m, key = time.monotonic(), f"{action}:{_client_ip(request)}"
    with _RATE_LOCK:
        dq = _WRITE_BUCKETS[key]
        while dq and (now_m - dq[0]) > 60.0:
            dq.popleft()
        if len(dq) >= 6:
            raise HTTPException(status_code=429, detail="요청이 너무 빠릅니다. 잠시 후 다시 시도해 주세요.")
        dq.append(now_m)


def _clean_line(s: str, max_len: int = 220) -> str:
    return re.sub(r"\s{2,}", " ", re.sub(r"[\r\n\t]+", " ", str(s or "")).strip())[:max_len]


def full_pick_text(row: Dict[str, Any]) -> str:
    return f"1. 핵심 근거: {row['line1_reason']}\n2. 변수 체크: {row['line2_factor']}\n3. 최종 승부픽: {row['line3_pick']}"


def is_indexable_pick(row: Dict[str, Any]) -> bool:
    if not row or row.get("status") != "active":
        return False
    return len(re.sub(r"\s+", "", f"{row.get('line1_reason','')}{row.get('line2_factor','')}{row.get('line3_pick','')}")) >= MIN_INDEXABLE_CHARS


def _norm_row(d: Dict[str, Any]) -> Dict[str, Any]:
    d["created_at"] = _to_kst_iso(d.get("created_at"))
    d["updated_at"] = _to_kst_iso(d["updated_at"]) if d.get("updated_at") else None
    return d


def _today_match_chips() -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    try:
        from app.sportiq_datacenter import get_live_scores  # type: ignore
        res = get_live_scores(sport="all", league="all", date_offset=0)
        lg_map = {"epl": "EPL", "kleague": "KLEAGUE", "laliga": "LALIGA", "bundesliga": "BUNDESLIGA", "seriea": "SERIEA", "ligue1": "LIGUE1", "ucl": "UCL", "nba": "NBA", "mlb": "MLB", "nhl": "NHL", "nfl": "NFL", "vleague": "VLEAGUE", "kbl": "KBL"}
        seen = set()
        for m in (res.get("matches") or res.get("items") or []):
            if not isinstance(m, dict) or m.get("is_sample"):
                continue
            h, a = str(m.get("home") or m.get("home_team") or "").strip(), str(m.get("away") or m.get("away_team") or "").strip()
            lg_c = lg_map.get(str(m.get("league_key") or m.get("league") or "").strip().lower(), "EPL")
            if h and a and f"{h} vs {a}" not in seen:
                seen.add(f"{h} vs {a}")
                out.append((lg_c, f"{h} vs {a}"))
            if len(out) >= 5:
                break
    except Exception:
        pass
    return out or FALLBACK_MATCH_CHIPS


def create_pick_record(data: Dict[str, Any]) -> int:
    _ensure_tables()
    payload = {**data, "created_at": _now_kst_iso()}
    with _db_tx() as ctx:
        row = _q_one(ctx, """INSERT INTO quick_picks
            (league,match_label,pick_code,confidence,headline,line1_reason,line2_factor,line3_pick,author_name,author_slug,pin_hash,created_at)
            VALUES (:league,:match_label,:pick_code,:confidence,:headline,:line1_reason,:line2_factor,:line3_pick,:author_name,:author_slug,:pin_hash,:created_at)
            RETURNING id""", payload)
        return int(row["id"])  # type: ignore[index]


def list_recent_picks(league: str = "", author_slug: str = "", limit: int = 30) -> List[Dict[str, Any]]:
    _ensure_tables()
    lg, slug = league.strip().upper(), author_slug.strip().lower()
    wh, params = ["status = 'active'"], {"lim": limit}
    if lg and lg in ALLOWED_LEAGUES:
        wh.append("league = :lg")
        params["lg"] = lg
    if slug:
        wh.append("author_slug = :slug")
        params["slug"] = slug
    with _db_tx() as ctx:
        return [_norm_row(r) for r in _q_all(ctx, "SELECT * FROM quick_picks WHERE " + " AND ".join(wh) + " ORDER BY id DESC LIMIT :lim", params)]


def get_pick_with_comments(pick_id: int, increment_view: bool = False) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], int]:
    _ensure_tables()
    with _db_tx() as ctx:
        if increment_view:
            _q_exec(ctx, "UPDATE quick_picks SET views_count = views_count + 1 WHERE id = :id AND status = 'active'", {"id": pick_id})
        row = _q_one(ctx, "SELECT * FROM quick_picks WHERE id = :id", {"id": pick_id})
        if not row:
            return None, [], 0
        pick = _norm_row(row)
        c_rows = _q_all(ctx, "SELECT * FROM quick_pick_comments WHERE pick_id = :id ORDER BY id ASC", {"id": pick_id})
        cnt_row = _q_one(ctx, "SELECT COUNT(*) AS cnt FROM quick_picks WHERE author_slug = :s AND status = 'active'", {"s": pick["author_slug"]})
        return pick, [{**cr, "created_at": _to_kst_iso(cr["created_at"])} for cr in c_rows], int((cnt_row or {}).get("cnt") or 1)


def add_comment_record(pick_id: int, author_name: str, body: str, pin: str) -> Dict[str, Any]:
    _ensure_tables()
    slug, ph, now_iso = _author_slug(author_name), _hash_pin(pin), _now_kst_iso()
    with _db_tx() as ctx:
        if not _q_one(ctx, "SELECT id FROM quick_picks WHERE id = :id AND status = 'active'", {"id": pick_id}):
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")
        row = _q_one(ctx, """INSERT INTO quick_pick_comments (pick_id,author_name,author_slug,body,pin_hash,created_at)
            VALUES (:pid,:an,:as,:bd,:ph,:ca) RETURNING id,created_at""", {"pid": pick_id, "an": author_name, "as": slug, "bd": body, "ph": ph, "ca": now_iso})
        _q_exec(ctx, "UPDATE quick_picks SET comments_count = comments_count + 1 WHERE id = :id", {"id": pick_id})
        return {"id": int(row["id"]), "author_name": author_name, "author_slug": slug, "body": body, "created_at": _to_kst_iso(row["created_at"])}  # type: ignore[index]


def toggle_like_record(pick_id: int, voter_hash: str) -> Tuple[bool, int]:
    _ensure_tables()
    with _db_tx() as ctx:
        row = _q_one(ctx, "SELECT likes_count FROM quick_picks WHERE id = :id AND status = 'active'", {"id": pick_id})
        if not row:
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")
        if _q_one(ctx, "SELECT 1 AS x FROM quick_pick_likes WHERE pick_id = :id AND voter_hash = :vh", {"id": pick_id, "vh": voter_hash}):
            return False, int(row["likes_count"])
        _q_exec(ctx, "INSERT INTO quick_pick_likes (pick_id,voter_hash,created_at) VALUES (:id,:vh,:ca)", {"id": pick_id, "vh": voter_hash, "ca": _now_kst_iso()})
        upd = _q_one(ctx, "UPDATE quick_picks SET likes_count = likes_count + 1 WHERE id = :id RETURNING likes_count", {"id": pick_id})
        return True, int((upd or {}).get("likes_count") or 0)


def soft_delete_pick_record(pick_id: int, pin: str) -> None:
    _ensure_tables()
    with _db_tx() as ctx:
        row = _q_one(ctx, "SELECT pin_hash, status FROM quick_picks WHERE id = :id", {"id": pick_id})
        if not row or row["status"] != "active":
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다.")
        if not _verify_pin(pin, str(row["pin_hash"])):
            raise HTTPException(status_code=403, detail="4자리 PIN 번호가 일치하지 않습니다.")
        _q_exec(ctx, "UPDATE quick_picks SET status = 'deleted', updated_at = :ua WHERE id = :id", {"ua": _now_kst_iso(), "id": pick_id})


def build_discussion_forum_jsonld(pick: Dict[str, Any], comments: List[Dict[str, Any]], author_post_count: int = 1) -> Dict[str, Any]:
    canonical_url = f"{SITE}/picks/{pick['id']}"
    author_url = f"{SITE}/picks/author/{urllib.parse.quote(str(pick['author_slug']), safe='')}"
    ld: Dict[str, Any] = {
        "@context": "https://schema.org", "@type": "DiscussionForumPosting", "@id": f"{canonical_url}#post",
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical_url}, "url": canonical_url,
        "headline": pick["headline"], "text": full_pick_text(pick), "inLanguage": "ko-KR",
        "articleSection": ALLOWED_LEAGUES.get(pick["league"], pick["league"]),
        "isPartOf": {"@type": "WebPage", "name": "SportIQ 3줄 퀵 픽 라운지", "url": f"{SITE}/picks"},
        "author": {"@type": "Person", "name": pick["author_name"], "url": author_url,
                   "agentInteractionStatistic": {"@type": "InteractionCounter", "interactionType": "https://schema.org/WriteAction", "userInteractionCount": max(1, int(author_post_count))}},
        "datePublished": pick["created_at"], "commentCount": len(comments),
        "interactionStatistic": [
            {"@type": "InteractionCounter", "interactionType": "https://schema.org/LikeAction", "userInteractionCount": int(pick.get("likes_count") or 0)},
            {"@type": "InteractionCounter", "interactionType": "https://schema.org/CommentAction", "userInteractionCount": len(comments)},
            {"@type": "InteractionCounter", "interactionType": "https://schema.org/ViewAction", "userInteractionCount": int(pick.get("views_count") or 0)},
        ],
    }
    if pick.get("updated_at") and pick["updated_at"] != pick["created_at"]:
        ld["dateModified"] = pick["updated_at"]
    if pick.get("status") == "deleted":
        ld["creativeWorkStatus"] = "Deleted"
    if comments:
        ld["comment"] = [{
            "@type": "Comment", "url": f"{canonical_url}#comment-{c['id']}", "text": c["body"], "datePublished": c["created_at"],
            "author": {"@type": "Person", "name": c["author_name"], "url": f"{SITE}/picks/author/{urllib.parse.quote(str(c['author_slug']), safe='')}"},
        } for c in comments]
    return ld


def _breadcrumbs_ld(items: List[Tuple[str, str]]) -> Dict[str, Any]:
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [{"@type": "ListItem", "position": i, "name": n, "item": u} for i, (n, u) in enumerate(items, start=1)]}


def _serialize_ld(obj: Dict[str, Any]) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/ld+json">{raw}</script>'


_SSR_CSS = (
    ":root{--bg:#1a1d23;--panel:#22262e;--panel2:#282d37;--border:#343a46;--txt:#f6f8fb;--sub:#d2dae6;--muted:#aeb8c7;--accent:#539bf5;--accent-dk:#2563eb;--gold:#ffd166;--ok:#22c55e}"
    "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:'Pretendard Variable',Pretendard,'Inter',system-ui,-apple-system,sans-serif;font-weight:450;letter-spacing:-0.014em;line-height:1.62;word-break:keep-all;font-synthesis:none}@media (-webkit-min-device-pixel-ratio:2),(min-resolution:192dpi){body{-webkit-font-smoothing:antialiased}}.meta-row,.badge,.meter-bar{font-variant-numeric:tabular-nums}"
    "a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.wrap{max-width:920px;margin:0 auto;padding:16px}"
    ".top-nav{border-bottom:1px solid var(--border);background:rgba(26,29,35,.94);position:sticky;top:0;z-index:30;backdrop-filter:blur(10px)}"
    ".top-nav .inner{max-width:920px;margin:0 auto;padding:12px 16px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px}"
    ".brand{font-weight:800;font-size:1.05rem;color:var(--txt);display:flex;align-items:center;gap:8px}.brand-dot{width:10px;height:10px;border-radius:999px;background:var(--accent);display:inline-block}"
    ".nav-links{display:flex;flex-wrap:wrap;gap:14px;font-size:.9rem;font-weight:600}.nav-links a{color:var(--sub)}.nav-links a.active,.nav-links a:hover{color:var(--accent);text-decoration:none}"
    ".card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:16px}h1{font-size:1.45rem;margin:4px 0 8px;line-height:1.3}h2{font-size:1.1rem;margin:0 0 12px;color:var(--gold)}.sub{color:var(--sub);font-size:.9rem;margin:0}"
    ".badge{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:999px;font-size:.78rem;font-weight:700;background:var(--panel2);border:1px solid var(--border);color:var(--gold)}.badge.pick{background:rgba(83,155,245,.16);border-color:rgba(83,155,245,.45);color:#8ec0ff}"
    ".grid-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:640px){.grid-2{grid-template-columns:1fr}}label.lbl{display:block;font-size:.82rem;font-weight:700;color:var(--sub);margin-bottom:5px}"
    "input.inp,select.inp,textarea.inp{width:100%;background:#161920;color:var(--txt);border:1px solid var(--border);border-radius:10px;padding:10px 12px;font-size:.92rem;font-family:inherit;outline:none}input.inp:focus,select.inp:focus,textarea.inp:focus{border-color:var(--accent)}"
    ".pick-pills{display:flex;flex-wrap:wrap;gap:6px}.pick-pill{flex:1 1 80px;padding:9px 10px;border-radius:10px;border:1px solid var(--border);background:#161920;color:var(--sub);font-weight:700;font-size:.86rem;cursor:pointer;text-align:center}.pick-pill.active{background:var(--accent);border-color:var(--accent);color:#14161b}"
    ".match-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}.match-chip{padding:5px 10px;border-radius:999px;background:#161920;border:1px solid var(--border);color:var(--sub);font-size:.78rem;font-weight:700;cursor:pointer}.match-chip:hover{border-color:var(--accent);color:#8ec0ff}"
    ".line-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}.line-num{flex:0 0 auto;width:82px;font-size:.78rem;font-weight:800;padding:7px 8px;border-radius:8px;background:var(--panel2);color:var(--gold);text-align:center;border:1px solid var(--border)}"
    ".meter-bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;padding:9px 12px;border-radius:10px;background:#161920;border:1px solid var(--border);font-size:.82rem;margin:10px 0 12px}.meter-ok{color:var(--ok);font-weight:700}.meter-warn{color:var(--gold);font-weight:700}"
    ".btn-primary{background:var(--accent);color:#14161b;border:none;border-radius:10px;padding:11px 18px;font-size:.94rem;font-weight:800;cursor:pointer}.btn-primary:hover{background:var(--accent-dk);color:#fff}.btn-ghost{background:var(--panel2);color:var(--sub);border:1px solid var(--border);border-radius:9px;padding:7px 12px;font-size:.84rem;font-weight:700;cursor:pointer}"
    ".pick-lines{list-style:none;padding:0;margin:12px 0;display:grid;gap:8px}.pick-lines li{background:#171a21;border-left:3px solid var(--accent);padding:10px 12px;border-radius:8px;font-size:.94rem}.pick-lines li strong{color:var(--gold);margin-right:6px}"
    ".meta-row{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;font-size:.82rem;color:var(--muted);margin-top:10px}.filter-strip{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}.filter-chip{padding:6px 11px;border-radius:999px;background:var(--panel);border:1px solid var(--border);color:var(--sub);font-size:.82rem;font-weight:700}.filter-chip.active{border-color:var(--accent);color:#8ec0ff;background:rgba(83,155,245,.12)}"
    ".hp-trap{position:absolute;left:-9999px;opacity:0;pointer-events:none}.toast-msg{margin-top:10px;font-size:.86rem;font-weight:700}"
    ".mc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}.mc{background:#171a21;border:1px solid var(--border);border-radius:12px;padding:12px 13px;display:flex;flex-direction:column;gap:7px}"
    ".mc-top{display:flex;justify-content:space-between;gap:8px;font-size:.76rem;color:var(--muted);font-variant-numeric:tabular-nums}.mc-teams{font-weight:800;font-size:.97rem;line-height:1.35}"
    ".sis-links{display:flex;flex-wrap:wrap;gap:6px}.sis-links a{padding:4px 9px;border-radius:8px;background:var(--panel2);border:1px solid var(--border);color:var(--sub);font-size:.78rem;font-weight:700}.sis-links a:hover{border-color:var(--accent);color:#8ec0ff;text-decoration:none}"
    ".mc .btn-ghost{margin-top:auto;align-self:flex-start}"
)

_GA4_SNIPPET = (
    f'<script async src="https://www.googletagmanager.com/gtag/js?id={GA4_ID}"></script>'
    "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
    "if(location.hostname!=='localhost'&&location.hostname!=='127.0.0.1'){"
    f"gtag('js',new Date());gtag('config','{GA4_ID}',{{allow_google_signals:false,allow_ad_personalization_signals:false}});}}</script>"
)


def _render_ssr_shell(*, canonical_path: str, title: str, description: str, robots: str, ld_blocks: List[Dict[str, Any]], body_html: str, og_type: str = "website") -> str:
    canonical_url = f"{SITE}{canonical_path}"
    ld_scripts = "\n  ".join(_serialize_ld(b) for b in ld_blocks)
    gsc_token = os.environ.get("GOOGLE_SITE_VERIFICATION", "5y6CyxixambA3NZ-cMgYxaAci0L-0c8ksCl9H-nPVUk").strip()
    gsc_meta = f'<meta name="google-site-verification" content="{_e(gsc_token)}" />' if gsc_token else ""
    return (
        f'<!DOCTYPE html>\n<html lang="ko">\n<head>\n  <meta charset="UTF-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n  {_GA4_SNIPPET}\n'
        f'  <title>{_e(title)}</title>\n  <meta name="description" content="{_e(description)}" />\n  <meta name="robots" content="{_e(robots)}" />\n  {gsc_meta}\n'
        f'  <link rel="canonical" href="{_e(canonical_url)}" />\n  <meta property="og:type" content="{_e(og_type)}" />\n  <meta property="og:site_name" content="SportIQ" />\n'
        f'  <meta property="og:locale" content="ko_KR" />\n  <meta property="og:title" content="{_e(title)}" />\n  <meta property="og:description" content="{_e(description)}" />\n'
        f'  <meta property="og:url" content="{_e(canonical_url)}" />\n  <meta property="og:image" content="{SITE}/og-cover.png?v=28" />\n  <meta name="twitter:card" content="summary_large_image" />\n'
        f'  <meta name="twitter:title" content="{_e(title)}" />\n  <meta name="twitter:description" content="{_e(description)}" />\n  <meta name="twitter:image" content="{SITE}/og-cover.png?v=28" />\n'
        f'  <meta name="theme-color" content="#539bf5" />\n  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />  <link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin />  <link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css" />\n  <style>{_SSR_CSS}</style>\n  {ld_scripts}\n</head>\n<body>\n'
        '  <header class="top-nav"><div class="inner"><a class="brand" href="/"><span class="brand-dot"></span>SportIQ</a><nav class="nav-links" aria-label="주요 메뉴">'
        '<a href="/">홈</a><a href="/livescore">라이브스코어</a><a href="/standings">리그 순위표</a><a class="active" href="/picks">3줄 퀵 픽</a></nav></div></header>\n'
        f'  <main class="wrap">{body_html}</main>\n'
        '  <footer class="wrap" style="border-top:1px solid #343a46;color:#aeb8c7;font-size:.82rem;padding-bottom:32px">'
        'SportIQ 3줄 퀵 픽 커뮤니티 · 4자리 PIN은 단방향 해시(PBKDF2)로만 저장되며 개인정보를 수집하지 않습니다. <a href="/">홈</a> · <a href="/livescore">라이브스코어</a> · <a href="/standings">리그 순위표</a></footer>\n</body>\n</html>\n'
    )


def _composer_widget_html(default_league: str = "EPL") -> str:
    lg_opts = "".join(f'<option value="{c}"{" selected" if c == default_league else ""}>{_e(lbl)}</option>' for c, lbl in ALLOWED_LEAGUES.items())
    pick_btns = "".join(f'<button type="button" class="pick-pill{" active" if i == 0 else ""}" data-pick-code="{c}">{_e(lbl)}</button>' for i, (c, lbl) in enumerate(PICK_CHOICES.items()))
    m_chips = "".join(f'<button type="button" class="match-chip" data-lg="{_e(lg)}" data-match="{_e(m)}">⚡ {_e(m)}</button>' for lg, m in _today_match_chips())
    return (
        '<section class="card" aria-labelledby="qpComposerTitle"><h2 id="qpComposerTitle">⚡ 3줄 퀵 픽 바로 쓰기</h2>'
        '<p class="sub" style="margin-bottom:10px">핵심 근거·변수·최종 승부픽 3줄만 적으면 영구 고유 URL로 등록되며, 충실한 글(45자 이상)은 구글 토론 포럼 리치 결과 형식으로 자동 색인됩니다.</p>'
        f'<div class="match-chips" id="qpQuickMatches" aria-label="추천 경기 빠른 선택">{m_chips}</div>'
        '<form id="qpWriteForm" novalidate><input type="text" name="website" class="hp-trap" tabindex="-1" autocomplete="off" />'
        f'<div class="grid-2" style="margin-bottom:10px"><div><label class="lbl" for="qpLeague">리그 선택</label><select id="qpLeague" class="inp" required>{lg_opts}</select></div>'
        '<div><label class="lbl" for="qpMatch">대상 매치업 (직접 입력 또는 위 버튼 클릭)</label><input id="qpMatch" class="inp" type="text" maxlength="120" placeholder="예: 아스널 vs 첼시 / FC 서울 vs 울산 HD" required /></div></div>'
        f'<div class="grid-2" style="margin-bottom:12px"><div><span class="lbl">승부 방향 선택</span><div class="pick-pills" id="qpPickGroup">{pick_btns}</div><input type="hidden" id="qpPickCode" value="HOME" /></div>'
        '<div><label class="lbl" for="qpConf">확신도 (★1~★5)</label><select id="qpConf" class="inp"><option value="5">★★★★★ (강력 추천 5성)</option><option value="4" selected>★★★★☆ (주력 픽 4성)</option><option value="3">★★★☆☆ (보통 3성)</option><option value="2">★★☆☆☆ (이변 노림수 2성)</option><option value="1">★☆☆☆☆ (가벼운 관전픽 1성)</option></select></div></div>'
        '<div style="margin-bottom:10px"><label class="lbl" for="qpHeadline">한 줄 제목 (비워두면 매치업+픽으로 자동 생성)</label><input id="qpHeadline" class="inp" type="text" maxlength="160" placeholder="예: [EPL] 아스널 홈 압박 우위, 2-1 홈승 봅니다" /></div>'
        '<div class="line-row"><span class="line-num">① 핵심근거</span><input id="qpLine1" class="inp" type="text" maxlength="200" placeholder="최근 5경기 흐름, 공수 지표, 맞대결 상성 한 줄 요약" required /></div>'
        '<div class="line-row"><span class="line-num">② 변수체크</span><input id="qpLine2" class="inp" type="text" maxlength="200" placeholder="주전 결장, 연전 체력 부담, 홈/원정 환경 변수" required /></div>'
        '<div class="line-row"><span class="line-num">③ 최종픽</span><input id="qpLine3" class="inp" type="text" maxlength="200" placeholder="최종 선택 방향과 예상 스코어 결론" required /></div>'
        f'<div class="meter-bar" id="qpSeoMeter"><span>검색 색인 품질 게이지: <strong id="qpCharCount">0</strong> / {MIN_INDEXABLE_CHARS}자</span><span id="qpSeoBadge" class="meter-warn">45자 이상 작성 시 Google 색인 + 사이트맵 자동 등재</span></div>'
        '<div class="grid-2" style="margin-bottom:12px"><div><label class="lbl" for="qpAuthor">닉네임 (2~20자)</label><input id="qpAuthor" class="inp" type="text" maxlength="20" placeholder="예: 북런던전술가" required /></div>'
        '<div><label class="lbl" for="qpPin">삭제용 4자리 숫자 PIN</label><input id="qpPin" class="inp" type="password" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" placeholder="숫자 4자리 (예: 1234)" required /></div></div>'
        '<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px"><span class="sub" style="font-size:.8rem">가입 없이 즉시 등록 · 영구 고유 URL 생성</span><button type="submit" class="btn-primary" id="qpSubmitBtn">3줄 퀵 픽 등록하기 →</button></div>'
        '<div id="qpFormMsg" class="toast-msg" role="status" aria-live="polite"></div></form></section>'
        '<script>(function(){var qm=document.getElementById("qpQuickMatches"),lgSel=document.getElementById("qpLeague"),mInp=document.getElementById("qpMatch");'
        'if(qm){qm.addEventListener("click",function(e){var b=e.target.closest("[data-match]");if(!b)return;if(lgSel&&b.getAttribute("data-lg"))lgSel.value=b.getAttribute("data-lg");if(mInp){mInp.value=b.getAttribute("data-match");mInp.focus();}});}'
        'var group=document.getElementById("qpPickGroup"),codeInp=document.getElementById("qpPickCode");if(group&&codeInp){group.addEventListener("click",function(e){var btn=e.target.closest("[data-pick-code]");if(!btn)return;codeInp.value=btn.getAttribute("data-pick-code");Array.prototype.forEach.call(group.querySelectorAll(".pick-pill"),function(b){b.classList.toggle("active",b===btn);});});}'
        f'var l1=document.getElementById("qpLine1"),l2=document.getElementById("qpLine2"),l3=document.getElementById("qpLine3"),cntEl=document.getElementById("qpCharCount"),badgeEl=document.getElementById("qpSeoBadge");function syncMeter(){{var raw=((l1&&l1.value)||"")+((l2&&l2.value)||"")+((l3&&l3.value)||"");var n=raw.replace(/\\s+/g,"").length;if(cntEl)cntEl.textContent=String(n);if(badgeEl){{if(n>={MIN_INDEXABLE_CHARS}){{badgeEl.className="meter-ok";badgeEl.textContent="✓ Google 색인(index, follow) 및 사이트맵 자동 등재 충족";}}else{{badgeEl.className="meter-warn";badgeEl.textContent=({MIN_INDEXABLE_CHARS}-n)+"자 더 입력하면 검색 색인 대상이 됩니다";}}}}}}'
        '[l1,l2,l3].forEach(function(el){if(el)el.addEventListener("input",syncMeter);});var form=document.getElementById("qpWriteForm"),msg=document.getElementById("qpFormMsg");'
        'if(form){form.addEventListener("submit",function(e){e.preventDefault();var btn=document.getElementById("qpSubmitBtn");if(btn)btn.disabled=true;fetch("/api/picks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({league:lgSel.value,match_label:mInp.value.trim(),pick_code:codeInp.value,confidence:parseInt(document.getElementById("qpConf").value||"4",10),headline:document.getElementById("qpHeadline").value.trim(),line1_reason:l1.value.trim(),line2_factor:l2.value.trim(),line3_pick:l3.value.trim(),author_name:document.getElementById("qpAuthor").value.trim(),pin:document.getElementById("qpPin").value.trim(),website:form.querySelector("input[name=website]").value})}).then(function(r){return r.json().then(function(d){return {ok:r.ok,data:d};});}).then(function(res){if(btn)btn.disabled=false;if(!res.ok){msg.style.color="#da8b93";msg.textContent=(res.data&&res.data.detail)||"입력 항목을 다시 확인해 주세요.";return;}msg.style.color="#22c55e";msg.textContent="등록 완료! 고유 페이지로 이동합니다...";window.location.href=res.data.url;}).catch(function(){if(btn)btn.disabled=false;msg.style.color="#da8b93";msg.textContent="네트워크 오류가 발생했습니다.";});});}})();</script>'
    )


def _pick_card_html(p: Dict[str, Any]) -> str:
    lg_name, pick_label = ALLOWED_LEAGUES.get(p["league"], p["league"]), PICK_CHOICES.get(p["pick_code"], p["pick_code"])
    author_href = f"/picks/author/{urllib.parse.quote(str(p['author_slug']), safe='')}"
    return (
        f'<article class="card"><div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px"><span class="badge">{_e(lg_name)}</span><span class="badge">{_e(p["match_label"])}</span><span class="badge pick">픽: {_e(pick_label)}</span><span class="badge">{"★" * int(p.get("confidence") or 4)}</span></div>'
        f'<h2 style="margin-bottom:8px"><a href="/picks/{p["id"]}" style="color:#f6f8fb">{_e(p["headline"])}</a></h2>'
        f'<ul class="pick-lines"><li><strong>① 핵심근거</strong>{_e(p["line1_reason"])}</li><li><strong>② 변수체크</strong>{_e(p["line2_factor"])}</li><li><strong>③ 최종픽</strong>{_e(p["line3_pick"])}</li></ul>'
        f'<div class="meta-row"><span>작성자 <a href="{_e(author_href)}">{_e(p["author_name"])}</a> · {_e(_fmt_kst_human(p["created_at"]))}</span><span>공감 {int(p.get("likes_count") or 0)} · 댓글 {int(p.get("comments_count") or 0)} · 조회 {int(p.get("views_count") or 0)}</span></div></article>'
    )


_SISTER_LABELS = (("ergrow", "분석 읽기 · 어그로"), ("zynktv", "기록 보기 · ZYNK"), ("gyeonggipan", "시청 가이드 · 경기판"))
_WEEKDAYS = "월화수목금토일"


def _sister_links_html(links: Dict[str, Optional[str]]) -> str:
    return "".join(f'<a href="{_e(links[k])}" target="_blank" rel="noopener">{_e(lbl)}</a>' for k, lbl in _SISTER_LABELS if links.get(k))


def _kickoff_label(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(KST)
    except (TypeError, ValueError):
        return ""
    day = {0: "오늘", 1: "내일", 2: "모레"}.get((dt.date() - datetime.now(KST).date()).days, f"{dt.month}/{dt.day}")
    return f"{day}({_WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def _match_cards_html(active_lg: str = "") -> str:
    """Upcoming matches with an ergrow preview (sportiq_sister_links.upcoming_match_cards): links to
    the analysis, record and watch guide of the same match, and a button that fills the composer."""
    try:
        from app.sportiq_sister_links import upcoming_match_cards
        cards = upcoming_match_cards(limit=24)
    except Exception:
        cards = []
    if active_lg:
        cards = [c for c in cards if c.get("pick_league") == active_lg]
    cards = cards[:8]
    if not cards:
        return ""
    items = []
    for c in cards:
        write = (f'<button type="button" class="btn-ghost" data-write-lg="{_e(c["pick_league"])}" data-write-match="{_e(c["match_label"])}">✍️ 이 경기 3줄 픽 쓰기</button>'
                 if c.get("pick_league") in ALLOWED_LEAGUES else "")
        items.append(f'<div class="mc"><div class="mc-top"><span>{_e(c.get("league") or "")}</span><span>{_e(_kickoff_label(c["kickoff"]))}</span></div>'
                     f'<div class="mc-teams">{_e(c["home"])} <span style="color:var(--muted);font-weight:600">vs</span> {_e(c["away"])}</div>'
                     f'<div class="sis-links">{_sister_links_html(c["links"])}</div>{write}</div>')
    grid = "".join(items)
    return ('<section class="card" aria-labelledby="qpMatchCardsTitle"><h2 id="qpMatchCardsTitle">📅 다가오는 경기 · 분석·기록·시청 가이드</h2>'
            '<p class="sub" style="margin-bottom:12px">어그로의 경기 분석, ZYNK의 전적·기록, 경기판의 중계·시청 정보까지 한 번에 확인하고 나만의 3줄 픽을 남겨 보세요.</p>'
            f'<div class="mc-grid" id="qpMatchCards">{grid}</div></section>'
            '<script>(function(){var g=document.getElementById("qpMatchCards");if(!g)return;g.addEventListener("click",function(e){var b=e.target.closest("[data-write-match]");if(!b)return;'
            'var lg=document.getElementById("qpLeague"),m=document.getElementById("qpMatch"),f=document.getElementById("qpWriteForm");if(lg)lg.value=b.getAttribute("data-write-lg");if(m)m.value=b.getAttribute("data-write-match");'
            'if(f)f.scrollIntoView({behavior:"smooth",block:"start"});var l1=document.getElementById("qpLine1");if(l1)setTimeout(function(){l1.focus({preventScroll:true});},350);});})();</script>')


def _pick_sister_html(pick: Dict[str, Any]) -> str:
    """"이 경기 더 보기" under a pick: the same match on ergrow / zynktv / 경기판, when it is found."""
    try:
        from app.sportiq_sister_links import links_for_pick
        created = datetime.fromisoformat(str(pick.get("created_at") or ""))
        links = links_for_pick(str(pick.get("league") or ""), str(pick.get("match_label") or ""), created)
    except Exception:
        return ""
    inner = _sister_links_html(links)
    if not inner:
        return ""
    return (f'<section class="card" aria-labelledby="qpSisterTitle"><h2 id="qpSisterTitle">🔗 이 경기 더 보기</h2>'
            f'<p class="sub" style="margin-bottom:10px">{_e(pick["match_label"])} 경기의 분석, 전적·기록, 중계·시청 정보를 이어서 확인해 보세요.</p><div class="sis-links">{inner}</div></section>')


class QuickPickCreateIn(BaseModel):
    league: str
    match_label: str
    pick_code: str = "HOME"
    confidence: int = 4
    headline: str = ""
    line1_reason: str
    line2_factor: str
    line3_pick: str
    author_name: str
    pin: str
    website: str = ""


class QuickPickCommentIn(BaseModel):
    author_name: str
    body: str
    pin: str
    website: str = ""


class QuickPickDeleteIn(BaseModel):
    pin: str


@router.api_route("/picks", methods=["GET", "HEAD"], response_class=HTMLResponse)
def ssr_picks_hub(request: Request):
    lg_param = (request.query_params.get("league") or "").strip().upper()
    has_query, active_lg = len(request.query_params) > 0, (lg_param if lg_param in ALLOWED_LEAGUES else "")
    picks = list_recent_picks(league=active_lg, limit=25)
    chips = [f'<a class="filter-chip{" active" if not active_lg else ""}" href="/picks">전체 리그</a>'] + [f'<a class="filter-chip{" active" if active_lg == c else ""}" href="/picks?league={c}">{_e(n)}</a>' for c, n in ALLOWED_LEAGUES.items()]
    cards = "".join(_pick_card_html(p) for p in picks) or '<div class="card"><p class="sub">아직 등록된 3줄 퀵 픽이 없습니다. 첫 번째 승부예측 퀵 픽을 남겨 보세요!</p></div>'
    body = '<div style="margin-bottom:14px"><h1>승부예측 3줄 퀵 픽 라운지</h1><p class="sub">경기별 핵심 근거·변수·최종 승부픽을 3줄로 간결하게 공유하고 팬들과 의견을 나누세요.</p></div>' + _match_cards_html(active_lg) + _composer_widget_html(default_league=active_lg or "EPL") + f'<nav class="filter-strip" aria-label="리그 필터">{"".join(chips)}</nav><section aria-label="최신 3줄 퀵 픽 목록">{cards}</section>'
    crumbs = _breadcrumbs_ld([("SportIQ", f"{SITE}/"), ("3줄 퀵 픽", f"{SITE}/picks")])
    col_ld = {"@context": "https://schema.org", "@type": "CollectionPage", "name": "SportIQ 승부예측 3줄 퀵 픽 라운지", "url": f"{SITE}/picks", "inLanguage": "ko-KR", "mainEntity": {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "position": i, "url": f"{SITE}/picks/{p['id']}", "name": p["headline"]} for i, p in enumerate(picks[:15], start=1)]}}
    html_out = _render_ssr_shell(canonical_path="/picks", title="승부예측 3줄 퀵 픽 라운지 · 경기별 한줄 근거 커뮤니티 | SportIQ", description="EPL·K리그1·NBA·MLB 오늘 경기의 핵심 근거, 결장 변수, 최종 승부픽을 3줄로 공유하는 실시간 스포츠 토론 포럼입니다.", robots=NOINDEX if has_query else INDEX_ROBOTS, ld_blocks=[crumbs, col_ld], body_html=body)
    return HTMLResponse(html_out, headers={"Cache-Control": "private, no-store", "X-Robots-Tag": NOINDEX} if has_query else {"Cache-Control": "public, max-age=30"})


@router.api_route("/picks/author/{author_slug}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def ssr_author_profile_page(author_slug: str, request: Request):
    slug = urllib.parse.unquote(author_slug or "").strip().lower()
    picks, canon_path = list_recent_picks(author_slug=slug, limit=30), f"/picks/author/{urllib.parse.quote(slug, safe='')}"
    if not picks:
        html_404 = _render_ssr_shell(canonical_path=canon_path, title="작성자 프로필을 찾을 수 없습니다 | SportIQ", description="등록된 3줄 퀵 픽이 없는 작성자입니다.", robots=NOINDEX, ld_blocks=[_breadcrumbs_ld([("SportIQ", f"{SITE}/"), ("3줄 퀵 픽", f"{SITE}/picks")])], body_html='<div class="card"><h1>작성자 프로필을 찾을 수 없습니다</h1><p class="sub"><a href="/picks">3줄 퀵 픽 목록</a>으로 돌아가기</p></div>')
        return HTMLResponse(html_404, status_code=404, headers={"X-Robots-Tag": NOINDEX, "Cache-Control": "private, no-store"})
    display_name, has_query = picks[0]["author_name"], len(request.query_params) > 0
    profile_ld = {"@context": "https://schema.org", "@type": "ProfilePage", "url": f"{SITE}{canon_path}", "mainEntity": {"@type": "Person", "name": display_name, "url": f"{SITE}{canon_path}", "agentInteractionStatistic": {"@type": "InteractionCounter", "interactionType": "https://schema.org/WriteAction", "userInteractionCount": len(picks)}}}
    crumbs = _breadcrumbs_ld([("SportIQ", f"{SITE}/"), ("3줄 퀵 픽", f"{SITE}/picks"), (display_name, f"{SITE}{canon_path}")])
    body = f'<div class="card"><h1>{_e(display_name)}님의 3줄 퀵 픽 모음</h1><p class="sub">작성한 공개 퀵 픽 총 {len(picks)}건</p></div>' + "".join(_pick_card_html(p) for p in picks)
    html_out = _render_ssr_shell(canonical_path=canon_path, title=f"{display_name} · 승부예측 3줄 퀵 픽 프로필 | SportIQ", description=f"{display_name}님이 작성한 축구·농구·야구 3줄 퀵 픽 승부예측 게시글 모음입니다.", robots=NOINDEX if has_query else "index, follow, max-snippet:-1", ld_blocks=[crumbs, profile_ld], body_html=body, og_type="profile")
    return HTMLResponse(html_out, headers={"Cache-Control": "private, no-store", "X-Robots-Tag": NOINDEX} if has_query else {"Cache-Control": "public, max-age=60"})


@router.api_route("/picks/{pick_id}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def ssr_pick_detail_page(pick_id: int, request: Request):
    pick, comments, author_total = get_pick_with_comments(pick_id, increment_view=(request.method == "GET" and not request.query_params))
    canon_path = f"/picks/{pick_id}"
    if not pick or pick.get("status") != "active":
        html_404 = _render_ssr_shell(canonical_path=canon_path, title="삭제되었거나 존재하지 않는 퀵 픽입니다 | SportIQ", description="요청하신 3줄 퀵 픽 게시글을 찾을 수 없습니다.", robots=NOINDEX, ld_blocks=[_breadcrumbs_ld([("SportIQ", f"{SITE}/"), ("3줄 퀵 픽", f"{SITE}/picks")])], body_html='<div class="card"><h1>게시글을 찾을 수 없습니다</h1><p class="sub">삭제되었거나 잘못된 주소입니다. <a href="/picks">3줄 퀵 픽 목록 보기 →</a></p></div>')
        return HTMLResponse(html_404, status_code=404, headers={"X-Robots-Tag": NOINDEX, "Cache-Control": "private, no-store"})
    lg_name, pick_label = ALLOWED_LEAGUES.get(pick["league"], p["league"]) if False else ALLOWED_LEAGUES.get(pick["league"], pick["league"]), PICK_CHOICES.get(pick["pick_code"], pick["pick_code"])
    indexable = is_indexable_pick(pick) and len(request.query_params) == 0
    desc = f"[{lg_name}] {pick['match_label']} ({pick_label}) — {pick['line1_reason']} / {pick['line3_pick']}"[:155]
    author_href = f"/picks/author/{urllib.parse.quote(str(pick['author_slug']), safe='')}"
    comments_html = "".join(f'<div id="comment-{c["id"]}" style="border-top:1px solid #343a46;padding:12px 0"><div style="font-size:.82rem;color:#9aa1ac;margin-bottom:4px"><strong style="color:#f3f5f7">{_e(c["author_name"])}</strong> · {_e(_fmt_kst_human(c["created_at"]))}</div><div style="font-size:.93rem">{_e(c["body"])}</div></div>' for c in comments) or '<p class="sub" style="padding:8px 0">첫 번째 의견 댓글을 남겨 보세요.</p>'
    detail_body = (
        f'<p style="margin:0 0 10px;font-size:.86rem"><a href="/picks">← 3줄 퀵 픽 전체 목록</a></p><article class="card"><div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:10px"><span class="badge">{_e(lg_name)}</span><span class="badge">{_e(pick["match_label"])}</span><span class="badge pick">승부픽: {_e(pick_label)}</span><span class="badge">확신도 {"★" * int(pick.get("confidence") or 4)}</span></div>'
        f'<h1>{_e(pick["headline"])}</h1><div class="meta-row" style="margin:6px 0 14px"><span>작성자 <a href="{_e(author_href)}"><strong>{_e(pick["author_name"])}</strong></a> · {_e(_fmt_kst_human(pick["created_at"]))}</span><span>조회 {int(pick.get("views_count") or 0)} · 댓글 {len(comments)}</span></div>'
        f'<ul class="pick-lines"><li><strong>① 핵심근거</strong>{_e(pick["line1_reason"])}</li><li><strong>② 변수체크</strong>{_e(pick["line2_factor"])}</li><li><strong>③ 최종픽</strong>{_e(pick["line3_pick"])}</li></ul>'
        f'<div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;margin-top:16px"><button type="button" class="btn-primary" id="qpLikeBtn">👍 공감하기 (<span id="qpLikeCnt">{int(pick.get("likes_count") or 0)}</span>)</button>'
        '<div style="display:flex;align-items:center;gap:6px"><input id="qpDelPin" class="inp" type="password" inputmode="numeric" maxlength="4" placeholder="PIN 4자리" style="width:105px;padding:7px 9px;font-size:.82rem" /><button type="button" class="btn-ghost" id="qpDelBtn">글 삭제</button></div></div><div id="qpActionMsg" class="toast-msg" role="status"></div></article>'
        + _pick_sister_html(pick) +
        f'<section class="card" aria-labelledby="qpCommentsHeading"><h2 id="qpCommentsHeading">💬 매치업 토론 댓글 ({len(comments)})</h2><div id="qpCommentList">{comments_html}</div>'
        '<form id="qpCommentForm" style="margin-top:14px;border-top:1px solid #343a46;padding-top:14px" novalidate><input type="text" name="website" class="hp-trap" tabindex="-1" autocomplete="off" /><div class="grid-2" style="margin-bottom:8px"><input id="qpCmtAuthor" class="inp" type="text" maxlength="20" placeholder="닉네임 (2~20자)" required /><input id="qpCmtPin" class="inp" type="password" inputmode="numeric" maxlength="4" placeholder="PIN 4자리" required /></div>'
        '<textarea id="qpCmtBody" class="inp" rows="2" maxlength="450" placeholder="경기에 대한 추가 의견이나 반론을 남겨 주세요." required style="margin-bottom:8px"></textarea><button type="submit" class="btn-primary">댓글 등록</button><div id="qpCmtMsg" class="toast-msg" role="status"></div></form></section>'
        f'<script>(function(){{var pid={pick_id},likeBtn=document.getElementById("qpLikeBtn"),actMsg=document.getElementById("qpActionMsg");if(likeBtn){{likeBtn.addEventListener("click",function(){{fetch("/api/picks/"+pid+"/like",{{method:"POST"}}).then(function(r){{return r.json();}}).then(function(d){{if(typeof d.likes_count==="number")document.getElementById("qpLikeCnt").textContent=String(d.likes_count);actMsg.style.color=d.added?"#22c55e":"#ffd166";actMsg.textContent=d.added?"공감이 반영되었습니다!":"이미 공감한 게시글입니다.";}});}});}}'
        'var delBtn=document.getElementById("qpDelBtn");if(delBtn){delBtn.addEventListener("click",function(){var pin=(document.getElementById("qpDelPin").value||"").trim();if(!/^\\d{4}$/.test(pin)){actMsg.style.color="#ff6372";actMsg.textContent="4자리 숫자 PIN을 입력해 주세요.";return;}fetch("/api/picks/"+pid,{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({pin:pin})}).then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});}).then(function(res){if(!res.ok){actMsg.style.color="#ff6372";actMsg.textContent=res.d.detail||"삭제 실패";return;}window.location.href="/picks";});});}'
        'var cForm=document.getElementById("qpCommentForm"),cMsg=document.getElementById("qpCmtMsg");if(cForm){cForm.addEventListener("submit",function(e){e.preventDefault();fetch("/api/picks/"+pid+"/comments",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({author_name:document.getElementById("qpCmtAuthor").value.trim(),pin:document.getElementById("qpCmtPin").value.trim(),body:document.getElementById("qpCmtBody").value.trim(),website:cForm.querySelector("input[name=website]").value})}).then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});}).then(function(res){if(!res.ok){cMsg.style.color="#ff6372";cMsg.textContent=res.d.detail||"댓글 등록 실패";return;}window.location.reload();});});}})();</script>'
    )
    crumbs = _breadcrumbs_ld([("SportIQ", f"{SITE}/"), ("3줄 퀵 픽", f"{SITE}/picks"), (pick["headline"], f"{SITE}{canon_path}")])
    forum_ld = build_discussion_forum_jsonld(pick, comments, author_post_count=author_total)
    html_out = _render_ssr_shell(canonical_path=canon_path, title=f"{pick['headline']} | SportIQ 3줄 퀵 픽", description=desc, robots=INDEX_ROBOTS if indexable else NOINDEX, ld_blocks=[crumbs, forum_ld], body_html=detail_body, og_type="article")
    return HTMLResponse(html_out, headers={"Cache-Control": "public, max-age=30"} if indexable else {"Cache-Control": "private, no-store", "X-Robots-Tag": NOINDEX})


@router.get("/api/picks")
def api_list_quick_picks(league: str = "", limit: int = 25):
    rows = list_recent_picks(league=league, limit=max(1, min(60, int(limit or 25))))
    return {"ok": True, "items": [{"id": r["id"], "league": r["league"], "match_label": r["match_label"], "pick_code": r["pick_code"], "confidence": r["confidence"], "headline": r["headline"], "line1_reason": r["line1_reason"], "line2_factor": r["line2_factor"], "line3_pick": r["line3_pick"], "author_name": r["author_name"], "author_slug": r["author_slug"], "likes_count": r["likes_count"], "comments_count": r["comments_count"], "views_count": r["views_count"], "created_at": r["created_at"], "url": f"/picks/{r['id']}", "indexable": is_indexable_pick(r)} for r in rows]}


@router.post("/api/picks")
def api_create_quick_pick(payload: QuickPickCreateIn, request: Request):
    if payload.website:
        raise HTTPException(status_code=400, detail="잘못된 요청입니다.")
    _check_rate_limit(request, "create_pick")
    lg, pcode, pin_str = (payload.league or "").strip().upper(), (payload.pick_code or "HOME").strip().upper(), (payload.pin or "").strip()
    if lg not in ALLOWED_LEAGUES:
        raise HTTPException(status_code=400, detail="지원하지 않는 리그 코드입니다.")
    if pcode not in PICK_CHOICES:
        raise HTTPException(status_code=400, detail="유효하지 않은 승부픽 코드입니다.")
    if not re.match(r"^\d{4}$", pin_str):
        raise HTTPException(status_code=400, detail="삭제용 PIN은 숫자 4자리여야 합니다.")
    match_lbl, l1, l2, l3, author = _clean_line(payload.match_label, 120), _clean_line(payload.line1_reason, 220), _clean_line(payload.line2_factor, 220), _clean_line(payload.line3_pick, 220), _clean_line(payload.author_name, 24)
    if len(match_lbl) < 3 or len(author) < 2 or len(l1) < 4 or len(l2) < 4 or len(l3) < 3:
        raise HTTPException(status_code=400, detail="매치업·닉네임 및 3줄 항목을 모두 입력해 주세요.")
    conf = max(1, min(5, int(payload.confidence or 4)))
    raw_head = _clean_line(payload.headline or "", 160) or f"[{ALLOWED_LEAGUES[lg]}] {match_lbl} — {PICK_CHOICES[pcode]} 3줄 퀵 픽 ({author})"
    record = {"league": lg, "match_label": match_lbl, "pick_code": pcode, "confidence": conf, "headline": raw_head, "line1_reason": l1, "line2_factor": l2, "line3_pick": l3, "author_name": author, "author_slug": _author_slug(author), "pin_hash": _hash_pin(pin_str)}
    new_id = create_pick_record(record)
    return JSONResponse(status_code=201, content={"ok": True, "id": new_id, "url": f"/picks/{new_id}", "canonical_url": f"{SITE}/picks/{new_id}", "indexable": is_indexable_pick({**record, "status": "active"})})


@router.post("/api/picks/{pick_id}/like")
def api_like_quick_pick(pick_id: int, request: Request):
    _check_rate_limit(request, "like_pick")
    voter_hash = hashlib.sha256(f"{pick_id}:{_client_ip(request)}:{request.headers.get('user-agent','')[:120]}".encode("utf-8")).hexdigest()[:32]
    added, total = toggle_like_record(pick_id, voter_hash)
    return {"ok": True, "added": added, "likes_count": total}


@router.post("/api/picks/{pick_id}/comments")
def api_comment_quick_pick(pick_id: int, payload: QuickPickCommentIn, request: Request):
    if payload.website:
        raise HTTPException(status_code=400, detail="잘못된 요청입니다.")
    _check_rate_limit(request, "comment_pick")
    pin_str, author, body = (payload.pin or "").strip(), _clean_line(payload.author_name, 24), _clean_line(payload.body, 450)
    if not re.match(r"^\d{4}$", pin_str):
        raise HTTPException(status_code=400, detail="댓글 PIN은 숫자 4자리여야 합니다.")
    if len(author) < 2 or len(body) < 2:
        raise HTTPException(status_code=400, detail="닉네임과 댓글 내용을 입력해 주세요.")
    return JSONResponse(status_code=201, content={"ok": True, "comment": add_comment_record(pick_id, author, body, pin_str)})


@router.delete("/api/picks/{pick_id}")
def api_delete_quick_pick(pick_id: int, payload: QuickPickDeleteIn, request: Request):
    _check_rate_limit(request, "delete_pick")
    pin_str = (payload.pin or "").strip()
    if not re.match(r"^\d{4}$", pin_str):
        raise HTTPException(status_code=400, detail="4자리 숫자 PIN을 입력해 주세요.")
    soft_delete_pick_record(pick_id, pin_str)
    return {"ok": True, "deleted_id": pick_id}


def sitemap_pick_entries(limit: int = 500) -> List[Tuple[str, str, str, str]]:
    try:
        indexable_rows = [p for p in list_recent_picks(limit=limit) if is_indexable_pick(p)]
    except Exception:
        # DB down: still serve /picks itself rather than failing the whole sitemap.
        indexable_rows = []
    now_hour = datetime.now(KST).replace(minute=0, second=0, microsecond=0).isoformat()
    out: List[Tuple[str, str, str, str]] = [("/picks", now_hour, "hourly", "0.8")]
    seen_authors = set()
    for p in indexable_rows:
        lm = _to_kst_iso(p.get("updated_at") or p.get("created_at") or now_hour)
        out.append((f"/picks/{p['id']}", lm, "daily", "0.7"))
        slug = str(p.get("author_slug") or "")
        if slug and slug not in seen_authors:
            seen_authors.add(slug)
            out.append((f"/picks/author/{urllib.parse.quote(slug, safe='')}", lm, "weekly", "0.5"))
    return out


@router.api_route("/sitemap-picks.xml", methods=["GET", "HEAD"])
def serve_sitemap_picks_xml() -> Response:
    urls = "".join(f"  <url>\n    <loc>{SITE}{path}</loc>\n    <lastmod>{lm}</lastmod>\n    <changefreq>{freq}</changefreq>\n    <priority>{prio}</priority>\n  </url>\n" for path, lm, freq, prio in sitemap_pick_entries())
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n"
    return Response(content=xml, media_type="application/xml; charset=utf-8", headers={"Cache-Control": "public, max-age=300"})
