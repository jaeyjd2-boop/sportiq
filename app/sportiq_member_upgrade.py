import email.utils
import functools
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import hashlib
import inspect
import sys
import hmac
import json
import secrets
import os
import re
import sqlite3
import struct
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.sqlite_guard import install_sqlite_guard, managed_sqlite_conn, run_with_db_retry

install_sqlite_guard()

upgrade_router = APIRouter(tags=["sportiq-member-upgrade"])

KST = timezone(timedelta(hours=9))
DB_PATH = os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db")
PIN_SALT = os.environ.get("SPORTIQ_PIN_SALT", "sportiq-pin-v1-salt")

ALL_KNOWN_SQLITE_TABLES: Tuple[str, ...] = (
    "shorts_clip_stats",
    "shorts_comments",
    "shorts_custom_clips",
    "shorts_user_actions",
    "global_match_rankings",
    "match_predictions",
    "ai_scorecard_records",
    "saved_tactics",
    "cheers",
    "daily_hashtags",
    "partner_inquiries",
    "post_reports",
    "cheer_hidden",
    "hot_issue_votes",
    "member_leaderboard",
    "datacenter_myteam_prefs",
    "datacenter_briefing_votes",
    "datacenter_briefing_authors",
    "datacenter_best_analyst_rewards",
    "auto_analyst_runs",
    "auto_analyst_posts",
    "live_prediction_ledger",
    "overseas_news_cache",
    "member_accounts",
    "community_member_posts",
    "community_post_comments",
    "member_raffle_entries",
    "admin_config",
    "raffle_draw_results",
    "wp_point_ledger",
    "member_match_predictions",
    "member_prediction_stats",
    "member_notifications",
    "community_post_blinds",
    "community_post_report_logs",
    "ai_iq_search_logs",
)

SHOP_CATALOG: List[Dict[str, Any]] = [
    {
        "item_key": "badge_odds_hunter",
        "name": "드롭오즈 헌터 칭호",
        "badge_label": "⚡ 드롭오즈 헌터",
        "badge_color": "#38bdf8",
        "cost": 80,
        "desc": "게시글·응원톡 닉네임 옆에 스카이블루 헌터 배지가 표시됩니다.",
    },
    {
        "item_key": "badge_xg_sniper",
        "name": "데이터 저격수 칭호",
        "badge_label": "🎯 데이터 저격수",
        "badge_color": "#34d399",
        "cost": 150,
        "desc": "기대득점(xG)·세이버메트릭스 분석가 전용 에메랄드 배지입니다.",
    },
    {
        "item_key": "badge_streak_tactician",
        "name": "연승 전술가 골드 칭호",
        "badge_label": "🔥 연승 전술가",
        "badge_color": "#fbbf24",
        "cost": 250,
        "desc": "커뮤니티 피드에서 눈에 띄는 골드 전술가 배지를 장착합니다.",
    },
    {
        "item_key": "badge_hall_crown",
        "name": "레전드 크라운 칭호",
        "badge_label": "👑 레전드 분석가",
        "badge_color": "#f472b6",
        "cost": 400,
        "desc": "최상위 활동 회원을 위한 로열 크라운 전용 칭호입니다.",
    },
]

_TEAM_CANON_MAP: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("newcastle", "뉴캐슬"), "newcastle"),
    (("arsenal", "아스널", "아스날"), "arsenal"),
    (("liverpool", "리버풀"), "liverpool"),
    (("manchester united", "man utd", "맨체스터 유나이티드", "맨유"), "manutd"),
    (("manchester city", "man city", "맨체스터 시티", "맨시티"), "mancity"),
    (("tottenham", "spurs", "토트넘"), "tottenham"),
    (("chelsea", "첼시"), "chelsea"),
    (("dortmund", "도르트문트"), "dortmund"),
    (("bayern", "바이에른", "뮌헨"), "bayern"),
    (("real madrid", "레알 마드리드", "레알"), "realmadrid"),
    (("celta", "셀타"), "celta"),
    (("barcelona", "바르셀로나", "바르샤"), "barcelona"),
    (("villarreal", "비야레알"), "villarreal"),
    (("atalanta", "아탈란타"), "atalanta"),
    (("inter", "인터 밀란", "인테르"), "inter"),
    (("lille", "릴 osc", "릴"), "lille"),
    (("spain", "스페인"), "spain"),
    (("england", "잉글랜드"), "england"),
    (("celtics", "셀틱스", "보스턴"), "celtics"),
    (("mavericks", "매버릭스", "댈러스"), "mavericks"),
    (("suns", "피닉스"), "suns"),
    (("lakers", "레이커스"), "lakers"),
    (("nuggets", "너기츠", "덴버"), "nuggets"),
    (("lions", "라이온스", "디트로이트"), "detroit_lions"),
    (("patriots", "패트리어츠", "뉴잉글랜드"), "patriots"),
    (("panthers", "팬서스", "플로리다"), "panthers"),
    (("oilers", "오일러스", "에드먼턴"), "oilers"),
    (("blackhawks", "블랙호크스", "시카고"), "blackhawks"),
    (("maple leafs", "메이플리프스", "토론토"), "mapleleafs"),
    (("rangers", "레인저스", "뉴욕"), "rangers"),
    (("canadiens", "캐나디언스", "몬트리올"), "canadiens"),
    (("lightning", "라이트닝", "탬파베이"), "lightning"),
    (("대한민국", "south korea", "korea"), "korea"),
    (("일본", "japan"), "japan"),
)

_PG_SYNC_STATE = {
    "initial_restore_done": False,
    "registered_tables": 0,
    "last_backup_kst": None,
}
_OG_PNG_CACHE: Optional[bytes] = None
_FAVICON_PNG_CACHE: Optional[bytes] = None
_PWA_ICON_CACHE: Dict[int, bytes] = {}


def _db_session(write: bool = False):
    return managed_sqlite_conn(DB_PATH, write=write, row_factory=sqlite3.Row)


def _hash_pin(nickname: str, pin: str) -> str:
    clean_nick = (nickname or "").strip().lower()
    clean_pin = (pin or "").strip()
    raw = f"{PIN_SALT}:{clean_nick}:{clean_pin}".encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", raw, PIN_SALT.encode("utf-8"), 12000).hex()


def _tier_for_points(points: int) -> str:
    if points >= 350:
        return "PRO"
    if points >= 150:
        return "MEMBER"
    return "GUEST"


def canonical_matchup_signature(matchup: str) -> str:
    raw = (matchup or "").strip().lower()
    if not raw:
        return ""
    parts = re.split(r"\s+vs\.?\s+|\s+대\s+", raw, maxsplit=1)
    norm_sides: List[str] = []
    for side in parts:
        s = side.strip()
        matched = None
        for aliases, canon in _TEAM_CANON_MAP:
            if any(a in s for a in aliases):
                matched = canon
                break
        norm_sides.append(matched or re.sub(r"[^a-z0-9가-힣]", "", s))
    return "|".join(sorted(norm_sides))


def _has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))


