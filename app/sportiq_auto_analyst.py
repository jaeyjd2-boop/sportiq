import os
import sqlite3
import threading
import time
import json
import re
try:
    import psycopg2
except Exception:
    psycopg2 = None
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
router = APIRouter(tags=["auto-analyst"])

KST = timezone(timedelta(hours=9))
DB_PATH = os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db")
_ANALYST_LOCK = threading.Lock()
_SCHEDULER_STARTED = False

def _now_kst() -> datetime:
    return datetime.now(KST)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

_PG_SNAPSHOT_TABLES = (
    "shorts_clip_stats",
    "shorts_comments",
    "shorts_custom_clips",
    "shorts_user_actions",
    "global_match_rankings",
    "match_predictions",
)
_PG_RESTORED_TABLES = set()
_PG_PERSISTENT_OK = False


def _get_pg_dsn() -> str:
    raw = (os.environ.get("DATABASE_URL") or "").strip()
    if not raw:
        return ""
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    elif raw.startswith("postgresql+psycopg2://"):
        raw = "postgresql://" + raw[len("postgresql+psycopg2://"):]
    return raw


def _pg_connect():
    dsn = _get_pg_dsn()
    if not dsn or psycopg2 is None:
        return None
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_pg_snapshot_table(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sportiq_lounge_snapshots (
                table_name TEXT PRIMARY KEY,
                rows_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    pg_conn.commit()


def _backup_tables_to_pg(*table_names: str) -> None:
    global _PG_PERSISTENT_OK
    targets = [t for t in table_names if t in _PG_SNAPSHOT_TABLES]
    if not targets:
        targets = list(_PG_SNAPSHOT_TABLES)
    try:
        pg_conn = _pg_connect()
        if pg_conn is None:
            return
        try:
            _ensure_pg_snapshot_table(pg_conn)
            now_iso = _now_kst().isoformat()
            with _get_conn() as sconn:
                for tname in targets:
                    rows = sconn.execute(f"SELECT * FROM {tname}").fetchall()
                    payload = json.dumps([dict(r) for r in rows], ensure_ascii=False)
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO sportiq_lounge_snapshots (table_name, rows_json, updated_at)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (table_name)
                            DO UPDATE SET rows_json = EXCLUDED.rows_json, updated_at = EXCLUDED.updated_at
                            """,
                            (tname, payload, now_iso),
                        )
            pg_conn.commit()
            _PG_PERSISTENT_OK = True
        finally:
            pg_conn.close()
    except Exception:
        pass


def _restore_snapshots_from_pg() -> None:
    global _PG_PERSISTENT_OK
    try:
        pg_conn = _pg_connect()
        if pg_conn is None:
            return
        try:
            _ensure_pg_snapshot_table(pg_conn)
            _PG_PERSISTENT_OK = True
            with _get_conn() as sconn:
                existing_tables = {
                    r[0]
                    for r in sconn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                pending = [
                    t for t in _PG_SNAPSHOT_TABLES
                    if t in existing_tables and t not in _PG_RESTORED_TABLES
                ]
                if not pending:
                    return
                with pg_conn.cursor() as cur:
                    cur.execute("SELECT table_name, rows_json FROM sportiq_lounge_snapshots")
                    snap_rows = cur.fetchall()
                snap_map = {tname: rows_json for tname, rows_json in snap_rows}
                for tname in pending:
                    _PG_RESTORED_TABLES.add(tname)
                    rows_json = snap_map.get(tname)
                    if not rows_json:
                        continue
                    try:
                        items = json.loads(rows_json)
                    except Exception:
                        items = []
                    if not isinstance(items, list) or not items:
                        continue
                    cols_info = sconn.execute(f"PRAGMA table_info({tname})").fetchall()
                    valid_cols = [c[1] for c in cols_info]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        cols = [c for c in valid_cols if c in item]
                        if not cols:
                            continue
                        placeholders = ", ".join("?" for _ in cols)
                        col_sql = ", ".join(cols)
                        vals = [item[c] for c in cols]
                        sconn.execute(
                            f"INSERT OR REPLACE INTO {tname} ({col_sql}) VALUES ({placeholders})",
                            vals,
                        )
                sconn.commit()
        finally:
            pg_conn.close()
    except Exception:
        pass


def init_auto_analyst_db() -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_analyst_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date_kst TEXT UNIQUE NOT NULL,
                last_run_kst TEXT NOT NULL,
                next_run_kst TEXT NOT NULL,
                analyzed_top_count INTEGER NOT NULL DEFAULT 20,
                trigger_source TEXT NOT NULL DEFAULT 'scheduler',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_analyst_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date_kst TEXT NOT NULL,
                match_key TEXT NOT NULL,
                rank_num INTEGER NOT NULL,
                is_summary INTEGER NOT NULL DEFAULT 0,
                sport TEXT NOT NULL,
                league TEXT NOT NULL,
                badge_text TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tactical_points TEXT NOT NULL,
                recommended_pick TEXT NOT NULL,
                confidence_score INTEGER NOT NULL,
                sentiment_home_pct INTEGER NOT NULL,
                sentiment_away_pct INTEGER NOT NULL,
                sentiment_home_label TEXT NOT NULL,
                sentiment_away_label TEXT NOT NULL,
                hashtags TEXT NOT NULL,
                likes INTEGER NOT NULL DEFAULT 0,
                created_at_kst TEXT NOT NULL
            )
            """
        )
        conn.commit()

def _fetch_top_20_matches(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT id, match_key, sport, league, league_color, matchup,
                   schedule_venue, votes, buzz_score, rank_change
            FROM global_match_rankings
            ORDER BY votes DESC, buzz_score DESC, id ASC
            LIMIT 20
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _split_teams(matchup: str) -> List[str]:
    for sep in [" vs ", " VS ", " v ", " - "]:
        if sep in matchup:
            parts = matchup.split(sep, 1)
            return [parts[0].strip(), parts[1].strip()]
    return [matchup.strip(), "원정팀"]


def _build_match_analysis(row: Dict[str, Any], rank_num: int, today_kst: str) -> Dict[str, Any]:
    match_key = row.get("match_key", f"match-{rank_num}")
    sport = row.get("sport", "축구")
    league = row.get("league", "GLOBAL")
    matchup = row.get("matchup", f"글로벌 빅매치 #{rank_num}")
    schedule_venue = row.get("schedule_venue", "오늘 주요 경기")
    votes = int(row.get("votes", 0) or 0)
    buzz_score = int(row.get("buzz_score", 85) or 85)
    rank_change = row.get("rank_change", "-")
    teams = _split_teams(matchup)
    home_team, away_team = teams[0], teams[1]

    if "리버풀" in matchup and ("맨체스터" in matchup or "맨유" in matchup):
        title = f"[00:00 커뮤니티 자동분석] {matchup} — 포체티노 3-4-2-1 쓰리백 대 안필드 하이프레싱 여론"
        content = (
            f"글로벌 화제성 #{rank_num}({votes:,}표 · 버즈 {buzz_score}점)에 오른 EPL 최대 라이벌전입니다. "
            f"국내외 커뮤니티에서는 맨유의 포체티노 3-4-2-1 쓰리백 전환이 안필드 원정의 강한 전방 하이프레싱을 "
            f"얼마나 버텨낼지가 최대 쟁점으로 꼽히고 있습니다. 리버풀의 측면 압박과 하프스페이스 침투 빈도가 높아 "
            f"후반 체력 저하 구간에서 홈팀 우세 여론이 우위입니다."
        )
        tactical = "안필드 하이프레싱 vs 3-4-2-1 윙백 배후 공간 공략 · 세트피스 세컨볼 집중도"
        pick = f"{home_team} 승 & 2.5 오버"
        confidence = 86
        home_pct, away_pct = 64, 36
        hashtags = "#EPL #리버풀 #맨유 #안필드 #포체티노 #커뮤니티자동분석"
    elif "대한민국" in matchup and "일본" in matchup:
        title = f"[00:00 커뮤니티 자동분석] {matchup} — 도쿄돔 한일전 초반 투수전 및 타선 화력 커뮤니티 분석"
        content = (
            f"글로벌 화제성 #{rank_num}({votes:,}표 · 버즈 {buzz_score}점) APBC 야구 결승 한일전입니다. "
            f"야구 커뮤니티 여론은 도쿄돔 특유의 돔구장 장타 변수보다 1~4회 선발 에이스들의 초반 투수전 양상에 "
            f"무게를 두고 있으며, 6회 이후 불펜 대결에서 좌타 라인과 대타 자원의 타선 화력이 승부를 가를 것으로 분석됩니다."
        )
        tactical = "도쿄돔 초반 투수전 · 6회 이후 필승조 매치업 및 클러치 타선 화력"
        pick = "5이닝 언더 & 대한민국 +1.5 핸디캡"
        confidence = 82
        home_pct, away_pct = 53, 47
        hashtags = "#APBC #한일전 #도쿄돔 #야구분석 #대한민국야구 #커뮤니티자동분석"
    elif "스페인" in matchup and "잉글랜드" in matchup:
        title = f"[00:00 커뮤니티 자동분석] {matchup} — 마드리드 조 1위 결정전 라민 야말 측면 매치업 분석"
        content = (
            f"글로벌 화제성 #{rank_num}({votes:,}표 · 버즈 {buzz_score}점) 네이션스리그 빅매치입니다. "
            f"마드리드 홈에서 열리는 조 1위 결정전으로, 해외 축구 커뮤니티에서는 스페인 우측면 라민 야말의 "
            f"1대1 돌파와 하프스페이스 컷백이 잉글랜드 좌측 수비 라인을 흔들 핵심 카드로 평가받고 있습니다."
        )
        tactical = "라민 야말 측면 아이솔레이션 · 중원 점유율 싸움과 전환 속도"
        pick = f"{home_team} 승 (무승부 보험)"
        confidence = 80
        home_pct, away_pct = 59, 41
        hashtags = "#네이션스리그 #스페인 #잉글랜드 #라민야말 #마드리드 #커뮤니티자동분석"
    elif "아스널" in matchup and "도르트문트" in matchup:
        title = f"[00:00 커뮤니티 자동분석] {matchup} — 아스널 연승 흐름 대 도르트문트 투톱 역습 분석"
        content = (
            f"글로벌 화제성 #{rank_num}({votes:,}표 · 버즈 {buzz_score}점) UEFA 챔피언스리그 맞대결입니다. "
            f"아스널의 최근 공식전 연승 흐름과 견고한 박스 수비가 높은 지지를 받는 가운데, "
            f"도르트문트의 빠른 투톱 역습 전환이 초반 선제골 변수로 거론되며 다득점 공방전 예상이 많습니다."
        )
        tactical = "아스널 하프스페이스 점유 · 도르트문트 투톱 뒷공간 역습 전개"
        pick = f"{home_team} 승 & 양팀 득점(BTTS)"
        confidence = 81
        home_pct, away_pct = 62, 38
        hashtags = "#UCL #챔피언스리그 #아스널 #도르트문트 #커뮤니티자동분석"
    elif "보스턴" in matchup and "댈러스" in matchup:
        title = f"[00:00 커뮤니티 자동분석] {matchup} — TD 가든 외곽 3점 효율 및 백코트 매치업 분석"
        content = (
            f"글로벌 화제성 #{rank_num}({votes:,}표 · 버즈 {buzz_score}점) NBA 빅매치입니다. "
            f"농구 커뮤니티에서는 TD 가든 홈 어드밴티지를 안은 보스턴의 스페이싱 기반 외곽 3점슛 효율과 "
            f"댈러스 백코트 듀오의 픽앤롤 클러치 득점력 대결에 높은 관심을 보이고 있습니다."
        )
        tactical = "코너 3점슛 성공률 · 백코트 스위치 수비 및 클러치 턴오버 관리"
        pick = f"{home_team} 마핸 & 기준점 오버"
        confidence = 79
        home_pct, away_pct = 58, 42
        hashtags = "#NBA #보스턴셀틱스 #댈러스매버릭스 #TD가든 #농구분석 #커뮤니티자동분석"
    elif "플로리다" in matchup and "에드먼턴" in matchup:
        title = f"[00:00 커뮤니티 자동분석] {matchup} — 스탠리컵 리매치 포체킹 대 파워플레이 커뮤니티 여론"
        content = (
            f"글로벌 화제성 #{rank_num}({votes:,}표 · 버즈 {buzz_score}점) NHL 스탠리컵 리매치입니다. "
            f"하키 커뮤니티 여론은 플로리다의 집요한 전방 포체킹(Forechecking) 압박이 에드먼턴의 "
            f"리그 최강급 파워플레이(Power Play) 기회를 얼마나 억제할 수 있는지에 집중되고 있습니다."
        )
        tactical = "플로리다 보드 배틀 포체킹 vs 에드먼턴 파워플레이 1유닛 효율"
        pick = "5.5 오버 & 플로리다 ML"
        confidence = 77
        home_pct, away_pct = 54, 46
        hashtags = "#NHL #플로리다팬서스 #에드먼턴오일러스 #스탠리컵 #하키분석 #커뮤니티자동분석"
    else:
        home_pct = min(72, max(48, 50 + (buzz_score % 18)))
        away_pct = 100 - home_pct
        confidence = min(88, max(72, 70 + (buzz_score % 16)))
        title = f"[00:00 커뮤니티 자동분석] {matchup} — {league} 글로벌 #{rank_num} 여론 & 핵심 매치업 브리핑"
        content = (
            f"글로벌 화제성 #{rank_num}({votes:,}표 · 버즈 {buzz_score}점 · 변동 {rank_change})를 기록 중인 "
            f"{league} 주요 경기입니다. 일정/장소는 '{schedule_venue}'이며, 온라인 커뮤니티에서는 "
            f"{home_team}의 홈 주도권과 {away_team}의 역습 효율을 두고 {home_pct}% 대 {away_pct}%의 여론 구도가 형성되었습니다."
        )
        tactical = f"{league} 최근 5경기 페이스를 반영한 공수 밸런스 및 핵심 로테이션 매치업"
        pick = f"{home_team} 우세 (실시간 배당 흐름 체크)"
        clean_league = league.replace(" ", "")
        clean_sport = sport.replace(" ", "")
        hashtags = f"#{clean_league} #{clean_sport} #{home_team.replace(' ', '')} #{away_team.replace(' ', '')} #커뮤니티자동분석"

    return {
        "run_date_kst": today_kst,
        "match_key": match_key,
        "rank_num": rank_num,
        "is_summary": 0,
        "sport": sport,
        "league": league,
        "badge_text": f"🤖 00:00 커뮤니티 자동분석 · 글로벌 화제성 #{rank_num}",
        "title": title,
        "content": content,
        "tactical_points": tactical,
        "recommended_pick": pick,
        "confidence_score": confidence,
        "sentiment_home_pct": home_pct,
        "sentiment_away_pct": away_pct,
        "sentiment_home_label": home_team,
        "sentiment_away_label": away_team,
        "hashtags": hashtags,
        "created_at_kst": f"{today_kst} 00:00 KST",
    }


def _build_summary_post(top_matches: List[Dict[str, Any]], today_kst: str) -> Dict[str, Any]:
    total_count = len(top_matches)
    total_votes = sum(int(m.get("votes", 0) or 0) for m in top_matches)
    avg_buzz = int(sum(int(m.get("buzz_score", 85) or 85) for m in top_matches) / total_count) if total_count else 88

    top3_lines = []
    for idx, m in enumerate(top_matches[:3], start=1):
        top3_lines.append(f"{idx}위 {m.get('matchup', '')}({m.get('league', '')}, {int(m.get('votes', 0) or 0):,}표)")
    top3_text = " · ".join(top3_lines) if top3_lines else "글로벌 빅매치 집계 중"

    sport_counts: Dict[str, int] = {}
    for m in top_matches:
        sp = m.get("sport", "기타")
        sport_counts[sp] = sport_counts.get(sp, 0) + 1
    sport_summary = ", ".join(f"{k} {v}경기" for k, v in sport_counts.items()) if sport_counts else "전체 종목"

    content = (
        f"{today_kst} 00:00 KST 기준 글로벌 화제성 TOP {total_count} 누적 투표수는 {total_votes:,}표(평균 버즈 {avg_buzz}점)로 집계되었습니다. "
        f"종목별 분포는 [{sport_summary}]이며, 커뮤니티 최상위 관심 매치는 {top3_text} 순입니다. "
        f"각 빅매치의 전술 포인트와 실시간 여론 비율은 아래 개별 [00:00 커뮤니티 자동분석] 피드에서 확인하실 수 있습니다."
    )

    return {
        "run_date_kst": today_kst,
        "match_key": "global-top20-summary",
        "rank_num": 0,
        "is_summary": 1,
        "sport": "전체",
        "league": "TOP 20 종합",
        "badge_text": "🤖 00:00 KST 글로벌 TOP 20 종합 리포트",
        "title": "[00:00 글로벌 TOP 20 종합 리포트] 온라인 커뮤니티 빅매치 여론 & 핵심 픽 브리핑",
        "content": content,
        "tactical_points": f"TOP {total_count} 커뮤니티 여론 집중 매치 · 멀티 종목 교차 밸런스 브리핑",
        "recommended_pick": "TOP 3 화제성 매치 중심 분할 접근 권장",
        "confidence_score": 85,
        "sentiment_home_pct": 61,
        "sentiment_away_pct": 39,
        "sentiment_home_label": "정배/홈 우세 여론",
        "sentiment_away_label": "역배/원정 변수 여론",
        "hashtags": "#글로벌TOP20 #종합리포트 #커뮤니티여론 #00시자동분석 #SportIQ",
        "created_at_kst": f"{today_kst} 00:00 KST",
    }


def run_auto_analysis(force: bool = False, trigger_source: str = "scheduler") -> Dict[str, Any]:
    with _ANALYST_LOCK:
        init_auto_analyst_db()
        now_kst = _now_kst()
        today_kst = now_kst.strftime("%Y-%m-%d")
        tomorrow_kst = (now_kst + timedelta(days=1)).strftime("%Y-%m-%d")
        last_run_kst = f"{today_kst} 00:00 KST"
        next_run_kst = f"{tomorrow_kst} 00:00 KST"

        with _get_conn() as conn:
            existing_run = conn.execute(
                "SELECT * FROM auto_analyst_runs WHERE run_date_kst = ?",
                (today_kst,),
            ).fetchone()

            if existing_run and not force:
                return dict(existing_run)

            top_matches = _fetch_top_20_matches(conn)
            like_rows = conn.execute(
                "SELECT match_key, likes FROM auto_analyst_posts WHERE run_date_kst = ?",
                (today_kst,),
            ).fetchall()
            preserved_likes = {r["match_key"]: int(r["likes"] or 0) for r in like_rows}

            conn.execute("DELETE FROM auto_analyst_posts WHERE run_date_kst = ?", (today_kst,))

            posts_to_insert: List[Dict[str, Any]] = [_build_summary_post(top_matches, today_kst)]
            for idx, row in enumerate(top_matches[:12], start=1):
                posts_to_insert.append(_build_match_analysis(row, idx, today_kst))

            for post in posts_to_insert:
                likes = preserved_likes.get(post["match_key"], 0)
                conn.execute(
                    """
                    INSERT INTO auto_analyst_posts (
                        run_date_kst, match_key, rank_num, is_summary, sport, league,
                        badge_text, title, content, tactical_points, recommended_pick,
                        confidence_score, sentiment_home_pct, sentiment_away_pct,
                        sentiment_home_label, sentiment_away_label, hashtags, likes, created_at_kst
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        post["run_date_kst"],
                        post["match_key"],
                        post["rank_num"],
                        post["is_summary"],
                        post["sport"],
                        post["league"],
                        post["badge_text"],
                        post["title"],
                        post["content"],
                        post["tactical_points"],
                        post["recommended_pick"],
                        post["confidence_score"],
                        post["sentiment_home_pct"],
                        post["sentiment_away_pct"],
                        post["sentiment_home_label"],
                        post["sentiment_away_label"],
                        post["hashtags"],
                        likes,
                        post["created_at_kst"],
                    ),
                )

            conn.execute(
                """
                INSERT INTO auto_analyst_runs (
                    run_date_kst, last_run_kst, next_run_kst, analyzed_top_count, trigger_source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_date_kst) DO UPDATE SET
                    last_run_kst = excluded.last_run_kst,
                    next_run_kst = excluded.next_run_kst,
                    analyzed_top_count = excluded.analyzed_top_count,
                    trigger_source = excluded.trigger_source,
                    created_at = excluded.created_at
                """,
                (
                    today_kst,
                    last_run_kst,
                    next_run_kst,
                    len(top_matches) if top_matches else 20,
                    trigger_source,
                    now_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
                ),
            )
            conn.commit()

            saved_run = conn.execute(
                "SELECT * FROM auto_analyst_runs WHERE run_date_kst = ?",
                (today_kst,),
            ).fetchone()
            return dict(saved_run) if saved_run else {
                "run_date_kst": today_kst,
                "last_run_kst": last_run_kst,
                "next_run_kst": next_run_kst,
                "analyzed_top_count": 20,
            }


def _scheduler_loop() -> None:
    while True:
        time.sleep(60)
        try:
            run_auto_analysis(force=False, trigger_source="scheduler")
        except Exception:
            pass


def start_auto_analyst_scheduler() -> None:
    global _SCHEDULER_STARTED
    init_auto_analyst_db()
    try:
        run_auto_analysis(force=False, trigger_source="startup")
    except Exception:
        pass
    if not _SCHEDULER_STARTED:
        _SCHEDULER_STARTED = True
        t = threading.Thread(target=_scheduler_loop, daemon=True, name="sportiq-auto-analyst-kst")
        t.start()


def _build_posts_payload() -> Dict[str, Any]:
    now_kst = _now_kst()
    today_kst = now_kst.strftime("%Y-%m-%d")
    tomorrow_kst = (now_kst + timedelta(days=1)).strftime("%Y-%m-%d")

    with _get_conn() as conn:
        run_row = conn.execute(
            "SELECT * FROM auto_analyst_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        post_rows = conn.execute(
            """
            SELECT * FROM auto_analyst_posts
            WHERE run_date_kst = ?
            ORDER BY is_summary DESC, rank_num ASC, id ASC
            """,
            (run_row["run_date_kst"] if run_row else today_kst,),
        ).fetchall()

    return {
        "status": "running",
        "scheduler_active": True,
        "last_run_kst": run_row["last_run_kst"] if run_row else f"{today_kst} 00:00 KST",
        "next_run_kst": run_row["next_run_kst"] if run_row else f"{tomorrow_kst} 00:00 KST",
        "last_run_label": "오늘 00:00 KST",
        "next_run_label": "내일 00:00 KST",
        "analyzed_top_count": 20,
        "total_posts": len(post_rows),
        "posts": [dict(r) for r in post_rows],
    }


@router.get("/api/auto-analyst/posts")
def get_auto_analyst_posts() -> Dict[str, Any]:
    start_auto_analyst_scheduler()
    return _build_posts_payload()


@router.post("/api/auto-analyst/run-now")
def trigger_auto_analyst_now() -> Dict[str, Any]:
    run_auto_analysis(force=True, trigger_source="manual")
    payload = _build_posts_payload()
    payload["refreshed_at_kst"] = _now_kst().strftime("%Y-%m-%d %H:%M:%S KST")
    return payload


@router.post("/api/auto-analyst/posts/{post_id}/like")
def like_auto_analyst_post(post_id: int) -> Dict[str, Any]:
    init_auto_analyst_db()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, likes FROM auto_analyst_posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Auto-analyst post not found")
        new_likes = int(row["likes"] or 0) + 1
        conn.execute(
            "UPDATE auto_analyst_posts SET likes = ? WHERE id = ?",
            (new_likes, post_id),
        )
        conn.commit()
    return {"id": post_id, "likes": new_likes}

class PredictionVoteRequest(BaseModel):
    choice: str

def init_match_predictions_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS match_predictions (
            match_key TEXT PRIMARY KEY,
            home_votes INTEGER NOT NULL DEFAULT 0,
            draw_votes INTEGER NOT NULL DEFAULT 0,
            away_votes INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );
        """)
        conn.commit()
    _restore_snapshots_from_pg()


def _ensure_prediction_row(conn: sqlite3.Connection, match_row: Dict[str, Any]) -> Dict[str, int]:
    match_key = str(match_row["match_key"])
    row = conn.execute(
        "SELECT home_votes, draw_votes, away_votes FROM match_predictions WHERE match_key = ?",
        (match_key,),
    ).fetchone()
    if not row:
        base = max(40, int(match_row.get("votes") or 100) // 10)
        sport = str(match_row.get("sport") or "")
        if sport in ("야구", "농구", "하키"):
            h = max(12, int(base * 0.46))
            d = max(4, int(base * 0.12))
            a = max(10, base - h - d)
        else:
            h = max(12, int(base * 0.42))
            d = max(8, int(base * 0.22))
            a = max(10, base - h - d)
        conn.execute(
            "INSERT OR IGNORE INTO match_predictions (match_key, home_votes, draw_votes, away_votes, updated_at) VALUES (?, ?, ?, ?, ?)",
            (match_key, h, d, a, time.time()),
        )
        row = conn.execute(
            "SELECT home_votes, draw_votes, away_votes FROM match_predictions WHERE match_key = ?",
            (match_key,),
        ).fetchone()
    h = int(row["home_votes"])
    d = int(row["draw_votes"])
    a = int(row["away_votes"])
    return {"home_votes": h, "draw_votes": d, "away_votes": a, "total_votes": h + d + a}


@router.get("/api/matches/predictions")
def get_match_predictions() -> Dict[str, Any]:
    init_match_predictions_db()
    with _get_conn() as conn:
        top_matches = _fetch_top_20_matches(conn)
        items: List[Dict[str, Any]] = []
        for idx, m in enumerate(top_matches, start=1):
            pred = _ensure_prediction_row(conn, m)
            items.append({
                "rank": idx,
                "match_key": m["match_key"],
                "sport": m["sport"],
                "league": m["league"],
                "league_color": m["league_color"],
                "matchup": m["matchup"],
                "schedule_venue": m["schedule_venue"],
                "votes": m["votes"],
                "buzz_score": m["buzz_score"],
                "rank_change": m["rank_change"],
                "home_votes": pred["home_votes"],
                "draw_votes": pred["draw_votes"],
                "away_votes": pred["away_votes"],
                "total_votes": pred["total_votes"],
            })
        conn.commit()
    return {"ok": True, "count": len(items), "matches": items}


@router.post("/api/matches/predictions/{match_key}/vote")
def vote_match_prediction(match_key: str, req: PredictionVoteRequest) -> Dict[str, Any]:
    choice = (req.choice or "").strip().lower()
    if choice not in ("home", "draw", "away"):
        raise HTTPException(status_code=400, detail="choice must be home, draw, or away")
    init_match_predictions_db()
    with _get_conn() as conn:
        m_row = conn.execute(
            "SELECT * FROM global_match_rankings WHERE match_key = ?",
            (match_key,),
        ).fetchone()
        m_dict = dict(m_row) if m_row else {"match_key": match_key, "sport": "", "votes": 100}
        _ensure_prediction_row(conn, m_dict)
        col = f"{choice}_votes"
        conn.execute(
            f"UPDATE match_predictions SET {col} = {col} + 1, updated_at = ? WHERE match_key = ?",
            (time.time(), match_key),
        )
        row = conn.execute(
            "SELECT home_votes, draw_votes, away_votes FROM match_predictions WHERE match_key = ?",
            (match_key,),
        ).fetchone()
        conn.commit()
    h = int(row["home_votes"])
    d = int(row["draw_votes"])
    a = int(row["away_votes"])
    _backup_tables_to_pg("match_predictions")
    return {
        "ok": True,
        "match_key": match_key,
        "choice": choice,
        "home_votes": h,
        "draw_votes": d,
        "away_votes": a,
        "total_votes": h + d + a,
    }


start_auto_analyst_scheduler()    

# --- Shorts Hub API ---
class ShortsInteractReq(BaseModel):
    clip_id: str
    action: str
    reaction: Optional[str] = None
    actor_key: Optional[str] = None


class ShortsCommentReq(BaseModel):
    clip_id: str
    time_sec: int = 0
    nickname: str = "SportIQ 팬"
    comment: str


class ShortsSubmitReq(BaseModel):
    title: str
    sport: str = "soccer"
    youtube_url: str
    point_note: str = ""
    nickname: str = "회원 제보"


SHORTS_DEFAULT_SEEDS = {
    "clip_1": (128, 42, 31, 25, 19, 28, 64, 36),
    "clip_2": (154, 58, 44, 19, 37, 33, 72, 28),
    "clip_3": (96, 29, 22, 41, 15, 24, 58, 42),
    "clip_4": (182, 74, 52, 28, 61, 45, 69, 31),
    "clip_5": (119, 38, 29, 36, 33, 21, 53, 47),
    "clip_6": (165, 63, 49, 27, 44, 39, 61, 39),
    "clip_7": (108, 34, 26, 22, 18, 27, 55, 45),
    "clip_8": (137, 49, 38, 19, 42, 31, 67, 33),
}


def init_shorts_hub_db():
    with _get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shorts_clip_stats (
                clip_id TEXT PRIMARY KEY,
                likes INTEGER NOT NULL DEFAULT 0,
                react_fire INTEGER NOT NULL DEFAULT 0,
                react_legend INTEGER NOT NULL DEFAULT 0,
                react_genius INTEGER NOT NULL DEFAULT 0,
                react_chills INTEGER NOT NULL DEFAULT 0,
                react_clap INTEGER NOT NULL DEFAULT 0,
                opt_a_votes INTEGER NOT NULL DEFAULT 0,
                opt_b_votes INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shorts_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id TEXT NOT NULL,
                time_sec INTEGER NOT NULL DEFAULT 0,
                nickname TEXT NOT NULL,
                comment TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shorts_custom_clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                sport TEXT NOT NULL,
                youtube_url TEXT NOT NULL,
                point_note TEXT NOT NULL DEFAULT '',
                nickname TEXT NOT NULL DEFAULT '회원 제보',
                created_at REAL NOT NULL,
                report_count INTEGER NOT NULL DEFAULT 0,
                is_hidden INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS shorts_user_actions (
                clip_id TEXT NOT NULL,
                action_group TEXT NOT NULL,
                actor_key TEXT NOT NULL,
                choice TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(clip_id, action_group, actor_key)
            );
            """
        )
        now = time.time()
        for cid, vals in SHORTS_DEFAULT_SEEDS.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO shorts_clip_stats
                (clip_id, likes, react_fire, react_legend, react_genius, react_chills, react_clap, opt_a_votes, opt_b_votes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (cid, vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], vals[6], vals[7], now),
            )
        cnt = conn.execute("SELECT COUNT(*) FROM shorts_comments").fetchone()[0]
        if cnt == 0:
            seed_comments = [
                ("clip_1", 5, "안필드코프", "아놀드 전환 패스 각도 실화냐", now),
                ("clip_1", 12, "전술덕후", "하프스페이스 3자 패스 타이밍 완벽!", now),
                ("clip_4", 6, "도쿄돔직관", "156km 하이패스트볼 터널링 미쳤다", now),
                ("clip_6", 8, "셀틱스팬", "스텝백 보폭이랑 릴리즈 속도 대박", now),
                ("clip_8", 9, "오일러스97", "164km 원타이머 소리까지 들리네", now),
            ]
            conn.executemany(
                "INSERT INTO shorts_comments (clip_id, time_sec, nickname, comment, created_at) VALUES (?, ?, ?, ?, ?)",
                seed_comments,
            )
        for alter_sql in (
            "ALTER TABLE shorts_custom_clips ADD COLUMN report_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE shorts_custom_clips ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(alter_sql)
            except Exception:
                pass
        conn.commit()
    _restore_snapshots_from_pg()
      

def _ensure_shorts_stat_row(conn: sqlite3.Connection, clip_id: str):
    conn.execute(
        """
        INSERT OR IGNORE INTO shorts_clip_stats
        (clip_id, likes, react_fire, react_legend, react_genius, react_chills, react_clap, opt_a_votes, opt_b_votes, updated_at)
        VALUES (?, 0, 0, 0, 0, 0, 0, 0, 0, ?)
        """,
        (clip_id, time.time()),
    )


def _format_stat_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "likes": int(row["likes"]),
        "fire": int(row["react_fire"]),
        "legend": int(row["react_legend"]),
        "genius": int(row["react_genius"]),
        "chills": int(row["react_chills"]),
        "clap": int(row["react_clap"]),
        "opt_a_votes": int(row["opt_a_votes"]),
        "opt_b_votes": int(row["opt_b_votes"]),
    }


@router.get("/api/shorts/hub")
def get_shorts_hub():
    init_shorts_hub_db()
    with _get_conn() as conn:
        stat_rows = conn.execute("SELECT * FROM shorts_clip_stats").fetchall()
        stats = {r["clip_id"]: _format_stat_row(r) for r in stat_rows}
        comment_rows = conn.execute(
            "SELECT id, clip_id, time_sec, nickname, comment, created_at FROM shorts_comments ORDER BY time_sec ASC, id ASC"
        ).fetchall()
        comments: Dict[str, List[Dict[str, Any]]] = {}
        for r in comment_rows:
            cid = r["clip_id"]
            comments.setdefault(cid, []).append(
                {
                    "id": int(r["id"]),
                    "clip_id": cid,
                    "time_sec": int(r["time_sec"]),
                    "nickname": r["nickname"],
                    "comment": r["comment"],
                    "created_at": r["created_at"],
                }
            )
        clip_rows = conn.execute(
            "SELECT id, clip_id, title, sport, youtube_url, point_note, nickname, created_at FROM shorts_custom_clips WHERE COALESCE(is_hidden, 0) = 0 ORDER BY id DESC"
        ).fetchall()
        custom_clips = [dict(r) for r in clip_rows]
    return {"ok": True, "stats": stats, "comments": comments, "custom_clips": custom_clips, "persistent_db": bool(_PG_PERSISTENT_OK)}


@router.post("/api/shorts/interact")
def post_shorts_interact(req: ShortsInteractReq, request: Request):
    init_shorts_hub_db()
    cid = (req.clip_id or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="clip_id required")
    action = (req.action or "").strip().lower()
    col_map = {
        "like": "likes",
        "vote_a": "opt_a_votes",
        "vote_b": "opt_b_votes",
        "opt_a": "opt_a_votes",
        "opt_b": "opt_b_votes", "fire": "react_fire", "legend": "react_legend", "genius": "react_genius", "chills": "react_chills", "clap": "react_clap",
    }
    if action == "react":
        rkey = (req.reaction or "").strip().lower()
        if rkey not in ("fire", "legend", "genius", "chills", "clap"):
            raise HTTPException(status_code=400, detail="invalid reaction")
        col = f"react_{rkey}"
    elif action in col_map:
        col = col_map[action]
    else:
        raise HTTPException(status_code=400, detail="invalid action")
    actor = (req.actor_key or "").strip() or (request.client.host if request.client else "unknown")
    with _get_conn() as conn:
        _ensure_shorts_stat_row(conn, cid)
        if action in ("like", "vote_a", "vote_b", "opt_a", "opt_b"):
            action_group = "like" if action == "like" else "balance"
            existing = conn.execute("SELECT choice FROM shorts_user_actions WHERE clip_id = ? AND action_group = ? AND actor_key = ?", (cid, action_group, actor)).fetchone()
            if existing:
                row = conn.execute("SELECT * FROM shorts_clip_stats WHERE clip_id = ?", (cid,)).fetchone()
                return {"ok": True, "already_participated": True, "clip_id": cid, "stats": _format_stat_row(row)}
                conn.execute("INSERT INTO shorts_user_actions (clip_id, action_group, actor_key, choice, created_at) VALUES (?, ?, ?, ?, ?)", (cid, action_group, actor, action, _now_kst().isoformat()))
            conn.execute(
            f"UPDATE shorts_clip_stats SET {col} = {col} + 1, updated_at = ? WHERE clip_id = ?",
            (time.time(), cid),
        )
        row = conn.execute("SELECT * FROM shorts_clip_stats WHERE clip_id = ?", (cid,)).fetchone()
        conn.commit()
    _backup_tables_to_pg("shorts_clip_stats", "shorts_user_actions")
    return {"ok": True, "clip_id": cid, "stats": _format_stat_row(row)}


@router.post("/api/shorts/comment")
def post_shorts_comment(req: ShortsCommentReq):
    init_shorts_hub_db()
    cid = (req.clip_id or "").strip()
    text = (req.comment or "").strip()
    if not cid or not text:
        raise HTTPException(status_code=400, detail="clip_id and comment required")
    nick = (req.nickname or "SportIQ 팬").strip() or "SportIQ 팬"
    tsec = max(0, int(req.time_sec or 0))
    now = time.time()
    with _get_conn() as conn:
        _ensure_shorts_stat_row(conn, cid)
        cur = conn.execute(
            "INSERT INTO shorts_comments (clip_id, time_sec, nickname, comment, created_at) VALUES (?, ?, ?, ?, ?)",
            (cid, tsec, nick[:24], text[:140], now),
        )
        conn.commit()
        new_id = cur.lastrowid
    _backup_tables_to_pg("shorts_comments")
    return {
        "ok": True,
        "comment": {
            "id": new_id,
            "clip_id": cid,
            "time_sec": tsec,
            "nickname": nick[:24],
            "comment": text[:140],
            "created_at": now,
        },
    }


@router.post("/api/shorts/submit")
def post_shorts_submit(req: ShortsSubmitReq):
    init_shorts_hub_db()
    title = (req.title or "").strip()
    yt = (req.youtube_url or "").strip()
    if not title or not yt:
        raise HTTPException(status_code=400, detail="title and youtube_url required")
    m = re.search(r"(?:youtube\.com/(?:shorts/|watch\?(?:[^#&]*&)*v=|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})", yt)
    if not m:
        raise HTTPException(status_code=400, detail="유효한 유튜브(youtube.com 또는 youtu.be) 링크만 등록할 수 있습니다.")
    vid = m.group(1)
    sport = (req.sport or "soccer").strip().lower()
    if sport not in ("soccer", "baseball", "basketball", "hockey"):
        sport = "soccer"
    note = (req.point_note or "").strip()
    nick = (req.nickname or "회원 제보").strip() or "회원 제보"
    now = time.time()
    cid = f"custom_{int(now * 1000)}"
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO shorts_custom_clips (clip_id, title, sport, youtube_url, point_note, nickname, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, title[:80], sport, yt[:300], note[:160], nick[:24], now),
        )
        _ensure_shorts_stat_row(conn, cid)
        conn.commit()
    _backup_tables_to_pg("shorts_custom_clips", "shorts_clip_stats")
    return {
        "ok": True,
        "clip": {
            "clip_id": cid,
            "title": title[:80],
            "sport": sport,
            "youtube_url": yt[:300],
            "point_note": note[:160],
            "nickname": nick[:24],
            "created_at": now,
            "video_id": vid,
            "thumbnail_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        },
    }
@router.post("/api/shorts/{clip_id}/report")
def post_shorts_report(clip_id: str):
    init_shorts_hub_db()
    cid = (clip_id or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="clip_id required")
    with _get_conn() as conn:
        cur = conn.execute("UPDATE shorts_custom_clips SET report_count = COALESCE(report_count, 0) + 1, is_hidden = 1 WHERE clip_id = ?", (cid,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="clip not found")
        row = conn.execute("SELECT clip_id, report_count, is_hidden FROM shorts_custom_clips WHERE clip_id = ?", (cid,)).fetchone()
        conn.commit()
    _backup_tables_to_pg("shorts_custom_clips")
    return {"ok": True, "clip_id": cid, "report_count": row["report_count"], "is_hidden": row["is_hidden"]}
    
