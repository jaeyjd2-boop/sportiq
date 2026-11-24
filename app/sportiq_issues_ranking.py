import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

DB_PATH = os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db")
KST = timezone(timedelta(hours=9))
_CHEER_COOLDOWN: Dict[str, float] = {}
_CHEER_COOLDOWN_SEC = 5.0

_ISSUE_RANKING_PG_TABLES = (
    "cheers",
    "cheer_hidden",
    "hot_issue_votes",
    "member_leaderboard",
    "daily_hashtags",
    "partner_inquiries",
    "issue_vote_actions",
)

try:
    import app.sportiq_auto_analyst as _auto_analyst
    from app.sportiq_auto_analyst import (
        _PG_SNAPSHOT_TABLES,
        _backup_tables_to_pg,
        _restore_snapshots_from_pg,
    )

    if isinstance(_auto_analyst._PG_SNAPSHOT_TABLES, list):
        for _tbl in _ISSUE_RANKING_PG_TABLES:
            if _tbl not in _auto_analyst._PG_SNAPSHOT_TABLES:
                _auto_analyst._PG_SNAPSHOT_TABLES.append(_tbl)
        _PG_SNAPSHOT_TABLES = _auto_analyst._PG_SNAPSHOT_TABLES
    else:
        _merged = list(_auto_analyst._PG_SNAPSHOT_TABLES)
        for _tbl in _ISSUE_RANKING_PG_TABLES:
            if _tbl not in _merged:
                _merged.append(_tbl)
        _auto_analyst._PG_SNAPSHOT_TABLES = tuple(_merged)
        _PG_SNAPSHOT_TABLES = _auto_analyst._PG_SNAPSHOT_TABLES
except Exception:
    _PG_SNAPSHOT_TABLES = ()

    def _backup_tables_to_pg(*tables: str) -> None:
        return None

    def _restore_snapshots_from_pg() -> None:
        return None


def _now_kst_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _ensure_cheers_columns(conn: sqlite3.Connection) -> set:
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(cheers)").fetchall()
    }
    if "nickname" not in cols:
        conn.execute("ALTER TABLE cheers ADD COLUMN nickname TEXT;")
        cols.add("nickname")
    if "session_id" not in cols:
        conn.execute("ALTER TABLE cheers ADD COLUMN session_id TEXT;")
        cols.add("session_id")
    if "created_at" not in cols:
        conn.execute("ALTER TABLE cheers ADD COLUMN created_at TEXT;")
        cols.add("created_at")
    return cols


def _migrate_issue_votes_from_shared_table(conn: sqlite3.Connection) -> None:
    """예전 shorts_user_actions(target_id 스키마)에 남은 이슈 투표 기록을 issue_vote_actions로 복사한다."""
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(shorts_user_actions)").fetchall()}
        if {"action_group", "target_id", "actor_key"}.issubset(cols):
            conn.execute(
                """
                INSERT OR IGNORE INTO issue_vote_actions (action_group, target_id, actor_key, choice, created_at)
                SELECT action_group, target_id, actor_key, COALESCE(choice, ''), COALESCE(created_at, '')
                FROM shorts_user_actions WHERE action_group = 'issue_vote'
                """
            )
    except sqlite3.Error:
        pass