def _init_upgrade_tables() -> None:
    def _op() -> None:
        with _db_session(write=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS member_accounts (
                    nickname TEXT PRIMARY KEY,
                    pin_hash TEXT NOT NULL,
                    points INTEGER NOT NULL DEFAULT 120,
                    role_tier TEXT NOT NULL DEFAULT 'GUEST',
                    streak_days INTEGER NOT NULL DEFAULT 0,
                    last_checkin_date TEXT NOT NULL DEFAULT '',
                    active_badge TEXT NOT NULL DEFAULT '',
                    owned_items_json TEXT NOT NULL DEFAULT '[]',
                    my_teams_json TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS community_member_posts (
                    id TEXT PRIMARY KEY,
                    league TEXT NOT NULL DEFAULT 'EPL',
                    author TEXT NOT NULL,
                    author_badge TEXT NOT NULL DEFAULT '',
                    is_pro INTEGER NOT NULL DEFAULT 0,
                    trust INTEGER NOT NULL DEFAULT 89,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    likes INTEGER NOT NULL DEFAULT 1,
                    pin_hash TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS community_post_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    user_badge TEXT NOT NULL DEFAULT '',
                    comment_text TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS member_raffle_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nickname TEXT NOT NULL,
                    round_key TEXT NOT NULL,
                    tickets INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_config (
                    cfg_key TEXT PRIMARY KEY,
                    cfg_value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raffle_draw_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_key TEXT NOT NULL,
                    winner_nickname TEXT NOT NULL,
                    winner_tickets INTEGER NOT NULL,
                    total_tickets INTEGER NOT NULL,
                    total_participants INTEGER NOT NULL,
                    prize_name TEXT NOT NULL,
                    draw_seed_hash TEXT NOT NULL,
                    claim_code TEXT NOT NULL,
                    fulfillment_note TEXT NOT NULL DEFAULT '지급 대기',
                    drawn_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wp_point_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nickname TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    ref_id TEXT NOT NULL DEFAULT '',
                    points_delta INTEGER NOT NULL,
                    kst_date TEXT NOT NULL,
                    site_origin TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS member_match_predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nickname TEXT NOT NULL,
                    match_key TEXT NOT NULL,
                    matchup TEXT NOT NULL,
                    league TEXT NOT NULL DEFAULT 'EPL',
                    pick TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    reward_points INTEGER NOT NULL DEFAULT 5,
                    settled_score TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    settled_at REAL NOT NULL DEFAULT 0,
                    UNIQUE(nickname, match_key)
                );
                CREATE TABLE IF NOT EXISTS member_prediction_stats (
                    nickname TEXT PRIMARY KEY,
                    total_picks INTEGER NOT NULL DEFAULT 0,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    miss_count INTEGER NOT NULL DEFAULT 0,
                    win_streak INTEGER NOT NULL DEFAULT 0,
                    best_streak INTEGER NOT NULL DEFAULT 0,
                    total_earned_points INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS member_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nickname TEXT NOT NULL,
                    notif_type TEXT NOT NULL DEFAULT 'SYSTEM',
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    link_target TEXT NOT NULL DEFAULT '',
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS community_post_blinds (
                    post_id TEXT PRIMARY KEY,
                    report_count INTEGER NOT NULL DEFAULT 0,
                    blinded INTEGER NOT NULL DEFAULT 0,
                    last_reason TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS community_post_report_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL,
                    reporter_key TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '스팸/도배',
                    created_at REAL NOT NULL,
                    UNIQUE(post_id, reporter_key)
                );
                CREATE TABLE IF NOT EXISTS ai_iq_search_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    norm_query TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'all',
                    nickname TEXT NOT NULL DEFAULT '',
                    result_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                """
            )

    run_with_db_retry(_op)


def deduplicate_global_match_rankings() -> int:
    """Canonicalize English/Korean team pairs across all rows regardless of match_key."""
    def _op() -> int:
        removed = 0
        with _db_session(write=True) as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='global_match_rankings'"
            ).fetchone()
            if not exists:
                return 0
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, match_key, matchup, votes, buzz_score FROM global_match_rankings ORDER BY votes DESC, id ASC"
                ).fetchall()
            ]
            groups: Dict[str, List[dict]] = {}
            for r in rows:
                sig = canonical_matchup_signature(r.get("matchup") or "")
                if not sig:
                    continue
                groups.setdefault(sig, []).append(r)

            for sig, grp in groups.items():
                if len(grp) < 2:
                    continue
                # Prefer Korean-labeled row first, then highest votes
                grp.sort(key=lambda x: (1 if _has_hangul(x.get("matchup", "")) else 0, int(x.get("votes") or 0)), reverse=True)
                keeper = grp[0]
                for dup in grp[1:]:
                    conn.execute("DELETE FROM global_match_rankings WHERE id = ?", (dup["id"],))
                    removed += 1
        return removed

    try:
        cnt = run_with_db_retry(_op)
        if cnt > 0:
            threading.Thread(target=_trigger_pg_backup, args=("global_match_rankings",), daemon=True).start()
        return cnt
    except Exception:
        return 0


def classify_overseas_sport_accurately(title: str, default_sport: str = "general") -> Tuple[str, str]:
    low = (title or "").lower()
    sp = (default_sport or "general").lower()
    if sp in ("basketball", "nba"):
        return "basketball", "NBA 속보"
    if sp == "nfl":
        return "nfl", "NFL 속보"
    if sp in ("hockey", "nhl"):
        return "hockey", "NHL 속보"
    if sp in ("baseball", "mlb"):
        return "baseball", "MLB 속보"

    nba_kw = (
        "nba", "lakers", "lebron", "warriors", "curry", "celtics", "basketball",
        "knicks", "nuggets", "jokic", "76ers", "sixers", "embiid", "bucks", "giannis",
        "suns", "durant", "mavericks", "mavs", "doncic", "clippers", "heat", "bulls",
        "spurs", "wembanyama", "thunder", "timberwolves", "cavs", "cavaliers", "pacers",
        "hawks", "nets", "pistons", "raptors", "grizzlies", "pelicans", "kings", "rockets",
    )
    nfl_kw = (
        "nfl", "quarterback", "touchdown", "chiefs", "mahomes", "bills", "dolphins",
        "49ers", "chargers", "ravens", "lamar", "eagles", "cowboys", "steelers", "packers",
        "lions", "vikings", "texans", "commanders", "falcons", "broncos", "seahawks",
        "bengals", "jets", "patriots", "raiders", "saints", "bears", "super bowl",
    )
    nhl_kw = (
        "nhl", "hockey", "oilers", "mcdavid", "maple leafs", "rangers", "stanley cup",
        "panthers", "bruins", "blackhawks", "penguins", "avalanche", "lightning",
    )
    mlb_kw = (
        "mlb", "baseball", "pitcher", "home run", "dodgers", "ohtani", "yankees",
        "mets", "braves", "padres", "phillies", "astros", "red sox", "cubs", "abl",
    )
    ufc_kw = ("ufc", "mma", "octagon", "knockout", "lightweight", "heavyweight")
    f1_kw = ("formula 1", "f1", "verstappen", "norris", "ferrari", "mclaren", "grand prix")

    if any(k in low for k in nba_kw):
        return "basketball", "NBA 속보"
    if any(k in low for k in nfl_kw):
        return "nfl", "NFL 속보"
    if any(k in low for k in nhl_kw):
        return "hockey", "NHL 속보"
    if any(k in low for k in mlb_kw):
        return "baseball", "MLB 속보"
    if any(k in low for k in ufc_kw):
        return "ufc", "UFC 속보"
    if any(k in low for k in f1_kw):
        return "f1", "F1 속보"
    return ("soccer" if sp == "general" else sp), "해외축구 속보"


def repair_overseas_news_sport_tags() -> int:
    def _op() -> int:
        fixed = 0
        with _db_session(write=True) as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='overseas_news_cache'"
            ).fetchone()
            if not exists:
                return 0
            rows = conn.execute(
                "SELECT id, title, source, sport, summary_tag FROM overseas_news_cache"
            ).fetchall()
            for r in rows:
                src = (r["source"] or "").lower()
                default_sp = "general" if src == "espn" else (r["sport"] or "general")
                new_sp, new_tag = classify_overseas_sport_accurately(r["title"] or "", default_sp)
                if new_sp != r["sport"]:
                    conn.execute(
                        "UPDATE overseas_news_cache SET sport = ?, summary_tag = ? WHERE id = ?",
                        (new_sp, new_tag, r["id"]),
                    )
                    fixed += 1
        return fixed

    try:
        return run_with_db_retry(_op)
    except Exception:
        return 0


def _trigger_pg_backup(*tables: str) -> None:
    if not _PG_SYNC_STATE["initial_restore_done"]:
        return
    try:
        from app import sportiq_auto_analyst as _analyst
        for t in tables:
            if t and t not in _analyst._PG_SNAPSHOT_TABLES:
                _analyst._PG_SNAPSHOT_TABLES = _analyst._PG_SNAPSHOT_TABLES + (t,)
        _analyst._backup_tables_to_pg(*tables)
        _PG_SYNC_STATE["last_backup_kst"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass


def _pg_full_persistence_daemon() -> None:
    time.sleep(2.2)
    _init_upgrade_tables()
    try:
        from app import sportiq_autonomous_sec as _sec
        _sec._classify_news_sport_and_tag = classify_overseas_sport_accurately
    except Exception:
        pass

    try:
        from app import sportiq_auto_analyst as _analyst
        with _db_session(write=False) as conn:
            discovered = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if r[0] != "presence_sessions"
            ]
        merged = list(dict.fromkeys([*_analyst._PG_SNAPSHOT_TABLES, *ALL_KNOWN_SQLITE_TABLES, *discovered]))
        newly_added = [t for t in merged if t not in _analyst._PG_SNAPSHOT_TABLES]
        _analyst._PG_SNAPSHOT_TABLES = tuple(merged)
        for t in newly_added:
            _analyst._PG_RESTORED_TABLES.discard(t)
        _analyst._restore_snapshots_from_pg()
        _PG_SYNC_STATE["registered_tables"] = len(_analyst._PG_SNAPSHOT_TABLES)
    except Exception:
        pass
    finally:
        _PG_SYNC_STATE["initial_restore_done"] = True

    deduplicate_global_match_rankings()
    repair_overseas_news_sport_tags()

    last_hashes: Dict[str, str] = {}
    while True:
        try:
            deduplicate_global_match_rankings()
            repair_overseas_news_sport_tags()
            from app import sportiq_auto_analyst as _analyst
            changed: List[str] = []
            with _db_session(write=False) as conn:
                existing = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                for extra_t in existing:
                    if extra_t != "presence_sessions" and extra_t not in _analyst._PG_SNAPSHOT_TABLES:
                        _analyst._PG_SNAPSHOT_TABLES = _analyst._PG_SNAPSHOT_TABLES + (extra_t,)
                _PG_SYNC_STATE["registered_tables"] = len(_analyst._PG_SNAPSHOT_TABLES)

                for tname in _analyst._PG_SNAPSHOT_TABLES:
                    if tname not in existing or tname == "presence_sessions":
                        continue
                    rows = conn.execute(f"SELECT * FROM {tname}").fetchall()
                    if not rows and tname not in last_hashes:
                        continue
                    raw_sig = hashlib.md5(
                        json.dumps([dict(r) for r in rows], ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest()
                    if last_hashes.get(tname) != raw_sig:
                        last_hashes[tname] = raw_sig
                        changed.append(tname)
            if changed:
                _analyst._backup_tables_to_pg(*changed)
                _PG_SYNC_STATE["last_backup_kst"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        time.sleep(45.0)


_init_upgrade_tables()
threading.Thread(target=_pg_full_persistence_daemon, daemon=True, name="sportiq-pg-full-sync").start()


# --- Schemas ---
class MemberSyncIn(BaseModel):
    nickname: str
    pin: str
    client_points: Optional[int] = None
    client_streak: Optional[int] = None
    client_checked_date: Optional[str] = None
    my_teams: Optional[List[str]] = None


class PostCreateIn(BaseModel):
    league: str = "EPL"
    title: str
    body: str
    tags: List[str] = []
    author: Optional[str] = ""
    pin: Optional[str] = ""
    role: Optional[str] = "GUEST"


class PostUpdateIn(BaseModel):
    title: str
    body: str
    league: Optional[str] = None
    tags: Optional[List[str]] = None
    pin: str
    author: Optional[str] = ""


class CommentCreateIn(BaseModel):
    user: str = "익명팬"
    text: str
    nickname: Optional[str] = ""


class ShopActionIn(BaseModel):
    nickname: str
    pin: str
    item_key: str


class RaffleEnterIn(BaseModel):
    nickname: str
    pin: str


# --- Member Sync & Shop Endpoints ---
@upgrade_router.get("/api/member/status")
def member_upgrade_status():
    return {
        "ok": True,
        "version": "3.6",
        "pg_sync": _PG_SYNC_STATE,
        "shop_items": len(SHOP_CATALOG),
        "pwa_enabled": True,
        "raffle_admin_enabled": True,
        "wp_pin_bridge_enabled": True,
        "p3_predictions_mypage": True,
        "p3_notifications_webpush": True,
        "p3_feed_tabs_search_blind": True,
        "match_chronological_sort": True,
        "ai_iq_online_search": True,
        "header_tagline": "실시간 분석 스포츠IQ 인사이트",
        "odds_alert_off_toggle": True,
        "iq_live_tv_tab": "https://jjangdoltv.com/",
    }


@upgrade_router.post("/api/member/sync")
def sync_member_account(payload: MemberSyncIn):
    nick = (payload.nickname or "").strip()[:24]
    pin = re.sub(r"\D", "", payload.pin or "")
    if len(nick) < 2:
        raise HTTPException(status_code=400, detail="닉네임은 2~24자로 입력해 주세요.")
    if len(pin) != 4:
        raise HTTPException(status_code=400, detail="기기 연동 PIN은 숫자 4자리로 입력해 주세요.")

    pin_h = _hash_pin(nick, pin)
    now = time.time()
    c_pts = max(0, int(payload.client_points if payload.client_points is not None else 120))
    c_streak = max(0, int(payload.client_streak if payload.client_streak is not None else 0))
    c_date = (payload.client_checked_date or "").strip()[:10]
    c_teams = [str(t)[:40] for t in (payload.my_teams or [])[:20]]

    def _op() -> Dict[str, Any]:
        with _db_session(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM member_accounts WHERE lower(nickname) = lower(?)", (nick,)
            ).fetchone()
            if row is None:
                init_pts = max(120, min(c_pts, 5000))
                init_streak = c_streak if c_date else 0
                tier = _tier_for_points(init_pts)
                conn.execute(
                    """
                    INSERT INTO member_accounts(
                        nickname, pin_hash, points, role_tier, streak_days,
                        last_checkin_date, active_badge, owned_items_json, my_teams_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '', '[]', ?, ?)
                    """,
                    (nick, pin_h, init_pts, tier, init_streak, c_date, json.dumps(c_teams, ensure_ascii=False), now),
                )
                return {
                    "created": True,
                    "nickname": nick,
                    "points": init_pts,
                    "role_tier": tier,
                    "streak_days": init_streak,
                    "last_checkin_date": c_date,
                    "active_badge": "",
                    "owned_items": [],
                    "my_teams": c_teams,
                }

            if row["pin_hash"] != pin_h:
                raise HTTPException(status_code=403, detail="4자리 PIN 번호가 일치하지 않습니다.")

            merged_pts = max(int(row["points"]), min(c_pts, int(row["points"]) + 60))
            merged_date = max(str(row["last_checkin_date"] or ""), c_date)
            merged_streak = int(row["streak_days"] or 0)
            if c_date and c_date > str(row["last_checkin_date"] or ""):
                merged_streak = max(merged_streak + 1, c_streak)
            tier = _tier_for_points(merged_pts)
            saved_teams = json.loads(row["my_teams_json"] or "[]")
            final_teams = c_teams if c_teams else saved_teams
            owned = json.loads(row["owned_items_json"] or "[]")

            conn.execute(
                """
                UPDATE member_accounts
                SET points = ?, role_tier = ?, streak_days = ?, last_checkin_date = ?,
                    my_teams_json = ?, updated_at = ?
                WHERE nickname = ?
                """,
                (merged_pts, tier, merged_streak, merged_date, json.dumps(final_teams, ensure_ascii=False), now, row["nickname"]),
            )
            return {
                "created": False,
                "nickname": row["nickname"],
                "points": merged_pts,
                "role_tier": tier,
                "streak_days": merged_streak,
                "last_checkin_date": merged_date,
                "active_badge": row["active_badge"] or "",
                "owned_items": owned,
                "my_teams": final_teams,
            }

    profile = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("member_accounts",), daemon=True).start()
    return {"ok": True, "profile": profile}


@upgrade_router.get("/api/member/shop")
def get_point_shop(nickname: Optional[str] = None):
    round_key = datetime.now(KST).strftime("%Y-W%W")

    def _op() -> Dict[str, Any]:
        with _db_session(write=False) as conn:
            total_tickets = conn.execute(
                "SELECT COALESCE(SUM(tickets), 0) AS t FROM member_raffle_entries WHERE round_key = ?",
                (round_key,),
            ).fetchone()["t"]
            my_tickets = 0
            profile = None
            if nickname:
                r = conn.execute(
                    "SELECT nickname, points, role_tier, active_badge, owned_items_json FROM member_accounts WHERE lower(nickname)=lower(?)",
                    (nickname.strip(),),
                ).fetchone()
                if r:
                    profile = {
                        "nickname": r["nickname"],
                        "points": r["points"],
                        "role_tier": r["role_tier"],
                        "active_badge": r["active_badge"],
                        "owned_items": json.loads(r["owned_items_json"] or "[]"),
                    }
                    mt = conn.execute(
                        "SELECT COALESCE(SUM(tickets), 0) AS t FROM member_raffle_entries WHERE round_key = ? AND lower(nickname)=lower(?)",
                        (round_key, nickname.strip()),
                    ).fetchone()
                    my_tickets = int(mt["t"] or 0)
            return {
                "catalog": SHOP_CATALOG,
                "raffle": {
                    "round_key": round_key,
                    "ticket_cost": 50,
                    "total_tickets": int(total_tickets or 0),
                    "my_tickets": my_tickets,
                    "prize_title": "주간 베스트 활동회원 편의점·커피 기프티콘 럭키드로우",
                },
                "tier_thresholds": {"MEMBER": 150, "PRO": 350},
                "profile": profile,
            }

    return {"ok": True, **run_with_db_retry(_op)}


@upgrade_router.post("/api/member/shop/buy")
def buy_shop_item(payload: ShopActionIn):
    nick = (payload.nickname or "").strip()[:24]
    pin = re.sub(r"\D", "", payload.pin or "")
    item = next((x for x in SHOP_CATALOG if x["item_key"] == payload.item_key), None)
    if not item:
        raise HTTPException(status_code=404, detail="존재하지 않는 상점 아이템입니다.")
    if len(pin) != 4:
        raise HTTPException(status_code=400, detail="먼저 닉네임과 4자리 PIN을 연동해 주세요.")
    pin_h = _hash_pin(nick, pin)

    def _op() -> Dict[str, Any]:
        with _db_session(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM member_accounts WHERE lower(nickname)=lower(?)", (nick,)
            ).fetchone()
            if not row or row["pin_hash"] != pin_h:
                raise HTTPException(status_code=403, detail="닉네임 또는 4자리 PIN이 일치하지 않습니다.")
            owned: List[str] = json.loads(row["owned_items_json"] or "[]")
            pts = int(row["points"])
            if item["item_key"] not in owned:
                if pts < item["cost"]:
                    raise HTTPException(status_code=400, detail=f"포인트가 부족합니다. (필요: {item['cost']}P / 보유: {pts}P)")
                pts -= item["cost"]
                owned.append(item["item_key"])
            new_badge = item["badge_label"]
            conn.execute(
                """
                UPDATE member_accounts
                SET points = ?, active_badge = ?, owned_items_json = ?, updated_at = ?
                WHERE nickname = ?
                """,
                (pts, new_badge, json.dumps(owned, ensure_ascii=False), time.time(), row["nickname"]),
            )
            return {
                "nickname": row["nickname"],
                "points": pts,
                "role_tier": row["role_tier"],
                "active_badge": new_badge,
                "owned_items": owned,
            }

    res = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("member_accounts",), daemon=True).start()
    return {"ok": True, "message": f"'{item['badge_label']}' 칭호가 구매 및 장착되었습니다!", "profile": res}


@upgrade_router.post("/api/member/raffle/enter")
def enter_weekly_raffle(payload: RaffleEnterIn):
    nick = (payload.nickname or "").strip()[:24]
    pin = re.sub(r"\D", "", payload.pin or "")
    if len(pin) != 4:
        raise HTTPException(status_code=400, detail="닉네임과 4자리 PIN 연동 후 응모할 수 있습니다.")
    pin_h = _hash_pin(nick, pin)
    round_key = datetime.now(KST).strftime("%Y-W%W")

    def _op() -> Dict[str, Any]:
        with _db_session(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM member_accounts WHERE lower(nickname)=lower(?)", (nick,)
            ).fetchone()
            if not row or row["pin_hash"] != pin_h:
                raise HTTPException(status_code=403, detail="닉네임 또는 4자리 PIN이 일치하지 않습니다.")
            pts = int(row["points"])
            if pts < 50:
                raise HTTPException(status_code=400, detail=f"응모에는 50P가 필요합니다. (현재 보유: {pts}P)")
            pts -= 50
            conn.execute(
                "UPDATE member_accounts SET points = ?, updated_at = ? WHERE nickname = ?",
                (pts, time.time(), row["nickname"]),
            )
            conn.execute(
                "INSERT INTO member_raffle_entries(nickname, round_key, tickets, created_at) VALUES (?, ?, 1, ?)",
                (row["nickname"], round_key, time.time()),
            )
            my_cnt = conn.execute(
                "SELECT COALESCE(SUM(tickets), 0) AS t FROM member_raffle_entries WHERE round_key = ? AND nickname = ?",
                (round_key, row["nickname"]),
            ).fetchone()["t"]
            tot_cnt = conn.execute(
                "SELECT COALESCE(SUM(tickets), 0) AS t FROM member_raffle_entries WHERE round_key = ?",
                (round_key,),
            ).fetchone()["t"]
            return {"points": pts, "my_tickets": int(my_cnt or 0), "total_tickets": int(tot_cnt or 0)}

    out = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("member_accounts", "member_raffle_entries"), daemon=True).start()
    return {"ok": True, **out}


# --- Server-Persisted Community Posts, Comments & 4-Digit PIN Edit/Delete ---
@upgrade_router.get("/api/posts")
def list_community_posts():
    def _op() -> Dict[str, Any]:
        with _db_session(write=False) as conn:
            p_rows = conn.execute(
                "SELECT * FROM community_member_posts ORDER BY created_at DESC LIMIT 60"
            ).fetchall()
            c_rows = conn.execute(
                "SELECT post_id, user_name, user_badge, comment_text, created_at FROM community_post_comments ORDER BY id ASC"
            ).fetchall()
            blind_set = {
                r["post_id"]
                for r in conn.execute("SELECT post_id FROM community_post_blinds WHERE blinded = 1").fetchall()
            }
        comments_by_post: Dict[str, List[Dict[str, str]]] = {}
        for c in c_rows:
            label = f"{c['user_name']} {c['user_badge']}".strip()
            comments_by_post.setdefault(c["post_id"], []).append({"user": label, "text": c["comment_text"]})

        now = time.time()
        posts = []
        for r in p_rows:
            pid = r["id"]
            is_bl = pid in blind_set
            mins = max(0, int((now - float(r["created_at"] or now)) // 60))
            posts.append(
                {
                    "id": pid,
                    "league": r["league"],
                    "isPro": bool(r["is_pro"]),
                    "author": r["author"],
                    "authorBadge": r["author_badge"] or "",
                    "trust": int(r["trust"] or 89),
                    "likes": int(r["likes"] or 1),
                    "bookmarked": False,
                    "liked": False,
                    "hidden": is_bl,
                    "blinded": is_bl,
                    "time": mins,
                    "title": "🚨 [클린 블라인드] 누적 신고로 숨김 처리된 게시글입니다." if is_bl else r["title"],
                    "body": "커뮤니티 운영 정책에 따라 신고 3회 이상 누적되어 자동 블라인드 처리되었습니다." if is_bl else r["body"],
                    "tags": json.loads(r["tags_json"] or "[]"),
                    "comments": [] if is_bl else comments_by_post.get(pid, []),
                    "isMemberServerPost": True,
                    "hasPin": bool(r["pin_hash"]),
                }
            )
        return {"ok": True, "posts": posts, "extra_comments": comments_by_post}

    return run_with_db_retry(_op)


@upgrade_router.post("/api/posts")
def create_community_post(payload: PostCreateIn):
    title = (payload.title or "").strip()[:140]
    body = (payload.body or "").strip()[:4000]
    if not title or not body:
        raise HTTPException(status_code=400, detail="제목과 분석 내용을 모두 입력해 주세요.")
    league = (payload.league or "EPL").strip()[:20]
    author = (payload.author or "익명 분석가").strip()[:28]
    raw_pin = re.sub(r"\D", "", payload.pin or "")
    if raw_pin and len(raw_pin) != 4:
        raise HTTPException(status_code=400, detail="수정·삭제용 비밀번호는 숫자 4자리로 입력해 주세요.")

    now = time.time()
    post_id = f"mp_{int(now * 1000)}"
    clean_tags = [t if t.startswith("#") else f"#{t}" for t in (payload.tags or [f"#{league}"]) if str(t).strip()][:8]
    pin_h = _hash_pin(author, raw_pin) if len(raw_pin) == 4 else ""

    def _op() -> Tuple[str, int, Optional[int]]:
        badge = ""
        new_pts: Optional[int] = None
        is_pro = 1 if (payload.role == "PRO") else 0
        with _db_session(write=True) as conn:
            acc = conn.execute(
                "SELECT nickname, pin_hash, points, role_tier, active_badge FROM member_accounts WHERE lower(nickname)=lower(?)",
                (author,),
            ).fetchone()
            effective_pin_h = pin_h
            if acc:
                badge = acc["active_badge"] or ""
                if not effective_pin_h:
                    effective_pin_h = acc["pin_hash"]
                if acc["pin_hash"] == pin_h or not raw_pin:
                    new_pts = int(acc["points"]) + 15
                    new_tier = _tier_for_points(new_pts)
                    is_pro = 1 if new_tier == "PRO" else is_pro
                    conn.execute(
                        "UPDATE member_accounts SET points = ?, role_tier = ?, updated_at = ? WHERE nickname = ?",
                        (new_pts, new_tier, now, acc["nickname"]),
                    )
            conn.execute(
                """
                INSERT INTO community_member_posts(
                    id, league, author, author_badge, is_pro, trust, title, body, tags_json, likes, pin_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 90, ?, ?, ?, 1, ?, ?, ?)
                """,
                (post_id, league, author, badge, is_pro, title, body, json.dumps(clean_tags, ensure_ascii=False), effective_pin_h, now, now),
            )
        return badge, is_pro, new_pts

    badge, is_pro, new_pts = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("community_member_posts", "member_accounts"), daemon=True).start()
    return {
        "ok": True,
        "awarded_points": 15,
        "updated_points": new_pts,
        "post": {
            "id": post_id,
            "league": league,
            "isPro": bool(is_pro),
            "author": author,
            "authorBadge": badge,
            "trust": 90,
            "likes": 1,
            "bookmarked": False,
            "liked": False,
            "hidden": False,
            "time": 0,
            "title": title,
            "body": body,
            "tags": clean_tags,
            "comments": [],
            "isMemberServerPost": True,
            "hasPin": bool(pin_h),
        },
    }


@upgrade_router.put("/api/posts/{post_id}")
def update_community_post(post_id: str, payload: PostUpdateIn):
    title = (payload.title or "").strip()[:140]
    body = (payload.body or "").strip()[:4000]
    raw_pin = re.sub(r"\D", "", payload.pin or "")
    if not title or not body:
        raise HTTPException(status_code=400, detail="수정할 제목과 본문을 입력해 주세요.")
    if len(raw_pin) != 4:
        raise HTTPException(status_code=400, detail="숫자 4자리 PIN을 입력해 주세요.")

    def _op() -> Dict[str, Any]:
        with _db_session(write=True) as conn:
            row = conn.execute("SELECT * FROM community_member_posts WHERE id = ?", (post_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="수정할 게시글을 찾을 수 없습니다.")
            expected = row["pin_hash"] or ""
            cand = _hash_pin(row["author"], raw_pin)
            if not expected or cand != expected:
                raise HTTPException(status_code=403, detail="작성 시 설정한 4자리 PIN이 일치하지 않습니다.")
            new_league = (payload.league or row["league"]).strip()[:20]
            new_tags = (
                [t if t.startswith("#") else f"#{t}" for t in payload.tags if str(t).strip()][:8]
                if payload.tags is not None
                else json.loads(row["tags_json"] or "[]")
            )
            conn.execute(
                "UPDATE community_member_posts SET league = ?, title = ?, body = ?, tags_json = ?, updated_at = ? WHERE id = ?",
                (new_league, title, body, json.dumps(new_tags, ensure_ascii=False), time.time(), post_id),
            )
            return {"id": post_id, "league": new_league, "title": title, "body": body, "tags": new_tags}

    updated = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("community_member_posts",), daemon=True).start()
    return {"ok": True, "post": updated}


@upgrade_router.delete("/api/posts/{post_id}")
def delete_community_post(post_id: str, pin: str = ""):
    raw_pin = re.sub(r"\D", "", pin or "")
    if len(raw_pin) != 4:
        raise HTTPException(status_code=400, detail="삭제하려면 숫자 4자리 PIN이 필요합니다.")

    def _op() -> None:
        with _db_session(write=True) as conn:
            row = conn.execute("SELECT id, author, pin_hash FROM community_member_posts WHERE id = ?", (post_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="삭제할 게시글을 찾을 수 없습니다.")
            expected = row["pin_hash"] or ""
            if not expected or _hash_pin(row["author"], raw_pin) != expected:
                raise HTTPException(status_code=403, detail="작성 시 설정한 4자리 PIN이 일치하지 않습니다.")
            conn.execute("DELETE FROM community_member_posts WHERE id = ?", (post_id,))
            conn.execute("DELETE FROM community_post_comments WHERE post_id = ?", (post_id,))

    run_with_db_retry(_op)
    threading.Thread(
        target=_trigger_pg_backup, args=("community_member_posts", "community_post_comments"), daemon=True
    ).start()
    return {"ok": True, "deleted_id": post_id}


@upgrade_router.post("/api/posts/{post_id}/comments")
def add_community_comment(post_id: str, payload: CommentCreateIn):
    txt = (payload.text or "").strip()[:500]
    if not txt:
        raise HTTPException(status_code=400, detail="댓글 내용을 입력해 주세요.")
    user = (payload.nickname or payload.user or "익명팬").strip()[:28]

    def _op() -> Dict[str, str]:
        badge = ""
        with _db_session(write=True) as conn:
            acc = conn.execute(
                "SELECT active_badge FROM member_accounts WHERE lower(nickname)=lower(?)", (user,)
            ).fetchone()
            if acc and acc["active_badge"]:
                badge = acc["active_badge"]
            now_c = time.time()
            conn.execute(
                "INSERT INTO community_post_comments(post_id, user_name, user_badge, comment_text, created_at) VALUES (?, ?, ?, ?, ?)",
                (post_id[:64], user, badge, txt, now_c),
            )
            p_owner = conn.execute(
                "SELECT author, title FROM community_member_posts WHERE id = ?",
                (post_id[:64],),
            ).fetchone()
            if p_owner and p_owner["author"] and p_owner["author"].strip().lower() != user.lower():
                conn.execute(
                    """
                    INSERT INTO member_notifications(nickname, notif_type, title, body, link_target, is_read, created_at)
                    VALUES (?, 'COMMENT', ?, ?, ?, 0, ?)
                    """,
                    (
                        p_owner["author"].strip()[:24],
                        f"💬 '{user}'님이 내 분석글에 댓글을 남겼습니다",
                        f"[{p_owner['title'][:36]}] {txt[:80]}",
                        post_id[:64],
                        now_c,
                    ),
                )
        label = f"{user} {badge}".strip()
        return {"user": label, "text": txt}

    c_obj = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("community_post_comments", "member_notifications"), daemon=True).start()
    return {"ok": True, "comment": c_obj}


# --- Pure-Stdlib Social OG Cover PNG, Favicon & SEO Routes ---
def _build_solid_gradient_png(width: int, height: int) -> bytes:
    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)
        fy = y / max(1, height - 1)
        r_base = int(11 + fy * 8)
        g_base = int(18 + fy * 22)
        b_base = int(34 + fy * 38)
        is_accent_bar = (height - 14) <= y < height or 0 <= y < 10
        is_pitch_box = (height // 5) <= y <= (height * 4 // 5)
        for x in range(width):
            if is_accent_bar:
                fx = x / max(1, width - 1)
                r = int(2 + fx * 14)
                g = int(132 + fx * 53)
                b = int(199 - fx * 70)
            elif is_pitch_box and x in (width // 8, width * 7 // 8, width // 2):
                r, g, b = 30, 88, 128
            elif y in (height // 5, height * 4 // 5) and (width // 8) <= x <= (width * 7 // 8):
                r, g, b = 30, 88, 128
            else:
                r, g, b = r_base, g_base, b_base
            raw_rows.extend((r, g, b))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=6)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


@upgrade_router.get("/og-cover.png")
def serve_og_cover_png():
    global _OG_PNG_CACHE
    if _OG_PNG_CACHE is None:
        _OG_PNG_CACHE = _build_solid_gradient_png(1200, 630)
    return Response(content=_OG_PNG_CACHE, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@upgrade_router.get("/favicon.ico")
def serve_favicon():
    global _FAVICON_PNG_CACHE
    if _FAVICON_PNG_CACHE is None:
        _FAVICON_PNG_CACHE = _build_solid_gradient_png(32, 32)
    return Response(content=_FAVICON_PNG_CACHE, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@upgrade_router.get("/robots.txt")
def serve_robots_txt():
    txt = "User-agent: *\nAllow: /\nSitemap: https://billnojay.com/sitemap.xml\n"
    return Response(content=txt, media_type="text/plain; charset=utf-8")


@upgrade_router.get("/sitemap.xml")
def serve_sitemap_xml():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>https://billnojay.com/</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml; charset=utf-8")




# ============================================================================
# STAGE 1: PWA Manifest, App Icons (192/512/180 PNG) & Safe Service Worker
# ============================================================================
_SQ_BITMAP_S = (
    "0111110",
    "1100011",
    "1100000",
    "0111100",
    "0000110",
    "1100011",
    "0111110",
)
_SQ_BITMAP_Q = (
    "0111100",
    "1100110",
    "1100110",
    "1100110",
    "1101110",
    "0111100",
    "0000111",
)


def _build_pwa_app_icon_png(size: int) -> bytes:
    """Generates a crisp standalone SportIQ stadium crest PNG icon in pure stdlib."""
    cx = (size - 1) / 2.0
    cy = (size - 1) / 2.0
    r_outer = size * 0.43
    r_ring_in = size * 0.385
    r_glow = size * 0.33

    glyph_scale = max(2, size // 22)
    glyph_w = 7 * glyph_scale
    glyph_h = 7 * glyph_scale
    gap = max(2, glyph_scale)
    total_gw = glyph_w * 2 + gap
    gx0 = (size - total_gw) // 2
    gy0 = (size - glyph_h) // 2

    raw_rows = bytearray()
    for y in range(size):
        raw_rows.append(0)
        fy = y / max(1, size - 1)
        bg_r = int(7 + fy * 10)
        bg_g = int(11 + fy * 16)
        bg_b = int(20 + fy * 28)
        dy = y - cy
        for x in range(size):
            dx = x - cx
            dist = (dx * dx + dy * dy) ** 0.5
            r, g, b = bg_r, bg_g, bg_b

            if r_ring_in <= dist <= r_outer:
                fx = x / max(1, size - 1)
                r = int(16 + fx * 40)
                g = int(185 + fx * 4)
                b = int(129 + fx * 119)
            elif dist < r_ring_in:
                inner_f = dist / max(1.0, r_ring_in)
                r = int(12 + (1.0 - inner_f) * 10)
                g = int(23 + (1.0 - inner_f) * 28)
                b = int(46 + (1.0 - inner_f) * 44)
                if abs(dist - r_glow) <= max(1.0, size * 0.008):
                    r, g, b = 30, 96, 138

            if gy0 <= y < gy0 + glyph_h and gx0 <= x < gx0 + total_gw:
                ry = (y - gy0) // glyph_scale
                rx_total = x - gx0
                on_pixel = False
                is_q_tail = False
                if rx_total < glyph_w:
                    rx = rx_total // glyph_scale
                    on_pixel = _SQ_BITMAP_S[ry][rx] == "1"
                elif rx_total >= glyph_w + gap:
                    rx = (rx_total - glyph_w - gap) // glyph_scale
                    if 0 <= rx < 7:
                        on_pixel = _SQ_BITMAP_Q[ry][rx] == "1"
                        is_q_tail = (ry >= 5 and rx >= 4 and on_pixel)
                if on_pixel:
                    if is_q_tail:
                        r, g, b = 251, 191, 36
                    else:
                        r, g, b = 248, 250, 252

            raw_rows.extend((r, g, b))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack("!IIBBBBB", size, size, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=6)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _get_pwa_icon_bytes(size: int) -> bytes:
    if size not in _PWA_ICON_CACHE:
        _PWA_ICON_CACHE[size] = _build_pwa_app_icon_png(size)
    return _PWA_ICON_CACHE[size]


@upgrade_router.get("/pwa-icon-192.png")
def serve_pwa_icon_192():
    return Response(
        content=_get_pwa_icon_bytes(192),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@upgrade_router.get("/pwa-icon-512.png")
def serve_pwa_icon_512():
    return Response(
        content=_get_pwa_icon_bytes(512),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@upgrade_router.get("/apple-touch-icon.png")
def serve_apple_touch_icon():
    return Response(
        content=_get_pwa_icon_bytes(180),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _manifest_payload() -> Dict[str, Any]:
    return {
        "id": "/",
        "name": "SportIQ — 실시간 스포츠 데이터 & 분석 라운지",
        "short_name": "SportIQ",
        "description": "9종목 실시간 스코어, 배당 급변 드롭오즈, AI 승부예측과 4자리 PIN 오픈 분석 커뮤니티",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#070B14",
        "theme_color": "#0F172A",
        "lang": "ko",
        "dir": "ltr",
        "categories": ["sports", "news", "social"],
        "icons": [
            {"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/pwa-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/pwa-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {
                "name": "실시간 데이터센터",
                "short_name": "데이터센터",
                "description": "실시간 배당 급변·리그 순위·명예의 전당 열기",
                "url": "/?tab=datacenter&source=pwa",
                "icons": [{"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png"}],
            },
            {
                "name": "포인트 상점 · PIN 연동",
                "short_name": "포인트 상점",
                "description": "4자리 PIN 기기 연동 및 기프티콘 래플 응모",
                "url": "/?open=shop&source=pwa",
                "icons": [{"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png"}],
            },
        ],
    }


@upgrade_router.get("/manifest.webmanifest")
@upgrade_router.get("/manifest.json")
def serve_pwa_manifest():
    return Response(
        content=json.dumps(_manifest_payload(), ensure_ascii=False, indent=2),
        media_type="application/manifest+json; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


SERVICE_WORKER_JS = (
    "/* SportIQ Safe Network-First PWA Service Worker v2.0 */\n"
    "const CACHE_NAME = 'sportiq-pwa-static-v2';\n"
    "const PRECACHE_URLS = ['/pwa-icon-192.png', '/pwa-icon-512.png', '/apple-touch-icon.png', '/favicon.ico'];\n"
    "self.addEventListener('install', (event) => {\n"
    "  self.skipWaiting();\n"
    "  event.waitUntil(caches.open(CACHE_NAME).then((c) => c.addAll(PRECACHE_URLS)).catch(() => {}));\n"
    "});\n"
    "self.addEventListener('activate', (event) => {\n"
    "  event.waitUntil(\n"
    "    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))).then(() => self.clients.claim())\n"
    "  );\n"
    "});\n"
    "self.addEventListener('fetch', (event) => {\n"
    "  const req = event.request;\n"
    "  if (req.method !== 'GET') return;\n"
    "  const url = new URL(req.url);\n"
    "  if (url.origin !== self.location.origin) return;\n"
    "  if (PRECACHE_URLS.includes(url.pathname)) {\n"
    "    event.respondWith(caches.match(req).then((cached) => cached || fetch(req)));\n"
    "    return;\n"
    "  }\n"
    "  if (req.mode === 'navigate') {\n"
    "    event.respondWith(\n"
    "      fetch(req).catch(() => new Response(\n"
    "        \"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>SportIQ 오프라인</title><style>body{background:#070b14;color:#f8fafc;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;text-align:center}.card{background:#0f172a;border:1px solid #334155;border-radius:16px;padding:24px;max-width:360px}button{margin-top:14px;background:#0284c7;color:#fff;border:0;border-radius:10px;padding:10px 18px;font-weight:800;cursor:pointer}</style></head><body><div class='card'><h2>SportIQ 네트워크 재연결 대기</h2><p style='color:#94a3b8;font-size:14px'>모바일 네트워크 연결을 확인한 뒤 새로고침을 눌러 실시간 스코어와 분석 피드를 불러오세요.</p><button onclick='location.reload()'>다시 연결하기</button></div></body></html>\",\n"
    "        { headers: { 'Content-Type': 'text/html; charset=utf-8' } }\n"
    "      ))\n"
    "    );\n"
    "  }\n"
    "});\n"
)


@upgrade_router.get("/sw.js")
def serve_pwa_service_worker():
    return Response(
        content=SERVICE_WORKER_JS,
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Service-Worker-Allowed": "/",
        },
    )


# ============================================================================
# STAGE 2: Weekly Gifticon Raffle Admin Dashboard & Provably-Fair Winner Draw
# ============================================================================
def _hash_admin_key(raw_key: str) -> str:
    clean = (raw_key or "").strip()
    return hashlib.pbkdf2_hmac(
        "sha256",
        f"sportiq-admin:{PIN_SALT}:{clean}".encode("utf-8"),
        PIN_SALT.encode("utf-8"),
        20000,
    ).hex()


def _get_stored_admin_hash() -> str:
    def _op() -> str:
        with _db_session(write=False) as conn:
            row = conn.execute(
                "SELECT cfg_value FROM admin_config WHERE cfg_key = 'admin_key_hash'"
            ).fetchone()
            return str(row["cfg_value"]) if row and row["cfg_value"] else ""

    try:
        return run_with_db_retry(_op)
    except Exception:
        return ""


def _is_admin_configured() -> bool:
    env_key = os.environ.get("SPORTIQ_ADMIN_KEY", "").strip()
    if env_key:
        return True
    return bool(_get_stored_admin_hash())


def _require_admin_auth(provided_key: Optional[str]) -> None:
    cand = (provided_key or "").strip()
    if not cand:
        raise HTTPException(status_code=401, detail="관리자 인증 키(X-SportIQ-Admin-Key)를 입력해 주세요.")
    env_key = os.environ.get("SPORTIQ_ADMIN_KEY", "").strip()
    if env_key:
        if not hmac.compare_digest(env_key, cand):
            raise HTTPException(status_code=403, detail="관리자 인증 키가 일치하지 않습니다.")
        return
    stored_hash = _get_stored_admin_hash()
    if not stored_hash:
        raise HTTPException(status_code=400, detail="먼저 관리자 마스터 키 초기 설정을 완료해 주세요.")
    cand_hash = _hash_admin_key(cand)
    if not hmac.compare_digest(stored_hash, cand_hash):
        raise HTTPException(status_code=403, detail="관리자 마스터 키가 일치하지 않습니다.")


class AdminKeySetupIn(BaseModel):
    new_admin_key: str
    current_admin_key: Optional[str] = ""


class AdminRaffleDrawIn(BaseModel):
    round_key: Optional[str] = ""
    winner_count: int = 1
    prize_name: str = "편의점·커피 기프티콘 5,000원권"
    exclude_already_won: bool = True


class AdminRaffleFulfillIn(BaseModel):
    draw_id: int
    fulfillment_note: str = "기프티콘 발송 완료"


class MemberMyWinsIn(BaseModel):
    nickname: str
    pin: str


def _mask_claim_code(code: str) -> str:
    s = (code or "").strip()
    if len(s) < 8:
        return "SQ-****"
    return s[:-4] + "****"


def _fmt_kst_ts(epoch_val: Any) -> str:
    try:
        return datetime.fromtimestamp(float(epoch_val), tz=KST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


@upgrade_router.get("/api/admin/raffle/auth-state")
def get_admin_raffle_auth_state():
    env_locked = bool(os.environ.get("SPORTIQ_ADMIN_KEY", "").strip())
    return {
        "ok": True,
        "configured": _is_admin_configured(),
        "env_locked": env_locked,
        "current_round": datetime.now(KST).strftime("%Y-W%W"),
    }


@upgrade_router.post("/api/admin/setup-key")
def setup_or_rotate_admin_key(payload: AdminKeySetupIn):
    if os.environ.get("SPORTIQ_ADMIN_KEY", "").strip():
        raise HTTPException(
            status_code=400,
            detail="Render 환경변수(SPORTIQ_ADMIN_KEY)로 관리자 키가 고정되어 있습니다.",
        )
    new_k = (payload.new_admin_key or "").strip()
    if len(new_k) < 6 or len(new_k) > 48:
        raise HTTPException(status_code=400, detail="관리자 마스터 키는 6~48자로 설정해 주세요.")

    existing_hash = _get_stored_admin_hash()
    if existing_hash:
        cur_k = (payload.current_admin_key or "").strip()
        if not cur_k or not hmac.compare_digest(existing_hash, _hash_admin_key(cur_k)):
            raise HTTPException(status_code=403, detail="기존 관리자 마스터 키가 일치하지 않습니다.")

    new_hash = _hash_admin_key(new_k)
    now = time.time()

    def _op() -> None:
        with _db_session(write=True) as conn:
            conn.execute(
                """
                INSERT INTO admin_config(cfg_key, cfg_value, updated_at)
                VALUES ('admin_key_hash', ?, ?)
                ON CONFLICT(cfg_key) DO UPDATE SET
                    cfg_value = excluded.cfg_value,
                    updated_at = excluded.updated_at
                """,
                (new_hash, now),
            )

    run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("admin_config",), daemon=True).start()
    return {
        "ok": True,
        "rotated": bool(existing_hash),
        "message": "관리자 마스터 키가 암호화되어 영구 저장되었습니다.",
    }


@upgrade_router.get("/api/raffle/public-winners")
def get_public_raffle_winners(limit: int = 8):
    lim = max(1, min(30, int(limit)))

    def _op() -> List[Dict[str, Any]]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                """
                SELECT r.*, COALESCE(m.active_badge, '') AS active_badge, COALESCE(m.role_tier, 'MEMBER') AS role_tier
                FROM raffle_draw_results r
                LEFT JOIN member_accounts m ON lower(m.nickname) = lower(r.winner_nickname)
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
            out = []
            for r in rows:
                tot = max(1, int(r["total_tickets"] or 1))
                wt = int(r["winner_tickets"] or 1)
                out.append(
                    {
                        "id": int(r["id"]),
                        "round_key": r["round_key"],
                        "winner_nickname": r["winner_nickname"],
                        "active_badge": r["active_badge"] or "",
                        "role_tier": r["role_tier"] or "MEMBER",
                        "winner_tickets": wt,
                        "total_tickets": tot,
                        "win_odds_pct": round(wt * 100.0 / tot, 1),
                        "prize_name": r["prize_name"],
                        "draw_seed_prefix": (r["draw_seed_hash"] or "")[:14],
                        "masked_claim_code": _mask_claim_code(r["claim_code"]),
                        "fulfillment_note": r["fulfillment_note"] or "지급 대기",
                        "drawn_at_kst": _fmt_kst_ts(r["drawn_at"]),
                    }
                )
            return out

    return {"ok": True, "winners": run_with_db_retry(_op)}


@upgrade_router.post("/api/member/raffle/my-wins")
def get_member_own_raffle_wins(payload: MemberMyWinsIn):
    nick = (payload.nickname or "").strip()[:24]
    pin = re.sub(r"\D", "", payload.pin or "")
    if len(nick) < 2 or len(pin) != 4:
        return {"ok": True, "wins": []}
    pin_h = _hash_pin(nick, pin)

    def _op() -> List[Dict[str, Any]]:
        with _db_session(write=False) as conn:
            acc = conn.execute(
                "SELECT nickname, pin_hash FROM member_accounts WHERE lower(nickname)=lower(?)",
                (nick,),
            ).fetchone()
            if not acc or acc["pin_hash"] != pin_h:
                return []
            rows = conn.execute(
                """
                SELECT id, round_key, prize_name, claim_code, fulfillment_note, drawn_at
                FROM raffle_draw_results
                WHERE lower(winner_nickname) = lower(?)
                ORDER BY id DESC
                LIMIT 10
                """,
                (acc["nickname"],),
            ).fetchall()
            return [
                {
                    "id": int(r["id"]),
                    "round_key": r["round_key"],
                    "prize_name": r["prize_name"],
                    "claim_code": r["claim_code"],
                    "fulfillment_note": r["fulfillment_note"],
                    "drawn_at_kst": _fmt_kst_ts(r["drawn_at"]),
                }
                for r in rows
            ]

    return {"ok": True, "wins": run_with_db_retry(_op)}


@upgrade_router.get("/api/admin/raffle/rounds")
def admin_list_raffle_rounds(
    round_key: Optional[str] = None,
    x_sportiq_admin_key: Optional[str] = Header(default=None),
):
    _require_admin_auth(x_sportiq_admin_key)
    cur_round = datetime.now(KST).strftime("%Y-W%W")
    target_round = (round_key or cur_round).strip()[:20] or cur_round

    def _op() -> Dict[str, Any]:
        with _db_session(write=False) as conn:
            r_keys = [
                r["round_key"]
                for r in conn.execute(
                    """
                    SELECT DISTINCT round_key FROM (
                        SELECT round_key FROM member_raffle_entries
                        UNION
                        SELECT round_key FROM raffle_draw_results
                    ) ORDER BY round_key DESC
                    """
                ).fetchall()
                if r["round_key"]
            ]
            if cur_round not in r_keys:
                r_keys.insert(0, cur_round)
            if target_round not in r_keys:
                r_keys.insert(0, target_round)

            entry_rows = conn.execute(
                """
                SELECT MIN(e.nickname) AS nickname,
                       SUM(e.tickets) AS tickets,
                       MAX(e.created_at) AS last_entered,
                       COALESCE(MAX(m.points), 0) AS points,
                       COALESCE(MAX(m.role_tier), 'GUEST') AS role_tier,
                       COALESCE(MAX(m.active_badge), '') AS active_badge
                FROM member_raffle_entries e
                LEFT JOIN member_accounts m ON lower(m.nickname) = lower(e.nickname)
                WHERE e.round_key = ?
                GROUP BY lower(e.nickname)
                ORDER BY tickets DESC, last_entered ASC
                """,
                (target_round,),
            ).fetchall()

            total_tickets = sum(int(r["tickets"] or 0) for r in entry_rows)
            participants = []
            for r in entry_rows:
                t_cnt = int(r["tickets"] or 0)
                participants.append(
                    {
                        "nickname": r["nickname"],
                        "role_tier": r["role_tier"],
                        "active_badge": r["active_badge"],
                        "points": int(r["points"] or 0),
                        "tickets": t_cnt,
                        "win_probability_pct": round((t_cnt * 100.0 / total_tickets), 2) if total_tickets > 0 else 0.0,
                        "last_entered_kst": _fmt_kst_ts(r["last_entered"]),
                    }
                )

            draw_rows = conn.execute(
                """
                SELECT id, round_key, winner_nickname, winner_tickets, total_tickets,
                       total_participants, prize_name, draw_seed_hash, claim_code,
                       fulfillment_note, drawn_at
                FROM raffle_draw_results
                WHERE round_key = ?
                ORDER BY id DESC
                """,
                (target_round,),
            ).fetchall()

            draws = [
                {
                    "id": int(d["id"]),
                    "round_key": d["round_key"],
                    "winner_nickname": d["winner_nickname"],
                    "winner_tickets": int(d["winner_tickets"]),
                    "total_tickets": int(d["total_tickets"]),
                    "total_participants": int(d["total_participants"]),
                    "prize_name": d["prize_name"],
                    "draw_seed_hash": d["draw_seed_hash"],
                    "claim_code": d["claim_code"],
                    "fulfillment_note": d["fulfillment_note"],
                    "drawn_at_kst": _fmt_kst_ts(d["drawn_at"]),
                }
                for d in draw_rows
            ]

            return {
                "current_round": cur_round,
                "selected_round": target_round,
                "available_rounds": r_keys,
                "summary": {
                    "total_participants": len(participants),
                    "total_tickets": total_tickets,
                    "drawn_winners_count": len(draws),
                },
                "participants": participants,
                "draws": draws,
            }

    return {"ok": True, **run_with_db_retry(_op)}


@upgrade_router.post("/api/admin/raffle/draw")
def admin_draw_raffle_winners(
    payload: AdminRaffleDrawIn,
    x_sportiq_admin_key: Optional[str] = Header(default=None),
):
    _require_admin_auth(x_sportiq_admin_key)
    cur_round = datetime.now(KST).strftime("%Y-W%W")
    target_round = (payload.round_key or cur_round).strip()[:20] or cur_round
    winner_count = max(1, min(5, int(payload.winner_count or 1)))
    prize_name = (payload.prize_name or "편의점·커피 기프티콘 5,000원권").strip()[:100]

    def _op() -> List[Dict[str, Any]]:
        with _db_session(write=True) as conn:
            rows = conn.execute(
                """
                SELECT MIN(nickname) AS nickname, SUM(tickets) AS tickets
                FROM member_raffle_entries
                WHERE round_key = ?
                GROUP BY lower(nickname)
                ORDER BY tickets DESC, nickname ASC
                """,
                (target_round,),
            ).fetchall()
            if not rows:
                raise HTTPException(status_code=400, detail=f"선택한 회차({target_round})에 응모 내역이 없습니다.")

            all_total_tickets = sum(int(r["tickets"] or 0) for r in rows)
            all_participants = len(rows)

            already_won = set()
            if payload.exclude_already_won:
                prev = conn.execute(
                    "SELECT winner_nickname FROM raffle_draw_results WHERE round_key = ?",
                    (target_round,),
                ).fetchall()
                already_won = {str(p["winner_nickname"]).lower() for p in prev}

            pool = [
                {"nickname": r["nickname"], "tickets": int(r["tickets"] or 0)}
                for r in rows
                if int(r["tickets"] or 0) > 0 and str(r["nickname"]).lower() not in already_won
            ]
            if not pool:
                raise HTTPException(
                    status_code=400,
                    detail="추첨 가능한 남은 응모자가 없습니다. (이미 전원 당첨되었거나 응모권이 0장입니다.)",
                )

            actual_count = min(winner_count, len(pool))
            selected: List[Dict[str, Any]] = []
            now = time.time()

            for _ in range(actual_count):
                pool_weight = sum(x["tickets"] for x in pool)
                pick_idx_ticket = secrets.randbelow(pool_weight)
                csprng_nonce = secrets.token_hex(16)
                seed_src = json.dumps(
                    {"round": target_round, "pool": pool, "pick": pick_idx_ticket, "nonce": csprng_nonce},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                seed_hash = hashlib.sha256(seed_src.encode("utf-8")).hexdigest()

                acc_w = 0
                chosen_i = 0
                for idx, item in enumerate(pool):
                    acc_w += item["tickets"]
                    if pick_idx_ticket < acc_w:
                        chosen_i = idx
                        break

                winner = pool.pop(chosen_i)
                claim_code = f"SQ-GIFT-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
                cur = conn.execute(
                    """
                    INSERT INTO raffle_draw_results(
                        round_key, winner_nickname, winner_tickets, total_tickets,
                        total_participants, prize_name, draw_seed_hash, claim_code,
                        fulfillment_note, drawn_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '지급 대기', ?)
                    """,
                    (
                        target_round,
                        winner["nickname"],
                        winner["tickets"],
                        all_total_tickets,
                        all_participants,
                        prize_name,
                        seed_hash,
                        claim_code,
                        now,
                    ),
                )
                selected.append(
                    {
                        "id": int(cur.lastrowid or 0),
                        "round_key": target_round,
                        "winner_nickname": winner["nickname"],
                        "winner_tickets": winner["tickets"],
                        "total_tickets": all_total_tickets,
                        "prize_name": prize_name,
                        "draw_seed_hash": seed_hash,
                        "claim_code": claim_code,
                        "fulfillment_note": "지급 대기",
                        "drawn_at_kst": _fmt_kst_ts(now),
                    }
                )
            return selected

    drawn = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("raffle_draw_results",), daemon=True).start()
    return {"ok": True, "drawn": drawn}


@upgrade_router.post("/api/admin/raffle/fulfill")
def admin_update_raffle_fulfillment(
    payload: AdminRaffleFulfillIn,
    x_sportiq_admin_key: Optional[str] = Header(default=None),
):
    _require_admin_auth(x_sportiq_admin_key)
    note = (payload.fulfillment_note or "기프티콘 발송 완료").strip()[:120]

    def _op() -> None:
        with _db_session(write=True) as conn:
            cur = conn.execute(
                "UPDATE raffle_draw_results SET fulfillment_note = ? WHERE id = ?",
                (note, int(payload.draw_id)),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="해당 추첨 결과를 찾을 수 없습니다.")

    run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("raffle_draw_results",), daemon=True).start()
    return {"ok": True, "draw_id": int(payload.draw_id), "fulfillment_note": note}


# ============================================================================
# STAGE 3: WordPress Plugin <-> SportIQ 4-Digit PIN Point & Badge Sync Bridge
# ============================================================================
def _make_wp_bridge_token(nickname: str, pin_hash: str) -> str:
    msg = f"wp-bridge:v1:{(nickname or '').strip().lower()}:{pin_hash}".encode("utf-8")
    return hmac.new(PIN_SALT.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _authenticate_wp_member(
    conn: sqlite3.Connection,
    nickname: str,
    pin: str = "",
    bridge_token: str = "",
    allow_create_with_pin: bool = False,
) -> sqlite3.Row:
    nick = (nickname or "").strip()[:24]
    if len(nick) < 2:
        raise HTTPException(status_code=400, detail="닉네임은 2~24자로 입력해 주세요.")
    row = conn.execute(
        "SELECT * FROM member_accounts WHERE lower(nickname) = lower(?)",
        (nick,),
    ).fetchone()

    clean_pin = re.sub(r"\D", "", pin or "")
    if row is None:
        if allow_create_with_pin and len(clean_pin) == 4:
            pin_h = _hash_pin(nick, clean_pin)
            now = time.time()
            conn.execute(
                """
                INSERT INTO member_accounts(
                    nickname, pin_hash, points, role_tier, streak_days,
                    last_checkin_date, active_badge, owned_items_json, my_teams_json, updated_at
                ) VALUES (?, ?, 120, 'GUEST', 0, '', '', '[]', '[]', ?)
                """,
                (nick, pin_h, now),
            )
            return conn.execute(
                "SELECT * FROM member_accounts WHERE lower(nickname) = lower(?)",
                (nick,),
            ).fetchone()
        raise HTTPException(status_code=404, detail="등록된 SportIQ 닉네임을 찾을 수 없습니다. PIN 4자리와 함께 연동해 주세요.")

    if len(clean_pin) == 4:
        if not hmac.compare_digest(row["pin_hash"], _hash_pin(row["nickname"], clean_pin)):
            raise HTTPException(status_code=403, detail="4자리 PIN 번호가 일치하지 않습니다.")
        return row

    if bridge_token:
        expected_tok = _make_wp_bridge_token(row["nickname"], row["pin_hash"])
        if hmac.compare_digest(expected_tok, bridge_token.strip()):
            return row
        raise HTTPException(status_code=403, detail="워드프레스 연동 토큰이 만료되었거나 유효하지 않습니다. 다시 PIN을 입력해 주세요.")

    raise HTTPException(status_code=401, detail="4자리 PIN 또는 연동 토큰이 필요합니다.")


def _serialize_wp_member_profile(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    round_key = datetime.now(KST).strftime("%Y-W%W")
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    mt = conn.execute(
        "SELECT COALESCE(SUM(tickets), 0) AS t FROM member_raffle_entries WHERE round_key = ? AND lower(nickname)=lower(?)",
        (round_key, row["nickname"]),
    ).fetchone()
    pts = int(row["points"] or 0)
    tier = _tier_for_points(pts)
    if pts < 150:
        next_tier = {"target_tier": "MEMBER", "needed_points": 150 - pts}
    elif pts < 350:
        next_tier = {"target_tier": "PRO", "needed_points": 350 - pts}
    else:
        next_tier = {"target_tier": "MAX", "needed_points": 0}
    owned_keys = json.loads(row["owned_items_json"] or "[]")
    owned_badges = [it for it in SHOP_CATALOG if it["item_key"] in owned_keys]
    return {
        "nickname": row["nickname"],
        "points": pts,
        "role_tier": tier,
        "streak_days": int(row["streak_days"] or 0),
        "last_checkin_date": row["last_checkin_date"] or "",
        "checked_in_today": (row["last_checkin_date"] == today_kst),
        "active_badge": row["active_badge"] or "",
        "owned_items": owned_keys,
        "owned_badges": owned_badges,
        "my_teams": json.loads(row["my_teams_json"] or "[]"),
        "raffle_round": round_key,
        "raffle_my_tickets": int(mt["t"] or 0),
        "next_tier": next_tier,
    }


class WPMemberVerifyIn(BaseModel):
    nickname: str
    pin: Optional[str] = ""
    bridge_token: Optional[str] = ""
    site_origin: Optional[str] = ""


class WPMemberCheckinIn(BaseModel):
    nickname: str
    pin: Optional[str] = ""
    bridge_token: Optional[str] = ""
    site_origin: Optional[str] = ""


class WPMemberEquipIn(BaseModel):
    nickname: str
    item_key: str = ""
    pin: Optional[str] = ""
    bridge_token: Optional[str] = ""


class WPMemberRewardIn(BaseModel):
    nickname: str
    event_type: str = "wp_comment"
    ref_id: str = ""
    bridge_token: Optional[str] = ""
    pin: Optional[str] = ""
    site_origin: Optional[str] = ""


@upgrade_router.get("/api/wp/member/public-profile")
def wp_get_public_member_profile(nickname: str):
    nick = (nickname or "").strip()[:24]
    if len(nick) < 2:
        raise HTTPException(status_code=400, detail="닉네임을 입력해 주세요.")

    def _op() -> Optional[Dict[str, Any]]:
        with _db_session(write=False) as conn:
            row = conn.execute(
                "SELECT nickname, points, role_tier, active_badge, streak_days FROM member_accounts WHERE lower(nickname)=lower(?)",
                (nick,),
            ).fetchone()
            if not row:
                return None
            pts = int(row["points"] or 0)
            return {
                "nickname": row["nickname"],
                "points": pts,
                "role_tier": _tier_for_points(pts),
                "active_badge": row["active_badge"] or "",
                "streak_days": int(row["streak_days"] or 0),
            }

    prof = run_with_db_retry(_op)
    if prof is None:
        return {"ok": False, "found": False}
    return {"ok": True, "found": True, "profile": prof}


@upgrade_router.post("/api/wp/member/verify")
def wp_verify_member_bridge(payload: WPMemberVerifyIn):
    def _op() -> Tuple[str, Dict[str, Any]]:
        with _db_session(write=True) as conn:
            row = _authenticate_wp_member(
                conn,
                nickname=payload.nickname,
                pin=payload.pin or "",
                bridge_token=payload.bridge_token or "",
                allow_create_with_pin=True,
            )
            tok = _make_wp_bridge_token(row["nickname"], row["pin_hash"])
            prof = _serialize_wp_member_profile(conn, row)
            return tok, prof

    tok, prof = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("member_accounts",), daemon=True).start()
    return {
        "ok": True,
        "bridge_token": tok,
        "profile": prof,
        "catalog": SHOP_CATALOG,
        "tier_thresholds": {"MEMBER": 150, "PRO": 350},
    }


@upgrade_router.post("/api/wp/member/checkin")
def wp_member_daily_checkin(payload: WPMemberCheckinIn):
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    yesterday_kst = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
    origin = (payload.site_origin or "wordpress").strip()[:120]

    def _op() -> Tuple[bool, int, Dict[str, Any]]:
        with _db_session(write=True) as conn:
            row = _authenticate_wp_member(
                conn,
                nickname=payload.nickname,
                pin=payload.pin or "",
                bridge_token=payload.bridge_token or "",
                allow_create_with_pin=False,
            )
            if (row["last_checkin_date"] or "") == today_kst:
                return True, 0, _serialize_wp_member_profile(conn, row)

            prev_streak = int(row["streak_days"] or 0)
            new_streak = (prev_streak + 1) if (row["last_checkin_date"] == yesterday_kst) else 1
            bonus = 20 + (10 if new_streak > 0 and new_streak % 7 == 0 else 0)
            new_pts = int(row["points"] or 0) + bonus
            new_tier = _tier_for_points(new_pts)
            now = time.time()

            conn.execute(
                """
                UPDATE member_accounts
                SET points = ?, role_tier = ?, streak_days = ?, last_checkin_date = ?, updated_at = ?
                WHERE nickname = ?
                """,
                (new_pts, new_tier, new_streak, today_kst, now, row["nickname"]),
            )
            conn.execute(
                """
                INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at)
                VALUES (?, 'daily_checkin', ?, ?, ?, ?, ?)
                """,
                (row["nickname"], today_kst, bonus, today_kst, origin, now),
            )
            updated_row = conn.execute(
                "SELECT * FROM member_accounts WHERE nickname = ?",
                (row["nickname"],),
            ).fetchone()
            return False, bonus, _serialize_wp_member_profile(conn, updated_row)

    already, awarded, prof = run_with_db_retry(_op)
    if not already:
        threading.Thread(
            target=_trigger_pg_backup, args=("member_accounts", "wp_point_ledger"), daemon=True
        ).start()
    msg = (
        "오늘 이미 출석체크를 완료하셨습니다. (오픈 라운지·워드프레스 통합 1일 1회)"
        if already
        else f"출석체크 완료! +{awarded}P가 통합 계정에 적립되었습니다."
    )
    return {"ok": True, "already_checked": already, "awarded_points": awarded, "message": msg, "profile": prof}


@upgrade_router.post("/api/wp/member/equip-badge")
def wp_member_equip_badge(payload: WPMemberEquipIn):
    target_key = (payload.item_key or "").strip()

    def _op() -> Dict[str, Any]:
        with _db_session(write=True) as conn:
            row = _authenticate_wp_member(
                conn,
                nickname=payload.nickname,
                pin=payload.pin or "",
                bridge_token=payload.bridge_token or "",
                allow_create_with_pin=False,
            )
            owned: List[str] = json.loads(row["owned_items_json"] or "[]")
            if not target_key:
                new_badge = ""
            else:
                item = next((x for x in SHOP_CATALOG if x["item_key"] == target_key), None)
                if not item or target_key not in owned:
                    raise HTTPException(status_code=400, detail="보유 중인 칭호만 장착할 수 있습니다.")
                new_badge = item["badge_label"]
            conn.execute(
                "UPDATE member_accounts SET active_badge = ?, updated_at = ? WHERE nickname = ?",
                (new_badge, time.time(), row["nickname"]),
            )
            updated_row = conn.execute(
                "SELECT * FROM member_accounts WHERE nickname = ?",
                (row["nickname"],),
            ).fetchone()
            return _serialize_wp_member_profile(conn, updated_row)

    prof = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("member_accounts",), daemon=True).start()
    return {"ok": True, "profile": prof}


@upgrade_router.post("/api/wp/member/reward")
def wp_member_activity_reward(payload: WPMemberRewardIn):
    ev = (payload.event_type or "wp_comment").strip().lower()
    rewards_spec = {
        "wp_comment": (5, 3, "워드프레스 댓글 작성"),
        "wp_post": (15, 2, "워드프레스 분석글 작성"),
    }
    if ev not in rewards_spec:
        raise HTTPException(status_code=400, detail="지원하지 않는 활동 보상 유형입니다.")
    pts_delta, daily_cap, ev_label = rewards_spec[ev]
    ref_id = (payload.ref_id or "").strip()[:64]
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    origin = (payload.site_origin or "wordpress").strip()[:120]

    def _op() -> Tuple[bool, int, str, Dict[str, Any]]:
        with _db_session(write=True) as conn:
            row = _authenticate_wp_member(
                conn,
                nickname=payload.nickname,
                pin=payload.pin or "",
                bridge_token=payload.bridge_token or "",
                allow_create_with_pin=False,
            )
            if ref_id:
                dup = conn.execute(
                    "SELECT 1 FROM wp_point_ledger WHERE lower(nickname)=lower(?) AND event_type=? AND ref_id=?",
                    (row["nickname"], ev, ref_id),
                ).fetchone()
                if dup:
                    return False, 0, "이미 포인트가 적립된 활동입니다.", _serialize_wp_member_profile(conn, row)

            cnt_today = conn.execute(
                "SELECT COUNT(*) AS c FROM wp_point_ledger WHERE lower(nickname)=lower(?) AND event_type=? AND kst_date=?",
                (row["nickname"], ev, today_kst),
            ).fetchone()["c"]
            if int(cnt_today or 0) >= daily_cap:
                return (
                    False,
                    0,
                    f"일일 {ev_label} 포인트 적립 한도({daily_cap}회)를 모두 달성했습니다.",
                    _serialize_wp_member_profile(conn, row),
                )

            now = time.time()
            new_pts = int(row["points"] or 0) + pts_delta
            new_tier = _tier_for_points(new_pts)
            conn.execute(
                "UPDATE member_accounts SET points = ?, role_tier = ?, updated_at = ? WHERE nickname = ?",
                (new_pts, new_tier, now, row["nickname"]),
            )
            conn.execute(
                """
                INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (row["nickname"], ev, ref_id, pts_delta, today_kst, origin, now),
            )
            updated_row = conn.execute(
                "SELECT * FROM member_accounts WHERE nickname = ?",
                (row["nickname"],),
            ).fetchone()
            return True, pts_delta, f"{ev_label} 보상 +{pts_delta}P 적립 완료!", _serialize_wp_member_profile(conn, updated_row)

    credited, delta, msg, prof = run_with_db_retry(_op)
    if credited:
        threading.Thread(
            target=_trigger_pg_backup, args=("member_accounts", "wp_point_ledger"), daemon=True
        ).start()
    return {"ok": True, "credited": credited, "awarded_points": delta, "message": msg, "profile": prof}


# --- Frontend Upgrade Bundle (/api/member/p0p1-upgrade.js) ---
UPGRADE_JS = r"""
/* SportIQ Member-First P0+P1+P2 Upgrade Bundle v2.0 (PWA + Raffle Admin + WP Bridge) */
(function () {
  if (window.__SPORTIQ_MEMBER_UPGRADE_V1__) return;
  window.__SPORTIQ_MEMBER_UPGRADE_V1__ = true;

  var LS_NICK = "sportiq_member_nick";
  var LS_PIN = "sportiq_member_pin";
  var LS_BADGE = "sportiq_member_badge";
  var top20ShowAll = false;
  var bootTime = Date.now();
  var editingPostId = null;

  var TEAM_CANON_RULES = [
    [["newcastle", "뉴캐슬"], "newcastle"],
    [["arsenal", "아스널", "아스날"], "arsenal"],
    [["liverpool", "리버풀"], "liverpool"],
    [["manchester united", "man utd", "맨체스터 유나이티드", "맨유"], "manutd"],
    [["manchester city", "man city", "맨체스터 시티", "맨시티"], "mancity"],
    [["tottenham", "spurs", "토트넘"], "tottenham"],
    [["chelsea", "첼시"], "chelsea"],
    [["dortmund", "도르트문트"], "dortmund"],
    [["bayern", "바이에른", "뮌헨"], "bayern"],
    [["real madrid", "레알 마드리드", "레알"], "realmadrid"],
    [["celta", "셀타"], "celta"],
    [["barcelona", "바르셀로나", "바르샤"], "barcelona"],
    [["villarreal", "비야레알"], "villarreal"],
    [["atalanta", "아탈란타"], "atalanta"],
    [["inter", "인터 밀란", "인테르"], "inter"],
    [["lille", "릴 osc", "릴"], "lille"],
    [["spain", "스페인"], "spain"],
    [["england", "잉글랜드"], "england"],
    [["celtics", "셀틱스", "보스턴"], "celtics"],
    [["mavericks", "매버릭스", "댈러스"], "mavericks"],
    [["suns", "피닉스"], "suns"],
    [["lakers", "레이커스"], "lakers"],
    [["nuggets", "너기츠", "덴버"], "nuggets"],
    [["lions", "라이온스", "디트로이트"], "detroit_lions"],
    [["patriots", "패트리어츠", "뉴잉글랜드"], "patriots"],
    [["panthers", "팬서스", "플로리다"], "panthers"],
    [["oilers", "오일러스", "에드먼턴"], "oilers"],
    [["blackhawks", "블랙호크스", "시카고"], "blackhawks"],
    [["maple leafs", "메이플리프스", "토론토"], "mapleleafs"],
    [["rangers", "레인저스", "뉴욕"], "rangers"],
    [["canadiens", "캐나디언스", "몬트리올"], "canadiens"],
    [["lightning", "라이트닝", "탬파베이"], "lightning"],
    [["대한민국", "south korea", "korea"], "korea"],
    [["일본", "japan"], "japan"]
  ];

  function canonMatchupKey(txt) {
    var raw = String(txt || "").trim().toLowerCase();
    if (!raw) return "";
    var parts = raw.split(/\s+vs\.?\s+|\s+대\s+/i);
    var out = parts.map(function (side) {
      var s = side.trim();
      for (var i = 0; i < TEAM_CANON_RULES.length; i++) {
        var aliases = TEAM_CANON_RULES[i][0];
        for (var j = 0; j < aliases.length; j++) {
          if (s.indexOf(aliases[j]) !== -1) return TEAM_CANON_RULES[i][1];
        }
      }
      return s.replace(/[^a-z0-9가-힣]/g, "");
    });
    out.sort();
    return out.join("|");
  }

  function escHtml(v) {
    return v == null ? "" : String(v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function getSavedNick() {
    return (localStorage.getItem(LS_NICK) || localStorage.getItem("sportiq_guest_nick") || "").trim();
  }
  function getSavedPin() {
    return (localStorage.getItem(LS_PIN) || "").trim();
  }
  function getSavedBadge() {
    return (localStorage.getItem(LS_BADGE) || "").trim();
  }

  var st = document.createElement("style");
  st.id = "sportiqP0P1Css";
  st.textContent = [
    "#dcToastWrap { top: 72px !important; right: 16px !important; max-width: 320px !important; pointer-events: none; }",
    "#dcToastWrap .dc-toast { pointer-events: auto; position: relative; padding-right: 28px !important; }",
    ".sq-up-btn { background: linear-gradient(135deg,#0284c7,#0f766e); color:#fff; border:1px solid rgba(56,189,248,.5); border-radius:10px; padding:5px 11px; font-size:12px; font-weight:800; cursor:pointer; display:inline-flex; align-items:center; gap:5px; transition:all .15s; }",
    ".sq-up-btn:hover { filter:brightness(1.1); transform:translateY(-1px); }",
    ".sq-modal-backdrop { position:fixed; inset:0; background:rgba(2,6,23,.8); z-index:100000; display:none; align-items:center; justify-content:center; padding:16px; }",
    ".sq-modal-panel { background:#0f172a; border:1px solid #334155; border-radius:18px; max-width:540px; width:100%; max-height:88vh; overflow-y:auto; padding:20px; color:#f8fafc; box-shadow:0 20px 50px rgba(0,0,0,.6); }",
    ".sq-seed-badge { display:inline-flex; align-items:center; gap:4px; background:rgba(245,158,11,.14); color:#fcd34d; border:1px solid rgba(245,158,11,.35); border-radius:999px; padding:2px 8px; font-size:11px; font-weight:700; margin-left:6px; }"
  ].join("\n");
  document.head.appendChild(st);

  function fixInitialAttendanceStreak() {
    try {
      var checked = localStorage.getItem("sportiq_checked");
      if (!checked && !getSavedNick() && typeof state === "object" && state) {
        state.streak = 0;
        localStorage.setItem("sportiq_streak", "0");
        var sd = document.getElementById("streakDays");
        if (sd) sd.textContent = "0";
      }
    } catch (e) {}
  }

  function setupToastLimiter() {
    var wrap = document.getElementById("dcToastWrap");
    if (!wrap || wrap.__sqToastLimited) return;
    wrap.__sqToastLimited = true;
    var obs = new MutationObserver(function () {
      var cards = wrap.querySelectorAll(".dc-toast");
      var maxAllowed = (Date.now() - bootTime < 4500) ? 1 : 2;
      for (var i = 0; i < cards.length - maxAllowed; i++) {
        if (cards[i] && cards[i].parentNode) cards[i].parentNode.removeChild(cards[i]);
      }
      wrap.querySelectorAll(".dc-toast").forEach(function (c) {
        if (c.__sqCloseAdded) return;
        c.__sqCloseAdded = true;
        var x = document.createElement("span");
        x.textContent = "✕";
        x.style.cssText = "position:absolute;top:7px;right:9px;cursor:pointer;font-size:11px;opacity:.75;font-weight:900;";
        x.onclick = function (ev) { ev.stopPropagation(); if (c.parentNode) c.parentNode.removeChild(c); };
        c.appendChild(x);
      });
    });
    obs.observe(wrap, { childList: true });
  }

  // Deduplicate rendered cards directly in #globalTop10Grid (works even when globalMatchesData is let-scoped!)
  function deduplicateAndCompactTop20Dom() {
    var grid = document.getElementById("globalTop10Grid");
    if (!grid) return;
    var rawCards = Array.prototype.slice.call(grid.children);
    if (!rawCards.length || (rawCards.length === 1 && rawCards[0].textContent.indexOf("표시할 경기가") !== -1)) return;

    // First pass: prefer Korean-labeled card when an English duplicate exists anywhere in the list
    var koreanKeys = {};
    rawCards.forEach(function (card) {
      var titleEl = card.querySelector(".text-sm.font-extrabold") || card.querySelector("h4, strong");
      var mTxt = titleEl ? titleEl.textContent.trim() : "";
      if (/[가-힣]/.test(mTxt)) {
        var k = canonMatchupKey(mTxt);
        if (k) koreanKeys[k] = true;
      }
    });

    var seenKeys = {};
    rawCards.forEach(function (card) {
      var titleEl = card.querySelector(".text-sm.font-extrabold") || card.querySelector("h4, strong");
      var mTxt = titleEl ? titleEl.textContent.trim() : "";
      var k = canonMatchupKey(mTxt);
      if (!k) return;
      var hasKo = /[가-힣]/.test(mTxt);
      if (seenKeys[k] || (!hasKo && koreanKeys[k])) {
        if (card.parentNode) card.parentNode.removeChild(card);
        return;
      }
      seenKeys[k] = true;
    });

    // Re-number remaining rank badges sequentially (#1, #2, #3, 4위...) so there are zero rank gaps
    var cleanCards = Array.prototype.slice.call(grid.children);
    cleanCards.forEach(function (card, idx) {
      var rankNum = idx + 1;
      var badgeRow = card.querySelector(".flex.flex-wrap.items-center");
      if (badgeRow && badgeRow.firstElementChild) {
        var first = badgeRow.firstElementChild;
        if (rankNum > 3 && /위|#\d+/.test(first.textContent || "")) {
          first.textContent = rankNum + "위";
        }
      }
      card.style.display = (!top20ShowAll && idx >= 4) ? "none" : "";
    });

    var prevBtnWrap = document.getElementById("sqTop20MoreWrap");
    if (prevBtnWrap && prevBtnWrap.parentNode) prevBtnWrap.parentNode.removeChild(prevBtnWrap);
    if (cleanCards.length > 4) {
      var moreWrap = document.createElement("div");
      moreWrap.id = "sqTop20MoreWrap";
      moreWrap.style.cssText = "margin-top:10px;text-align:center;";
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "dc-pill";
      btn.style.cssText = "padding:6px 16px;font-weight:800;border-color:#f59e0b;color:#fde68a;";
      btn.textContent = top20ShowAll
        ? "▲ 상위 4개 빅매치만 간략히 보기"
        : "🔥 빅매치 TOP " + cleanCards.length + " 전체 펼치기 (나머지 " + (cleanCards.length - 4) + "경기 더보기 ▼)";
      btn.onclick = function () {
        top20ShowAll = !top20ShowAll;
        deduplicateAndCompactTop20Dom();
      };
      moreWrap.appendChild(btn);
      grid.parentNode.appendChild(moreWrap);
    }
  }

  function applyAboveTheFoldDiet() {
    var dcCol = document.getElementById("dcHubCollapsible");
    if (dcCol && !dcCol.__sqDietApplied) {
      dcCol.__sqDietApplied = true;
      var launcherGrid = document.getElementById("dcLauncherGrid");
      var hubBody = document.getElementById("dcHubBody");
      if (hubBody && launcherGrid) {
        hubBody.style.display = "none";
        var hint = document.createElement("div");
        hint.id = "dcQuickExpandHint";
        hint.style.cssText = "text-align:center;font-size:12px;color:#94a3b8;padding:6px 0 2px;cursor:pointer;";
        hint.innerHTML = "💡 위 데이터센터 탭 카드를 클릭하면 실시간 상세 표가 0.01초 만에 펼쳐집니다 <span style='color:#38bdf8;font-weight:700;'>(표 바로 펼치기 ▼)</span>";
        hint.onclick = function () {
          hubBody.style.display = "block";
          hint.style.display = "none";
        };
        dcCol.appendChild(hint);
        launcherGrid.addEventListener("click", function () {
          hubBody.style.display = "block";
          if (hint) hint.style.display = "none";
        }, true);
      }
    }

    if (typeof window.renderGlobalTop10 === "function" && !window.__sqTop20Wrapped) {
      window.__sqTop20Wrapped = true;
      var origRenderTop10 = window.renderGlobalTop10;
      window.renderGlobalTop10 = function () {
        origRenderTop10();
        deduplicateAndCompactTop20Dom();
      };
    }
    deduplicateAndCompactTop20Dom();
  }

  // Capture-phase Escape & Backdrop click handler so inputs/handlers never swallow Escape
  function setupUniversalModalClose() {
    if (window.__sqEscInstalled) return;
    window.__sqEscInstalled = true;

    function isModalVisible(el) {
      if (!el) return false;
      if (el.classList && el.classList.contains("hidden")) return false;
      var disp = window.getComputedStyle ? window.getComputedStyle(el).display : el.style.display;
      return disp && disp !== "none";
    }

    function hideKnownModal(el) {
      if (!el) return;
      if (el.id === "sqAdminRaffleModal" || el.id === "sqPwaIosModal" || el.id === "sqMemberHubModal" || el.id === "dcMatchModal" || el.id === "dcMatchDetailModal") {
        el.style.display = "none";
        return;
      }
      if (typeof window.closeModal === "function" && el.id) {
        try { window.closeModal(el.id); } catch (e) {}
      }
      if (el.classList) el.classList.add("hidden");
    }

    window.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape" && ev.key !== "Esc" && ev.keyCode !== 27) return;
      var ids = ["sqAdminRaffleModal", "sqPwaIosModal", "sqMemberHubModal", "commentModal", "writeModal", "dcMatchModal", "dcMatchDetailModal"];
      for (var i = 0; i < ids.length; i++) {
        var m = document.getElementById(ids[i]);
        if (isModalVisible(m)) {
          ev.preventDefault();
          ev.stopPropagation();
          if (document.activeElement && typeof document.activeElement.blur === "function") {
            document.activeElement.blur();
          }
          hideKnownModal(m);
          break;
        }
      }
    }, true);

    document.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.id) return;
      if (t.id === "sqAdminRaffleModal" || t.id === "sqPwaIosModal" || t.id === "sqMemberHubModal" || t.id === "commentModal" || t.id === "writeModal" || t.id === "dcMatchModal") {
        hideKnownModal(t);
      }
    });
  }

  function hookRoleAndAttendance() {
    if (typeof window.setRole === "function" && !window.__sqRoleHooked) {
      window.__sqRoleHooked = true;
      var origSetRole = window.setRole;
      window.setRole = function (role, notify) {
        var pts = (typeof state === "object" && state) ? Number(state.points || 0) : 120;
        if (notify && role === "PRO" && pts < 350) {
          if (typeof showToast === "function") {
            showToast("🔒 PRO 분석가 등급은 350P 달성 시 자동 승급됩니다! (현재 " + pts + "P · 출석/분석글 작성으로 적립)");
          }
          return;
        }
        if (notify && role === "MEMBER" && pts < 150) {
          if (typeof showToast === "function") {
            showToast("🔒 정회원(MEMBER) 등급은 150P 달성 시 자동 승급됩니다! (현재 " + pts + "P)");
          }
          return;
        }
        return origSetRole(role, notify);
      };
    }

    if (typeof window.checkAttendance === "function" && !window.__sqAttHooked) {
      window.__sqAttHooked = true;
      var origAtt = window.checkAttendance;
      window.checkAttendance = function () {
        origAtt();
        autoCheckTierUpgrade();
        syncAccountSilently();
      };
    }
  }

  function autoCheckTierUpgrade() {
    if (typeof state !== "object" || !state || typeof window.setRole !== "function") return;
    var pts = Number(state.points || 0);
    if (pts >= 350 && state.role !== "PRO") {
      window.setRole("PRO", false);
    } else if (pts >= 150 && state.role === "GUEST") {
      window.setRole("MEMBER", false);
    }
  }

  function syncAccountSilently() {
    var nick = getSavedNick();
    var pin = getSavedPin();
    if (!nick || pin.length !== 4 || typeof state !== "object" || !state) return;
    var teams = [];
    try { teams = JSON.parse(localStorage.getItem("sq_my_teams") || "[]"); } catch (e) {}
    fetch("/api/member/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        nickname: nick,
        pin: pin,
        client_points: Number(state.points || 120),
        client_streak: Number(state.streak || 0),
        client_checked_date: localStorage.getItem("sportiq_checked") || "",
        my_teams: teams
      })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok && d.profile) applyServerProfile(d.profile, false);
    }).catch(function () {});
  }

  function applyServerProfile(prof, announce) {
    if (!prof) return;
    localStorage.setItem(LS_NICK, prof.nickname);
    localStorage.setItem("sportiq_guest_nick", prof.nickname);
    if (prof.active_badge !== undefined) localStorage.setItem(LS_BADGE, prof.active_badge || "");
    if (Array.isArray(prof.my_teams) && prof.my_teams.length) {
      localStorage.setItem("sq_my_teams", JSON.stringify(prof.my_teams));
    }
    if (typeof state === "object" && state) {
      state.points = Number(prof.points || 120);
      state.streak = Number(prof.streak_days || 0);
      if (prof.last_checkin_date) {
        localStorage.setItem("sportiq_checked", prof.last_checkin_date);
        state.checkedToday = prof.last_checkin_date === (function(){ try { return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul" }).format(new Date()); } catch(e) { return new Date().toISOString().slice(0, 10); } })();
      }
      if (typeof window.updatePointsUI === "function") window.updatePointsUI();
      if (typeof window.setRole === "function") window.setRole(prof.role_tier || "GUEST", false);
    }
    refreshHeaderBadgeDisplay();
    if (announce && typeof showToast === "function") {
      showToast("✅ '" + prof.nickname + "' 계정 동기화 완료! (" + prof.points + "P · " + prof.role_tier + ")");
    }
  }

  function setupWriteModalAndServerPosts() {
    var wm = document.getElementById("writeModal");
    if (wm && !document.getElementById("sqWriteAuthorPinRow")) {
      var contentEl = document.getElementById("postContent");
      if (contentEl && contentEl.parentNode) {
        var row = document.createElement("div");
        row.id = "sqWriteAuthorPinRow";
        row.style.cssText = "display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;";
        row.innerHTML = [
          "<input id='sqPostAuthorInput' placeholder='작성자 닉네임 (예: 안필드전술가)' style='background:#020617;border:1px solid #334155;border-radius:10px;padding:8px 10px;color:#fff;font-size:12px;' />",
          "<input id='sqPostPinInput' type='password' maxlength='4' inputmode='numeric' placeholder='수정·삭제용 숫자 4자리 PIN' style='background:#020617;border:1px solid #334155;border-radius:10px;padding:8px 10px;color:#fff;font-size:12px;' />"
        ].join("");
        contentEl.parentNode.insertBefore(row, contentEl);
      }
    }

    document.addEventListener("click", function () {
      var ai = document.getElementById("sqPostAuthorInput");
      var pi = document.getElementById("sqPostPinInput");
      if (ai && !ai.value && getSavedNick()) ai.value = getSavedNick();
      if (pi && !pi.value && getSavedPin()) pi.value = getSavedPin();
    });

    window.submitNewPost = function () {
      var titleEl = document.getElementById("postTitle");
      var bodyEl = document.getElementById("postContent");
      var lgEl = document.getElementById("postLeague");
      var tagsEl = document.getElementById("postTags");
      var authEl = document.getElementById("sqPostAuthorInput");
      var pinEl = document.getElementById("sqPostPinInput");

      var title = (titleEl ? titleEl.value : "").trim();
      var body = (bodyEl ? bodyEl.value : "").trim();
      var league = lgEl ? lgEl.value : "EPL";
      var tagsRaw = (tagsEl ? tagsEl.value : "").trim();
      var author = ((authEl ? authEl.value : "") || getSavedNick() || ((typeof state === "object" && state.role) ? state.role + " 분석가" : "오픈분석가")).trim();
      var pin = ((pinEl ? pinEl.value : "") || getSavedPin()).replace(/\D/g, "");

      if (!title || !body) {
        if (typeof showToast === "function") showToast("제목과 분석 내용을 입력해 주세요.");
        return;
      }
      if (pin && pin.length !== 4) {
        if (typeof showToast === "function") showToast("수정·삭제용 PIN은 숫자 4자리로 입력해 주세요.");
        return;
      }
      var tags = tagsRaw ? tagsRaw.split(/\s+/).map(function (t) { return t.indexOf("#") === 0 ? t : "#" + t; }) : ["#" + league];

      if (editingPostId) {
        if (pin.length !== 4) {
          if (typeof showToast === "function") showToast("글 수정을 위해 작성 시 설정한 4자리 PIN을 입력해 주세요.");
          return;
        }
        fetch("/api/posts/" + encodeURIComponent(editingPostId), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title, body: body, league: league, tags: tags, pin: pin, author: author })
        }).then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
          .then(function (res) {
            if (res.status !== 200 || !res.data.ok) {
              if (typeof showToast === "function") showToast("❌ " + ((res.data && res.data.detail) || "수정 실패"));
              return;
            }
            var target = (state.posts || []).find(function (x) { return x.id === editingPostId; });
            if (target) {
              target.title = title;
              target.body = body;
              target.league = league;
              target.tags = tags;
            }
            editingPostId = null;
            if (titleEl) titleEl.value = "";
            if (bodyEl) bodyEl.value = "";
            if (typeof closeModal === "function") closeModal("writeModal");
            else document.getElementById("writeModal").classList.add("hidden");
            if (typeof renderFeed === "function") renderFeed();
            if (typeof showToast === "function") showToast("✏️ 분석글이 수정되어 영구 저장되었습니다.");
          });
        return;
      }

      fetch("/api/posts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          league: league,
          title: title,
          body: body,
          tags: tags,
          author: author,
          pin: pin,
          role: (typeof state === "object" && state.role) ? state.role : "GUEST"
        })
      }).then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
        .then(function (res) {
          if (res.status !== 200 || !res.data.ok) {
            if (typeof showToast === "function") showToast("❌ " + ((res.data && res.data.detail) || "등록 실패"));
            return;
          }
          if (pin.length === 4) {
            localStorage.setItem(LS_NICK, author);
            localStorage.setItem(LS_PIN, pin);
          }
          state.posts.unshift(res.data.post);
          state.points = res.data.updated_points != null ? Number(res.data.updated_points) : Number(state.points || 120) + 15;
          if (typeof updatePointsUI === "function") updatePointsUI();
          autoCheckTierUpgrade();
          if (titleEl) titleEl.value = "";
          if (bodyEl) bodyEl.value = "";
          if (typeof closeModal === "function") closeModal("writeModal");
          else document.getElementById("writeModal").classList.add("hidden");
          if (typeof switchTab === "function") switchTab("feed");
          if (typeof renderFeed === "function") renderFeed();
          if (typeof showToast === "function") showToast("✅ 새 분석글 영구 저장 완료! (+15P 적립)");
        });
    };

    window.submitComment = function () {
      var input = document.getElementById("commentModalInput");
      if (!input) return;
      var text = input.value.trim();
      if (!text || !state.activePostId) return;
      var nick = getSavedNick() || (state.role === "PRO" ? "PRO분석가" : (state.role === "MEMBER" ? "정회원" : "오픈게스트"));
      fetch("/api/posts/" + encodeURIComponent(state.activePostId) + "/comments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user: nick, nickname: nick, text: text })
      }).then(function (r) { return r.json(); }).then(function (d) {
        var p = (state.posts || []).find(function (x) { return x.id === state.activePostId; });
        if (p) {
          p.comments = p.comments || [];
          p.comments.push((d && d.comment) ? d.comment : { user: nick, text: text });
          input.value = "";
          if (typeof renderCommentModalList === "function") renderCommentModalList(p);
          if (typeof renderFeed === "function") renderFeed();
        }
      });
    };

    if (typeof window.renderFeed === "function" && !window.__sqFeedWrapped) {
      window.__sqFeedWrapped = true;
      var origRenderFeed = window.renderFeed;
      window.renderFeed = function () {
        origRenderFeed();
        var box = document.getElementById("feedList");
        if (!box || !Array.isArray(state.posts)) return;
        box.querySelectorAll("article").forEach(function (art) {
          var likeBtn = art.querySelector("button[onclick^='toggleLike(']");
          if (!likeBtn) return;
          var m = (likeBtn.getAttribute("onclick") || "").match(/toggleLike\(['"]([^'"]+)['"]\)/);
          if (!m) return;
          var pid = m[1];
          var postObj = state.posts.find(function (x) { return x.id === pid; });
          if (!postObj) return;

          if (postObj.authorBadge && !art.querySelector(".sq-author-badge")) {
            var authSpan = art.querySelector("span.text-slate-200");
            if (authSpan) {
              var b = document.createElement("span");
              b.className = "sq-author-badge";
              b.style.cssText = "background:rgba(56,189,248,.16);border:1px solid rgba(56,189,248,.4);color:#7dd3fc;padding:1px 7px;border-radius:999px;font-size:10.5px;font-weight:800;";
              b.textContent = postObj.authorBadge;
              authSpan.insertAdjacentElement("afterend", b);
            }
          }

          if (postObj.isMemberServerPost && !art.querySelector(".sq-edit-del-bar")) {
            var actionRow = likeBtn.parentNode;
            var wrap = document.createElement("span");
            wrap.className = "sq-edit-del-bar";
            wrap.style.cssText = "margin-left:auto;display:inline-flex;gap:6px;";
            wrap.innerHTML = [
              "<button type='button' class='rounded-lg bg-slate-800 px-2.5 py-1 text-[11px] text-sky-300 hover:bg-slate-700' data-sq-edit='" + escHtml(pid) + "'>✏️ 수정</button>",
              "<button type='button' class='rounded-lg bg-slate-800 px-2.5 py-1 text-[11px] text-rose-300 hover:bg-slate-700' data-sq-del='" + escHtml(pid) + "'>🗑️ 삭제</button>"
            ].join("");
            actionRow.appendChild(wrap);
          }
        });
      };

      document.addEventListener("click", function (ev) {
        var eb = ev.target.closest ? ev.target.closest("[data-sq-edit]") : null;
        if (eb) {
          var pid = eb.getAttribute("data-sq-edit");
          var p = (state.posts || []).find(function (x) { return x.id === pid; });
          if (!p) return;
          editingPostId = pid;
          document.getElementById("postLeague").value = p.league || "EPL";
          document.getElementById("postTitle").value = p.title || "";
          document.getElementById("postContent").value = p.body || "";
          document.getElementById("postTags").value = (p.tags || []).join(" ");
          var ai = document.getElementById("sqPostAuthorInput");
          var pi = document.getElementById("sqPostPinInput");
          if (ai) ai.value = p.author || "";
          if (pi) pi.value = getSavedPin() || "";
          if (typeof openModal === "function") openModal("writeModal");
          else document.getElementById("writeModal").classList.remove("hidden");
          return;
        }
        var db = ev.target.closest ? ev.target.closest("[data-sq-del]") : null;
        if (db) {
          var dpid = db.getAttribute("data-sq-del");
          var pin = window.prompt("게시글 삭제를 위해 작성 시 설정한 숫자 4자리 PIN을 입력해 주세요:", getSavedPin() || "");
          if (!pin) return;
          fetch("/api/posts/" + encodeURIComponent(dpid) + "?pin=" + encodeURIComponent(pin.trim()), { method: "DELETE" })
            .then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
            .then(function (res) {
              if (res.status !== 200 || !res.data.ok) {
                if (typeof showToast === "function") showToast("❌ " + ((res.data && res.data.detail) || "삭제 실패"));
                return;
              }
              state.posts = (state.posts || []).filter(function (x) { return x.id !== dpid; });
              window.renderFeed();
              if (typeof showToast === "function") showToast("🗑️ 게시글이 삭제되었습니다.");
            });
        }
      });
    }

    fetch("/api/posts")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok || typeof state !== "object" || !state) return;
        var existingIds = {};
        (state.posts || []).forEach(function (p) { existingIds[p.id] = p; });
        var extraComments = d.extra_comments || {};
        Object.keys(extraComments).forEach(function (pid) {
          if (existingIds[pid]) {
            existingIds[pid].comments = (existingIds[pid].comments || []).concat(extraComments[pid]);
          }
        });
        var newPosts = (d.posts || []).filter(function (p) { return !existingIds[p.id]; });
        if (newPosts.length) {
          state.posts = newPosts.concat(state.posts || []);
        }
        if (typeof window.renderFeed === "function") window.renderFeed();
      }).catch(function () {});
  }

  function mountMemberHubButtonsAndModal() {
    var ptsBadge = document.getElementById("pointsBadge");
    if (ptsBadge && ptsBadge.parentNode && !document.getElementById("sqOpenMemberHubBtn")) {
      var btn = document.createElement("button");
      btn.id = "sqOpenMemberHubBtn";
      btn.type = "button";
      btn.className = "sq-up-btn";
      btn.innerHTML = "🔐 PIN 기기연동 · 🛍️ 포인트 상점";
      btn.onclick = function () { openMemberHubModal(); };
      ptsBadge.insertAdjacentElement("afterend", btn);
    }

    if (!document.getElementById("sqMemberHubModal")) {
      var modal = document.createElement("div");
      modal.id = "sqMemberHubModal";
      modal.className = "sq-modal-backdrop";
      modal.setAttribute("tabindex", "-1");
      modal.innerHTML = [
        "<div class='sq-modal-panel'>",
        "  <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;'>",
        "    <h3 style='margin:0;font-size:17px;font-weight:900;color:#f8fafc;'>🔐 4자리 PIN 기기 연동 &amp; 🛍️ 포인트 라운지 상점</h3>",
        "    <button type='button' id='sqCloseMemberHubBtn' style='background:#1e293b;border:1px solid #334155;color:#cbd5e1;border-radius:8px;padding:4px 10px;cursor:pointer;font-weight:800;'>✕ 닫기 (Esc)</button>",
        "  </div>",
        "  <div style='background:#020617;border:1px solid #1e293b;border-radius:12px;padding:13px;margin-bottom:14px;'>",
        "    <div style='font-size:12px;color:#93c5fd;font-weight:700;margin-bottom:8px;'>📱 PC ↔ 스마트폰 간편 연동 (회원가입 없이 닉네임 + 숫자 4자리 PIN)</div>",
        "    <div style='display:grid;grid-template-columns:1fr 120px auto;gap:8px;'>",
        "      <input id='sqSyncNickInput' placeholder='닉네임 (2~24자)' style='background:#0f172a;border:1px solid #334155;border-radius:9px;padding:8px 10px;color:#fff;font-size:12.5px;' />",
        "      <input id='sqSyncPinInput' type='password' maxlength='4' inputmode='numeric' placeholder='숫자 4자리' style='background:#0f172a;border:1px solid #334155;border-radius:9px;padding:8px 10px;color:#fff;font-size:12.5px;' />",
        "      <button type='button' id='sqDoSyncBtn' class='sq-up-btn'>연동·불러오기</button>",
        "    </div>",
        "    <div id='sqSyncStatusMsg' style='font-size:11.5px;color:#94a3b8;margin-top:8px;'>자동 승급 기준: 150P 이상 정회원(MEMBER) · 350P 이상 PRO 분석가 자동 부여</div>",
        "  </div>",
        "  <div id='sqShopBodyArea' style='font-size:12.5px;color:#cbd5e1;'>상점 목록을 불러오는 중...</div>",
        "</div>"
      ].join("");
      document.body.appendChild(modal);
      document.getElementById("sqCloseMemberHubBtn").onclick = function () { modal.style.display = "none"; };
      document.getElementById("sqDoSyncBtn").onclick = function () {
        var nk = document.getElementById("sqSyncNickInput").value.trim();
        var pn = document.getElementById("sqSyncPinInput").value.replace(/\D/g, "");
        var msgEl = document.getElementById("sqSyncStatusMsg");
        var teams = [];
        try { teams = JSON.parse(localStorage.getItem("sq_my_teams") || "[]"); } catch (e) {}
        fetch("/api/member/sync", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            nickname: nk,
            pin: pn,
            client_points: (typeof state === "object" && state) ? Number(state.points || 120) : 120,
            client_streak: (typeof state === "object" && state) ? Number(state.streak || 0) : 0,
            client_checked_date: localStorage.getItem("sportiq_checked") || "",
            my_teams: teams
          })
        }).then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
          .then(function (res) {
            if (res.status !== 200 || !res.data.ok) {
              msgEl.style.color = "#f87171";
              msgEl.textContent = "❌ " + ((res.data && res.data.detail) || "연동 실패");
              return;
            }
            localStorage.setItem(LS_PIN, pn);
            applyServerProfile(res.data.profile, true);
            msgEl.style.color = "#4ade80";
            msgEl.textContent = "✅ '" + res.data.profile.nickname + "' 연동 완료! 보유 포인트: " + res.data.profile.points + "P · 등급: " + res.data.profile.role_tier;
            loadShopItems();
          });
      };
    }
    refreshHeaderBadgeDisplay();
  }

  function refreshHeaderBadgeDisplay() {
    var roleEl = document.getElementById("currentRoleBadge");
    if (!roleEl) return;
    var nick = getSavedNick();
    var badge = getSavedBadge();
    if (nick) {
      roleEl.textContent = nick + (badge ? " · " + badge : "");
    }
  }

  function openMemberHubModal() {
    var m = document.getElementById("sqMemberHubModal");
    if (!m) return;
    var ni = document.getElementById("sqSyncNickInput");
    var pi = document.getElementById("sqSyncPinInput");
    if (ni) ni.value = getSavedNick();
    if (pi) pi.value = getSavedPin();
    m.style.display = "flex";
    if (m.focus) m.focus();
    loadShopItems();
  }
  window.__openSportiqMemberHub = openMemberHubModal;

  function loadShopItems() {
    var box = document.getElementById("sqShopBodyArea");
    if (!box) return;
    fetch("/api/member/shop?nickname=" + encodeURIComponent(getSavedNick()))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        var owned = (d.profile && d.profile.owned_items) ? d.profile.owned_items : [];
        var activeBadge = (d.profile && d.profile.active_badge) || getSavedBadge();
        var cards = (d.catalog || []).map(function (it) {
          var isOwned = owned.indexOf(it.item_key) !== -1;
          var isEquipped = activeBadge === it.badge_label;
          var btnLabel = isEquipped ? "장착 중" : (isOwned ? "무료 장착하기" : (it.cost + "P 구매·장착"));
          return [
            "<div style='background:#0b1220;border:1px solid #1e293b;border-radius:12px;padding:11px;display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:8px;'>",
            "  <div>",
            "    <div style='font-weight:800;font-size:13px;color:" + escHtml(it.badge_color) + ";'>" + escHtml(it.badge_label) + "</div>",
            "    <div style='font-size:11.5px;color:#94a3b8;margin-top:2px;'>" + escHtml(it.desc) + "</div>",
            "  </div>",
            "  <button type='button' data-sq-buy='" + escHtml(it.item_key) + "' class='dc-pill' style='font-weight:800;border-color:#38bdf8;color:#e0f2fe;'>" + escHtml(btnLabel) + "</button>",
            "</div>"
          ].join("");
        }).join("");

        var rf = d.raffle || {};
        var raffleHtml = [
          "<div style='background:linear-gradient(135deg,rgba(217,119,6,.16),rgba(15,23,42,.95));border:1px solid rgba(245,158,11,.45);border-radius:12px;padding:12px;margin-top:12px;'>",
          "  <div style='display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;'>",
          "    <div>",
          "      <div style='font-weight:800;color:#fde68a;font-size:13px;'>🎁 " + escHtml(rf.prize_title || "주간 럭키드로우") + " (" + escHtml(rf.round_key || "") + ")</div>",
          "      <div style='font-size:11.5px;color:#cbd5e1;margin-top:3px;'>전체 응모: <strong>" + escHtml(rf.total_tickets || 0) + "장</strong> · 내 응모권: <strong style='color:#4ade80;'>" + escHtml(rf.my_tickets || 0) + "장</strong></div>",
          "    </div>",
          "    <div style='display:flex;gap:6px;align-items:center;'>",
          "      <button type='button' id='sqEnterRaffleBtn' class='sq-up-btn' style='background:linear-gradient(135deg,#d97706,#b45309);'>50P 응모하기</button>",
          "      <button type='button' id='sqOpenAdminRaffleBtn' class='dc-pill' style='font-size:11px;padding:5px 9px;border-color:#475569;color:#cbd5e1;' title='관리자 전용 응모 조회 및 공정 추첨'>👑 추첨 관리</button>",
          "    </div>",
          "  </div>",
          "  <div id='sqMyWinningClaimArea' style='margin-top:8px;'></div>",
          "  <div id='sqPublicWinnersArea' style='margin-top:8px;padding-top:8px;border-top:1px solid rgba(148,163,184,.18);font-size:11.5px;color:#94a3b8;'>최근 당첨자 발표 내역을 불러오는 중...</div>",
          "</div>"
        ].join("");

        box.innerHTML = "<div style='font-weight:800;color:#f8fafc;margin-bottom:8px;'>🏅 전용 칭호·배지 상점 (구매 즉시 게시글·응원톡·워드프레스 연동 표시)</div>" + cards + raffleHtml;
        loadPublicAndMyRaffleWinners();
        var admBtn = document.getElementById("sqOpenAdminRaffleBtn");
        if (admBtn) admBtn.onclick = function () { openAdminRaffleModal(); };

        box.querySelectorAll("[data-sq-buy]").forEach(function (b) {
          b.onclick = function () {
            var ik = b.getAttribute("data-sq-buy");
            var nk = (document.getElementById("sqSyncNickInput").value || getSavedNick()).trim();
            var pn = (document.getElementById("sqSyncPinInput").value || getSavedPin()).replace(/\D/g, "");
            fetch("/api/member/shop/buy", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ nickname: nk, pin: pn, item_key: ik })
            }).then(function (r) { return r.json().then(function (x) { return { status: r.status, data: x }; }); })
              .then(function (res) {
                if (res.status !== 200 || !res.data.ok) {
                  if (typeof showToast === "function") showToast("❌ " + ((res.data && res.data.detail) || "구매 실패"));
                  return;
                }
                localStorage.setItem(LS_PIN, pn);
                applyServerProfile(res.data.profile, false);
                if (typeof showToast === "function") showToast("🎉 " + res.data.message);
                loadShopItems();
              });
          };
        });

        var rBtn = document.getElementById("sqEnterRaffleBtn");
        if (rBtn) {
          rBtn.onclick = function () {
            var nk = (document.getElementById("sqSyncNickInput").value || getSavedNick()).trim();
            var pn = (document.getElementById("sqSyncPinInput").value || getSavedPin()).replace(/\D/g, "");
            fetch("/api/member/raffle/enter", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ nickname: nk, pin: pn })
            }).then(function (r) { return r.json().then(function (x) { return { status: r.status, data: x }; }); })
              .then(function (res) {
                if (res.status !== 200 || !res.data.ok) {
                  if (typeof showToast === "function") showToast("❌ " + ((res.data && res.data.detail) || "응모 실패"));
                  return;
                }
                if (typeof state === "object" && state) {
                  state.points = Number(res.data.points);
                  if (typeof updatePointsUI === "function") updatePointsUI();
                }
                if (typeof showToast === "function") showToast("🎁 주간 럭키드로우 응모 완료! (내 응모권 " + res.data.my_tickets + "장)");
                loadShopItems();
              });
          };
        }
      });
  }

  function polishLabelsAndProvenance() {
    document.querySelectorAll("button, a, span").forEach(function (el) {
      if (!el.children.length && el.textContent && el.textContent.indexOf("하이라이트 유튜브 숏츠") !== -1) {
        el.textContent = el.textContent.replace("하이라이트 유튜브 숏츠", "9:16 AI 전술 숏츠");
      }
    });

    var dcBody = document.getElementById("dcHubBody");
    if (dcBody && !dcBody.querySelector(".sq-seed-badge")) {
      var txt = dcBody.textContent || "";
      if (txt.indexOf("부상·결장 사유") !== -1 || txt.indexOf("구단별 결장") !== -1 || txt.indexOf("몸값 TOP") !== -1) {
        var firstHdr = dcBody.querySelector("div, h4");
        if (firstHdr && !firstHdr.querySelector(".sq-seed-badge")) {
          var sb = document.createElement("span");
          sb.className = "sq-seed-badge";
          sb.textContent = "📌 주요 구단 기준 시드 포함 (참고용)";
          firstHdr.appendChild(sb);
        }
      }
    }
  }


  // ==========================================================================
  // STAGE 1 FRONTEND: PWA Service Worker, Header Install Button & Mobile Banner
  // ==========================================================================
  var deferredPwaPrompt = null;
  var LS_PWA_DISMISS = "sq_pwa_dismiss_until";

  function isStandalonePwa() {
    try {
      return (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || window.navigator.standalone === true;
    } catch (e) {
      return false;
    }
  }

  function isIosSafariDevice() {
    var ua = (window.navigator.userAgent || "").toLowerCase();
    var isIos = /iphone|ipad|ipod/.test(ua);
    var isCriOS = /crios|fxios|edgios/.test(ua);
    return isIos && !isCriOS && !isStandalonePwa();
  }

  function showIosInstallGuideModal() {
    var m = document.getElementById("sqPwaIosModal");
    if (!m) {
      m = document.createElement("div");
      m.id = "sqPwaIosModal";
      m.className = "sq-modal-backdrop";
      m.setAttribute("tabindex", "-1");
      m.innerHTML = [
        "<div class='sq-modal-panel' style='max-width:400px;'>",
        "  <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;'>",
        "    <h3 style='margin:0;font-size:16px;font-weight:900;color:#f8fafc;'>📲 아이폰·아이패드 홈 화면 앱 추가</h3>",
        "    <button type='button' id='sqCloseIosGuideBtn' style='background:#1e293b;border:1px solid #334155;color:#cbd5e1;border-radius:8px;padding:4px 9px;cursor:pointer;font-weight:800;'>✕ (Esc)</button>",
        "  </div>",
        "  <div style='background:#020617;border:1px solid #1e293b;border-radius:12px;padding:13px;font-size:13px;line-height:1.65;color:#e2e8f0;'>",
        "    <div style='margin-bottom:8px;'>1️⃣ Safari 하단 바 가운데의 <strong style='color:#38bdf8;'>[공유 ↑]</strong> 아이콘을 탭합니다.</div>",
        "    <div style='margin-bottom:8px;'>2️⃣ 메뉴를 살짝 올려 <strong style='color:#4ade80;'>[홈 화면에 추가 ➕]</strong>를 선택합니다.</div>",
        "    <div style='color:#94a3b8;font-size:11.5px;'>설치 즉시 주소창 없는 전체화면 네이티브 앱 모드로 실시간 스코어와 드롭오즈를 0.01초 만에 열 수 있습니다.</div>",
        "  </div>",
        "</div>"
      ].join("");
      document.body.appendChild(m);
      document.getElementById("sqCloseIosGuideBtn").onclick = function () { m.style.display = "none"; };
    }
    m.style.display = "flex";
  }

  function triggerPwaInstallAction() {
    if (deferredPwaPrompt) {
      deferredPwaPrompt.prompt();
      deferredPwaPrompt.userChoice.then(function (choice) {
        if (choice && choice.outcome === "accepted") {
          if (typeof showToast === "function") showToast("🎉 SportIQ 앱이 홈 화면에 추가되었습니다!");
          hidePwaInstallElements();
        }
        deferredPwaPrompt = null;
      });
      return;
    }
    if (isIosSafariDevice()) {
      showIosInstallGuideModal();
      return;
    }
    if (typeof showToast === "function") {
      showToast("📲 브라우저 우측 상단 메뉴(⋮) → '앱 설치' 또는 '홈 화면에 추가'를 눌러 바로 설치하실 수 있습니다.");
    }
  }

  function hidePwaInstallElements() {
    var b = document.getElementById("sqPwaInstallBanner");
    if (b) b.style.display = "none";
    var hb = document.getElementById("sqHeaderPwaBtn");
    if (hb && isStandalonePwa()) hb.style.display = "none";
  }

  function mountPwaInstallUi() {
    if (isStandalonePwa()) {
      hidePwaInstallElements();
      return;
    }
    var hubBtn = document.getElementById("sqOpenMemberHubBtn");
    if (hubBtn && hubBtn.parentNode && !document.getElementById("sqHeaderPwaBtn")) {
      var pbtn = document.createElement("button");
      pbtn.id = "sqHeaderPwaBtn";
      pbtn.type = "button";
      pbtn.className = "sq-up-btn";
      pbtn.style.background = "linear-gradient(135deg,#1e293b,#0f172a)";
      pbtn.style.borderColor = "rgba(52,211,153,.55)";
      pbtn.style.color = "#6ee7b7";
      pbtn.innerHTML = "📲 앱 설치";
      pbtn.onclick = triggerPwaInstallAction;
      hubBtn.insertAdjacentElement("afterend", pbtn);
    }

    var dismissUntil = Number(localStorage.getItem(LS_PWA_DISMISS) || 0);
    var shouldShowBanner = (Date.now() > dismissUntil) && (Boolean(deferredPwaPrompt) || isIosSafariDevice() || window.innerWidth <= 820);
    if (shouldShowBanner && !document.getElementById("sqPwaInstallBanner")) {
      var banner = document.createElement("div");
      banner.id = "sqPwaInstallBanner";
      banner.style.cssText = "position:fixed;left:14px;bottom:16px;z-index:9990;background:rgba(15,23,42,.96);border:1px solid rgba(56,189,248,.45);border-radius:14px;padding:11px 13px;max-width:340px;box-shadow:0 14px 32px rgba(0,0,0,.55);display:flex;align-items:center;gap:10px;backdrop-filter:blur(8px);";
      banner.innerHTML = [
        "<img src='/pwa-icon-192.png' alt='SportIQ' style='width:38px;height:38px;border-radius:10px;flex-shrink:0;' />",
        "<div style='flex:1;min-width:0;'>",
        "  <div style='font-size:12.5px;font-weight:800;color:#f8fafc;'>SportIQ 홈 화면 앱으로 열기</div>",
        "  <div style='font-size:11px;color:#94a3b8;margin-top:1px;'>전체화면 스코어 · 실시간 드롭오즈 빠른 실행</div>",
        "</div>",
        "<button type='button' id='sqBannerInstallNow' class='sq-up-btn' style='padding:6px 10px;font-size:11.5px;white-space:nowrap;'>설치</button>",
        "<button type='button' id='sqBannerDismissPwa' aria-label='닫기' style='background:transparent;border:0;color:#94a3b8;cursor:pointer;font-weight:900;font-size:13px;padding:2px 4px;'>✕</button>"
      ].join("");
      document.body.appendChild(banner);
      document.getElementById("sqBannerInstallNow").onclick = triggerPwaInstallAction;
      document.getElementById("sqBannerDismissPwa").onclick = function () {
        localStorage.setItem(LS_PWA_DISMISS, String(Date.now() + 7 * 86400 * 1000));
        banner.style.display = "none";
      };
    }
  }

  function setupPwaSupport() {
    if ("serviceWorker" in navigator && !window.__sqSwRegistered) {
      window.__sqSwRegistered = true;
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
    }
    window.addEventListener("beforeinstallprompt", function (e) {
      e.preventDefault();
      deferredPwaPrompt = e;
      mountPwaInstallUi();
    });
    window.addEventListener("appinstalled", function () {
      deferredPwaPrompt = null;
      hidePwaInstallElements();
    });
    mountPwaInstallUi();

    try {
      var params = new URLSearchParams(window.location.search || "");
      if (params.get("open") === "shop" && !window.__sqShortcutHandled) {
        window.__sqShortcutHandled = true;
        setTimeout(function () { openMemberHubModal(); }, 350);
      } else if (params.get("tab") === "datacenter" && !window.__sqShortcutHandled) {
        window.__sqShortcutHandled = true;
        setTimeout(function () {
          var hb = document.getElementById("dcHubBody");
          var hint = document.getElementById("dcQuickExpandHint");
          if (hb) hb.style.display = "block";
          if (hint) hint.style.display = "none";
        }, 350);
      }
    } catch (e) {}
  }

  // ==========================================================================
  // STAGE 2 FRONTEND: Public Winners Feed, Personal Claim Code & Admin Console
  // ==========================================================================
  var SS_ADMIN_KEY = "sq_admin_session_key";

  function loadPublicAndMyRaffleWinners() {
    var pubBox = document.getElementById("sqPublicWinnersArea");
    var myBox = document.getElementById("sqMyWinningClaimArea");
    if (pubBox) {
      fetch("/api/raffle/public-winners?limit=5")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var list = (d && d.winners) || [];
          if (!list.length) {
            pubBox.innerHTML = "🏆 <strong>주간 당첨자 투명 발표:</strong> 이번 회차 추첨 대기 중 (CSPRNG 가중치 해시 검증 적용)";
            return;
          }
          pubBox.innerHTML = "<div style='font-weight:800;color:#fde68a;margin-bottom:5px;'>🏆 최근 주간 기프티콘 당첨자 (해시 검증 완료)</div>" +
            list.map(function (w) {
              return "<div style='display:flex;justify-content:space-between;align-items:center;gap:6px;padding:4px 0;border-bottom:1px dashed rgba(148,163,184,.12);'>" +
                "<span><strong>" + escHtml(w.round_key) + "</strong> · <span style='color:#38bdf8;font-weight:800;'>" + escHtml(w.winner_nickname) + "</span> " + escHtml(w.active_badge || "") + " (" + escHtml(w.winner_tickets) + "장/" + escHtml(w.win_odds_pct) + "%)</span>" +
                "<span style='color:#86efac;font-size:11px;'>" + escHtml(w.fulfillment_note) + " · #" + escHtml((w.draw_seed_prefix || "").slice(0, 8)) + "</span>" +
                "</div>";
            }).join("");
        }).catch(function () {});
    }

    var nk = getSavedNick();
    var pn = getSavedPin();
    if (myBox && nk && pn.length === 4) {
      fetch("/api/member/raffle/my-wins", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nickname: nk, pin: pn })
      }).then(function (r) { return r.json(); })
        .then(function (d) {
          var wins = (d && d.wins) || [];
          if (!wins.length) { myBox.innerHTML = ""; return; }
          myBox.innerHTML = wins.map(function (w) {
            return "<div style='background:rgba(16,185,129,.16);border:1px solid rgba(52,211,153,.55);border-radius:10px;padding:9px 11px;margin-top:6px;font-size:12px;color:#ecfdf5;'>" +
              "🎉 <strong>[" + escHtml(w.round_key) + "] 당첨 축하드립니다!</strong> (" + escHtml(w.prize_name) + ")<br/>" +
              "🔑 본인 전용 수령 인증코드: <code style='background:#020617;padding:2px 7px;border-radius:6px;color:#fde68a;font-weight:900;'>" + escHtml(w.claim_code) + "</code> · 상태: <strong>" + escHtml(w.fulfillment_note) + "</strong>" +
              "</div>";
          }).join("");
        }).catch(function () {});
    }
  }

  function openAdminRaffleModal() {
    var m = document.getElementById("sqAdminRaffleModal");
    if (!m) {
      m = document.createElement("div");
      m.id = "sqAdminRaffleModal";
      m.className = "sq-modal-backdrop";
      m.style.zIndex = "100020";
      m.setAttribute("tabindex", "-1");
      m.innerHTML = [
        "<div class='sq-modal-panel' style='max-width:660px;'>",
        "  <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;'>",
        "    <h3 style='margin:0;font-size:16.5px;font-weight:900;color:#fde68a;'>👑 주간 기프티콘 응모 조회 &amp; 공정 추첨 관리 콘솔</h3>",
        "    <button type='button' id='sqCloseAdminRaffleBtn' style='background:#1e293b;border:1px solid #334155;color:#cbd5e1;border-radius:8px;padding:4px 10px;cursor:pointer;font-weight:800;'>✕ 닫기 (Esc)</button>",
        "  </div>",
        "  <div id='sqAdminAuthBox' style='background:#020617;border:1px solid #1e293b;border-radius:12px;padding:12px;margin-bottom:12px;'></div>",
        "  <div id='sqAdminConsoleBody' style='font-size:12.5px;color:#cbd5e1;'>관리자 인증 상태를 확인하는 중...</div>",
        "</div>"
      ].join("");
      document.body.appendChild(m);
      document.getElementById("sqCloseAdminRaffleBtn").onclick = function () { m.style.display = "none"; };
    }
    m.style.display = "flex";
    if (m.focus) m.focus();
    refreshAdminRaffleState();
  }

  function refreshAdminRaffleState(selectedRound) {
    var authBox = document.getElementById("sqAdminAuthBox");
    var bodyBox = document.getElementById("sqAdminConsoleBody");
    if (!authBox || !bodyBox) return;

    fetch("/api/admin/raffle/auth-state")
      .then(function (r) { return r.json(); })
      .then(function (st) {
        if (!st || !st.configured) {
          authBox.innerHTML = [
            "<div style='font-size:12px;color:#fde68a;font-weight:800;margin-bottom:7px;'>🔐 최초 1회 관리자 마스터 키 설정 (6~48자, PBKDF2 암호화·PostgreSQL 영구 백업)</div>",
            "<div style='display:grid;grid-template-columns:1fr auto;gap:8px;'>",
            "  <input id='sqNewAdminKeyInput' type='password' placeholder='사용하실 관리자 전용 마스터 비밀번호 (6자 이상)' style='background:#0f172a;border:1px solid #334155;border-radius:9px;padding:8px 10px;color:#fff;font-size:12.5px;' />",
            "  <button type='button' id='sqSaveAdminKeyBtn' class='sq-up-btn'>마스터 키 저장</button>",
            "</div>"
          ].join("");
          bodyBox.innerHTML = "<div style='padding:12px;color:#94a3b8;text-align:center;'>마스터 키를 설정하면 즉시 회차별 응모자 목록 조회 및 당첨자 추첨을 진행할 수 있습니다.</div>";
          document.getElementById("sqSaveAdminKeyBtn").onclick = function () {
            var nk = document.getElementById("sqNewAdminKeyInput").value.trim();
            fetch("/api/admin/setup-key", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ new_admin_key: nk })
            }).then(function (r) { return r.json().then(function (x) { return { status: r.status, data: x }; }); })
              .then(function (res) {
                if (res.status !== 200 || !res.data.ok) {
                  if (typeof showToast === "function") showToast("❌ " + ((res.data && res.data.detail) || "설정 실패"));
                  return;
                }
                sessionStorage.setItem(SS_ADMIN_KEY, nk);
                if (typeof showToast === "function") showToast("✅ 관리자 마스터 키가 설정되었습니다!");
                refreshAdminRaffleState(selectedRound);
              });
          };
          return;
        }

        var savedKey = sessionStorage.getItem(SS_ADMIN_KEY) || "";
        authBox.innerHTML = [
          "<div style='display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;'>",
          "  <input id='sqAdminKeyInput' type='password' value='" + escHtml(savedKey) + "' placeholder='관리자 마스터 키 입력' style='background:#0f172a;border:1px solid #334155;border-radius:9px;padding:8px 10px;color:#fff;font-size:12.5px;' />",
          "  <button type='button' id='sqLoadAdminRoundsBtn' class='sq-up-btn'>응모 현황 불러오기</button>",
          "</div>"
        ].join("");
        document.getElementById("sqLoadAdminRoundsBtn").onclick = function () {
          var k = document.getElementById("sqAdminKeyInput").value.trim();
          sessionStorage.setItem(SS_ADMIN_KEY, k);
          loadAdminRoundData(k, selectedRound || st.current_round);
        };
        if (savedKey) {
          loadAdminRoundData(savedKey, selectedRound || st.current_round);
        } else {
          bodyBox.innerHTML = "<div style='padding:12px;color:#94a3b8;text-align:center;'>관리자 마스터 키를 입력하고 [응모 현황 불러오기]를 눌러주세요.</div>";
        }
      });
  }

  function loadAdminRoundData(adminKey, roundKey) {
    var bodyBox = document.getElementById("sqAdminConsoleBody");
    if (!bodyBox) return;
    fetch("/api/admin/raffle/rounds?round_key=" + encodeURIComponent(roundKey || ""), {
      headers: { "X-SportIQ-Admin-Key": adminKey }
    }).then(function (r) { return r.json().then(function (x) { return { status: r.status, data: x }; }); })
      .then(function (res) {
        if (res.status !== 200 || !res.data.ok) {
          bodyBox.innerHTML = "<div style='padding:12px;color:#f87171;font-weight:700;'>❌ " + escHtml((res.data && res.data.detail) || "권한 확인 실패") + "</div>";
          return;
        }
        var d = res.data;
        var opts = (d.available_rounds || []).map(function (rk) {
          return "<option value='" + escHtml(rk) + "'" + (rk === d.selected_round ? " selected" : "") + ">" + escHtml(rk) + "</option>";
        }).join("");

        var pRows = (d.participants || []).map(function (p, i) {
          return "<tr style='border-bottom:1px solid #1e293b;'>" +
            "<td style='padding:6px;'>" + (i + 1) + "</td>" +
            "<td style='padding:6px;font-weight:800;color:#f8fafc;'>" + escHtml(p.nickname) + " <span style='color:#38bdf8;font-size:11px;'>" + escHtml(p.active_badge || "") + "</span></td>" +
            "<td style='padding:6px;'>" + escHtml(p.role_tier) + " (" + escHtml(p.points) + "P)</td>" +
            "<td style='padding:6px;font-weight:800;color:#4ade80;'>" + escHtml(p.tickets) + "장</td>" +
            "<td style='padding:6px;color:#fde68a;'>" + escHtml(p.win_probability_pct) + "%</td>" +
            "</tr>";
        }).join("");

        var dRows = (d.draws || []).map(function (w) {
          var isDone = (w.fulfillment_note || "").indexOf("완료") !== -1;
          var nextStatus = isDone ? "지급 대기" : "기프티콘 발송 완료";
          return "<div style='background:#0b1220;border:1px solid #1e293b;border-radius:10px;padding:9px 11px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;'>" +
            "<div>" +
            "  <div style='font-weight:800;color:#4ade80;'>🎉 " + escHtml(w.winner_nickname) + " (" + escHtml(w.winner_tickets) + "장 / 총 " + escHtml(w.total_tickets) + "장) — " + escHtml(w.prize_name) + "</div>" +
            "  <div style='font-size:11px;color:#94a3b8;margin-top:2px;'>수령코드: <code style='color:#fde68a;font-weight:800;'>" + escHtml(w.claim_code) + "</code> · 추첨시각: " + escHtml(w.drawn_at_kst) + " · 상태: <strong>" + escHtml(w.fulfillment_note) + "</strong></div>" +
            "</div>" +
            "<button type='button' class='dc-pill' data-sq-fulfill='" + escHtml(w.id) + "' data-sq-nextnote='" + escHtml(nextStatus) + "' style='font-size:11px;font-weight:800;'>" + (isDone ? "↩ 대기 전환" : "✅ 발송 완료 처리") + "</button>" +
            "</div>";
        }).join("");

        bodyBox.innerHTML = [
          "<div style='display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px;'>",
          "  <div>회차 선택: <select id='sqAdminRoundSelect' style='background:#0f172a;border:1px solid #334155;border-radius:8px;padding:5px 9px;color:#fff;'>" + opts + "</select></div>",
          "  <div style='font-size:12px;color:#93c5fd;'>응모 회원 <strong>" + escHtml(d.summary.total_participants) + "명</strong> · 총 응모권 <strong>" + escHtml(d.summary.total_tickets) + "장</strong> · 당첨 <strong>" + escHtml(d.summary.drawn_winners_count) + "명</strong></div>",
          "</div>",
          "<div style='background:#020617;border:1px solid #1e293b;border-radius:12px;padding:11px;margin-bottom:12px;'>",
          "  <div style='font-weight:800;color:#fde68a;margin-bottom:7px;'>🎲 CSPRNG 응모권 가중치 공정 추첨 실행</div>",
          "  <div style='display:grid;grid-template-columns:1fr 90px auto;gap:8px;'>",
          "    <input id='sqAdminPrizeName' value='편의점·커피 기프티콘 5,000원권' placeholder='경품명 입력' style='background:#0f172a;border:1px solid #334155;border-radius:8px;padding:7px 10px;color:#fff;font-size:12px;' />",
          "    <input id='sqAdminWinCount' type='number' min='1' max='5' value='1' title='추첨 인원' style='background:#0f172a;border:1px solid #334155;border-radius:8px;padding:7px 10px;color:#fff;font-size:12px;' />",
          "    <button type='button' id='sqRunDrawNowBtn' class='sq-up-btn' style='background:linear-gradient(135deg,#d97706,#b45309);'>🎲 즉시 추첨하기</button>",
          "  </div>",
          "</div>",
          "<div style='font-weight:800;color:#f8fafc;margin-bottom:6px;'>📋 회차별 응모자 &amp; 가중치 당첨 확률</div>",
          "<div style='max-height:180px;overflow-y:auto;border:1px solid #1e293b;border-radius:10px;margin-bottom:12px;'>",
          "  <table style='width:100%;border-collapse:collapse;font-size:12px;text-align:left;'>",
          "    <thead><tr style='background:#020617;color:#94a3b8;'><th style='padding:6px;'>#</th><th style='padding:6px;'>닉네임·칭호</th><th style='padding:6px;'>등급·보유P</th><th style='padding:6px;'>응모권</th><th style='padding:6px;'>당첨확률</th></tr></thead>",
          "    <tbody>" + (pRows || "<tr><td colspan='5' style='padding:12px;text-align:center;color:#94a3b8;'>이 회차에 아직 응모한 회원이 없습니다.</td></tr>") + "</tbody>",
          "  </table>",
          "</div>",
          "<div style='font-weight:800;color:#f8fafc;margin-bottom:6px;'>🏆 당첨 결과 및 수령 코드 관리</div>",
          (dRows || "<div style='padding:10px;color:#94a3b8;background:#020617;border-radius:10px;text-align:center;'>아직 추첨된 당첨자가 없습니다.</div>")
        ].join("");

        document.getElementById("sqAdminRoundSelect").onchange = function (ev) {
          loadAdminRoundData(adminKey, ev.target.value);
        };
        document.getElementById("sqRunDrawNowBtn").onclick = function () {
          var prize = document.getElementById("sqAdminPrizeName").value.trim();
          var cnt = parseInt(document.getElementById("sqAdminWinCount").value || "1", 10);
          fetch("/api/admin/raffle/draw", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-SportIQ-Admin-Key": adminKey },
            body: JSON.stringify({ round_key: d.selected_round, winner_count: cnt, prize_name: prize, exclude_already_won: true })
          }).then(function (r) { return r.json().then(function (x) { return { status: r.status, data: x }; }); })
            .then(function (dr) {
              if (dr.status !== 200 || !dr.data.ok) {
                if (typeof showToast === "function") showToast("❌ " + ((dr.data && dr.data.detail) || "추첨 실패"));
                return;
              }
              var names = (dr.data.drawn || []).map(function (x) { return x.winner_nickname; }).join(", ");
              if (typeof showToast === "function") showToast("🎉 당첨자 추첨 완료: " + names);
              loadAdminRoundData(adminKey, d.selected_round);
              loadPublicAndMyRaffleWinners();
            });
        };
        bodyBox.querySelectorAll("[data-sq-fulfill]").forEach(function (fb) {
          fb.onclick = function () {
            var did = parseInt(fb.getAttribute("data-sq-fulfill"), 10);
            var nnote = fb.getAttribute("data-sq-nextnote") || "기프티콘 발송 완료";
            fetch("/api/admin/raffle/fulfill", {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-SportIQ-Admin-Key": adminKey },
              body: JSON.stringify({ draw_id: did, fulfillment_note: nnote })
            }).then(function (r) { return r.json(); }).then(function () {
              loadAdminRoundData(adminKey, d.selected_round);
              loadPublicAndMyRaffleWinners();
            });
          };
        });
      });
  }

  function bootUpgrade() {
    setupPwaSupport();
    fixInitialAttendanceStreak();
    setupToastLimiter();
    applyAboveTheFoldDiet();
    setupUniversalModalClose();
    hookRoleAndAttendance();
    setupWriteModalAndServerPosts();
    mountMemberHubButtonsAndModal();
    mountPwaInstallUi();
    polishLabelsAndProvenance();
    autoCheckTierUpgrade();
    if (getSavedNick() && getSavedPin().length === 4) {
      syncAccountSilently();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { setTimeout(bootUpgrade, 150); });
  } else {
    setTimeout(bootUpgrade, 150);
  }
  setInterval(function () {
    setupToastLimiter();
    applyAboveTheFoldDiet();
    mountMemberHubButtonsAndModal();
    polishLabelsAndProvenance();
  }, 1200);
})();
"""


@upgrade_router.get("/api/member/p0p1-upgrade.js")
def serve_p0p1_upgrade_js():
    return Response(
        content=UPGRADE_JS,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


def _rebrand_scobi_text(text: str) -> str:
    if not text:
        return text
    out = text
    if "오픈 스포츠 인사이트" in out:
        out = re.sub(
            r"billnojay\.com\s*(?:[·|\-]\s*)?오픈\s*스포츠\s*인사이트",
            "실시간 분석 스포츠IQ 인사이트",
            out,
            flags=re.IGNORECASE,
        )
        out = out.replace("오픈 스포츠 인사이트", "실시간 분석 스포츠IQ 인사이트")
    if "스코비" in out:
        out = (
            out.replace("AI 스코비", "AI 아이큐")
            .replace("AI스코비", "AI아이큐")
            .replace("스코비", "아이큐")
        )
    return out


def inject_upgrade_into_html(raw_html: str) -> str:
    pwa_head = (
        '  <link rel="manifest" href="/manifest.webmanifest" />\n'
        '  <link rel="apple-touch-icon" href="/apple-touch-icon.png" />\n'
        '  <meta name="theme-color" content="#0F172A" />\n'
        '  <meta name="mobile-web-app-capable" content="yes" />\n'
        '  <meta name="apple-mobile-web-app-capable" content="yes" />\n'
        '  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />\n'
        '  <meta name="apple-mobile-web-app-title" content="SportIQ" />'
    )
    out = _rebrand_scobi_text(raw_html)
    if 'rel="manifest"' not in out and "rel='manifest'" not in out:
        h_idx = out.find("</head>")
        if h_idx != -1:
            out = out[:h_idx] + pwa_head + "\n" + out[h_idx:]

    tag = '<script src="/api/member/p0p1-upgrade.js?v=11" defer></script>'
    if "/api/member/p0p1-upgrade.js" in out:
        return re.sub(r'/api/member/p0p1-upgrade\.js(?:\?v=\d+)?', '/api/member/p0p1-upgrade.js?v=11', out)
    idx = out.rfind("</body>")
    if idx != -1:
        return out[:idx] + "\n" + tag + "\n" + out[idx:]
    return out + "\n" + tag + "\n"
# ============================================================================
# STAGE P3 (v3.2) + UNIVERSAL CHRONOLOGICAL MATCH SORTER (v7.0)
# ============================================================================
def _push_member_notification(
    conn: sqlite3.Connection,
    nickname: str,
    notif_type: str,
    title: str,
    body: str,
    link_target: str = "",
) -> None:
    nick = (nickname or "").strip()[:24]
    if not nick:
        return
    conn.execute(
        """
        INSERT INTO member_notifications(nickname, notif_type, title, body, link_target, is_read, created_at)
        VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        (nick, notif_type[:20], title[:120], body[:280], link_target[:120], time.time()),
    )


