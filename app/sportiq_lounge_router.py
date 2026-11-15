import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

router = APIRouter()

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "sportiq_community.html")
DB_PATH = os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _init_db() -> None:
    with _get_conn() as conn:
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
            """
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
    with _get_conn() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "service": "sportiq-lounge", "storage": "sqlite-wal"}


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
    with _get_conn() as conn:
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
        stats = _presence_stats(conn)
    return {"ok": True, **stats}


@router.get("/api/presence")
def get_presence():
    with _get_conn() as conn:
        stats = _presence_stats(conn)
    return stats


@router.get("/api/hashtags/daily")
def get_daily_hashtags():
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT tag, count, updated_at FROM daily_hashtags ORDER BY count DESC, updated_at DESC LIMIT 24"
        ).fetchall()
    return {"hashtags": [dict(r) for r in rows]}


@router.post("/api/hashtags/register")
def register_hashtag(payload: HashtagIn):
    raw = (payload.tag or "").strip().replace(" ", "")
    if not raw:
        raise HTTPException(status_code=400, detail="해시태그를 입력해 주세요.")
    tag = raw if raw.startswith("#") else f"#{raw}"
    if len(tag) < 2 or len(tag) > 32:
        raise HTTPException(status_code=400, detail="해시태그는 1~30자 이내로 입력해 주세요.")
    now = time.time()
    with _get_conn() as conn:
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
    return {"ok": True, "tag": tag, "hashtags": [dict(r) for r in rows]}


@router.get("/api/partner-inquiries")
def list_partner_inquiries():
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, company, contact_name, contact_info, tier, message, created_at FROM partner_inquiries ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return {"inquiries": [dict(r) for r in rows]}


@router.post("/api/partner-inquiries")
def create_partner_inquiry(payload: PartnerInquiryIn):
    if not payload.company.strip() or not payload.contact_name.strip() or not payload.contact_info.strip():
        raise HTTPException(status_code=400, detail="업체명, 담당자명, 연락처를 모두 입력해 주세요.")
    now = time.time()
    with _get_conn() as conn:
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
        new_id = cur.lastrowid
    return {"ok": True, "id": new_id, "message": "파트너 입점 및 제휴 문의가 정상 접수되었습니다."}


@router.get("/api/cheers")
def list_cheers():
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, author, role, team, message, created_at FROM cheers ORDER BY id DESC LIMIT 50"
        ).fetchall()
    result = []
    for r in rows:
        dt = datetime.fromtimestamp(r["created_at"], tz=KST).strftime("%H:%M:%S")
        result.append({**dict(r), "time_label": dt})
    return {"cheers": result}


@router.post("/api/cheers")
def post_cheer(payload: CheerIn, request: Request):
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="응원 메시지를 입력해 주세요.")
    sid = (payload.session_id or (request.client.host if request.client else "anon")).strip()[:64]
    now = time.time()
    with _get_conn() as conn:
        last = conn.execute(
            "SELECT created_at FROM cheers WHERE session_id = ? ORDER BY id DESC LIMIT 1", (sid,)
        ).fetchone()
        if last and (now - last["created_at"]) < 5.0:
            remain = max(1, int(round(5.0 - (now - last["created_at"]))))
            raise HTTPException(status_code=429, detail=f"도배 방지를 위해 {remain}초 후 다시 전송할 수 있습니다.")
        cur = conn.execute(
            "INSERT INTO cheers(session_id, author, role, team, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                sid,
                (payload.author or "익명팬").strip()[:30],
                (payload.role or "GUEST").strip()[:20],
                (payload.team or "ALL").strip()[:30],
                msg[:300],
                now,
            ),
        )
        new_id = cur.lastrowid
    dt = datetime.fromtimestamp(now, tz=KST).strftime("%H:%M:%S")
    return {
        "ok": True,
        "cheer": {
            "id": new_id,
            "author": (payload.author or "익명팬").strip()[:30],
            "role": (payload.role or "GUEST").strip()[:20],
            "team": (payload.team or "ALL").strip()[:30],
            "message": msg[:300],
            "created_at": now,
            "time_label": dt,
        },
    }


@router.post("/api/posts/{post_id}/report")
def report_post(post_id: str, payload: Optional[ReportIn] = None):
    now = time.time()
    sid = (payload.session_id if payload else "") or ""
    reason = (payload.reason if payload else "") or "커뮤니티 가이드라인 위반 신고"
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO post_reports(post_id, session_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (post_id[:64], sid[:64], reason[:200], now),
        )
    return {"ok": True, "post_id": post_id, "reported": True}
