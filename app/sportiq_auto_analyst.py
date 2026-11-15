import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

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


start_auto_analyst_scheduler()