def _parse_score_outcome(score_text: str) -> Optional[Tuple[str, str]]:
    m = re.search(r"(\d+)\s*[:\-]\s*(\d+)", score_text or "")
    if not m:
        return None
    h, a = int(m.group(1)), int(m.group(2))
    outcome = "HOME" if h > a else ("AWAY" if a > h else "DRAW")
    return outcome, f"{h}:{a}"


def _parse_match_kst_epoch(item: Dict[str, Any]) -> Optional[float]:
    for k in ("kickoff_ts", "start_ts", "timestamp", "epoch", "commence_ts"):
        v = item.get(k)
        if isinstance(v, (int, float)) and v > 1_000_000_000:
            return float(v / 1000.0) if v > 10_000_000_000 else float(v)

    for k in ("commence_time", "utc_date", "start_time", "kickoff_utc", "date", "match_date"):
        v = item.get(k)
        if isinstance(v, str) and len(v) >= 10 and ("T" in v or "-" in v):
            try:
                dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=KST)
                return dt.timestamp()
            except Exception:
                pass

    dt_txt = " ".join(
        str(item.get(k) or "")
        for k in ("match_dt_kst", "kickoff_kst", "schedule_venue", "time_kst", "display_time")
    ).strip()
    if not dt_txt:
        return None

    now_kst = datetime.now(KST)
    m_iso = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\D{0,14}?(\d{1,2}):(\d{2})", dt_txt)
    if m_iso:
        try:
            return datetime(
                int(m_iso.group(1)),
                int(m_iso.group(2)),
                int(m_iso.group(3)),
                int(m_iso.group(4)),
                int(m_iso.group(5)),
                tzinfo=KST,
            ).timestamp()
        except ValueError:
            pass

    m_md = re.search(r"(\d{1,2})[./](\d{1,2})\s*(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})", dt_txt)
    if m_md:
        yr = now_kst.year
        mo, da, hh, mm = int(m_md.group(1)), int(m_md.group(2)), int(m_md.group(3)), int(m_md.group(4))
        try:
            cand = datetime(yr, mo, da, hh, mm, tzinfo=KST)
            if (cand - now_kst).total_seconds() < -180 * 86400:
                cand = datetime(yr + 1, mo, da, hh, mm, tzinfo=KST)
            return cand.timestamp()
        except ValueError:
            return None

    m_rel = re.search(r"(오늘|내일|어제)\s*(\d{1,2}):(\d{2})", dt_txt)
    if m_rel:
        day_shift = 0 if m_rel.group(1) == "오늘" else (1 if m_rel.group(1) == "내일" else -1)
        base_d = (now_kst + timedelta(days=day_shift)).date()
        return datetime(base_d.year, base_d.month, base_d.day, int(m_rel.group(2)), int(m_rel.group(3)), tzinfo=KST).timestamp()

    return None