def init_issues_ranking_db() -> None:
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cheers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname TEXT,
                team TEXT,
                message TEXT,
                session_id TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS cheer_hidden (
                cheer_id INTEGER PRIMARY KEY,
                reported_at TEXT
            );

            CREATE TABLE IF NOT EXISTS hot_issue_votes (
                issue_id TEXT PRIMARY KEY,
                opt_a_votes INTEGER DEFAULT 0,
                opt_b_votes INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS member_leaderboard (
                actor_key TEXT PRIMARY KEY,
                nickname TEXT,
                role_badge TEXT,
                points INTEGER DEFAULT 120,
                predictions_count INTEGER DEFAULT 0,
                updated_at TEXT
            );

            -- 이슈 투표 중복 방지 기록. 쇼츠 허브(sportiq_auto_analyst)의 shorts_user_actions와
            -- 같은 이름·다른 스키마로 충돌하던 문제를 피하려고 별도 표를 쓴다.
            CREATE TABLE IF NOT EXISTS issue_vote_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_group TEXT NOT NULL,
                target_id TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                choice TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(action_group, target_id, actor_key)
            );
        """)
        _ensure_cheers_columns(conn)
        _migrate_issue_votes_from_shared_table(conn)
        conn.commit()
    finally:
        conn.close()

    try:
        _restore_snapshots_from_pg()
    except Exception:
        pass

    conn = _get_conn()
    try:
        conn.executescript("""
            INSERT OR IGNORE INTO hot_issue_votes (issue_id, opt_a_votes, opt_b_votes) VALUES
                ('iss-1', 142, 98),
                ('iss-2', 118, 105),
                ('iss-3', 164, 79),
                ('iss-4', 93, 127),
                ('iss-5', 131, 84),
                ('iss-6', 109, 114),
                ('iss-7', 152, 68),
                ('iss-8', 87, 136);

            INSERT OR IGNORE INTO member_leaderboard (actor_key, nickname, role_badge, points, predictions_count, updated_at) VALUES
                ('seed-member-01', '안필드전술가', 'PRO 분석가', 485, 34, '2026-11-15 09:00:00'),
                ('seed-member-02', '도쿄돔직관러', '야구 마스터', 440, 29, '2026-11-15 09:05:00'),
                ('seed-member-03', '보스턴3점장인', 'NBA 슈터', 395, 26, '2026-11-15 09:10:00'),
                ('seed-member-04', '맥데이비드팬', 'NHL 스카우트', 360, 23, '2026-11-15 09:15:00'),
                ('seed-member-05', '라리가분석왕', '전술 코치', 325, 21, '2026-11-15 09:20:00'),
                ('seed-member-06', '세이버메트릭스', '데이터픽', 290, 19, '2026-11-15 09:25:00'),
                ('seed-member-07', '챔스올빼미', '유럽축구팬', 255, 16, '2026-11-15 09:30:00'),
                ('seed-member-08', '주말승부사', '베테랑', 215, 14, '2026-11-15 09:35:00'),
                ('seed-member-09', '데이터픽마스터', '루키 분석가', 180, 11, '2026-11-15 09:40:00'),
                ('seed-member-10', '하이패스트볼', '선발 투수', 145, 8, '2026-11-15 09:45:00');
        """)

        cheers_count = conn.execute("SELECT COUNT(*) AS cnt FROM cheers").fetchone()["cnt"]
        if cheers_count == 0:
            cols = _ensure_cheers_columns(conn)
            if "author" in cols:
                conn.executescript("""
                    INSERT INTO cheers (session_id, author, nickname, role, team, message, created_at) VALUES
                        ('seed-cheer-1', '안필드전술가', '안필드전술가', 'MEMBER', '축구 · EPL', '오늘 리버풀 중원 압박 라인 진짜 미쳤네요! 후반 추가골 기대합니다!', '2026-11-15 10:00:00'),
                        ('seed-cheer-2', '도쿄돔직관러', '도쿄돔직관러', 'MEMBER', '야구 · KBO/MLB', '9회말 투아웃 역전 적시타 한 방만 터져라! 불펜 총력전 가보자!', '2026-11-15 10:05:00'),
                        ('seed-cheer-3', '보스턴3점장인', '보스턴3점장인', 'MEMBER', '농구 · NBA', '코너 3점슛 성공률 45% 돌파! 4쿼터 클러치 타임 집중합시다!', '2026-11-15 10:10:00'),
                        ('seed-cheer-4', '맥데이비드팬', '맥데이비드팬', 'MEMBER', '하키 · NHL', '파워플레이 한 번에 분위기 뒤집었다! 3피리어드 셧아웃 가자!', '2026-11-15 10:15:00'),
                        ('seed-cheer-5', '챔스올빼미', '챔스올빼미', 'MEMBER', '축구 · UCL', '새벽 챔스 직관 중인데 양 팀 골키퍼 선방쇼가 역대급이네요!', '2026-11-15 10:20:00');
                """)
            else:
                conn.executescript("""
                    INSERT INTO cheers (nickname, team, message, session_id, created_at) VALUES
                        ('안필드전술가', '축구 · EPL', '오늘 리버풀 중원 압박 라인 진짜 미쳤네요! 후반 추가골 기대합니다!', 'seed-cheer-1', '2026-11-15 10:00:00'),
                        ('도쿄돔직관러', '야구 · KBO/MLB', '9회말 투아웃 역전 적시타 한 방만 터져라! 불펜 총력전 가보자!', 'seed-cheer-2', '2026-11-15 10:05:00'),
                        ('보스턴3점장인', '농구 · NBA', '코너 3점슛 성공률 45% 돌파! 4쿼터 클러치 타임 집중합시다!', 'seed-cheer-3', '2026-11-15 10:10:00'),
                        ('맥데이비드팬', '하키 · NHL', '파워플레이 한 번에 분위기 뒤집었다! 3피리어드 셧아웃 가자!', 'seed-cheer-4', '2026-11-15 10:15:00'),
                        ('챔스올빼미', '축구 · UCL', '새벽 챔스 직관 중인데 양 팀 골키퍼 선방쇼가 역대급이네요!', 'seed-cheer-5', '2026-11-15 10:20:00');
                """)
        conn.commit()
    finally:
        conn.close()


class IssueVoteRequest(BaseModel):
    issue_id: str
    choice: str
    actor_key: str = "guest"


class CheerSendRequest(BaseModel):
    nickname: str = Field(..., min_length=1, max_length=40)
    team: str = Field(default="ALL", max_length=40)
    message: str = Field(..., min_length=1, max_length=300)
    actor_key: str = ""


class LeaderboardSyncRequest(BaseModel):
    actor_key: str = Field(..., min_length=1, max_length=80)
    nickname: str = Field(..., min_length=1, max_length=40)
    role_badge: str = Field(default="MEMBER", max_length=40)
    points: int = 120
    predictions_count: int = 0


def _fetch_live_cheers(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    cols = _ensure_cheers_columns(conn)
    if "author" in cols:
        rows = conn.execute(
            """
            SELECT c.id,
                   COALESCE(NULLIF(c.nickname, ''), NULLIF(c.author, ''), '익명') AS nickname,
                   COALESCE(c.team, 'ALL') AS team,
                   c.message,
                   c.created_at
            FROM cheers c
            WHERE c.id NOT IN (SELECT cheer_id FROM cheer_hidden)
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT c.id,
                   COALESCE(NULLIF(c.nickname, ''), '익명') AS nickname,
                   COALESCE(c.team, 'ALL') AS team,
                   c.message,
                   c.created_at
            FROM cheers c
            WHERE c.id NOT IN (SELECT cheer_id FROM cheer_hidden)
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_normalize_cheer_row(dict(r)) for r in rows]


