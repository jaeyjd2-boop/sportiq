import hashlib
import json
import os
import re
import sqlite3
import struct
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
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
        "version": "1.1",
        "pg_sync": _PG_SYNC_STATE,
        "shop_items": len(SHOP_CATALOG),
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
        comments_by_post: Dict[str, List[Dict[str, str]]] = {}
        for c in c_rows:
            label = f"{c['user_name']} {c['user_badge']}".strip()
            comments_by_post.setdefault(c["post_id"], []).append({"user": label, "text": c["comment_text"]})

        now = time.time()
        posts = []
        for r in p_rows:
            pid = r["id"]
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
                    "hidden": False,
                    "time": mins,
                    "title": r["title"],
                    "body": r["body"],
                    "tags": json.loads(r["tags_json"] or "[]"),
                    "comments": comments_by_post.get(pid, []),
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
            conn.execute(
                "INSERT INTO community_post_comments(post_id, user_name, user_badge, comment_text, created_at) VALUES (?, ?, ?, ?, ?)",
                (post_id[:64], user, badge, txt, time.time()),
            )
        label = f"{user} {badge}".strip()
        return {"user": label, "text": txt}

    c_obj = run_with_db_retry(_op)
    threading.Thread(target=_trigger_pg_backup, args=("community_post_comments",), daemon=True).start()
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


# --- Frontend Upgrade Bundle (/api/member/p0p1-upgrade.js) ---
UPGRADE_JS = r"""
/* SportIQ Member-First P0+P1 Upgrade Bundle v1.1 */
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
      if (el.id === "sqMemberHubModal" || el.id === "dcMatchModal" || el.id === "dcMatchDetailModal") {
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
      var ids = ["sqMemberHubModal", "commentModal", "writeModal", "dcMatchModal", "dcMatchDetailModal"];
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
      if (t.id === "sqMemberHubModal" || t.id === "commentModal" || t.id === "writeModal" || t.id === "dcMatchModal") {
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
        state.checkedToday = prof.last_checkin_date === new Date().toISOString().slice(0, 10);
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
          "<div style='background:linear-gradient(135deg,rgba(217,119,6,.16),rgba(15,23,42,.95));border:1px solid rgba(245,158,11,.45);border-radius:12px;padding:12px;margin-top:12px;display:flex;justify-content:space-between;align-items:center;gap:10px;'>",
          "  <div>",
          "    <div style='font-weight:800;color:#fde68a;font-size:13px;'>🎁 " + escHtml(rf.prize_title || "주간 럭키드로우") + " (" + escHtml(rf.round_key || "") + ")</div>",
          "    <div style='font-size:11.5px;color:#cbd5e1;margin-top:3px;'>전체 응모: <strong>" + escHtml(rf.total_tickets || 0) + "장</strong> · 내 응모권: <strong style='color:#4ade80;'>" + escHtml(rf.my_tickets || 0) + "장</strong></div>",
          "  </div>",
          "  <button type='button' id='sqEnterRaffleBtn' class='sq-up-btn' style='background:linear-gradient(135deg,#d97706,#b45309);'>50P 응모하기</button>",
          "</div>"
        ].join("");

        box.innerHTML = "<div style='font-weight:800;color:#f8fafc;margin-bottom:8px;'>🏅 전용 칭호·배지 상점 (구매 즉시 게시글·응원톡 닉네임 옆 표시)</div>" + cards + raffleHtml;

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

  function bootUpgrade() {
    fixInitialAttendanceStreak();
    setupToastLimiter();
    applyAboveTheFoldDiet();
    setupUniversalModalClose();
    hookRoleAndAttendance();
    setupWriteModalAndServerPosts();
    mountMemberHubButtonsAndModal();
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


def inject_upgrade_into_html(raw_html: str) -> str:
    tag = '<script src="/api/member/p0p1-upgrade.js?v=2" defer></script>'
    if "/api/member/p0p1-upgrade.js" in raw_html:
        return re.sub(r'/api/member/p0p1-upgrade\.js\?v=\d+', '/api/member/p0p1-upgrade.js?v=2', raw_html)
    idx = raw_html.rfind("</body>")
    if idx != -1:
        return raw_html[:idx] + "\n" + tag + "\n" + raw_html[idx:]
    return raw_html + "\n" + tag + "\n"