def sort_matches_nearest_recent(items: List[Any]) -> List[Any]:
    """
    Orders match cards by nearest/recent date & time:
      Bucket 0: LIVE / in-progress games first (most recent kickoff first)
      Bucket 1: Upcoming / Scheduled games in ascending chronological order (today 11.17 -> ... -> 11.21 04:30 -> 11.21 04:45)
      Bucket 2: Finished / FT games in descending chronological order (most recently finished first)
    """
    if not isinstance(items, list) or len(items) < 2:
        return items
    now_ts = time.time()

    def _sort_key(entry: Tuple[int, Any]) -> Tuple[int, float, int, int]:
        idx, m = entry
        if not isinstance(m, dict):
            return (9, float(idx), 1, idx)
        ts = _parse_match_kst_epoch(m)
        state_raw = " ".join(
            str(m.get(k) or "") for k in ("state", "status", "status_badge")
        ).lower()
        is_sched = "scheduled" in state_raw or "예정" in state_raw or state_raw.strip() == "pre"
        is_live = (not is_sched) and (
            state_raw.strip() in ("in", "live")
            or any(w in state_raw for w in ("live", "진행", "전반", "후반", "ht", "q1", "q2", "q3", "q4", "세트", "회초", "회말"))
        )
        is_post = (not is_sched) and (
            state_raw.strip() in ("post", "ft", "final", "ended")
            or any(w in state_raw for w in ("종료", "final", "ft", "경기종료"))
        )
        my_tie = 0 if m.get("is_my_team") else 1

        if is_live:
            return (0, -(ts if ts is not None else now_ts), my_tie, idx)
        if is_post:
            return (2, -(ts if ts is not None else 0.0), my_tie, idx)
        if ts is not None:
            return (1, ts, my_tie, idx)
        return (3, float(idx), my_tie, idx)

    return [x[1] for x in sorted(enumerate(items), key=_sort_key)]