def _normalize_cheer_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """cheers 표는 라운지 POST /api/cheers(created_at=epoch 숫자)와 함께 쓰므로,
    프런트가 기대하는 'YYYY-MM-DD HH:MM:SS'(KST) 문자열로 맞춘다."""
    raw = item.get("created_at")
    ts: Optional[float] = None
    if isinstance(raw, (int, float)):
        ts = float(raw)
    elif isinstance(raw, str):
        try:
            ts = float(raw.strip())
        except ValueError:
            ts = None
    if ts is not None:
        if ts > 1e12:  # 밀리초 단위
            ts /= 1000.0
        try:
            item["created_at"] = datetime.fromtimestamp(ts, tz=KST).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            item["created_at"] = ""
    return item


def _fetch_leaderboard_payload(conn: sqlite3.Connection, actor_key: str = "") -> Dict[str, Any]:
    top_rows = conn.execute(
        """
        SELECT actor_key, nickname, role_badge, points, predictions_count, updated_at
        FROM member_leaderboard
        ORDER BY points DESC, predictions_count DESC, updated_at ASC
        LIMIT 15
        """
    ).fetchall()
    clean_actor = (actor_key or "").strip()
    members = []
    for idx, row in enumerate(top_rows, start=1):
        item = dict(row)
        # actor_key는 회원 식별 키이므로 공개 응답에 넣지 않는다. 프런트는 is_me로 본인 행을 표시한다.
        row_actor = item.pop("actor_key", "") or ""
        item["is_me"] = bool(clean_actor) and row_actor == clean_actor
        item["is_sample"] = row_actor.startswith("seed-")
        item["rank"] = idx
        members.append(item)

    my_rank: Optional[int] = None
    if clean_actor:
        me = conn.execute(
            "SELECT points, predictions_count, updated_at FROM member_leaderboard WHERE actor_key = ?",
            (clean_actor,),
        ).fetchone()
        if me is not None:
            higher = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM member_leaderboard
                WHERE points > ?
                   OR (points = ? AND predictions_count > ?)
                   OR (points = ? AND predictions_count = ? AND updated_at < ?)
                """,
                (
                    me["points"],
                    me["points"],
                    me["predictions_count"],
                    me["points"],
                    me["predictions_count"],
                    me["updated_at"],
                ),
            ).fetchone()["cnt"]
            my_rank = int(higher) + 1

    return {
        "status": "ok",
        "members": members,
        "my_rank": my_rank,
    }


@router.get("/api/issues/hub")
def get_issues_hub() -> Dict[str, Any]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT issue_id, opt_a_votes, opt_b_votes FROM hot_issue_votes ORDER BY issue_id ASC"
        ).fetchall()
        issues_map = {
            row["issue_id"]: {
                "opt_a_votes": int(row["opt_a_votes"] or 0),
                "opt_b_votes": int(row["opt_b_votes"] or 0),
            }
            for row in rows
        }
        # 이슈별 투표 수에는 초기 시드 기준값이 포함되어 있다.
        return {"status": "ok", "issues": issues_map, "votes_include_seed_baseline": True}
    finally:
        conn.close()


@router.post("/api/issues/vote")
def vote_hot_issue(payload: IssueVoteRequest) -> Dict[str, Any]:
    issue_id = payload.issue_id.strip()
    choice = payload.choice.strip().lower()
    actor_key = (payload.actor_key or "guest").strip() or "guest"

    if choice not in ("a", "b"):
        raise HTTPException(status_code=400, detail="choice must be 'a' or 'b'")

    conn = _get_conn()
    try:
        existing = conn.execute(
            """
            SELECT id, choice FROM issue_vote_actions
            WHERE action_group = 'issue_vote' AND target_id = ? AND actor_key = ?
            """,
            (issue_id, actor_key),
        ).fetchone()

        if existing is not None:
            row = conn.execute(
                "SELECT opt_a_votes, opt_b_votes FROM hot_issue_votes WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
            return {
                "status": "ok",
                "already_participated": True,
                "issue_id": issue_id,
                "opt_a_votes": int(row["opt_a_votes"]) if row else 0,
                "opt_b_votes": int(row["opt_b_votes"]) if row else 0,
            }

        conn.execute(
            """
            INSERT INTO issue_vote_actions (action_group, target_id, actor_key, choice, created_at)
            VALUES ('issue_vote', ?, ?, ?, ?)
            """,
            (issue_id, actor_key, choice, _now_kst_iso()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO hot_issue_votes (issue_id, opt_a_votes, opt_b_votes) VALUES (?, 0, 0)",
            (issue_id,),
        )
        if choice == "a":
            conn.execute(
                "UPDATE hot_issue_votes SET opt_a_votes = opt_a_votes + 1 WHERE issue_id = ?",
                (issue_id,),
            )
        else:
            conn.execute(
                "UPDATE hot_issue_votes SET opt_b_votes = opt_b_votes + 1 WHERE issue_id = ?",
                (issue_id,),
            )
        conn.commit()

        row = conn.execute(
            "SELECT opt_a_votes, opt_b_votes FROM hot_issue_votes WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        _backup_tables_to_pg("hot_issue_votes", "issue_vote_actions")
        return {
            "status": "ok",
            "already_participated": False,
            "issue_id": issue_id,
            "opt_a_votes": int(row["opt_a_votes"]) if row else 0,
            "opt_b_votes": int(row["opt_b_votes"]) if row else 0,
        }
    finally:
        conn.close()


@router.get("/api/cheers/live")
def get_live_cheers() -> Dict[str, Any]:
    conn = _get_conn()
    try:
        cheers_list = _fetch_live_cheers(conn, limit=50)
        return {"status": "ok", "cheers": cheers_list}
    finally:
        conn.close()


@router.post("/api/cheers/send")
def send_live_cheer(payload: CheerSendRequest, request: Request) -> Dict[str, Any]:
    from app.sportiq_client_ip import client_ip as _sportiq_client_ip
    client_ip = _sportiq_client_ip(request)
    rate_key = (payload.actor_key or "").strip() or f"ip:{client_ip}"
    now_ts = time.monotonic()
    last_ts = _CHEER_COOLDOWN.get(rate_key)
    if last_ts is not None and (now_ts - last_ts) < _CHEER_COOLDOWN_SEC:
        raise HTTPException(
            status_code=429,
            detail="도배 방지를 위해 잠시 후 다시 전송해 주세요.",
        )

    nickname = payload.nickname.strip()
    team = (payload.team or "ALL").strip() or "ALL"
    message = payload.message.strip()
    if not nickname or not message:
        raise HTTPException(status_code=400, detail="닉네임과 메시지를 입력해 주세요.")

    _CHEER_COOLDOWN[rate_key] = now_ts
    created_at = _now_kst_iso()

    conn = _get_conn()
    try:
        cols = _ensure_cheers_columns(conn)
        if "author" in cols:
            conn.execute(
                """
                INSERT INTO cheers (session_id, author, nickname, role, team, message, created_at)
                VALUES (?, ?, ?, 'MEMBER', ?, ?, ?)
                """,
                (rate_key, nickname, nickname, team, message, created_at),
            )
        else:
            conn.execute(
                """
                INSERT INTO cheers (nickname, team, message, session_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (nickname, team, message, rate_key, created_at),
            )
        conn.commit()
        cheers_list = _fetch_live_cheers(conn, limit=50)
        _backup_tables_to_pg("cheers")
        return {"status": "ok", "cheers": cheers_list}
    finally:
        conn.close()


