import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.sqlite_guard import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DEFAULT_TIMEOUT_SEC,
    install_sqlite_guard,
    managed_sqlite_conn,
    run_with_db_retry,
)

install_sqlite_guard()

router = APIRouter()

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "sportiq_community.html")
DB_PATH = os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=DEFAULT_TIMEOUT_SEC, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _db_session(write: bool = False):
    return managed_sqlite_conn(DB_PATH, write=write, row_factory=sqlite3.Row)


def _init_db() -> None:
    def _op() -> None:
        with _db_session(write=False) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS presence_sessions (
                    session_id TEXT PRIMARY KEY,
                    role TEXT DEFAULT 'GUEST',
                    visit_date TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_hashtags (
                    tag TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS partner_inquiries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company TEXT NOT NULL,
                    contact_name TEXT NOT NULL,
                    contact_info TEXT NOT NULL,
                    tier TEXT DEFAULT 'STANDARD',
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cheers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    role TEXT DEFAULT 'GUEST',
                    team TEXT DEFAULT 'ALL',
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS post_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL,
                    session_id TEXT DEFAULT '',
                    reason TEXT DEFAULT '부적절한 게시글',
                    created_at REAL NOT NULL
                );
CREATE TABLE IF NOT EXISTS global_match_rankings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key TEXT UNIQUE NOT NULL,
    sport TEXT NOT NULL,
    league TEXT NOT NULL,
    league_color TEXT NOT NULL,
    matchup TEXT NOT NULL,
    schedule_venue TEXT NOT NULL,
    votes INTEGER NOT NULL DEFAULT 0,
    buzz_score INTEGER NOT NULL DEFAULT 80,
    rank_change TEXT NOT NULL DEFAULT '-'
);
INSERT OR IGNORE INTO global_match_rankings(match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change) VALUES
('epl-liv-mun', 'football', 'EPL', '#38003c', '리버풀 vs 맨체스터 유나이티드', '11.22 · 안필드 노스웨스트 더비', 2840, 99, '-'), ('unl-esp-eng', 'football', '네이션스리그 A', '#003399', '스페인 vs 잉글랜드', '11.16 · 매치데이 6 빅매치', 2610, 97, '▲1'),
('ucl-ars-bvb', 'football', '챔피언스리그', '#0e1e5b', '아스널 vs 보루시아 도르트문트', '11.25 · 매치데이 5', 2390, 95, '▲2'), ('nba-bos-dal', 'basketball', 'NBA', '#c9082a', '보스턴 셀틱스 vs 댈러스 매버릭스', '11.16 · TD 가든', 2150, 93, '▼1'), ('epl-new-ars', 'football', 'EPL', '#38003c', '뉴캐슬 유나이티드 vs 아스널', '11.21 · 세인트 제임스 파크', 1980, 91, '▲1'),
('nba-phx-lal', 'basketball', 'NBA', '#c9082a', '피닉스 선즈 vs LA 레이커스', '11.16 · 서부 라이벌전', 1840, 89, '-'), ('laliga-bar-vil', 'football', '라리가', '#ee8707', 'FC 바르셀로나 vs 비야레알', '11.22 · 스포티파이 캄 노우', 1690, 87, 'NEW'), ('nfl-det-ne', 'nfl', 'NFL', '#013369', '디트로이트 라이온스 vs 뉴잉글랜드 패트리어츠', '11.15 · 뮌헨 인터내셔널 게임', 1540, 85, '▲2'),
('laliga-rma-cel', 'football', '라리가', '#ee8707', '레알 마드리드 vs 셀타 비고', '11.22 · 산티아고 베르나베우', 1420, 83, '▼1'), ('nba-den-phx', 'basketball', 'NBA', '#c9082a', '덴버 너기츠 vs 피닉스 선즈', '11.17 · 볼 아레나', 1290, 81, 'NEW');
UPDATE global_match_rankings SET votes = 2980 WHERE match_key = 'epl-liv-mun' AND votes < 2980; INSERT OR IGNORE INTO global_match_rankings(match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change) VALUES ('apbc-kor-jpn', 'baseball', 'APBC 야구 결승', '#0047a0', '대한민국 vs 일본', '11.15 · 도쿄돔 결승 한일전', 2890, 98, 'HOT'), ('nhl-fla-edm', 'hockey', 'NHL 하키', '#0284c7', '플로리다 팬서스 vs 에드먼턴 오일러스', '11.17 · 스탠리컵 리매치', 2040, 92, 'NEW'), ('nhl-chi-tor', 'hockey', 'NHL 하키', '#0284c7', '시카고 블랙호크스 vs 토론토 메이플리프스', '11.17 · 오리지널 6 매치', 1760, 88, 'NEW'), ('apbc-tpe-aus', 'baseball', 'APBC 야구', '#0047a0', '대만 vs 호주', '11.15 · 도쿄돔 3·4위 결정전', 1610, 86, 'NEW'), ('seriea-ata-int', 'football', '세리에A', '#008fd7', '아탈란타 vs 인터 밀란', '11.22 · 베르가모 상위권 격돌', 1480, 84, 'NEW'), ('nhl-nyr-mtl', 'hockey', 'NHL 하키', '#0284c7', '뉴욕 레인저스 vs 몬트리올 캐나디언스', '11.16 · 매디슨 스퀘어 가든', 1360, 82, 'NEW'), ('lidom-lic-esc', 'baseball', 'LIDOM 야구', '#0047a0', '티그레스 델 리세이 vs 에스코히도', '11.15 · 도미니카 윈터리그 더비', 1210, 79, 'NEW'), ('ucl-lil-bay', 'football', '챔피언스리그', '#0e1e5b', '릴 OSC vs 바이에른 뮌헨', '11.25 · 매치데이 5', 1150, 78, '▲1'), ('nhl-tbl-edm', 'hockey', 'NHL 하키', '#0284c7', '탬파베이 라이트닝 vs 에드먼턴 오일러스', '11.19 · 아말리 아레나', 1090, 77, 'NEW'), ('abl-ade-bri', 'baseball', 'ABL 프로야구', '#0047a0', '애들레이드 자이언츠 vs 브리즈번 밴디츠', '11.20 · 호주리그 개막전', 1020, 76, 'NEW');            """
            )
            row = conn.execute("SELECT COUNT(*) AS cnt FROM daily_hashtags").fetchone()
            if row["cnt"] == 0:
                now = time.time()
                seeds = [
                    ("#UCL결승", 142),
                    ("#손흥민", 128),
                    ("#EPL전술", 96),
                    ("#KBO리그", 84),
                    ("#NBA플레이오프", 79),
                    ("#xG데이터", 63),
                    ("#T1월즈", 58),
                ]
                conn.executemany(
                    "INSERT OR IGNORE INTO daily_hashtags(tag, count, updated_at) VALUES (?, ?, ?)",
                    [(t, c, now) for t, c in seeds],
                )
            crow = conn.execute("SELECT COUNT(*) AS cnt FROM cheers").fetchone()
            if crow["cnt"] == 0:
                now = time.time()
                cseeds = [
                    ("seed-1", "데이터헌터", "PRO", "토트넘", "오늘 후반전 하프스페이스 침투 xG 수치가 심상치 않네요!", now - 180),
                    ("seed-2", "야구덕후K", "MEMBER", "KIA", "선발 투수 릴리스 포인트 안정적이라 6이닝 무실점 기대합니다.", now - 90),
                    ("seed-3", "클러치타임", "GUEST", "NBA", "4쿼터 클러치 수비 로테이션이 승부를 가를 듯합니다!", now - 30),
                ]
                conn.executemany(
                    "INSERT INTO cheers(session_id, author, role, team, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    cseeds,
                )

    run_with_db_retry(_op)


_init_db()


class PresencePingIn(BaseModel):
    session_id: str = "anon"
    role: Optional[str] = "GUEST"


class HashtagIn(BaseModel):
    tag: str


class PartnerInquiryIn(BaseModel):
    company: str
    contact_name: str
    contact_info: str
    tier: Optional[str] = "PREMIUM_SPONSOR"
    message: str


class CheerIn(BaseModel):
    session_id: str = "anon"
    author: str = "익명팬"
    role: Optional[str] = "GUEST"
    team: Optional[str] = "ALL"
    message: str


class ReportIn(BaseModel):
    session_id: Optional[str] = ""
    reason: Optional[str] = "신고 접수"


@router.get("/", response_class=HTMLResponse)
def serve_lounge_home():
    if os.path.exists(HTML_PATH):
        return FileResponse(HTML_PATH, media_type="text/html; charset=utf-8")
    return HTMLResponse("<h1>SportIQ Lounge</h1><p>HTML file missing.</p>", status_code=200)


@router.get("/api/lounge/health")
def lounge_health():
    def _op() -> None:
        with _db_session(write=False) as conn:
            conn.execute("SELECT 1").fetchone()

    run_with_db_retry(_op)
    return {"status": "ok", "service": "sportiq-lounge", "storage": "sqlite-wal", "busy_timeout_ms": DEFAULT_BUSY_TIMEOUT_MS}


def _presence_stats(conn: sqlite3.Connection) -> dict:
    now = time.time()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    active = conn.execute(
        "SELECT COUNT(*) AS cnt FROM presence_sessions WHERE last_seen >= ?", (now - 120,)
    ).fetchone()["cnt"]
    today_cnt = conn.execute(
        "SELECT COUNT(*) AS cnt FROM presence_sessions WHERE visit_date = ?", (today,)
    ).fetchone()["cnt"]
    return {"active_now": max(active, 1), "today_visitors": max(today_cnt, 1)}


@router.post("/api/presence/ping")
def presence_ping(payload: PresencePingIn):
    now = time.time()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    sid = (payload.session_id or "anon").strip()[:64]
    role = (payload.role or "GUEST").strip()[:24]

    def _op() -> dict:
        with _db_session(write=True) as conn:
            conn.execute(
                """
                INSERT INTO presence_sessions(session_id, role, visit_date, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    role = excluded.role,
                    visit_date = excluded.visit_date,
                    last_seen = excluded.last_seen
                """,
                (sid, role, today, now, now),
            )
            return _presence_stats(conn)

    stats = run_with_db_retry(_op)
    return {"ok": True, **stats}


@router.get("/api/presence")
def get_presence():
    def _op() -> dict:
        with _db_session(write=False) as conn:
            return _presence_stats(conn)

    return run_with_db_retry(_op)


@router.get("/api/hashtags/daily")
def get_daily_hashtags():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT tag, count, updated_at FROM daily_hashtags ORDER BY count DESC, updated_at DESC LIMIT 24"
            ).fetchall()
            return [dict(r) for r in rows]

    return {"hashtags": run_with_db_retry(_op)}


@router.post("/api/hashtags/register")
def register_hashtag(payload: HashtagIn):
    raw = (payload.tag or "").strip().replace(" ", "")
    if not raw:
        raise HTTPException(status_code=400, detail="해시태그를 입력해 주세요.")
    tag = raw if raw.startswith("#") else f"#{raw}"
    if len(tag) < 2 or len(tag) > 32:
        raise HTTPException(status_code=400, detail="해시태그는 1~30자 이내로 입력해 주세요.")
    now = time.time()

    def _op() -> list[dict]:
        with _db_session(write=True) as conn:
            conn.execute(
                """
                INSERT INTO daily_hashtags(tag, count, updated_at) VALUES (?, 1, ?)
                ON CONFLICT(tag) DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
                """,
                (tag, now),
            )
            rows = conn.execute(
                "SELECT tag, count, updated_at FROM daily_hashtags ORDER BY count DESC, updated_at DESC LIMIT 24"
            ).fetchall()
            return [dict(r) for r in rows]

    hashtags = run_with_db_retry(_op)
    try:
        from app.sportiq_auto_analyst import _backup_tables_to_pg
        _backup_tables_to_pg("daily_hashtags")
    except Exception:
        pass
    return {"ok": True, "tag": tag, "hashtags": hashtags}


def _mask_contact(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if "@" in s:
        local, domain = s.split("@", 1)
        return f"{local[:2]}***@{domain}"
    if len(s) <= 6:
        return s[:2] + "*****"
    return f"{s[:3]}****{s[-3:]}"


@router.get("/api/partner-inquiries")
def list_partner_inquiries():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT id, company, contact_name, contact_info, tier, message, created_at FROM partner_inquiries ORDER BY id DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]

    rows = run_with_db_retry(_op)
    items = []
    for d in rows:
        d["contact_info"] = _mask_contact(d.get("contact_info", ""))
        items.append(d)
    return {"inquiries": items}


@router.post("/api/partner-inquiries")
def create_partner_inquiry(payload: PartnerInquiryIn):
    _c = payload.contact_info.strip()
    if not payload.company.strip() or not payload.contact_name.strip() or not payload.contact_info.strip():
        raise HTTPException(status_code=400, detail="업체명, 담당자명, 연락처를 모두 입력해 주세요.")
    if not (("@" in _c and "." in _c.split("@", 1)[-1]) or sum(ch.isdigit() for ch in _c) >= 8):
        raise HTTPException(status_code=400, detail="연락처는 유효한 이메일 또는 전화번호 형식으로 입력해 주세요.")
    now = time.time()

    def _op() -> int:
        with _db_session(write=True) as conn:
            cur = conn.execute(
                "INSERT INTO partner_inquiries(company, contact_name, contact_info, tier, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    payload.company.strip()[:80],
                    payload.contact_name.strip()[:60],
                    payload.contact_info.strip()[:100],
                    (payload.tier or "STANDARD").strip()[:40],
                    (payload.message or "").strip()[:1000],
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    new_id = run_with_db_retry(_op)
    try:
        from app.sportiq_auto_analyst import _backup_tables_to_pg
        _backup_tables_to_pg("partner_inquiries")
    except Exception:
        pass
    return {"ok": True, "id": new_id, "message": "파트너 입점 및 제휴 문의가 정상 접수되었습니다."}


@router.get("/api/cheers")
def list_cheers():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT id, author, role, team, message, created_at FROM cheers ORDER BY id DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]

    rows = run_with_db_retry(_op)
    result = []
    for r in rows:
        dt = datetime.fromtimestamp(r["created_at"], tz=KST).strftime("%H:%M:%S")
        result.append({**r, "time_label": dt})
    return {"cheers": result}


@router.post("/api/cheers")
def post_cheer(payload: CheerIn, request: Request):
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="응원 메시지를 입력해 주세요.")
    sid = (payload.session_id or (request.client.host if request.client else "anon")).strip()[:64]
    now = time.time()
    author = (payload.author or "익명팬").strip()[:30]
    role = (payload.role or "GUEST").strip()[:20]
    team = (payload.team or "ALL").strip()[:30]
    clean_msg = msg[:300]

    def _op() -> int:
        with _db_session(write=True) as conn:
            last = conn.execute(
                "SELECT created_at FROM cheers WHERE session_id = ? ORDER BY id DESC LIMIT 1", (sid,)
            ).fetchone()
            if last and (now - last["created_at"]) < 5.0:
                remain = max(1, int(round(5.0 - (now - last["created_at"]))))
                raise HTTPException(status_code=429, detail=f"도배 방지를 위해 {remain}초 후 다시 전송할 수 있습니다.")
            cur = conn.execute(
                "INSERT INTO cheers(session_id, author, role, team, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, author, role, team, clean_msg, now),
            )
            return int(cur.lastrowid or 0)

    new_id = run_with_db_retry(_op)
    dt = datetime.fromtimestamp(now, tz=KST).strftime("%H:%M:%S")
    return {
        "ok": True,
        "cheer": {
            "id": new_id,
            "author": author,
            "role": role,
            "team": team,
            "message": clean_msg,
            "created_at": now,
            "time_label": dt,
        },
    }


@router.post("/api/posts/{post_id}/report")
def report_post(post_id: str, payload: Optional[ReportIn] = None):
    now = time.time()
    sid = (payload.session_id if payload else "") or ""
    reason = (payload.reason if payload else "") or "커뮤니티 가이드라인 위반 신고"

    def _op() -> None:
        with _db_session(write=True) as conn:
            conn.execute(
                "INSERT INTO post_reports(post_id, session_id, reason, created_at) VALUES (?, ?, ?, ?)",
                (post_id[:64], sid[:64], reason[:200], now),
            )

    run_with_db_retry(_op)
    return {"ok": True, "post_id": post_id, "reported": True}


@router.get("/api/rankings/global-matches")
def get_global_matches():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT id, match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change FROM global_match_rankings ORDER BY votes DESC, buzz_score DESC, id ASC"
            ).fetchall()
            return [
                {
                    "rank": i + 1,
                    "id": r["id"],
                    "match_key": r["match_key"],
                    "sport": r["sport"],
                    "league": r["league"],
                    "league_color": r["league_color"],
                    "matchup": r["matchup"],
                    "schedule_venue": r["schedule_venue"],
                    "votes": r["votes"],
                    "buzz_score": r["buzz_score"],
                    "rank_change": r["rank_change"],
                }
                for i, r in enumerate(rows)
            ]

    return {"matches": run_with_db_retry(_op)}


@router.post("/api/rankings/global-matches/{match_id}/vote")
def vote_global_match(match_id: int):
    def _op() -> tuple[Optional[dict], list[dict]]:
        with _db_session(write=True) as conn:
            cur = conn.execute("UPDATE global_match_rankings SET votes = votes + 1 WHERE id = ?", (match_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="매치를 찾을 수 없습니다.")
            rows = conn.execute(
                "SELECT id, match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change FROM global_match_rankings ORDER BY votes DESC, buzz_score DESC, id ASC"
            ).fetchall()
            items = [
                {
                    "rank": i + 1,
                    "id": r["id"],
                    "match_key": r["match_key"],
                    "sport": r["sport"],
                    "league": r["league"],
                    "league_color": r["league_color"],
                    "matchup": r["matchup"],
                    "schedule_venue": r["schedule_venue"],
                    "votes": r["votes"],
                    "buzz_score": r["buzz_score"],
                    "rank_change": r["rank_change"],
                }
                for i, r in enumerate(rows)
            ]
            voted = next((m for m in items if m["id"] == match_id), None)
            return voted, items

    voted, items = run_with_db_retry(_op)
    try:
        from app.sportiq_auto_analyst import _backup_tables_to_pg
        _backup_tables_to_pg("global_match_rankings")
    except Exception:
        pass
    return {"ok": True, "match": voted, "matches": items}