def _sort_datacenter_payload_any(payload: Any) -> Any:
    if isinstance(payload, dict):
        out = dict(payload)
        for key in ("items", "matches", "alerts", "rows", "games", "fixtures"):
            if isinstance(out.get(key), list) and len(out[key]) > 1:
                out[key] = sort_matches_nearest_recent(out[key])
        return out
    if isinstance(payload, Response) and getattr(payload, "body", None):
        try:
            parsed = json.loads(payload.body.decode("utf-8"))
            if isinstance(parsed, dict):
                sorted_dict = _sort_datacenter_payload_any(parsed)
                return Response(
                    content=json.dumps(sorted_dict, ensure_ascii=False),
                    status_code=payload.status_code,
                    media_type="application/json; charset=utf-8",
                )
        except Exception:
            pass
    return payload


def _install_datacenter_chronological_patch() -> bool:
    patched_any = False
    try:
        from app import sportiq_datacenter as _dc
        if not getattr(_dc, "__sq_chrono_sorted_v7__", False):
            orig_collect = getattr(_dc, "_collect_all_matches", None)
            if callable(orig_collect):
                @functools.wraps(orig_collect)
                def _sorted_collect(*args, **kwargs):
                    res = orig_collect(*args, **kwargs)
                    return sort_matches_nearest_recent(res) if isinstance(res, list) else res

                _dc._collect_all_matches = _sorted_collect
                _dc.__sq_chrono_sorted_v7__ = True
                patched_any = True
                try:
                    from app import sportiq_lounge_router as _lr
                    with _lr._DC_SWR_LOCK:
                        _lr._DC_SWR_CACHE.clear()
                except Exception:
                    pass
    except Exception:
        pass

    # Scan all loaded modules in sys.modules for any FastAPI / APIRouter holding datacenter routes
    try:
        for mod in list(sys.modules.values()):
            if mod is None:
                continue
            for attr_name in ("app", "router", "datacenter_router"):
                container = getattr(mod, attr_name, None)
                routes = getattr(container, "routes", None)
                if not isinstance(routes, list):
                    continue
                for r in routes:
                    r_path = getattr(r, "path", "") or ""
                    if not any(k in r_path for k in ("live-scores", "dropping-odds", "datacenter/matches", "datacenter/scores")):
                        continue
                    dep = getattr(r, "dependant", None)
                    call_fn = getattr(dep, "call", None) if dep is not None else None
                    if callable(call_fn) and not getattr(call_fn, "__sq_route_sorted_v7__", False):
                        if inspect.iscoroutinefunction(call_fn):
                            @functools.wraps(call_fn)
                            async def _async_sorted_ep(*args, __orig=call_fn, **kwargs):
                                res = await __orig(*args, **kwargs)
                                return _sort_datacenter_payload_any(res)

                            _async_sorted_ep.__sq_route_sorted_v7__ = True
                            dep.call = _async_sorted_ep
                            r.endpoint = _async_sorted_ep
                        else:
                            @functools.wraps(call_fn)
                            def _sync_sorted_ep(*args, __orig=call_fn, **kwargs):
                                res = __orig(*args, **kwargs)
                                return _sort_datacenter_payload_any(res)

                            _sync_sorted_ep.__sq_route_sorted_v7__ = True
                            dep.call = _sync_sorted_ep
                            r.endpoint = _sync_sorted_ep
                        patched_any = True
    except Exception:
        pass

    return patched_any


def _settle_pending_predictions_once() -> int:
    def _op() -> int:
        settled_cnt = 0
        now = time.time()
        today_kst = datetime.now(KST).strftime("%Y-%m-%d")
        with _db_session(write=True) as conn:
            pending = conn.execute(
                "SELECT * FROM member_match_predictions WHERE status = 'PENDING' LIMIT 200"
            ).fetchall()
            if not pending:
                return 0

            finished_map: Dict[str, Tuple[str, str]] = {}
            for tbl, m_col, s_col, st_col in (
                ("live_prediction_ledger", "matchup", "final_score", "status"),
                ("ai_scorecard_records", "matchup", "actual_score", "result"),
            ):
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
                ).fetchone()
                if not exists:
                    continue
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if m_col not in cols or s_col not in cols:
                    continue
                for qr in conn.execute(f"SELECT * FROM {tbl} ORDER BY rowid DESC LIMIT 300").fetchall():
                    qd = dict(qr)
                    st_val = str(qd.get(st_col, "FT") or "FT").upper()
                    if st_col in cols and not any(k in st_val for k in ("FT", "FINAL", "END", "HIT", "MISS", "종료", "완료")):
                        continue
                    parsed = _parse_score_outcome(str(qd.get(s_col) or ""))
                    sig = canonical_matchup_signature(str(qd.get(m_col) or ""))
                    if parsed and sig:
                        finished_map[sig] = parsed

            if not finished_map:
                return 0

            for p in pending:
                sig = canonical_matchup_signature(p["matchup"]) or p["match_key"]
                res = finished_map.get(sig)
                if not res:
                    continue
                actual_side, clean_score = res
                nick = p["nickname"]
                is_hit = (p["pick"] == actual_side)
                new_status = "HIT" if is_hit else "MISS"

                st_row = conn.execute(
                    "SELECT * FROM member_prediction_stats WHERE lower(nickname)=lower(?)", (nick,)
                ).fetchone()
                prev_streak = int(st_row["win_streak"]) if st_row else 0
                prev_best = int(st_row["best_streak"]) if st_row else 0
                prev_hits = int(st_row["hit_count"]) if st_row else 0
                prev_miss = int(st_row["miss_count"]) if st_row else 0
                prev_earned = int(st_row["total_earned_points"]) if st_row else 0

                bonus_pts = 0
                if is_hit:
                    new_streak = prev_streak + 1
                    bonus_pts = 30 + (25 if new_streak == 3 else 0) + (60 if new_streak == 5 else 0)
                    new_hits, new_miss = prev_hits + 1, prev_miss
                else:
                    new_streak = 0
                    new_hits, new_miss = prev_hits, prev_miss + 1
                new_best = max(prev_best, new_streak)

                conn.execute(
                    """
                    UPDATE member_match_predictions
                    SET status = ?, reward_points = reward_points + ?, settled_score = ?, settled_at = ?
                    WHERE id = ?
                    """,
                    (new_status, bonus_pts, clean_score, now, p["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO member_prediction_stats(
                        nickname, total_picks, hit_count, miss_count, win_streak, best_streak, total_earned_points, updated_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(nickname) DO UPDATE SET
                        hit_count = ?, miss_count = ?, win_streak = ?, best_streak = ?,
                        total_earned_points = ?, updated_at = ?
                    """,
                    (
                        nick, new_hits, new_miss, new_streak, new_best, prev_earned + bonus_pts, now,
                        new_hits, new_miss, new_streak, new_best, prev_earned + bonus_pts, now,
                    ),
                )

                if is_hit:
                    acc = conn.execute(
                        "SELECT * FROM member_accounts WHERE lower(nickname)=lower(?)", (nick,)
                    ).fetchone()
                    if acc:
                        updated_pts = int(acc["points"] or 0) + bonus_pts
                        owned = json.loads(acc["owned_items_json"] or "[]")
                        badge_now = acc["active_badge"] or ""
                        if new_streak >= 5 and "badge_streak_tactician" not in owned:
                            owned.append("badge_streak_tactician")
                            if not badge_now:
                                badge_now = "🔥 연승 전술가"
                        conn.execute(
                            """
                            UPDATE member_accounts
                            SET points = ?, role_tier = ?, active_badge = ?, owned_items_json = ?, updated_at = ?
                            WHERE nickname = ?
                            """,
                            (
                                updated_pts,
                                _tier_for_points(updated_pts),
                                badge_now,
                                json.dumps(owned, ensure_ascii=False),
                                now,
                                acc["nickname"],
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at)
                            VALUES (?, 'prediction_hit', ?, ?, ?, 'billnojay.com', ?)
                            """,
                            (acc["nickname"], str(p["id"]), bonus_pts, today_kst, now),
                        )
                    _push_member_notification(
                        conn,
                        nick,
                        "SETTLE",
                        f"🎯 승부예측 적중! (+{bonus_pts}P 지급)",
                        f"[{p['matchup']}] 최종 {clean_score} 적중 완료! 현재 {new_streak}연승 기록 중입니다.",
                        "mypage",
                    )
                settled_cnt += 1
        return settled_cnt

    try:
        cnt = run_with_db_retry(_op)
        if cnt > 0:
            threading.Thread(
                target=_trigger_pg_backup,
                args=("member_match_predictions", "member_prediction_stats", "member_accounts", "member_notifications", "wp_point_ledger"),
                daemon=True,
            ).start()
        return cnt
    except Exception:
        return 0


def _p3_background_daemon() -> None:
    time.sleep(1.5)
    for _ in range(15):
        _install_datacenter_chronological_patch()
        time.sleep(1.0)
    while True:
        _install_datacenter_chronological_patch()
        _settle_pending_predictions_once()
        time.sleep(60.0)


threading.Thread(target=_p3_background_daemon, daemon=True, name="sportiq-p3-daemon").start()


# --- P3 Schemas ---
class PredictionPickIn(BaseModel):
    nickname: str
    pin: str
    match_key: str
    matchup: str
    league: str = "EPL"
    pick: str


class NotificationReadIn(BaseModel):
    nickname: str
    pin: Optional[str] = ""
    notif_id: Optional[int] = None


class PostReportIn(BaseModel):
    reporter: Optional[str] = ""
    reason: str = "스팸·도배 또는 부적절한 게시물"


# --- P3-A: Match Prediction Pick & Mini MyPage Endpoints ---
@upgrade_router.post("/api/member/predictions/pick")
def submit_member_match_pick(payload: PredictionPickIn):
    nick = (payload.nickname or "").strip()[:24]
    pin = re.sub(r"\D", "", payload.pin or "")
    pick = (payload.pick or "").strip().upper()
    if pick not in ("HOME", "DRAW", "AWAY"):
        raise HTTPException(status_code=400, detail="예측 선택값은 HOME, DRAW, AWAY 중 하나여야 합니다.")
    if len(nick) < 2 or len(pin) != 4:
        raise HTTPException(status_code=400, detail="닉네임과 4자리 PIN 연동 후 승부예측에 참여할 수 있습니다.")
    pin_h = _hash_pin(nick, pin)
    matchup = (payload.matchup or "").strip()[:120]
    mkey = (payload.match_key or canonical_matchup_signature(matchup) or matchup)[:80]
    league = (payload.league or "SPORT").strip()[:24]
    now = time.time()
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")

    def _op() -> Dict[str, Any]:
        with _db_session(write=True) as conn:
            acc = conn.execute(
                "SELECT * FROM member_accounts WHERE lower(nickname)=lower(?)", (nick,)
            ).fetchone()
            if not acc or acc["pin_hash"] != pin_h:
                raise HTTPException(status_code=403, detail="닉네임 또는 4자리 PIN이 일치하지 않습니다.")
            dup = conn.execute(
                "SELECT pick, status FROM member_match_predictions WHERE lower(nickname)=lower(?) AND match_key=?",
                (acc["nickname"], mkey),
            ).fetchone()
            if dup:
                raise HTTPException(status_code=400, detail=f"이미 '{dup['pick']}' 픽으로 참여한 경기입니다.")

            new_pts = int(acc["points"] or 0) + 5
            conn.execute(
                """
                INSERT INTO member_match_predictions(nickname, match_key, matchup, league, pick, status, reward_points, created_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', 5, ?)
                """,
                (acc["nickname"], mkey, matchup, league, pick, now),
            )
            conn.execute(
                """
                INSERT INTO member_prediction_stats(nickname, total_picks, hit_count, miss_count, win_streak, best_streak, total_earned_points, updated_at)
                VALUES (?, 1, 0, 0, 0, 0, 5, ?)
                ON CONFLICT(nickname) DO UPDATE SET
                    total_picks = total_picks + 1,
                    total_earned_points = total_earned_points + 5,
                    updated_at = excluded.updated_at
                """,
                (acc["nickname"], now),
            )
            conn.execute(
                "UPDATE member_accounts SET points = ?, role_tier = ?, updated_at = ? WHERE nickname = ?",
                (new_pts, _tier_for_points(new_pts), now, acc["nickname"]),
            )
            conn.execute(
                """
                INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at)
                VALUES (?, 'prediction_pick', ?, 5, ?, 'billnojay.com', ?)
                """,
                (acc["nickname"], mkey, today_kst, now),
            )
            return {"nickname": acc["nickname"], "points": new_pts, "pick": pick, "match_key": mkey}

    res = run_with_db_retry(_op)
    threading.Thread(
        target=_trigger_pg_backup,
        args=("member_match_predictions", "member_prediction_stats", "member_accounts", "wp_point_ledger"),
        daemon=True,
    ).start()
    return {"ok": True, "awarded_points": 5, **res}


@upgrade_router.get("/api/member/mypage")
def get_member_mini_mypage(nickname: str, pin: str = ""):
    nick = (nickname or "").strip()[:24]
    clean_pin = re.sub(r"\D", "", pin or "")
    if len(nick) < 2:
        raise HTTPException(status_code=400, detail="닉네임을 입력해 주세요.")

    def _op() -> Dict[str, Any]:
        with _db_session(write=False) as conn:
            acc = conn.execute(
                "SELECT * FROM member_accounts WHERE lower(nickname)=lower(?)", (nick,)
            ).fetchone()
            if not acc:
                raise HTTPException(status_code=404, detail="연동된 계정을 찾을 수 없습니다.")
            if clean_pin and acc["pin_hash"] != _hash_pin(acc["nickname"], clean_pin):
                raise HTTPException(status_code=403, detail="4자리 PIN이 일치하지 않습니다.")

            prof = _serialize_wp_member_profile(conn, acc)
            st = conn.execute(
                "SELECT * FROM member_prediction_stats WHERE lower(nickname)=lower(?)", (acc["nickname"],)
            ).fetchone()
            picks = [
                {
                    "id": r["id"],
                    "matchup": r["matchup"],
                    "league": r["league"],
                    "pick": r["pick"],
                    "status": r["status"],
                    "reward_points": r["reward_points"],
                    "settled_score": r["settled_score"],
                    "created_kst": _fmt_kst_ts(r["created_at"]),
                }
                for r in conn.execute(
                    "SELECT * FROM member_match_predictions WHERE lower(nickname)=lower(?) ORDER BY id DESC LIMIT 12",
                    (acc["nickname"],),
                ).fetchall()
            ]
            ledger = [
                {
                    "event_type": r["event_type"],
                    "points_delta": r["points_delta"],
                    "kst_date": r["kst_date"],
                    "created_kst": _fmt_kst_ts(r["created_at"]),
                }
                for r in conn.execute(
                    "SELECT * FROM wp_point_ledger WHERE lower(nickname)=lower(?) ORDER BY id DESC LIMIT 10",
                    (acc["nickname"],),
                ).fetchall()
            ]
            settled_total = (int(st["hit_count"]) + int(st["miss_count"])) if st else 0
            hit_rate = round((int(st["hit_count"]) * 100.0 / settled_total), 1) if settled_total > 0 else 0.0
            return {
                "profile": prof,
                "stats": {
                    "total_picks": int(st["total_picks"]) if st else 0,
                    "hit_count": int(st["hit_count"]) if st else 0,
                    "miss_count": int(st["miss_count"]) if st else 0,
                    "hit_rate_pct": hit_rate,
                    "win_streak": int(st["win_streak"]) if st else 0,
                    "best_streak": int(st["best_streak"]) if st else 0,
                    "total_earned_points": int(st["total_earned_points"]) if st else 0,
                },
                "recent_picks": picks,
                "recent_ledger": ledger,
            }

    return {"ok": True, **run_with_db_retry(_op)}


# --- P3-B: In-App Notification Bell ---
@upgrade_router.get("/api/member/notifications")
def list_member_notifications(nickname: str, limit: int = 20):
    nick = (nickname or "").strip()[:24]
    if len(nick) < 2:
        return {"ok": True, "unread_count": 0, "items": []}
    lim = max(1, min(50, int(limit)))

    def _op() -> Dict[str, Any]:
        with _db_session(write=False) as conn:
            unread = conn.execute(
                "SELECT COUNT(*) AS c FROM member_notifications WHERE lower(nickname)=lower(?) AND is_read=0",
                (nick,),
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT * FROM member_notifications WHERE lower(nickname)=lower(?) ORDER BY id DESC LIMIT ?",
                (nick, lim),
            ).fetchall()
            return {
                "unread_count": int(unread or 0),
                "items": [
                    {
                        "id": int(r["id"]),
                        "notif_type": r["notif_type"],
                        "title": r["title"],
                        "body": r["body"],
                        "link_target": r["link_target"],
                        "is_read": bool(r["is_read"]),
                        "created_kst": _fmt_kst_ts(r["created_at"]),
                    }
                    for r in rows
                ],
            }

    return {"ok": True, **run_with_db_retry(_op)}


@upgrade_router.post("/api/member/notifications/read-all")
def mark_all_notifications_read(payload: NotificationReadIn):
    nick = (payload.nickname or "").strip()[:24]
    if len(nick) < 2:
        return {"ok": True}

    def _op() -> None:
        with _db_session(write=True) as conn:
            if payload.notif_id:
                conn.execute(
                    "UPDATE member_notifications SET is_read=1 WHERE id=? AND lower(nickname)=lower(?)",
                    (int(payload.notif_id), nick),
                )
            else:
                conn.execute(
                    "UPDATE member_notifications SET is_read=1 WHERE lower(nickname)=lower(?)",
                    (nick,),
                )

    run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("member_notifications",), daemon=True).start()
    return {"ok": True}


# --- P3-C: 3-Strike Auto Blind Moderation ---
@upgrade_router.get("/api/posts/blinds")
def list_blinded_posts():
    def _op() -> Dict[str, Any]:
        with _db_session(write=False) as conn:
            rows = conn.execute("SELECT post_id, report_count, blinded FROM community_post_blinds").fetchall()
            return {
                "blinded_ids": [r["post_id"] for r in rows if int(r["blinded"] or 0) == 1],
                "counts": {r["post_id"]: int(r["report_count"] or 0) for r in rows},
            }

    return {"ok": True, **run_with_db_retry(_op)}


@upgrade_router.post("/api/posts/{post_id}/report-clean")
def report_post_with_auto_blind(post_id: str, payload: PostReportIn):
    pid = (post_id or "").strip()[:64]
    reporter = (payload.reporter or "anon").strip()[:40] or "anon"
    reason = (payload.reason or "스팸·도배").strip()[:120]
    rep_key = hashlib.sha256(f"{pid}:{reporter}".encode("utf-8")).hexdigest()[:24]
    now = time.time()

    def _op() -> Dict[str, Any]:
        with _db_session(write=True) as conn:
            dup = conn.execute(
                "SELECT 1 FROM community_post_report_logs WHERE post_id=? AND reporter_key=?",
                (pid, rep_key),
            ).fetchone()
            if dup:
                cur = conn.execute("SELECT report_count, blinded FROM community_post_blinds WHERE post_id=?", (pid,)).fetchone()
                return {
                    "already_reported": True,
                    "report_count": int(cur["report_count"]) if cur else 1,
                    "blinded": bool(cur["blinded"]) if cur else False,
                }

            conn.execute(
                "INSERT INTO community_post_report_logs(post_id, reporter_key, reason, created_at) VALUES (?, ?, ?, ?)",
                (pid, rep_key, reason, now),
            )
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM community_post_report_logs WHERE post_id=?", (pid,)
            ).fetchone()["c"]
            is_blinded = 1 if int(cnt) >= 3 else 0
            conn.execute(
                """
                INSERT INTO community_post_blinds(post_id, report_count, blinded, last_reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    report_count = excluded.report_count,
                    blinded = CASE WHEN community_post_blinds.blinded = 1 THEN 1 ELSE excluded.blinded END,
                    last_reason = excluded.last_reason,
                    updated_at = excluded.updated_at
                """,
                (pid, int(cnt), is_blinded, reason, now),
            )
            return {"already_reported": False, "report_count": int(cnt), "blinded": bool(is_blinded)}

    out = run_with_db_retry(_op)
    threading.Thread(
        target=_trigger_pg_backup, args=("community_post_blinds", "community_post_report_logs"), daemon=True
    ).start()
    return {"ok": True, **out}