@router.post("/api/cheers/{cheer_id}/report")
def report_cheer(cheer_id: int) -> Dict[str, Any]:
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO cheer_hidden (cheer_id, reported_at) VALUES (?, ?)",
            (cheer_id, _now_kst_iso()),
        )
        conn.commit()
        _backup_tables_to_pg("cheer_hidden", "cheers")
        return {
            "status": "ok",
            "hidden": True,
            "cheer_id": cheer_id,
            "message": "신고된 응원톡이 숨김 처리되었습니다.",
        }
    finally:
        conn.close()


@router.get("/api/leaderboard/members")
def get_member_leaderboard(actor_key: str = "") -> Dict[str, Any]:
    conn = _get_conn()
    try:
        return _fetch_leaderboard_payload(conn, actor_key=actor_key)
    finally:
        conn.close()


@router.post("/api/leaderboard/sync-me")
def sync_member_leaderboard(payload: LeaderboardSyncRequest) -> Dict[str, Any]:
    actor_key = payload.actor_key.strip()
    nickname = payload.nickname.strip()
    role_badge = (payload.role_badge or "MEMBER").strip() or "MEMBER"
    points = max(0, int(payload.points))
    predictions_count = max(0, int(payload.predictions_count))
    updated_at = _now_kst_iso()

    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO member_leaderboard (actor_key, nickname, role_badge, points, predictions_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(actor_key) DO UPDATE SET
                nickname = excluded.nickname,
                role_badge = excluded.role_badge,
                points = excluded.points,
                predictions_count = excluded.predictions_count,
                updated_at = excluded.updated_at
            """,
            (actor_key, nickname, role_badge, points, predictions_count, updated_at),
        )
        conn.commit()
        _backup_tables_to_pg("member_leaderboard")
        return _fetch_leaderboard_payload(conn, actor_key=actor_key)
    finally:
        conn.close()


init_issues_ranking_db()