@upgrade_router.post("/api/admin/posts/{post_id}/unblind")
def admin_unblind_community_post(
    post_id: str,
    x_sportiq_admin_key: Optional[str] = Header(default=None),
):
    _require_admin_auth(x_sportiq_admin_key)

    def _op() -> None:
        with _db_session(write=True) as conn:
            conn.execute(
                "UPDATE community_post_blinds SET blinded=0, report_count=0, updated_at=? WHERE post_id=?",
                (time.time(), post_id[:64]),
            )
            conn.execute("DELETE FROM community_post_report_logs WHERE post_id=?", (post_id[:64],))

    run_with_db_retry(_op)
    threading.Thread(
        target=_trigger_pg_backup, args=("community_post_blinds", "community_post_report_logs"), daemon=True
    ).start()
    return {"ok": True, "unblinded_id": post_id}


# Upgrade Service Worker for Web Push Notification Click Handling (v3.2)
SERVICE_WORKER_JS = SERVICE_WORKER_JS + (
    "\nself.addEventListener('notificationclick', (event) => {\n"
    "  event.notification.close();\n"
    "  event.waitUntil(clients.openWindow('/?source=pwa_notif'));\n"
    "});\n"
)

# Frontend v7.0: Universal Match Date-Time Sorter + P3 Full Suite + Guaranteed Header/Floating Buttons
P3_AND_CHRONO_JS = r"""
/* SportIQ v7.0: Nearest/Recent Match Date-Time Sorter + P3 Full Suite */
(function () {
  if (window.__SPORTIQ_V7_DATE_P3__) return;
  window.__SPORTIQ_V7_DATE_P3__ = true;

  var p3FeedSourceFilter = "ALL";
  var p3SearchQuery = "";
  var blindedPostSet = {};
  var lastUnreadNotifCount = -1;
  var matchSortMode = localStorage.getItem("sq_match_sort_mode") || "NEAREST";

  function sqNick() { return (localStorage.getItem("sportiq_member_nick") || localStorage.getItem("sportiq_guest_nick") || "").trim(); }
  function sqPin() { return (localStorage.getItem("sportiq_member_pin") || "").trim(); }
  function esc(v) { return v == null ? "" : String(v).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

  function parseMatchEpochFromText(s) {
    var txt = String(s || "");
    var now = new Date();
    var yr = now.getFullYear();

    var iso = txt.match(/(20\d{2})[.\-\/](\d{1,2})[.\-\/](\d{1,2})[^\d]{0,14}?(\d{1,2}):(\d{2})/);
    if (iso) {
      return new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]), Number(iso[4]), Number(iso[5])).getTime();
    }
    var md = txt.match(/(\d{1,2})[.\/](\d{1,2})\s*(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})/);
    if (md) {
      var mo = Number(md[1]), da = Number(md[2]), hh = Number(md[3]), mm = Number(md[4]);
      var t = new Date(yr, mo - 1, da, hh, mm).getTime();
      if (t - now.getTime() < -180 * 86400 * 1000) {
        t = new Date(yr + 1, mo - 1, da, hh, mm).getTime();
      }
      return t;
    }
    var rel = txt.match(/(오늘|내일|어제)\s*(\d{1,2}):(\d{2})/);
    if (rel) {
      var shift = rel[1] === "오늘" ? 0 : (rel[1] === "내일" ? 1 : -1);
      var d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + shift, Number(rel[2]), Number(rel[3]));
      return d.getTime();
    }
    return null;
  }

  function computeMatchOrderTuple(statusText, epochMs, fallbackIdx) {
    var st = String(statusText || "");
    var isScheduled = /Scheduled|예정/i.test(st) || /^\s*pre\s*$/i.test(st);
    var isLive = !isScheduled && (/\bLIVE\b|^\s*in\s*$|진행중|전반|후반|\bHT\b|\bQ[1-4]\b|회초|회말|세트/i.test(st));
    var isEnded = !isScheduled && (/\bFT\b|\bFinal\b|^\s*post\s*$|종료|경기종료/i.test(st));

    if (matchSortMode === "DESC") {
      if (isLive) return { bucket: 0, key: -(epochMs != null ? epochMs : Date.now()) };
      if (epochMs == null) return { bucket: 9, key: fallbackIdx };
      return { bucket: 1, key: -epochMs };
    }

    if (isLive) return { bucket: 0, key: -(epochMs != null ? epochMs : Date.now()) };
    if (epochMs == null) return { bucket: 9, key: fallbackIdx };
    if (isEnded) return { bucket: 2, key: -epochMs };
    return { bucket: 1, key: epochMs };
  }

  function looksLikeMatchItem(m) {
    if (!m || typeof m !== "object") return false;
    return Boolean(m.match_dt_kst || m.kickoff_ts || m.commence_time || (m.home && m.away) || m.matchup);
  }

  function sortMatchDataArray(arr) {
    if (!Array.isArray(arr) || arr.length < 2 || !looksLikeMatchItem(arr[0])) return arr;
    return arr.map(function (m, idx) {
      var rawDt = (m && (m.match_dt_kst || m.commence_time || m.kickoff_kst || m.date || m.schedule_venue)) || "";
      var ep = null;
      if (m && typeof m.kickoff_ts === "number") {
        ep = m.kickoff_ts > 10000000000 ? m.kickoff_ts : m.kickoff_ts * 1000;
      } else {
        ep = parseMatchEpochFromText(rawDt);
      }
      var stTxt = (m && (m.state || m.status_badge || m.status)) || "";
      var ord = computeMatchOrderTuple(stTxt, ep, idx);
      var myTie = (m && m.is_my_team) ? 0 : 1;
      return { m: m, idx: idx, myTie: myTie, bucket: ord.bucket, key: ord.key };
    }).sort(function (a, b) {
      if (a.bucket !== b.bucket) return a.bucket - b.bucket;
      if (a.key !== b.key) return a.key - b.key;
      if (a.myTie !== b.myTie) return a.myTie - b.myTie;
      return a.idx - b.idx;
    }).map(function (w) { return w.m; });
  }

  if (typeof window.fetch === "function" && !window.__sqChronoFetchHooked) {
    window.__sqChronoFetchHooked = true;
    var origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      var urlStr = typeof input === "string" ? input : (input && input.url ? input.url : "");
      var isApiCall = urlStr.indexOf("/api/") !== -1 && urlStr.indexOf("/api/rankings/global-matches") === -1;
      if (!isApiCall) return origFetch(input, init);

      return origFetch(input, init).then(function (resp) {
        if (!resp || !resp.ok || typeof resp.clone !== "function") return resp;
        var ct = (resp.headers && resp.headers.get && resp.headers.get("content-type")) || "";
        if (ct && ct.indexOf("json") === -1) return resp;
        return resp.clone().json().then(function (data) {
          if (!data || typeof data !== "object") return resp;
          var touched = false;
          ["items", "matches", "alerts", "rows", "games", "fixtures"].forEach(function (arrKey) {
            if (Array.isArray(data[arrKey]) && data[arrKey].length > 1 && looksLikeMatchItem(data[arrKey][0])) {
              data[arrKey] = sortMatchDataArray(data[arrKey]);
              touched = true;
            }
          });
          if (!touched) return resp;
          return new Response(JSON.stringify(data), {
            status: resp.status,
            statusText: resp.statusText,
            headers: resp.headers
          });
        }).catch(function () { return resp; });
      });
    };
  }

  function sortRenderedMatchCardStrips(forceResort) {
    document.querySelectorAll("div, section, ul").forEach(function (container) {
      if (!container.children || container.children.length < 2 || container.children.length > 500) return;
      if (container.id === "feedList" || container.id === "dcLauncherGrid" || container.id === "globalTop10Grid") return;
      var kids = Array.prototype.slice.call(container.children);
      var matchLikeCount = 0;
      kids.forEach(function (ch) {
        var t = ch.textContent || "";
        if (parseMatchEpochFromText(t) != null && (/Scheduled|예정|종료|LIVE|배당|스코어|vs/i.test(t))) {
          matchLikeCount++;
        }
      });
      if (matchLikeCount < 2 || matchLikeCount < Math.ceil(kids.length * 0.4)) return;

      mountSortToggleAboveStrip(container);

      var decorated = kids.map(function (el, idx) {
        var t = el.textContent || "";
        var ep = parseMatchEpochFromText(t);
        var ord = computeMatchOrderTuple(t, ep, idx);
        var isMy = /⭐\s*MY\b/.test(t) ? 0 : 1;
        return { el: el, idx: idx, isMy: isMy, bucket: ord.bucket, key: ord.key };
      });

      var sorted = decorated.slice().sort(function (a, b) {
        if (a.bucket !== b.bucket) return a.bucket - b.bucket;
        if (a.key !== b.key) return a.key - b.key;
        if (a.isMy !== b.isMy) return a.isMy - b.isMy;
        return a.idx - b.idx;
      });

      var changed = Boolean(forceResort);
      if (!changed) {
        for (var i = 0; i < sorted.length; i++) {
          if (sorted[i].el !== kids[i]) { changed = true; break; }
        }
      }
      if (!changed) return;

      var prevScrollLeft = container.scrollLeft || 0;
      sorted.forEach(function (item) { container.appendChild(item.el); });
      container.scrollLeft = forceResort ? 0 : prevScrollLeft;
    });
  }

  function mountSortToggleAboveStrip(stripEl) {
    if (!stripEl || !stripEl.parentNode) return;
    if (stripEl.parentNode.querySelector(".sq-match-sort-bar")) return;
    var bar = document.createElement("div");
    bar.className = "sq-match-sort-bar";
    bar.style.cssText = "display:flex;justify-content:flex-end;align-items:center;gap:6px;margin:0 0 6px 0;font-size:11px;color:#94a3b8;";
    bar.innerHTML = [
      "<span>시간·날짜 정렬:</span>",
      "<button type='button' data-sq-msort='NEAREST' class='dc-pill' style='padding:2px 9px;font-size:11px;font-weight:800;'>🕒 가까운·최근 시간순</button>",
      "<button type='button' data-sq-msort='DESC' class='dc-pill' style='padding:2px 9px;font-size:11px;font-weight:800;'>📅 최신 날짜순</button>"
    ].join("");
    stripEl.parentNode.insertBefore(bar, stripEl);

    function syncBtnStyle() {
      bar.querySelectorAll("[data-sq-msort]").forEach(function (b) {
        var on = b.getAttribute("data-sq-msort") === matchSortMode;
        b.style.borderColor = on ? "#38bdf8" : "";
        b.style.color = on ? "#e0f2fe" : "";
      });
    }
    syncBtnStyle();
    bar.querySelectorAll("[data-sq-msort]").forEach(function (b) {
      b.onclick = function () {
        matchSortMode = b.getAttribute("data-sq-msort") || "NEAREST";
        localStorage.setItem("sq_match_sort_mode", matchSortMode);
        document.querySelectorAll(".sq-match-sort-bar [data-sq-msort]").forEach(function (x) {
          var on = x.getAttribute("data-sq-msort") === matchSortMode;
          x.style.borderColor = on ? "#38bdf8" : "";
          x.style.color = on ? "#e0f2fe" : "";
        });
        sortRenderedMatchCardStrips(true);
      };
    });
  }

  // --- P3-A & P3-B: Guaranteed Header Buttons + Floating Quick Dock Fallback ---
  function mountMyPageAndBellHeader() {
    var anchor = document.getElementById("sqOpenMemberHubBtn") || document.getElementById("pointsBadge");
    if (!anchor) {
      var hdr = document.querySelector("header .flex") || document.querySelector("header");
      if (hdr && !document.getElementById("sqFallbackTopBar")) {
        var fb = document.createElement("div");
        fb.id = "sqFallbackTopBar";
        fb.style.cssText = "display:inline-flex;gap:6px;align-items:center;margin-left:auto;";
        hdr.appendChild(fb);
        anchor = fb;
      }
    }
    if (!anchor || !anchor.parentNode) return;

    if (!document.getElementById("sqOpenMyPageBtn")) {
      var mpBtn = document.createElement("button");
      mpBtn.id = "sqOpenMyPageBtn";
      mpBtn.type = "button";
      mpBtn.className = "sq-up-btn";
      mpBtn.style.background = "linear-gradient(135deg,#4f46e5,#0284c7)";
      mpBtn.innerHTML = "📊 내 프로필";
      mpBtn.onclick = openMyPageModal;
      anchor.insertAdjacentElement("afterend", mpBtn);
    }

    if (!document.getElementById("sqNotifBellBtn")) {
      var bell = document.createElement("button");
      bell.id = "sqNotifBellBtn";
      bell.type = "button";
      bell.className = "sq-up-btn";
      bell.style.position = "relative";
      bell.style.background = "#0f172a";
      bell.innerHTML = "🔔 알림 <span id='sqNotifBadge' style='display:none;background:#ef4444;color:#fff;border-radius:999px;padding:0 6px;font-size:10.5px;font-weight:900;'>0</span>";
      bell.onclick = openNotificationsModal;
      anchor.insertAdjacentElement("afterend", bell);
    }
  }

  function openMyPageModal() {
    var nk = sqNick();
    if (!nk) {
      if (typeof window.__openSportiqMemberHub === "function") window.__openSportiqMemberHub();
      if (typeof showToast === "function") showToast("🔐 먼저 닉네임 + 4자리 PIN을 연동하면 내 적중률 프로필이 활성화됩니다!");
      return;
    }
    var m = document.getElementById("sqMyPageModal");
    if (!m) {
      m = document.createElement("div");
      m.id = "sqMyPageModal";
      m.className = "sq-modal-backdrop";
      m.innerHTML = "<div class='sq-modal-panel' style='max-width:580px;'><div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;'><h3 style='margin:0;font-size:16.5px;font-weight:900;'>📊 내 적중률 &amp; 포인트 마이페이지</h3><button type='button' id='sqCloseMyPageBtn' style='background:#1e293b;border:1px solid #334155;color:#cbd5e1;border-radius:8px;padding:4px 10px;cursor:pointer;font-weight:800;'>✕ 닫기 (Esc)</button></div><div id='sqMyPageBody'>불러오는 중...</div></div>";
      document.body.appendChild(m);
      document.getElementById("sqCloseMyPageBtn").onclick = function(){ m.style.display = "none"; };
      m.addEventListener("click", function(e){ if (e.target === m) m.style.display = "none"; });
    }
    m.style.display = "flex";
    var body = document.getElementById("sqMyPageBody");
    fetch("/api/member/mypage?nickname=" + encodeURIComponent(nk) + "&pin=" + encodeURIComponent(sqPin()))
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d || !d.ok) { body.innerHTML = "<div style='color:#f87171;'>프로필을 불러오지 못했습니다.</div>"; return; }
        var pf = d.profile || {};
        var st = d.stats || {};
        var picksHtml = (d.recent_picks || []).map(function(p){
          var col = p.status === "HIT" ? "#4ade80" : (p.status === "MISS" ? "#f87171" : "#fde68a");
          return "<div style='display:flex;justify-content:space-between;padding:7px 9px;background:#020617;border:1px solid #1e293b;border-radius:8px;margin-bottom:5px;font-size:12px;'>" +
            "<span><strong>[" + esc(p.league) + "]</strong> " + esc(p.matchup) + " · 픽: <strong style='color:#38bdf8;'>" + esc(p.pick) + "</strong></span>" +
            "<span style='color:" + col + ";font-weight:800;'>" + esc(p.status) + (p.settled_score ? " (" + esc(p.settled_score) + ")" : "") + " +" + esc(p.reward_points) + "P</span></div>";
        }).join("") || "<div style='color:#94a3b8;font-size:12px;padding:8px;'>아직 참여한 승부예측이 없습니다. 상단 빅매치 카드에서 홈승/무/원정승을 픽해보세요! (+5P 즉시 지급 · 적중 시 +30P)</div>";

        body.innerHTML = [
          "<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px;text-align:center;'>",
          "  <div style='background:#020617;border:1px solid #1e293b;border-radius:10px;padding:10px;'><div style='font-size:11px;color:#94a3b8;'>보유 포인트</div><div style='font-size:16px;font-weight:900;color:#38bdf8;'>" + esc(pf.points) + "P</div></div>",
          "  <div style='background:#020617;border:1px solid #1e293b;border-radius:10px;padding:10px;'><div style='font-size:11px;color:#94a3b8;'>예측 적중률</div><div style='font-size:16px;font-weight:900;color:#4ade80;'>" + esc(st.hit_rate_pct) + "%</div></div>",
          "  <div style='background:#020617;border:1px solid #1e293b;border-radius:10px;padding:10px;'><div style='font-size:11px;color:#94a3b8;'>현재 / 최고연승</div><div style='font-size:16px;font-weight:900;color:#fbbf24;'>" + esc(st.win_streak) + " / " + esc(st.best_streak) + "</div></div>",
          "  <div style='background:#020617;border:1px solid #1e293b;border-radius:10px;padding:10px;'><div style='font-size:11px;color:#94a3b8;'>연속 출석</div><div style='font-size:16px;font-weight:900;color:#f472b6;'>" + esc(pf.streak_days) + "일</div></div>",
          "</div>",
          "<div style='font-weight:800;font-size:13px;margin-bottom:6px;color:#f8fafc;'>🎯 최근 승부예측 &amp; 자동 정산 내역 (적중 +30P · 3연승 +25P · 5연승 +60P)</div>",
          picksHtml
        ].join("");
      });
  }

  function injectQuickPickButtonsOnTop20() {
    var grid = document.getElementById("globalTop10Grid");
    if (!grid) return;
    Array.prototype.slice.call(grid.children).forEach(function(card){
      if (card.querySelector(".sq-p3-pickbar")) return;
      var titleEl = card.querySelector(".text-sm.font-extrabold") || card.querySelector("h4, strong");
      var matchup = titleEl ? titleEl.textContent.trim() : "";
      if (!matchup || matchup.indexOf("vs") === -1) return;
      var bar = document.createElement("div");
      bar.className = "sq-p3-pickbar";
      bar.style.cssText = "display:flex;gap:5px;margin-top:8px;padding-top:7px;border-top:1px dashed rgba(148,163,184,.16);";
      [["HOME","🏠 홈승"],["DRAW","⚖️ 무"],["AWAY","✈️ 원정승"]].forEach(function(pair){
        var b = document.createElement("button");
        b.type = "button";
        b.className = "dc-pill";
        b.style.cssText = "flex:1;font-size:11px;padding:4px 6px;font-weight:800;";
        b.textContent = pair[1];
        b.onclick = function(ev){
          ev.stopPropagation();
          if (!sqNick() || sqPin().length !== 4) {
            if (typeof window.__openSportiqMemberHub === "function") window.__openSportiqMemberHub();
            if (typeof showToast === "function") showToast("🔐 승부예측 자동 정산을 위해 닉네임 + 4자리 PIN을 먼저 연동해 주세요!");
            return;
          }
          fetch("/api/member/predictions/pick", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ nickname: sqNick(), pin: sqPin(), match_key: matchup, matchup: matchup, league: "TOP매치", pick: pair[0] })
          }).then(function(r){ return r.json().then(function(x){ return {status:r.status, data:x}; }); })
            .then(function(res){
              if (res.status !== 200 || !res.data.ok) {
                if (typeof showToast === "function") showToast("ℹ️ " + ((res.data && res.data.detail) || "참여 실패"));
                return;
              }
              if (typeof state === "object" && state) { state.points = Number(res.data.points); if (typeof updatePointsUI === "function") updatePointsUI(); }
              if (typeof showToast === "function") showToast("🎯 [" + matchup + "] " + pair[1] + " 예측 완료! (+5P 즉시 지급 · 경기 종료 후 자동 정산)");
            });
        };
        bar.appendChild(b);
      });
      card.appendChild(bar);
    });
  }

  function pollMemberNotifications() {
    var nk = sqNick();
    if (!nk) return;
    fetch("/api/member/notifications?nickname=" + encodeURIComponent(nk) + "&limit=15")
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d || !d.ok) return;
        var unread = Number(d.unread_count || 0);
        var badge = document.getElementById("sqNotifBadge");
        if (badge) {
          badge.style.display = unread > 0 ? "inline-block" : "none";
          badge.textContent = String(unread);
        }
        if (lastUnreadNotifCount >= 0 && unread > lastUnreadNotifCount && d.items && d.items[0]) {
          var latest = d.items[0];
          if (typeof showToast === "function") showToast(latest.title);
          if ("Notification" in window && Notification.permission === "granted" && navigator.serviceWorker) {
            navigator.serviceWorker.ready.then(function(reg){
              reg.showNotification(latest.title, { body: latest.body, icon: "/pwa-icon-192.png" });
            }).catch(function(){});
          }
        }
        lastUnreadNotifCount = unread;
      }).catch(function(){});
  }

  function openNotificationsModal() {
    var nk = sqNick();
    var m = document.getElementById("sqNotifModal");
    if (!m) {
      m = document.createElement("div");
      m.id = "sqNotifModal";
      m.className = "sq-modal-backdrop";
      m.innerHTML = "<div class='sq-modal-panel' style='max-width:480px;'><div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;'><h3 style='margin:0;font-size:16px;font-weight:900;'>🔔 실시간 알림 센터</h3><div style='display:flex;gap:6px;'><button type='button' id='sqEnablePushBtn' class='dc-pill' style='font-size:11px;'>📲 푸시 허용</button><button type='button' id='sqReadAllNotifBtn' class='dc-pill' style='font-size:11px;'>모두 읽음</button><button type='button' id='sqCloseNotifBtn' style='background:#1e293b;border:1px solid #334155;color:#cbd5e1;border-radius:8px;padding:4px 9px;cursor:pointer;'>✕</button></div></div><div id='sqNotifListArea' style='font-size:12.5px;'>불러오는 중...</div></div>";
      document.body.appendChild(m);
      document.getElementById("sqCloseNotifBtn").onclick = function(){ m.style.display = "none"; };
      m.addEventListener("click", function(e){ if (e.target === m) m.style.display = "none"; });
      document.getElementById("sqEnablePushBtn").onclick = function(){
        if (!("Notification" in window)) return;
        Notification.requestPermission().then(function(p){
          if (typeof showToast === "function") showToast(p === "granted" ? "✅ 웹 푸시 알림이 활성화되었습니다!" : "브라우저 설정에서 알림 권한을 허용해 주세요.");
        });
      };
      document.getElementById("sqReadAllNotifBtn").onclick = function(){
        if (!sqNick()) return;
        fetch("/api/member/notifications/read-all", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({nickname:sqNick()}) })
          .then(function(){ pollMemberNotifications(); openNotificationsModal(); });
      };
    }
    m.style.display = "flex";
    var box = document.getElementById("sqNotifListArea");
    if (!nk) { box.innerHTML = "<div style='color:#94a3b8;padding:12px;'>닉네임 + 4자리 PIN 연동 후 내 글 댓글·승부예측 정산 알림을 실시간으로 받아보실 수 있습니다.</div>"; return; }
    fetch("/api/member/notifications?nickname=" + encodeURIComponent(nk) + "&limit=20")
      .then(function(r){ return r.json(); })
      .then(function(d){
        var items = (d && d.items) || [];
        if (!items.length) { box.innerHTML = "<div style='color:#94a3b8;padding:12px;text-align:center;'>새로운 알림이 없습니다.</div>"; return; }
        box.innerHTML = items.map(function(n){
          var bg = n.is_read ? "#0b1220" : "rgba(2,132,199,.16)";
          return "<div style='background:" + bg + ";border:1px solid #1e293b;border-radius:10px;padding:10px;margin-bottom:7px;'>" +
            "<div style='font-weight:800;color:#f8fafc;'>" + esc(n.title) + "</div>" +
            "<div style='color:#cbd5e1;font-size:12px;margin-top:3px;'>" + esc(n.body) + "</div>" +
            "<div style='color:#64748b;font-size:10.5px;margin-top:4px;'>" + esc(n.created_kst) + "</div></div>";
        }).join("");
      });
  }

  function loadBlindedSet() {
    fetch("/api/posts/blinds").then(function(r){return r.json();}).then(function(d){
      if (!d || !d.ok) return;
      blindedPostSet = {};
      (d.blinded_ids || []).forEach(function(id){ blindedPostSet[id] = true; });
      applyFeedFilterAndBlindDom();
    }).catch(function(){});
  }

  function mountFeedFilterAndSearchBar() {
    var feedBox = document.getElementById("feedList");
    if (!feedBox || !feedBox.parentNode || document.getElementById("sqP3FeedToolbar")) return;
    var bar = document.createElement("div");
    bar.id = "sqP3FeedToolbar";
    bar.style.cssText = "display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;margin-bottom:12px;background:#0b1220;border:1px solid #1e293b;border-radius:12px;padding:9px 11px;";
    bar.innerHTML = [
      "<div style='display:flex;gap:6px;flex-wrap:wrap;'>",
      "  <button type='button' data-sq-ftab='ALL' class='dc-pill' style='font-weight:800;border-color:#38bdf8;color:#e0f2fe;'>전체 피드</button>",
      "  <button type='button' data-sq-ftab='MEMBER' class='dc-pill' style='font-weight:800;'>✍️ 회원 분석글</button>",
      "  <button type='button' data-sq-ftab='AI' class='dc-pill' style='font-weight:800;'>🤖 AI 데이터 브리핑</button>",
      "</div>",
      "<input id='sqP3FeedSearchInput' placeholder='🔍 팀명 · 리그 · 작성자 · #해시태그 검색' style='flex:1;min-width:200px;max-width:300px;background:#020617;border:1px solid #334155;border-radius:9px;padding:6px 10px;color:#fff;font-size:12px;' />"
    ].join("");
    feedBox.parentNode.insertBefore(bar, feedBox);

    bar.querySelectorAll("[data-sq-ftab]").forEach(function(btn){
      btn.onclick = function(){
        p3FeedSourceFilter = btn.getAttribute("data-sq-ftab") || "ALL";
        bar.querySelectorAll("[data-sq-ftab]").forEach(function(x){
          var active = x.getAttribute("data-sq-ftab") === p3FeedSourceFilter;
          x.style.borderColor = active ? "#38bdf8" : "";
          x.style.color = active ? "#e0f2fe" : "";
        });
        applyFeedFilterAndBlindDom();
      };
    });
    document.getElementById("sqP3FeedSearchInput").addEventListener("input", function(ev){
      p3SearchQuery = (ev.target.value || "").trim().toLowerCase();
      applyFeedFilterAndBlindDom();
    });
  }

  function applyFeedFilterAndBlindDom() {
    var box = document.getElementById("feedList");
    if (!box || typeof state !== "object" || !state) return;
    box.querySelectorAll("article").forEach(function(art){
      var likeBtn = art.querySelector("button[onclick^='toggleLike(']");
      var m = likeBtn ? (likeBtn.getAttribute("onclick") || "").match(/toggleLike\(['"]([^'"]+)['"]\)/) : null;
      var pid = m ? m[1] : "";
      var postObj = (state.posts || []).find(function(x){ return x.id === pid; });

      if (pid && blindedPostSet[pid]) {
        art.innerHTML = "<div style='padding:14px;text-align:center;color:#94a3b8;font-size:12.5px;'>🚨 다수 회원의 누적 신고(3회 이상)로 자동 블라인드 처리된 게시글입니다.</div>";
        return;
      }

      if (pid && likeBtn && !art.querySelector("[data-sq-report]")) {
        var repBtn = document.createElement("button");
        repBtn.type = "button";
        repBtn.setAttribute("data-sq-report", pid);
        repBtn.className = "rounded-lg bg-slate-800 px-2 py-1 text-[11px] text-slate-400 hover:text-amber-300";
        repBtn.textContent = "🚨 신고";
        repBtn.onclick = function(){
          fetch("/api/posts/" + encodeURIComponent(pid) + "/report-clean", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ reporter: sqNick() || ("guest_" + (localStorage.getItem("sportiq_checked") || "1")), reason: "커뮤니티 클린 신고" })
          }).then(function(r){ return r.json(); }).then(function(d){
            if (d && d.blinded) { blindedPostSet[pid] = true; applyFeedFilterAndBlindDom(); }
            if (typeof showToast === "function") showToast(d && d.already_reported ? "이미 신고 접수된 게시글입니다." : "🚨 클린 신고가 접수되었습니다. (누적 " + ((d && d.report_count) || 1) + "/3)");
          });
        };
        likeBtn.parentNode.appendChild(repBtn);
      }

      var isAiPost = false;
      if (postObj) {
        var au = String(postObj.author || "").toLowerCase();
        isAiPost = !postObj.isMemberServerPost && (au.indexOf("ai") !== -1 || au.indexOf("봇") !== -1 || au.indexOf("데스크") !== -1 || au.indexOf("앙상블") !== -1 || String(pid).indexOf("mp_") !== 0);
      }
      var matchTab = (p3FeedSourceFilter === "ALL") || (p3FeedSourceFilter === "MEMBER" && !isAiPost) || (p3FeedSourceFilter === "AI" && isAiPost);
      var matchSearch = !p3SearchQuery || (art.textContent || "").toLowerCase().indexOf(p3SearchQuery) !== -1;
      art.style.display = (matchTab && matchSearch) ? "" : "none";
    });
  }

  window.addEventListener("keydown", function(ev){
    if (ev.key !== "Escape" && ev.keyCode !== 27) return;
    ["sqNotifModal", "sqMyPageModal"].forEach(function(id){
      var el = document.getElementById(id);
      if (el && el.style.display === "flex") el.style.display = "none";
    });
  }, true);

  setTimeout(function(){
    sortRenderedMatchCardStrips(false);
    loadBlindedSet();
    mountMyPageAndBellHeader();
    mountFeedFilterAndSearchBar();
    injectQuickPickButtonsOnTop20();
    pollMemberNotifications();
  }, 200);

  setInterval(function(){
    sortRenderedMatchCardStrips(false);
    mountMyPageAndBellHeader();
    mountFeedFilterAndSearchBar();
    injectQuickPickButtonsOnTop20();
    applyFeedFilterAndBlindDom();
  }, 900);
  setInterval(pollMemberNotifications, 25000);
})();
"""

UPGRADE_JS = UPGRADE_JS + "\n" + P3_AND_CHRONO_JS
# ============================================================================
# STAGE P4 (v3.3 / ?v=8): "AI 스코비" -> "AI 아이큐" Rebrand & Live AI Online Search
# ============================================================================
_AI_IQ_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_AI_IQ_CACHE_LOCK = threading.Lock()
_AI_IQ_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
_AI_IQ_STARTER_TRENDS: Tuple[str, ...] = (
    "오늘 해외축구 빅매치 속보",
    "손흥민 최신 뉴스",
    "EPL 주요 구단 부상자 명단",
    "NBA 오늘 경기 프리뷰",
    "오타니 쇼헤이 최신 소식",
    "챔피언스리그 우승 배당 흐름",
    "기대득점 xG 전술 뜻",
)


def _patch_scobi_across_modules() -> None:
    """Replaces 'AI 스코비' / '스코비' in any loaded app.* module string attributes."""
    try:
        for mname, mod in list(sys.modules.items()):
            if not mod or not mname.startswith("app"):
                continue
            if mname.endswith("sportiq_member_upgrade"):
                continue
            for attr in dir(mod):
                if attr.startswith("__"):
                    continue
                try:
                    val = getattr(mod, attr)
                    if isinstance(val, str) and ("스코비" in val or "오픈 스포츠 인사이트" in val):
                        setattr(mod, attr, _rebrand_scobi_text(val))
                except Exception:
                    pass
    except Exception:
        pass


def _http_get_bytes(url: str, timeout: float = 3.6) -> bytes:
    req = urllib.request.Request(url, headers=_AI_IQ_HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_rss_items(xml_bytes: bytes, default_source: str = "온라인 뉴스", limit: int = 8) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return items

    for item_el in root.findall(".//item"):
        raw_title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        pub_raw = (item_el.findtext("pubDate") or "").strip()
        src_el = item_el.find("source")
        source_name = src_el.text.strip() if (src_el is not None and src_el.text) else ""

        if not source_name and " - " in raw_title:
            parts = raw_title.rsplit(" - ", 1)
            if 1 < len(parts[1].strip()) <= 28:
                raw_title, source_name = parts[0].strip(), parts[1].strip()
        elif source_name and raw_title.endswith(" - " + source_name):
            raw_title = raw_title[: -(len(source_name) + 3)].strip()

        desc_raw = item_el.findtext("description") or ""
        desc_clean = re.sub(r"<[^>]+>", " ", html.unescape(desc_raw))
        desc_clean = re.sub(r"\s+", " ", desc_clean).strip()
        clean_title = html.unescape(raw_title).strip()
        if desc_clean.startswith(clean_title):
            desc_clean = desc_clean[len(clean_title) :].strip(" -·|")
        if source_name and desc_clean.endswith(source_name):
            desc_clean = desc_clean[: -len(source_name)].strip(" -·|")

        pub_kst = "실시간"
        ts = 0.0
        if pub_raw:
            try:
                dt = email.utils.parsedate_to_datetime(pub_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
                pub_kst = dt.astimezone(KST).strftime("%Y.%m.%d %H:%M")
            except Exception:
                pub_kst = pub_raw[:16]

        if clean_title and link.startswith(("http://", "https://")):
            items.append(
                {
                    "title": clean_title,
                    "url": link,
                    "source": source_name or default_source,
                    "published_kst": pub_kst,
                    "published_ts": ts,
                    "snippet": desc_clean[:180],
                }
            )
        if len(items) >= limit:
            break
    return items


def _fetch_google_news_online(query: str, lang: str = "ko", limit: int = 8) -> List[Dict[str, Any]]:
    q_enc = urllib.parse.quote(query)
    if lang == "ko":
        url = f"https://news.google.com/rss/search?q={q_enc}&hl=ko&gl=KR&ceid=KR:ko"
    else:
        url = f"https://news.google.com/rss/search?q={q_enc}&hl=en-US&gl=US&ceid=US:en"
    try:
        raw = _http_get_bytes(url, timeout=3.6)
        return _parse_rss_items(raw, default_source="Google 뉴스", limit=limit)
    except Exception:
        return []


def _fetch_bing_news_online(query: str, limit: int = 6) -> List[Dict[str, Any]]:
    q_enc = urllib.parse.quote(query)
    url = f"https://www.bing.com/news/search?q={q_enc}&format=rss&mkt=ko-KR"
    try:
        raw = _http_get_bytes(url, timeout=3.5)
        return _parse_rss_items(raw, default_source="웹 뉴스", limit=limit)
    except Exception:
        return []


def _fetch_wikipedia_knowledge(query: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for lang, label in (("ko", "한국어 위키백과"), ("en", "Wikipedia")):
        try:
            params = urllib.parse.urlencode(
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 2,
                    "utf8": 1,
                    "format": "json",
                }
            )
            raw = _http_get_bytes(f"https://{lang}.wikipedia.org/w/api.php?{params}", timeout=3.2)
            data = json.loads(raw.decode("utf-8"))
            hits = ((data.get("query") or {}).get("search")) or []
            if not hits:
                continue
            for h in hits[:2]:
                title = (h.get("title") or "").strip()
                if not title:
                    continue
                sum_url = (
                    f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
                    + urllib.parse.quote(title.replace(" ", "_"), safe="")
                )
                try:
                    s_raw = _http_get_bytes(sum_url, timeout=2.8)
                    s_data = json.loads(s_raw.decode("utf-8"))
                    extract = (s_data.get("extract") or "").strip()
                    page_url = (
                        ((s_data.get("content_urls") or {}).get("desktop") or {}).get("page")
                        or f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
                    )
                except Exception:
                    snippet_html = h.get("snippet") or ""
                    extract = re.sub(r"<[^>]+>", "", html.unescape(snippet_html)).strip()
                    page_url = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"

                if extract:
                    results.append(
                        {
                            "title": title,
                            "summary": extract[:420],
                            "url": page_url,
                            "source": label,
                        }
                    )
            if results:
                break
        except Exception:
            continue
    return results


def _fetch_ddg_instant_answer(query: str) -> List[Dict[str, Any]]:
    q_enc = urllib.parse.quote(query)
    url = f"https://api.duckduckgo.com/?q={q_enc}&format=json&no_html=1&skip_disambig=1"
    out: List[Dict[str, Any]] = []
    try:
        raw = _http_get_bytes(url, timeout=3.0)
        data = json.loads(raw.decode("utf-8"))
        abstract = (data.get("AbstractText") or "").strip()
        abs_url = (data.get("AbstractURL") or "").strip()
        heading = (data.get("Heading") or query).strip()
        if abstract and abs_url:
            out.append(
                {
                    "title": heading,
                    "summary": abstract[:400],
                    "url": abs_url,
                    "source": data.get("AbstractSource") or "웹 백과",
                }
            )
    except Exception:
        pass
    return out


def _search_sportiq_internal_context(query: str) -> Dict[str, List[Dict[str, Any]]]:
    tokens = [t.lower() for t in re.split(r"\s+", query.strip()) if len(t.strip()) >= 2][:5]
    if not tokens and query.strip():
        tokens = [query.strip().lower()]

    matched_games: List[Dict[str, Any]] = []
    matched_posts: List[Dict[str, Any]] = []
    cached_news: List[Dict[str, Any]] = []

    # 1. Check live/upcoming matches from sportiq_datacenter if loaded
    try:
        from app import sportiq_datacenter as _dc
        collect_fn = getattr(_dc, "_collect_all_matches", None)
        if callable(collect_fn):
            live_list = collect_fn(0) or []
            for m in live_list:
                if not isinstance(m, dict):
                    continue
                hay = " ".join(
                    str(m.get(k) or "") for k in ("home", "away", "league", "sport", "match_dt_kst", "status_badge")
                ).lower()
                if any(tok in hay for tok in tokens):
                    ai = m.get("ai") if isinstance(m.get("ai"), dict) else {}
                    matched_games.append(
                        {
                            "league": str(m.get("league") or "SPORT"),
                            "matchup": f"{m.get('home', '-')} vs {m.get('away', '-')}",
                            "dt_kst": str(m.get("match_dt_kst") or "일정 확인"),
                            "status": str(m.get("status_badge") or "Scheduled"),
                            "score": f"{m.get('home_score', 0)} : {m.get('away_score', 0)}",
                            "ai_prob": f"홈 {ai.get('home_pct', 50)}% / 원정 {ai.get('away_pct', 50)}%",
                            "drop_badge": str(m.get("drop_badge") or ""),
                        }
                    )
                    if len(matched_games) >= 5:
                        break
    except Exception:
        pass

    # 2. Check SQLite tables (global_match_rankings, community_member_posts, auto_analyst_posts, overseas_news_cache)
    def _db_lookup() -> None:
        with _db_session(write=False) as conn:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if len(matched_games) < 5 and "global_match_rankings" in tables:
                rows = conn.execute(
                    "SELECT league, matchup, schedule_venue, votes, buzz_score FROM global_match_rankings ORDER BY votes DESC LIMIT 25"
                ).fetchall()
                for r in rows:
                    hay = f"{r['league']} {r['matchup']} {r['schedule_venue']}".lower()
                    if any(tok in hay for tok in tokens):
                        matched_games.append(
                            {
                                "league": r["league"],
                                "matchup": r["matchup"],
                                "dt_kst": r["schedule_venue"],
                                "status": f"🔥 관심도 {r['buzz_score']}",
                                "score": f"투표 {r['votes']}표",
                                "ai_prob": "빅매치 랭킹 등재",
                                "drop_badge": "",
                            }
                        )
                        if len(matched_games) >= 5:
                            break

            if "community_member_posts" in tables:
                prows = conn.execute(
                    "SELECT id, league, author, title, body FROM community_member_posts ORDER BY created_at DESC LIMIT 40"
                ).fetchall()
                for r in prows:
                    hay = f"{r['league']} {r['author']} {r['title']} {r['body']}".lower()
                    if any(tok in hay for tok in tokens):
                        matched_posts.append(
                            {
                                "id": r["id"],
                                "league": r["league"],
                                "author": r["author"],
                                "title": r["title"],
                                "excerpt": (r["body"] or "")[:110],
                            }
                        )
                        if len(matched_posts) >= 4:
                            break

            if "overseas_news_cache" in tables:
                nrows = conn.execute(
                    "SELECT title, link, source, summary_tag FROM overseas_news_cache ORDER BY rowid DESC LIMIT 50"
                ).fetchall()
                for r in nrows:
                    hay = f"{r['title']} {r['summary_tag']}".lower()
                    if any(tok in hay for tok in tokens):
                        cached_news.append(
                            {
                                "title": r["title"],
                                "url": r["link"] or "",
                                "source": (r["source"] or "해외속보").upper(),
                                "published_kst": r["summary_tag"] or "최신 외신",
                                "published_ts": 1.0,
                                "snippet": "",
                            }
                        )
                        if len(cached_news) >= 4:
                            break

    try:
        run_with_db_retry(_db_lookup)
    except Exception:
        pass

    return {"games": matched_games, "posts": matched_posts, "cached_news": cached_news}


def _try_llm_grounded_summary(
    query: str,
    web_news: List[Dict[str, Any]],
    knowledge: List[Dict[str, Any]],
    internal: Dict[str, List[Dict[str, Any]]],
) -> Optional[str]:
    """Uses Gemini or OpenAI API automatically if configured in Render environment variables."""
    gemini_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not gemini_key and not openai_key:
        return None

    ctx_lines = [f"검색어: {query}"]
    for k in knowledge[:2]:
        ctx_lines.append(f"[백과] {k['title']}: {k['summary']}")
    for n in web_news[:5]:
        ctx_lines.append(f"[온라인뉴스/{n['source']}/{n['published_kst']}] {n['title']} - {n.get('snippet','')}")
    for g in internal.get("games", [])[:3]:
        ctx_lines.append(f"[SportIQ 경기] [{g['league']}] {g['matchup']} ({g['dt_kst']}) 상태:{g['status']} {g['ai_prob']} {g['drop_badge']}")

    prompt = (
        "당신은 스포츠 데이터 플랫폼 SportIQ의 실시간 검색 어시스턴트 'AI 아이큐'입니다. "
        "아래 실시간 온라인 검색 결과와 SportIQ 경기 데이터를 바탕으로 회원에게 핵심만 4~6문장으로 명확하고 친절하게 한국어 요약을 작성하세요.\n\n"
        + "\n".join(ctx_lines)
    )

    if gemini_key:
        try:
            g_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={urllib.parse.quote(gemini_key)}"
            payload = json.dumps(
                {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 420, "temperature": 0.3}}
            ).encode("utf-8")
            req = urllib.request.Request(g_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=4.5) as resp:
                gd = json.loads(resp.read().decode("utf-8"))
            txt = gd["candidates"][0]["content"]["parts"][0]["text"].strip()
            if txt:
                return txt
        except Exception:
            pass

    if openai_key:
        try:
            o_url = "https://api.openai.com/v1/chat/completions"
            payload = json.dumps(
                {
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400,
                    "temperature": 0.3,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                o_url,
                data=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {openai_key}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4.5) as resp:
                od = json.loads(resp.read().decode("utf-8"))
            txt = od["choices"][0]["message"]["content"].strip()
            if txt:
                return txt
        except Exception:
            pass

    return None


def _synthesize_ai_iq_briefing(
    query: str,
    web_news: List[Dict[str, Any]],
    knowledge: List[Dict[str, Any]],
    internal: Dict[str, List[Dict[str, Any]]],
) -> str:
    llm_txt = _try_llm_grounded_summary(query, web_news, knowledge, internal)
    if llm_txt:
        return llm_txt

    bullets: List[str] = []
    if knowledge:
        k0 = knowledge[0]
        bullets.append(f"📌 핵심 개요 ({k0['source']}): {k0['summary']}")

    games = internal.get("games") or []
    if games:
        g_summaries = [
            f"[{g['league']}] {g['matchup']} ({g['dt_kst']} · {g['ai_prob']}{' · ' + g['drop_badge'] if g['drop_badge'] else ''})"
            for g in games[:2]
        ]
        bullets.append("⚽ SportIQ 실시간 매치 데이터: " + " / ".join(g_summaries))

    if web_news:
        top_n = web_news[:3]
        headlines = [f"• [{n['source']} · {n['published_kst']}] {n['title']}" for n in top_n]
        bullets.append(f"🌐 실시간 온라인 뉴스 핵심 흐름 (총 {len(web_news)}건 포착):\n" + "\n".join(headlines))

    posts = internal.get("posts") or []
    if posts:
        bullets.append(f"💬 라운지 연관 분석글: '{posts[0]['title']}' 외 {len(posts)}건의 커뮤니티 의견이 등록되어 있습니다.")

    if not bullets:
        return (
            f"'{query}' 키워드에 대한 실시간 웹 탐색을 완료했으나 직결된 최신 기사나 백과 문서가 적습니다. "
            "구단명·선수명(예: 손흥민, 아스널, 바이에른, NBA, 오타니)이나 리그명을 포함해 검색하시면 훨씬 정확한 실시간 온라인 결과를 확인하실 수 있습니다."
        )
    return "\n\n".join(bullets)


class AiIqSearchIn(BaseModel):
    query: str
    mode: str = "all"  # all | news | wiki
    nickname: Optional[str] = ""
    pin: Optional[str] = ""


def _award_ai_search_bonus_if_eligible(nickname: str, pin: str, query: str) -> Tuple[int, Optional[int]]:
    nick = (nickname or "").strip()[:24]
    clean_pin = re.sub(r"\D", "", pin or "")
    if len(nick) < 2 or len(clean_pin) != 4:
        return 0, None
    pin_h = _hash_pin(nick, clean_pin)
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    now = time.time()

    def _op() -> Tuple[int, Optional[int]]:
        with _db_session(write=True) as conn:
            acc = conn.execute(
                "SELECT nickname, pin_hash, points FROM member_accounts WHERE lower(nickname)=lower(?)",
                (nick,),
            ).fetchone()
            if not acc or acc["pin_hash"] != pin_h:
                return 0, None
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM wp_point_ledger WHERE lower(nickname)=lower(?) AND event_type='ai_iq_search' AND kst_date=?",
                (acc["nickname"], today_kst),
            ).fetchone()["c"]
            if int(cnt or 0) >= 2:
                return 0, int(acc["points"] or 0)
            new_pts = int(acc["points"] or 0) + 3
            conn.execute(
                "UPDATE member_accounts SET points=?, role_tier=?, updated_at=? WHERE nickname=?",
                (new_pts, _tier_for_points(new_pts), now, acc["nickname"]),
            )
            conn.execute(
                """
                INSERT INTO wp_point_ledger(nickname, event_type, ref_id, points_delta, kst_date, site_origin, created_at)
                VALUES (?, 'ai_iq_search', ?, 3, ?, 'billnojay.com', ?)
                """,
                (acc["nickname"], query[:40], today_kst, now),
            )
            return 3, new_pts

    try:
        awarded, updated_pts = run_with_db_retry(_op)
        if awarded > 0:
            threading.Thread(target=_trigger_pg_backup, args=("member_accounts", "wp_point_ledger"), daemon=True).start()
        return awarded, updated_pts
    except Exception:
        return 0, None


@upgrade_router.get("/api/ai-iq/trending")
def get_ai_iq_trending():
    def _op() -> List[str]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                """
                SELECT MIN(query) AS q, COUNT(*) AS cnt, MAX(created_at) AS last_ts
                FROM ai_iq_search_logs
                WHERE length(norm_query) >= 2
                GROUP BY norm_query
                ORDER BY cnt DESC, last_ts DESC
                LIMIT 8
                """
            ).fetchall()
            popular = [r["q"] for r in rows if r["q"]]
            for st in _AI_IQ_STARTER_TRENDS:
                if len(popular) >= 8:
                    break
                if st.lower() not in {p.lower() for p in popular}:
                    popular.append(st)
            return popular[:8]

    return {"ok": True, "assistant_name": "AI 아이큐", "keywords": run_with_db_retry(_op)}


@upgrade_router.post("/api/ai-iq/search")
def run_ai_iq_online_search(payload: AiIqSearchIn):
    q = re.sub(r"\s+", " ", (payload.query or "").strip())[:100]
    if len(q) < 1:
        raise HTTPException(status_code=400, detail="검색어를 입력해 주세요.")
    mode = (payload.mode or "all").strip().lower()
    if mode not in ("all", "news", "wiki"):
        mode = "all"

    cache_key = f"{mode}:{q.lower()}"
    now_m = time.monotonic()
    cached_payload: Optional[Dict[str, Any]] = None
    with _AI_IQ_CACHE_LOCK:
        hit = _AI_IQ_CACHE.get(cache_key)
        if hit and (now_m - hit[0]) <= 120.0:
            cached_payload = dict(hit[1])

    if cached_payload is None:
        bucket: Dict[str, Any] = {"gnews_ko": [], "gnews_en": [], "bnews": [], "wiki": [], "ddg": []}

        def _t_gko():
            if mode in ("all", "news"):
                bucket["gnews_ko"] = _fetch_google_news_online(q, lang="ko", limit=8)

        def _t_gen():
            if mode in ("all", "news") and re.search(r"[a-zA-Z]", q):
                bucket["gnews_en"] = _fetch_google_news_online(q, lang="en", limit=5)

        def _t_bing():
            if mode in ("all", "news"):
                bucket["bnews"] = _fetch_bing_news_online(q, limit=6)

        def _t_wiki():
            if mode in ("all", "wiki"):
                bucket["wiki"] = _fetch_wikipedia_knowledge(q)
                if not bucket["wiki"]:
                    bucket["ddg"] = _fetch_ddg_instant_answer(q)

        workers = [
            threading.Thread(target=fn, daemon=True)
            for fn in (_t_gko, _t_gen, _t_bing, _t_wiki)
        ]
        for w in workers:
            w.start()
        internal_ctx = _search_sportiq_internal_context(q)
        for w in workers:
            w.join(timeout=4.0)

        # Merge, deduplicate, and sort online news by newest published_ts first
        seen_titles: set[str] = set()
        merged_news: List[Dict[str, Any]] = []
        for item in [*bucket["gnews_ko"], *bucket["bnews"], *bucket["gnews_en"], *internal_ctx.get("cached_news", [])]:
            norm_t = re.sub(r"[^a-z0-9가-힣]", "", (item.get("title") or "").lower())[:42]
            if not norm_t or norm_t in seen_titles:
                continue
            seen_titles.add(norm_t)
            merged_news.append(item)
        merged_news.sort(key=lambda x: float(x.get("published_ts") or 0.0), reverse=True)
        merged_news = merged_news[:10]

        knowledge_cards = (bucket["wiki"] + bucket["ddg"])[:3]
        summary_text = _synthesize_ai_iq_briefing(q, merged_news, knowledge_cards, internal_ctx)

        related_queries = [
            f"{q} 최신 뉴스",
            f"{q} 부상·라인업",
            f"{q} 경기 일정·배당",
        ]

        cached_payload = {
            "query": q,
            "mode": mode,
            "searched_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            "ai_summary": summary_text,
            "knowledge": knowledge_cards,
            "web_news": merged_news,
            "sportiq_matches": internal_ctx.get("games", []),
            "sportiq_posts": internal_ctx.get("posts", []),
            "related_queries": related_queries,
        }
        with _AI_IQ_CACHE_LOCK:
            if len(_AI_IQ_CACHE) > 200:
                _AI_IQ_CACHE.clear()
            _AI_IQ_CACHE[cache_key] = (now_m, cached_payload)

    total_cnt = (
        len(cached_payload.get("web_news") or [])
        + len(cached_payload.get("knowledge") or [])
        + len(cached_payload.get("sportiq_matches") or [])
    )

    def _log_op() -> None:
        with _db_session(write=True) as conn:
            conn.execute(
                """
                INSERT INTO ai_iq_search_logs(query, norm_query, mode, nickname, result_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (q, q.lower(), mode, (payload.nickname or "").strip()[:24], total_cnt, time.time()),
            )

    try:
        run_with_db_retry(_log_op)
        threading.Thread(target=_trigger_pg_backup, args=("ai_iq_search_logs",), daemon=True).start()
    except Exception:
        pass

    awarded_pts, updated_pts = _award_ai_search_bonus_if_eligible(
        payload.nickname or "", payload.pin or "", q
    )

    return {
        "ok": True,
        "assistant": "AI 아이큐",
        "awarded_points": awarded_pts,
        "updated_points": updated_pts,
        **cached_payload,
    }


# Run Scoby -> AI IQ module rebrander in background as well
def _ai_iq_rebrand_daemon() -> None:
    for _ in range(10):
        _patch_scobi_across_modules()
        time.sleep(2.0)


threading.Thread(target=_ai_iq_rebrand_daemon, daemon=True, name="sportiq-ai-iq-rebrand").start()


# --- Frontend v8.0: "AI 스코비" -> "AI 아이큐" Rebrand + Online AI Search Modal ---
AI_IQ_ONLINE_SEARCH_JS = r"""
/* SportIQ v8.0: 'AI 스코비' -> 'AI 아이큐' Rebrand & Live Online AI Search Hub */
(function () {
  if (window.__SPORTIQ_V8_AI_IQ__) return;
  window.__SPORTIQ_V8_AI_IQ__ = true;

  var LS_AI_HISTORY = "sq_ai_iq_recent_queries";
  var currentAiMode = "all";
  var lastSearchResult = null;

  function sqNick() { return (localStorage.getItem("sportiq_member_nick") || localStorage.getItem("sportiq_guest_nick") || "").trim(); }
  function sqPin() { return (localStorage.getItem("sportiq_member_pin") || "").trim(); }
  function esc(v) { return v == null ? "" : String(v).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

  // Inject dedicated styles for AI 아이큐 pill & online search modal
  var st = document.createElement("style");
  st.id = "sqAiIqStyles";
  st.textContent = [
    ".sq-ai-iq-fab { position:fixed; right:20px; bottom:20px; z-index:9995; background:linear-gradient(135deg,#06b6d4,#10b981); color:#020617; border:0; border-radius:999px; padding:11px 20px; font-size:14px; font-weight:900; cursor:pointer; display:inline-flex; align-items:center; gap:7px; box-shadow:0 10px 28px rgba(6,182,212,.42); transition:transform .16s, filter .16s; letter-spacing:-0.2px; }",
    ".sq-ai-iq-fab:hover { transform:translateY(-2px) scale(1.02); filter:brightness(1.06); }",
    ".sq-ai-iq-card { background:#0b1220; border:1px solid #1e293b; border-radius:12px; padding:12px 14px; margin-bottom:9px; transition:border-color .15s; }",
    ".sq-ai-iq-card:hover { border-color:#38bdf8; }",
    "@keyframes sqPulseDot { 0%,100%{opacity:.35;transform:scale(.9);} 50%{opacity:1;transform:scale(1.15);} }",
    ".sq-ai-loading-dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:#22d3ee; margin:0 2px; animation:sqPulseDot 1.1s infinite; }"
  ].join("\n");
  document.head.appendChild(st);

  function rebrandTextString(val) {
    if (!val) return val;
    var out = val;
    if (out.indexOf("오픈 스포츠 인사이트") !== -1) {
      out = out
        .replace(/billnojay\.com\s*(?:·\s*)?오픈\s*스포츠\s*인사이트/gi, "실시간 분석 스포츠IQ 인사이트")
        .replace(/오픈\s*스포츠\s*인사이트/g, "실시간 분석 스포츠IQ 인사이트");
    }
    if (out.indexOf("스코비") !== -1) {
      out = out.replace(/AI\s*스코비/g, "AI 아이큐").replace(/스코비/g, "아이큐");
    }
    return out;
  }

  // 1) Replace every 'billnojay.com 오픈 스포츠 인사이트' & 'AI 스코비' text node / split header subtitle / attribute across DOM
  function replaceScobiInNode(root) {
    if (!root) return;
    if (root.querySelectorAll) {
      // Handle cases where 'billnojay.com' and '오픈 스포츠 인사이트' are in adjacent spans inside a single subtitle line
      root.querySelectorAll("header p, header div, header span, p, div, span").forEach(function (el) {
        if (!el || el.children.length > 3) return;
        var flat = (el.textContent || "").replace(/\s+/g, " ").trim();
        if (/^billnojay\.com\s*(?:·\s*)?오픈\s*스포츠\s*인사이트$/i.test(flat)) {
          el.textContent = "실시간 분석 스포츠IQ 인사이트";
        }
      });
    }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    var node;
    while ((node = walker.nextNode())) {
      var val = node.nodeValue;
      if (val && (val.indexOf("스코비") !== -1 || val.indexOf("오픈 스포츠 인사이트") !== -1)) {
        node.nodeValue = rebrandTextString(val);
      }
    }
    if (root.querySelectorAll) {
      root.querySelectorAll("[placeholder*='스코비'], [title*='스코비'], [aria-label*='스코비'], [content*='오픈 스포츠 인사이트']").forEach(function (el) {
        ["placeholder", "title", "aria-label", "content"].forEach(function (attr) {
          var v = el.getAttribute(attr);
          if (v && (v.indexOf("스코비") !== -1 || v.indexOf("오픈 스포츠 인사이트") !== -1)) {
            el.setAttribute(attr, rebrandTextString(v));
          }
        });
      });
    }
    if (document.title && (document.title.indexOf("스코비") !== -1 || document.title.indexOf("오픈 스포츠 인사이트") !== -1)) {
      document.title = rebrandTextString(document.title);
    }
  }

  function isLauncherPillElement(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.id === "sqAiIqModal" || el.closest("#sqAiIqModal")) return false;
    var btn = el.closest("button, a, [role='button'], .sq-ai-iq-fab");
    if (!btn) return false;
    if (btn.closest("#sqAiIqModal")) return false;
    if (btn.id === "sqAiIqFloatingFab" || btn.hasAttribute("data-sq-ai-iq-trigger")) return true;
    var txt = (btn.textContent || "").replace(/\s+/g, " ").trim();
    if (txt.length <= 28 && (txt.indexOf("AI 아이큐") !== -1 || txt.indexOf("AI 스코비") !== -1 || txt.indexOf("AI아이큐") !== -1)) {
      return true;
    }
    return false;
  }

  function hideLegacyScobiPopups() {
    document.querySelectorAll("[id*='scobi' i], [id*='scoby' i], [class*='scobi' i], [class*='scoby' i]").forEach(function (el) {
      if (isLauncherPillElement(el) || el.querySelector && isLauncherPillElement(el.querySelector("button"))) return;
      var cs = window.getComputedStyle ? window.getComputedStyle(el) : null;
      var w = el.offsetWidth || 0;
      var h = el.offsetHeight || 0;
      if (w > 220 && h > 180 && cs && (cs.position === "fixed" || cs.position === "absolute")) {
        el.style.display = "none";
      }
    });
  }

  // Ensure the bottom-right '🤖 AI 아이큐' pill button is always visible & styled cleanly
  function ensureAiIqLauncherButtons() {
    replaceScobiInNode(document.body);

    var foundExistingPill = null;
    document.querySelectorAll("button, a, div[role='button']").forEach(function (el) {
      if (el.closest("#sqAiIqModal") || el.id === "sqHeaderAiIqBtn" || el.id === "sqFeedAiIqBtn") return;
      var t = (el.textContent || "").trim();
      if (t.length <= 28 && (t.indexOf("AI 아이큐") !== -1 || t.indexOf("AI 스코비") !== -1)) {
        foundExistingPill = el;
        el.setAttribute("data-sq-ai-iq-trigger", "1");
        el.title = "AI 아이큐 — 실시간 온라인 AI 검색 열기 (단축키 Ctrl+K)";
      }
    });

    if (!foundExistingPill && !document.getElementById("sqAiIqFloatingFab")) {
      var fab = document.createElement("button");
      fab.id = "sqAiIqFloatingFab";
      fab.type = "button";
      fab.className = "sq-ai-iq-fab";
      fab.setAttribute("data-sq-ai-iq-trigger", "1");
      fab.innerHTML = "🤖 <span>AI 아이큐</span>";
      fab.title = "AI 아이큐 — 실시간 온라인 AI 검색 열기 (단축키 Ctrl+K)";
      document.body.appendChild(fab);
    }

    // Add quick '🤖 AI 아이큐 검색' button inside the Community search toolbar as well
    var feedToolbar = document.getElementById("sqP3FeedToolbar");
    if (feedToolbar && !document.getElementById("sqFeedAiIqBtn")) {
      var qb = document.createElement("button");
      qb.id = "sqFeedAiIqBtn";
      qb.type = "button";
      qb.setAttribute("data-sq-ai-iq-trigger", "1");
      qb.style.cssText = "background:linear-gradient(135deg,#06b6d4,#10b981);color:#020617;border:0;border-radius:9px;padding:6px 12px;font-size:12px;font-weight:900;cursor:pointer;white-space:nowrap;";
      qb.innerHTML = "🤖 AI 아이큐 온라인 검색";
      feedToolbar.appendChild(qb);
    }
  }

  // Intercept clicks on any '🤖 AI 아이큐' trigger in capture phase so legacy Scoby never blocks or overlaps
  window.addEventListener("click", function (ev) {
    if (!isLauncherPillElement(ev.target)) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (typeof ev.stopImmediatePropagation === "function") ev.stopImmediatePropagation();
    hideLegacyScobiPopups();
    openAiIqModal();
  }, true);

  // Keyboard shortcut: Ctrl+K or Cmd+K opens AI 아이큐 search, Escape closes it
  window.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === "k" || ev.key === "K")) {
      ev.preventDefault();
      openAiIqModal();
      return;
    }
    if (ev.key === "Escape" || ev.keyCode === 27) {
      var m = document.getElementById("sqAiIqModal");
      if (m && m.style.display === "flex") {
        ev.preventDefault();
        ev.stopPropagation();
        m.style.display = "none";
      }
    }
  }, true);

  function getRecentHistory() {
    try {
      var arr = JSON.parse(localStorage.getItem(LS_AI_HISTORY) || "[]");
      return Array.isArray(arr) ? arr.slice(0, 6) : [];
    } catch (e) { return []; }
  }

  function saveRecentHistory(q) {
    var clean = String(q || "").trim();
    if (!clean) return;
    var list = getRecentHistory().filter(function (x) { return x.toLowerCase() !== clean.toLowerCase(); });
    list.unshift(clean);
    try { localStorage.setItem(LS_AI_HISTORY, JSON.stringify(list.slice(0, 6))); } catch (e) {}
  }

  function buildAiIqModalIfNeeded() {
    var m = document.getElementById("sqAiIqModal");
    if (m) return m;

    m = document.createElement("div");
    m.id = "sqAiIqModal";
    m.className = "sq-modal-backdrop";
    m.style.zIndex = "100050";
    m.innerHTML = [
      "<div class='sq-modal-panel' style='max-width:680px;padding:20px;border-color:rgba(6,182,212,.5);'>",
      "  <div style='display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px;'>",
      "    <div style='display:flex;align-items:center;gap:8px;'>",
      "      <span style='background:linear-gradient(135deg,#06b6d4,#10b981);color:#020617;border-radius:999px;padding:4px 11px;font-size:12.5px;font-weight:900;'>🤖 AI 아이큐</span>",
      "      <h3 style='margin:0;font-size:16.5px;font-weight:900;color:#f8fafc;'>실시간 온라인 AI 스포츠·통합 검색</h3>",
      "    </div>",
      "    <button type='button' id='sqCloseAiIqModalBtn' style='background:#1e293b;border:1px solid #334155;color:#cbd5e1;border-radius:8px;padding:4px 10px;cursor:pointer;font-weight:800;'>✕ 닫기 (Esc)</button>",
      "  </div>",
      "  <div style='font-size:12px;color:#94a3b8;margin-bottom:10px;'>구글·웹 실시간 뉴스, 위키백과 지식, SportIQ 실시간 경기·배당·분석글을 동시에 온라인 탐색해 요약합니다. (PIN 연동 회원 일일 리서치 <strong style='color:#34d399;'>+3P</strong> 적립)</div>",
      "  <form id='sqAiIqSearchForm' style='display:flex;gap:8px;margin-bottom:10px;'>",
      "    <input id='sqAiIqInput' type='search' autocomplete='off' placeholder='선수·구단·부상속보·배당·전술 등 무엇이든 온라인 검색 (예: 손흥민 최신 뉴스)' style='flex:1;background:#020617;border:1px solid #38bdf8;border-radius:11px;padding:10px 13px;color:#f8fafc;font-size:13.5px;outline:none;' />",
      "    <button type='submit' id='sqAiIqSubmitBtn' style='background:linear-gradient(135deg,#06b6d4,#10b981);color:#020617;border:0;border-radius:11px;padding:0 18px;font-size:13px;font-weight:900;cursor:pointer;white-space:nowrap;'>🔍 AI 검색</button>",
      "  </form>",
      "  <div id='sqAiIqModeBar' style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;'>",
      "    <button type='button' data-ai-mode='all' class='dc-pill' style='font-size:11.5px;font-weight:800;border-color:#22d3ee;color:#cffafe;'>🌐 통합 AI 온라인 검색</button>",
      "    <button type='button' data-ai-mode='news' class='dc-pill' style='font-size:11.5px;font-weight:800;'>📰 최신 온라인 속보 집중</button>",
      "    <button type='button' data-ai-mode='wiki' class='dc-pill' style='font-size:11.5px;font-weight:800;'>📚 선수·구단·용어 백과</button>",
      "  </div>",
      "  <div id='sqAiIqTrendingWrap' style='margin-bottom:12px;'></div>",
      "  <div id='sqAiIqResultArea' style='font-size:13px;color:#e2e8f0;line-height:1.6;'>",
      "    <div style='background:#020617;border:1px dashed #334155;border-radius:12px;padding:16px;text-align:center;color:#94a3b8;'>",
      "      💡 궁금한 팀·선수·매치업이나 스포츠 이슈를 위 검색창에 입력하거나 추천 키워드를 눌러보세요!",
      "    </div>",
      "  </div>",
      "</div>"
    ].join("");
    document.body.appendChild(m);

    document.getElementById("sqCloseAiIqModalBtn").onclick = function () { m.style.display = "none"; };
    m.addEventListener("click", function (ev) { if (ev.target === m) m.style.display = "none"; });

    document.getElementById("sqAiIqModeBar").querySelectorAll("[data-ai-mode]").forEach(function (btn) {
      btn.onclick = function () {
        currentAiMode = btn.getAttribute("data-ai-mode") || "all";
        document.getElementById("sqAiIqModeBar").querySelectorAll("[data-ai-mode]").forEach(function (b) {
          var on = b.getAttribute("data-ai-mode") === currentAiMode;
          b.style.borderColor = on ? "#22d3ee" : "";
          b.style.color = on ? "#cffafe" : "";
        });
        var curQ = (document.getElementById("sqAiIqInput").value || "").trim();
        if (curQ) executeAiIqSearch(curQ);
      };
    });

    document.getElementById("sqAiIqSearchForm").onsubmit = function (ev) {
      ev.preventDefault();
      var q = (document.getElementById("sqAiIqInput").value || "").trim();
      if (!q) {
        if (typeof showToast === "function") showToast("검색어를 입력해 주세요.");
        return;
      }
      executeAiIqSearch(q);
    };

    return m;
  }

  function loadTrendingAndHistoryChips() {
    var wrap = document.getElementById("sqAiIqTrendingWrap");
    if (!wrap) return;
    fetch("/api/ai-iq/trending")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var trends = (d && d.keywords) || [];
        var recent = getRecentHistory();
        var htmlParts = [];
        if (recent.length) {
          htmlParts.push(
            "<div style='display:flex;gap:5px;flex-wrap:wrap;align-items:center;margin-bottom:6px;'>" +
            "<span style='font-size:11px;color:#64748b;font-weight:700;'>최근 검색:</span>" +
            recent.map(function (rk) {
              return "<button type='button' data-ai-chip='" + esc(rk) + "' style='background:#0f172a;border:1px solid #334155;color:#cbd5e1;border-radius:999px;padding:2px 9px;font-size:11px;cursor:pointer;'>🕒 " + esc(rk) + "</button>";
            }).join("") +
            "</div>"
          );
        }
        if (trends.length) {
          htmlParts.push(
            "<div style='display:flex;gap:5px;flex-wrap:wrap;align-items:center;'>" +
            "<span style='font-size:11px;color:#38bdf8;font-weight:800;'>🔥 실시간 추천:</span>" +
            trends.slice(0, 6).map(function (tk) {
              return "<button type='button' data-ai-chip='" + esc(tk) + "' style='background:rgba(6,182,212,.12);border:1px solid rgba(34,211,238,.35);color:#a5f3fc;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:700;cursor:pointer;'>" + esc(tk) + "</button>";
            }).join("") +
            "</div>"
          );
        }
        wrap.innerHTML = htmlParts.join("");
        wrap.querySelectorAll("[data-ai-chip]").forEach(function (c) {
          c.onclick = function () {
            var q = c.getAttribute("data-ai-chip") || "";
            var inp = document.getElementById("sqAiIqInput");
            if (inp) inp.value = q;
            executeAiIqSearch(q);
          };
        });
      }).catch(function () {});
  }

  function executeAiIqSearch(query) {
    var box = document.getElementById("sqAiIqResultArea");
    var btn = document.getElementById("sqAiIqSubmitBtn");
    if (!box) return;
    saveRecentHistory(query);
    if (btn) { btn.disabled = true; btn.textContent = "탐색 중..."; }
    box.innerHTML = [
      "<div style='background:#020617;border:1px solid #1e293b;border-radius:12px;padding:22px;text-align:center;'>",
      "  <div><span class='sq-ai-loading-dot'></span><span class='sq-ai-loading-dot' style='animation-delay:.2s'></span><span class='sq-ai-loading-dot' style='animation-delay:.4s'></span></div>",
      "  <div style='margin-top:8px;font-weight:800;color:#67e8f9;'>🤖 AI 아이큐가 실시간 온라인 뉴스·백과·매치 데이터를 검색하고 있습니다...</div>",
      "  <div style='font-size:11.5px;color:#64748b;margin-top:3px;'>검색어: " + esc(query) + "</div>",
      "</div>"
    ].join("");

    fetch("/api/ai-iq/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: query,
        mode: currentAiMode,
        nickname: sqNick(),
        pin: sqPin()
      })
    })
      .then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
      .then(function (res) {
        if (btn) { btn.disabled = false; btn.textContent = "🔍 AI 검색"; }
        if (res.status !== 200 || !res.data || !res.data.ok) {
          box.innerHTML = "<div style='color:#f87171;padding:12px;'>❌ " + esc((res.data && res.data.detail) || "온라인 검색 중 일시적인 오류가 발생했습니다.") + "</div>";
          return;
        }
        var d = res.data;
        lastSearchResult = d;
        if (d.awarded_points && d.awarded_points > 0) {
          if (typeof state === "object" && state && d.updated_points != null) {
            state.points = Number(d.updated_points);
            if (typeof window.updatePointsUI === "function") window.updatePointsUI();
          }
          if (typeof showToast === "function") {
            showToast("🤖 AI 아이큐 리서치 보너스 +" + d.awarded_points + "P 적립 완료!");
          }
        }
        renderAiIqSearchResult(d);
        loadTrendingAndHistoryChips();
      })
      .catch(function () {
        if (btn) { btn.disabled = false; btn.textContent = "🔍 AI 검색"; }
        box.innerHTML = "<div style='color:#f87171;padding:12px;'>네트워크 연결을 확인한 뒤 다시 검색해 주세요.</div>";
      });
  }

  function renderAiIqSearchResult(d) {
    var box = document.getElementById("sqAiIqResultArea");
    if (!box) return;
    var summaryHtml = esc(d.ai_summary || "").replace(/\n/g, "<br/>");

    var out = [
      "<div style='background:linear-gradient(145deg,rgba(6,182,212,.14),rgba(15,23,42,.96));border:1px solid rgba(34,211,238,.45);border-radius:14px;padding:14px 15px;margin-bottom:12px;'>",
      "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;'>",
      "    <span style='font-weight:900;color:#67e8f9;font-size:13.5px;'>🧠 AI 아이큐 실시간 온라인 브리핑</span>",
      "    <span style='font-size:11px;color:#94a3b8;'>" + esc(d.searched_at_kst || "") + "</span>",
      "  </div>",
      "  <div style='font-size:13px;color:#f1f5f9;line-height:1.68;'>" + summaryHtml + "</div>",
      "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-top:11px;padding-top:9px;border-top:1px dashed rgba(148,163,184,.2);'>",
      "    <div style='display:flex;gap:5px;flex-wrap:wrap;'>",
      (d.related_queries || []).map(function (rq) {
        return "<button type='button' data-ai-rel='" + esc(rq) + "' style='background:#0f172a;border:1px solid #334155;color:#93c5fd;border-radius:999px;padding:2px 9px;font-size:11px;cursor:pointer;'>🔎 " + esc(rq) + "</button>";
      }).join(""),
      "    </div>",
      "    <button type='button' id='sqAiWriteFromSearchBtn' style='background:#0284c7;color:#fff;border:0;border-radius:8px;padding:5px 11px;font-size:11.5px;font-weight:800;cursor:pointer;'>✍️ 이 주제로 분석글 쓰기 (+15P)</button>",
      "  </div>",
      "</div>"
    ];

    // Knowledge / Encyclopedia Cards
    var know = d.knowledge || [];
    if (know.length) {
      out.push("<div style='font-weight:800;color:#fde68a;font-size:12.5px;margin:10px 0 6px;'>📚 백과 · 핵심 배경지식</div>");
      know.forEach(function (k) {
        out.push(
          "<div class='sq-ai-iq-card'>" +
          "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;'>" +
          "    <strong style='color:#f8fafc;font-size:13px;'>" + esc(k.title) + " <span style='font-size:11px;color:#38bdf8;font-weight:700;'>(" + esc(k.source) + ")</span></strong>" +
          "    <a href='" + esc(k.url) + "' target='_blank' rel='noopener noreferrer' style='font-size:11px;color:#34d399;font-weight:800;text-decoration:none;'>원문 보기 ↗</a>" +
          "  </div>" +
          "  <div style='font-size:12px;color:#cbd5e1;margin-top:5px;'>" + esc(k.summary) + "</div>" +
          "</div>"
        );
      });
    }

    // SportIQ Live Match & Odds Cards
    var games = d.sportiq_matches || [];
    if (games.length) {
      out.push("<div style='font-weight:800;color:#4ade80;font-size:12.5px;margin:10px 0 6px;'>📊 SportIQ 실시간 매치 · 배당 연동</div>");
      games.forEach(function (g) {
        out.push(
          "<div class='sq-ai-iq-card' style='display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;'>" +
          "  <div>" +
          "    <span style='background:#1e293b;color:#38bdf8;border-radius:6px;padding:2px 6px;font-size:10.5px;font-weight:800;margin-right:6px;'>" + esc(g.league) + "</span>" +
          "    <strong style='color:#f8fafc;font-size:13px;'>" + esc(g.matchup) + "</strong>" +
          "    <div style='font-size:11.5px;color:#94a3b8;margin-top:3px;'>📅 " + esc(g.dt_kst) + " · 🎯 " + esc(g.ai_prob) + (g.drop_badge ? " · <span style='color:#fbbf24;font-weight:800;'>" + esc(g.drop_badge) + "</span>" : "") + "</div>" +
          "  </div>" +
          "  <span style='font-size:11.5px;font-weight:800;color:#e2e8f0;background:#020617;border:1px solid #334155;border-radius:8px;padding:4px 9px;'>" + esc(g.status) + "</span>" +
          "</div>"
        );
      });
    }

    // Live Online Web News List
    var news = d.web_news || [];
    if (news.length) {
      out.push("<div style='font-weight:800;color:#38bdf8;font-size:12.5px;margin:10px 0 6px;'>🌐 실시간 온라인 뉴스 &amp; 웹 문서 (최신순 " + news.length + "건)</div>");
      news.forEach(function (n) {
        out.push(
          "<a href='" + esc(n.url) + "' target='_blank' rel='noopener noreferrer' class='sq-ai-iq-card' style='display:block;text-decoration:none;'>" +
          "  <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;'>" +
          "    <span style='font-size:11px;font-weight:800;color:#22d3ee;'>📰 " + esc(n.source) + "</span>" +
          "    <span style='font-size:11px;color:#94a3b8;'>🕒 " + esc(n.published_kst) + " · 원문 열기 ↗</span>" +
          "  </div>" +
          "  <div style='font-weight:800;color:#f8fafc;font-size:13px;'>" + esc(n.title) + "</div>" +
          (n.snippet ? "<div style='font-size:11.5px;color:#94a3b8;margin-top:3px;'>" + esc(n.snippet) + "</div>" : "") +
          "</a>"
        );
      });
    }

    box.innerHTML = out.join("");

    box.querySelectorAll("[data-ai-rel]").forEach(function (rb) {
      rb.onclick = function () {
        var rq = rb.getAttribute("data-ai-rel") || "";
        var inp = document.getElementById("sqAiIqInput");
        if (inp) inp.value = rq;
        executeAiIqSearch(rq);
      };
    });

    var wBtn = document.getElementById("sqAiWriteFromSearchBtn");
    if (wBtn) {
      wBtn.onclick = function () {
        var modal = document.getElementById("sqAiIqModal");
        if (modal) modal.style.display = "none";
        var tEl = document.getElementById("postTitle");
        var bEl = document.getElementById("postContent");
        if (tEl) tEl.value = "[AI 아이큐 리서치] " + (d.query || "") + " 핵심 분석";
        if (bEl) {
          var topLinks = (d.web_news || []).slice(0, 2).map(function (x) { return "- " + x.title + " (" + x.source + ")"; }).join("\n");
          bEl.value = (d.ai_summary || "") + (topLinks ? "\n\n[참고 온라인 뉴스]\n" + topLinks : "") + "\n\n[내 승부예측 의견]\n";
        }
        if (typeof window.openModal === "function") window.openModal("writeModal");
        else {
          var wm = document.getElementById("writeModal");
          if (wm) wm.classList.remove("hidden");
        }
      };
    }
  }

  function openAiIqModal(initialQuery) {
    var m = buildAiIqModalIfNeeded();
    m.style.display = "flex";
    loadTrendingAndHistoryChips();
    var inp = document.getElementById("sqAiIqInput");
    if (inp) {
      if (initialQuery) inp.value = initialQuery;
      setTimeout(function () { inp.focus(); }, 60);
    }
    if (initialQuery) executeAiIqSearch(initialQuery);
  }
  window.__openSportiqAiIq = openAiIqModal;

  // MutationObserver so if any script renders 'AI 스코비' dynamically, it turns into 'AI 아이큐' immediately
  function installScobiRebrandObserver() {
    if (!document.body || window.__sqAiIqObsInstalled) return;
    window.__sqAiIqObsInstalled = true;
    replaceScobiInNode(document.body);
    var obs = new MutationObserver(function (mutations) {
      mutations.forEach(function (mu) {
        if (mu.type === "characterData" && mu.target && mu.target.nodeValue && (mu.target.nodeValue.indexOf("스코비") !== -1 || mu.target.nodeValue.indexOf("오픈 스포츠 인사이트") !== -1)) {
          mu.target.nodeValue = rebrandTextString(mu.target.nodeValue);
        }
        if (mu.addedNodes && mu.addedNodes.length) {
          for (var i = 0; i < mu.addedNodes.length; i++) {
            var n = mu.addedNodes[i];
            if (n.nodeType === 1) replaceScobiInNode(n);
            else if (n.nodeType === 3 && n.nodeValue && (n.nodeValue.indexOf("스코비") !== -1 || n.nodeValue.indexOf("오픈 스포츠 인사이트") !== -1)) {
              n.nodeValue = rebrandTextString(n.nodeValue);
            }
          }
        }
      });
    });
    obs.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  setTimeout(function () {
    installScobiRebrandObserver();
    ensureAiIqLauncherButtons();
  }, 150);

  setInterval(function () {
    ensureAiIqLauncherButtons();
  }, 1000);
})();
"""

UPGRADE_JS = UPGRADE_JS + "\n" + AI_IQ_ONLINE_SEARCH_JS
# ============================================================================
# STAGE P5 (v3.5 / ?v=10): "📉 배당급변 알림 ON" <-> "🔕 배당급변 알림 OFF" Toggle
# ============================================================================
ODDS_ALERT_TOGGLE_JS = r"""
/* SportIQ v10.0: Interactive Dropping-Odds Alert ON/OFF Toggle & Real Toast Blocker */
(function () {
  if (window.__SPORTIQ_V10_ODDS_TOGGLE__) return;
  window.__SPORTIQ_V10_ODDS_TOGGLE__ = true;

  var LS_ODDS_ALERT = "sq_odds_drop_alert_enabled";
  var manualFeedbackBypass = false;

  // Read saved state (default ON = true unless user explicitly turned it OFF = "0" / "false")
  function isOddsAlertOn() {
    var v = localStorage.getItem(LS_ODDS_ALERT);
    if (v === "0" || v === "false" || v === "OFF") return false;
    return true;
  }

  function setOddsAlertState(turnOn) {
    localStorage.setItem(LS_ODDS_ALERT, turnOn ? "1" : "0");
    // Sync common global/state flags if sportiq_community.html defines any
    try {
      ["oddsAlertOn", "dcOddsAlertOn", "dropOddsAlertOn", "steamAlertOn"].forEach(function (k) {
        if (k in window) window[k] = Boolean(turnOn);
      });
      if (typeof state === "object" && state) {
        if ("oddsAlert" in state) state.oddsAlert = Boolean(turnOn);
        if ("oddsAlertOn" in state) state.oddsAlertOn = Boolean(turnOn);
      }
    } catch (e) {}
  }

  // Inject clean CSS classes so ON (sky-blue) and OFF (dark muted slate matching '🔕 득점알림 OFF') look crisp
  var st = document.createElement("style");
  st.id = "sqOddsAlertToggleCss";
  st.textContent = [
    "[data-sq-odds-alert-pill='1'] { cursor:pointer !important; user-select:none !important; transition:all .16s ease !important; display:inline-flex !important; align-items:center !important; gap:5px !important; }",
    "[data-sq-odds-alert-pill='1']:hover { filter:brightness(1.08); transform:translateY(-1px); }",
    "[data-sq-odds-alert-pill='1'][data-sq-odds-state='on'] { background:#0284c7 !important; border:1px solid rgba(56,189,248,.6) !important; color:#ffffff !important; font-weight:800 !important; box-shadow:0 4px 14px rgba(2,132,199,.28) !important; }",
    "[data-sq-odds-alert-pill='1'][data-sq-odds-state='off'] { background:#0f172a !important; border:1px solid #334155 !important; color:#94a3b8 !important; font-weight:700 !important; box-shadow:none !important; }"
  ].join("\n");
  document.head.appendChild(st);

  var ODDS_PILL_REGEX = /^(?:📉|🔕|🔔|⚡|📊)?\s*(?:실시간\s*)?배당\s*급변\s*알림(?:\s*(?:ON|OFF))?$/i;
  var ODDS_TOAST_REGEX = /배당\s*급변|배당\s*급락|드롭\s*오즈|드롭오즈|스팀\s*무브|STEAM\s*MOVE|DROP\s*ODDS|배당률?\s*하락|📉\s*\[/i;

  function isOddsDropMessage(txt) {
    if (!txt) return false;
    var s = String(txt);
    if (ODDS_TOAST_REGEX.test(s)) return true;
    if (s.indexOf("배당") !== -1 && /-\d+(?:\.\d+)?%/.test(s)) return true;
    return false;
  }

  function findOddsAlertPillElements() {
    var matched = [];
    var candidates = document.querySelectorAll("button, span, a, div");
    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      if (!el || el.id === "dcToastWrap" || el.closest("#dcToastWrap") || el.closest("#sqAiIqModal")) continue;
      if (el.getAttribute("data-sq-odds-alert-pill") === "1") {
        matched.push(el);
        continue;
      }
      if (el.children && el.children.length > 2) continue;
      var txt = (el.textContent || "").replace(/\s+/g, " ").trim();
      if (!txt || txt.length > 26) continue;
      if (ODDS_PILL_REGEX.test(txt)) {
        // Avoid matching a wrapper if a direct child also matches
        var childMatches = false;
        for (var j = 0; j < el.children.length; j++) {
          var cTxt = (el.children[j].textContent || "").replace(/\s+/g, " ").trim();
          if (ODDS_PILL_REGEX.test(cTxt)) { childMatches = true; break; }
        }
        if (!childMatches) matched.push(el);
      }
    }
    return matched;
  }

  function applyPillVisualState(el, on) {
    if (!el) return;
    var desiredState = on ? "on" : "off";
    var desiredLabel = on ? "📉 배당급변 알림 ON" : "🔕 배당급변 알림 OFF";
    if (el.getAttribute("data-sq-odds-alert-pill") !== "1") {
      el.setAttribute("data-sq-odds-alert-pill", "1");
      if (el.tagName !== "BUTTON" && !el.getAttribute("role")) {
        el.setAttribute("role", "button");
      }
      if (el.tagName !== "BUTTON" && !el.hasAttribute("tabindex")) {
        el.setAttribute("tabindex", "0");
      }
    }
    if (el.getAttribute("data-sq-odds-state") !== desiredState) {
      el.setAttribute("data-sq-odds-state", desiredState);
    }
    el.setAttribute("aria-pressed", on ? "true" : "false");
    el.title = on
      ? "실시간 배당급변 알림이 켜져 있습니다 (클릭하면 OFF 전환)"
      : "실시간 배당급변 알림이 꺼져 있습니다 (클릭하면 ON 전환)";
    var curTxt = (el.textContent || "").replace(/\s+/g, " ").trim();
    if (curTxt !== desiredLabel) {
      el.textContent = desiredLabel;
    }
  }

  function syncAllOddsAlertPills() {
    var on = isOddsAlertOn();
    var pills = findOddsAlertPillElements();
    for (var i = 0; i < pills.length; i++) {
      applyPillVisualState(pills[i], on);
    }
    if (!on) purgeActiveOddsToasts();
  }

  function purgeActiveOddsToasts() {
    var wrap = document.getElementById("dcToastWrap");
    if (!wrap) return;
    Array.prototype.slice.call(wrap.children).forEach(function (card) {
      if (!card || card.getAttribute("data-sq-manual-feedback") === "1") return;
      if (isOddsDropMessage(card.textContent || "")) {
        if (card.parentNode) card.parentNode.removeChild(card);
      }
    });
  }

  function showToggleFeedback(turnOn) {
    var msg = turnOn
      ? "📉 배당급변 실시간 알림이 ON 되었습니다."
      : "🔕 배당급변 실시간 알림이 OFF 되었습니다. (배당 급락 팝업 숨김)";
    manualFeedbackBypass = true;
    try {
      if (typeof window.showToast === "function") {
        window.showToast(msg);
      } else {
        var wrap = document.getElementById("dcToastWrap");
        if (wrap) {
          var t = document.createElement("div");
          t.className = "dc-toast";
          t.setAttribute("data-sq-manual-feedback", "1");
          t.style.cssText = "background:#0f172a;border:1px solid #38bdf8;color:#f8fafc;border-radius:12px;padding:10px 14px;font-size:12.5px;font-weight:800;margin-top:8px;box-shadow:0 10px 25px rgba(0,0,0,.5);";
          t.textContent = msg;
          wrap.appendChild(t);
          setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 2600);
        }
      }
    } finally {
      setTimeout(function () { manualFeedbackBypass = false; }, 120);
    }
  }

  function toggleOddsAlertFromUi(ev) {
    if (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (typeof ev.stopImmediatePropagation === "function") ev.stopImmediatePropagation();
    }
    var nextState = !isOddsAlertOn();
    setOddsAlertState(nextState);
    syncAllOddsAlertPills();
    if (!nextState) purgeActiveOddsToasts();
    showToggleFeedback(nextState);
  }

  // Capture-phase click & keyboard handler so clicking '📉 배당급변 알림 ON' ALWAYS toggles ON <-> OFF cleanly
  window.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!t || !t.closest) return;
    var pill = t.closest("[data-sq-odds-alert-pill='1']");
    if (!pill) {
      var cand = t.closest("button, span, a, div");
      if (cand && (!cand.children || cand.children.length <= 2)) {
        var txt = (cand.textContent || "").replace(/\s+/g, " ").trim();
        if (txt.length <= 26 && ODDS_PILL_REGEX.test(txt)) {
          pill = cand;
        }
      }
    }
    if (!pill) return;
    toggleOddsAlertFromUi(ev);
  }, true);

  window.addEventListener("keydown", function (ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    var active = document.activeElement;
    if (active && active.getAttribute && active.getAttribute("data-sq-odds-alert-pill") === "1") {
      toggleOddsAlertFromUi(ev);
    }
  }, true);

  // Hook global toast functions + MutationObserver on #dcToastWrap so OFF truly silences all odds-drop popups
  function hookToastFunctionsForOddsMute() {
    ["showToast", "showDcToast", "pushDcToast", "triggerOddsToast"].forEach(function (fnName) {
      var fn = window[fnName];
      if (typeof fn === "function" && !fn.__sqOddsMuteWrapped) {
        var wrapped = function () {
          var firstArg = arguments.length > 0 ? String(arguments[0] || "") : "";
          if (!manualFeedbackBypass && !isOddsAlertOn() && isOddsDropMessage(firstArg)) {
            return;
          }
          return fn.apply(this, arguments);
        };
        wrapped.__sqOddsMuteWrapped = true;
        window[fnName] = wrapped;
      }
    });

    var wrap = document.getElementById("dcToastWrap");
    if (wrap && !wrap.__sqOddsMuteObserver) {
      wrap.__sqOddsMuteObserver = true;
      var obs = new MutationObserver(function (mutations) {
        if (manualFeedbackBypass) {
          mutations.forEach(function (m) {
            for (var i = 0; i < m.addedNodes.length; i++) {
              var n = m.addedNodes[i];
              if (n && n.nodeType === 1) n.setAttribute("data-sq-manual-feedback", "1");
            }
          });
          return;
        }
        if (!isOddsAlertOn()) {
          purgeActiveOddsToasts();
        }
      });
      obs.observe(wrap, { childList: true, subtree: true });
    }
  }

  setOddsAlertState(isOddsAlertOn());
  setTimeout(function () {
    syncAllOddsAlertPills();
    hookToastFunctionsForOddsMute();
  }, 120);

  setInterval(function () {
    syncAllOddsAlertPills();
    hookToastFunctionsForOddsMute();
  }, 800);
})();
"""

UPGRADE_JS = UPGRADE_JS + "\n" + ODDS_ALERT_TOGGLE_JS
# ============================================================================
# STAGE P6 (v3.6 / ?v=11): "📺 IQ중계티비" Floating Tab (Above AI 아이큐) -> 짱돌티비 바로가기
# ============================================================================
IQ_LIVE_TV_JS = r"""
/* SportIQ v11.0: 'IQ중계티비' Floating Tab Above 'AI 아이큐' + 짱돌티비 바로가기 Modal */
(function () {
  if (window.__SPORTIQ_V11_IQ_TV__) return;
  window.__SPORTIQ_V11_IQ_TV__ = true;

  var JJANGDOL_URL = "https://jjangdoltv.com/";

  var st = document.createElement("style");
  st.id = "sqIqLiveTvCss";
  st.textContent = [
    ".sq-iq-tv-fab {",
    "  position: fixed;",
    "  right: 20px;",
    "  bottom: 76px;",
    "  z-index: 9996;",
    "  background: linear-gradient(135deg, #06b6d4, #10b981);",
    "  color: #020617;",
    "  border: 0;",
    "  border-radius: 999px;",
    "  padding: 11px 20px;",
    "  font-size: 14px;",
    "  font-weight: 900;",
    "  cursor: pointer;",
    "  display: inline-flex;",
    "  align-items: center;",
    "  justify-content: center;",
    "  gap: 7px;",
    "  box-shadow: 0 10px 28px rgba(6, 182, 212, .42);",
    "  transition: transform .16s ease, filter .16s ease;",
    "  letter-spacing: -0.2px;",
    "  white-space: nowrap;",
    "}",
    ".sq-iq-tv-fab:hover { transform: translateY(-2px) scale(1.02); filter: brightness(1.06); }",
    ".sq-jjangdol-cta {",
    "  display: flex;",
    "  align-items: center;",
    "  justify-content: center;",
    "  gap: 8px;",
    "  width: 100%;",
    "  background: linear-gradient(135deg, #06b6d4, #10b981);",
    "  color: #020617 !important;",
    "  text-decoration: none !important;",
    "  border-radius: 13px;",
    "  padding: 14px 18px;",
    "  font-size: 15.5px;",
    "  font-weight: 900;",
    "  box-shadow: 0 10px 26px rgba(16, 185, 129, .32);",
    "  transition: transform .15s ease, filter .15s ease;",
    "}",
    ".sq-jjangdol-cta:hover { transform: translateY(-1px); filter: brightness(1.06); }"
  ].join("\n");
  document.head.appendChild(st);

  function findAiIqFloatingButton() {
    var byId = document.getElementById("sqAiIqFloatingFab");
    if (byId) return byId;
    var cands = document.querySelectorAll("button, a, div[role='button']");
    for (var i = 0; i < cands.length; i++) {
      var el = cands[i];
      if (!el || el.id === "sqIqTvFloatingFab" || el.id === "sqFeedAiIqBtn" || el.id === "sqFeedIqTvBtn") continue;
      if (el.closest("#sqAiIqModal") || el.closest("#sqIqTvModal")) continue;
      var txt = (el.textContent || "").replace(/\s+/g, " ").trim();
      if (txt.length <= 28 && (txt.indexOf("AI 아이큐") !== -1 || txt.indexOf("AI아이큐") !== -1)) {
        var cs = window.getComputedStyle ? window.getComputedStyle(el) : null;
        if (cs && cs.position === "fixed") return el;
      }
    }
    return null;
  }

  function alignIqTvAboveAiIq(tvFab) {
    if (!tvFab) return;
    var aiBtn = findAiIqFloatingButton();
    if (!aiBtn || !aiBtn.getBoundingClientRect) return;
    var rect = aiBtn.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 24) return;
    var viewportW = window.innerWidth || document.documentElement.clientWidth || 1280;
    var viewportH = window.innerHeight || document.documentElement.clientHeight || 800;
    var computedRight = Math.max(12, Math.round(viewportW - rect.right));
    var computedBottom = Math.max(64, Math.round(viewportH - rect.top + 10));
    tvFab.style.right = computedRight + "px";
    tvFab.style.bottom = computedBottom + "px";
    tvFab.style.minWidth = Math.round(rect.width) + "px";
    var aiZ = parseInt((window.getComputedStyle(aiBtn).zIndex || "9995"), 10);
    if (!isNaN(aiZ) && aiZ > 0) {
      tvFab.style.zIndex = String(aiZ);
    }
  }

  function buildIqTvModalIfNeeded() {
    var m = document.getElementById("sqIqTvModal");
    if (m) return m;

    m = document.createElement("div");
    m.id = "sqIqTvModal";
    m.className = "sq-modal-backdrop";
    m.style.zIndex = "100060";
    m.innerHTML = [
      "<div class='sq-modal-panel' style='max-width:440px;padding:22px;border-color:rgba(6,182,212,.55);'>",
      "  <div style='display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px;'>",
      "    <div style='display:flex;align-items:center;gap:8px;'>",
      "      <span style='background:linear-gradient(135deg,#06b6d4,#10b981);color:#020617;border-radius:999px;padding:4px 11px;font-size:12.5px;font-weight:900;'>📺 IQ중계티비</span>",
      "      <h3 style='margin:0;font-size:16.5px;font-weight:900;color:#f8fafc;'>실시간 스포츠 중계 안내</h3>",
      "    </div>",
      "    <button type='button' id='sqCloseIqTvModalBtn' style='background:#1e293b;border:1px solid #334155;color:#cbd5e1;border-radius:8px;padding:4px 10px;cursor:pointer;font-weight:800;'>✕ 닫기 (Esc)</button>",
      "  </div>",
      "  <div style='background:linear-gradient(145deg,rgba(6,182,212,.12),rgba(15,23,42,.98));border:1px solid rgba(56,189,248,.35);border-radius:15px;padding:18px;text-align:center;'>",
      "    <div style='font-size:15px;font-weight:900;color:#f8fafc;margin-bottom:6px;'>⚡ 전 종목 고화질 실시간 라이브 중계</div>",
      "    <div style='font-size:12.5px;color:#94a3b8;line-height:1.6;margin-bottom:16px;'>해외축구 · 야구 · 농구 · 배구 · 하키 · E스포츠 실시간 생중계를<br/><strong style='color:#67e8f9;'>짱돌티비</strong>에서 끊김 없이 바로 시청하실 수 있습니다.</div>",
      "    <a id='sqGoJjangdolTvLink' href='" + JJANGDOL_URL + "' target='_blank' rel='noopener noreferrer' class='sq-jjangdol-cta'>",
      "      <span>🎬 짱돌티비 바로가기</span>",
      "      <span style='font-size:14px;'>↗</span>",
      "    </a>",
      "    <div style='margin-top:10px;font-size:11px;color:#64748b;'>클릭 시 공식 중계 페이지(jjangdoltv.com)로 즉시 이동합니다</div>",
      "  </div>",
      "</div>"
    ].join("");
    document.body.appendChild(m);

    document.getElementById("sqCloseIqTvModalBtn").onclick = function () {
      m.style.display = "none";
    };
    m.addEventListener("click", function (ev) {
      if (ev.target === m) m.style.display = "none";
    });
    document.getElementById("sqGoJjangdolTvLink").addEventListener("click", function () {
      setTimeout(function () { m.style.display = "none"; }, 180);
    });

    return m;
  }

  function openIqTvModal() {
    var m = buildIqTvModalIfNeeded();
    m.style.display = "flex";
  }
  window.__openSportiqIqTv = openIqTvModal;

  function ensureIqTvFloatingTab() {
    var tvFab = document.getElementById("sqIqTvFloatingFab");
    if (!tvFab && document.body) {
      tvFab = document.createElement("button");
      tvFab.id = "sqIqTvFloatingFab";
      tvFab.type = "button";
      tvFab.className = "sq-iq-tv-fab";
      tvFab.innerHTML = "📺 <span>IQ중계티비</span>";
      tvFab.title = "IQ중계티비 — 짱돌티비 실시간 스포츠 중계 바로가기";
      tvFab.onclick = function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        openIqTvModal();
      };
      document.body.appendChild(tvFab);
    }
    alignIqTvAboveAiIq(tvFab);
  }

  window.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" || ev.keyCode === 27) {
      var m = document.getElementById("sqIqTvModal");
      if (m && m.style.display === "flex") {
        ev.preventDefault();
        ev.stopPropagation();
        m.style.display = "none";
      }
    }
  }, true);

  window.addEventListener("resize", function () {
    alignIqTvAboveAiIq(document.getElementById("sqIqTvFloatingFab"));
  });

  setTimeout(ensureIqTvFloatingTab, 160);
  setInterval(ensureIqTvFloatingTab, 900);
})();
"""

UPGRADE_JS = UPGRADE_JS + "\n" + IQ_LIVE_TV_JS
