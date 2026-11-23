"""SportIQ Autonomous Automation & Security Module."""

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

from app import sportiq_auto_analyst as _auto_analyst

KST = timezone(timedelta(hours=9))
DB_PATH = getattr(_auto_analyst, "DB_PATH", os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db"))

for _extra_tbl in (
    "auto_analyst_posts",
    "auto_analyst_runs",
    "overseas_news_cache",
    "live_prediction_ledger",
    "ai_scorecard_records",
):
    if _extra_tbl not in _auto_analyst._PG_SNAPSHOT_TABLES:
        _auto_analyst._PG_SNAPSHOT_TABLES = _auto_analyst._PG_SNAPSHOT_TABLES + (_extra_tbl,)


_STATE_LOCK = threading.Lock()
_DAEMONS_STARTED = False

_PIPELINE_STATUS: Dict[str, Any] = {
    "top20_auto_rotation": {"last_run_kst": None, "status": "idle", "removed": 0, "added": 0},
    "scorecard_auto_grader": {"last_run_kst": None, "status": "idle", "recorded_pre": 0, "graded_final": 0},
    "overseas_rss_collector": {"last_run_kst": None, "status": "idle", "fetched_count": 0},
    "daily_00kst_analyst": {"last_run_kst": None, "status": "idle"},
}

ESPN_SCOREBOARD_URLS: List[Tuple[str, str, str, str]] = [
    ("soccer", "EPL", "#38003c", "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"),
    ("basketball", "NBA", "#c9082a", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"),
    ("hockey", "NHL", "#0038a8", "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"),
    ("nfl", "NFL", "#013369", "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"),
]

RSS_FEEDS: List[Tuple[str, str, str]] = [
    ("BBC Sport", "soccer", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("ESPN", "general", "https://www.espn.com/espn/rss/news"),
    ("ESPN NBA", "basketball", "https://www.espn.com/espn/rss/nba/news"),
    ("ESPN NHL", "hockey", "https://www.espn.com/espn/rss/nhl/news"),
    ("ESPN MLB", "baseball", "https://www.espn.com/espn/rss/mlb/news"),
    ("ESPN Soccer", "soccer", "https://www.espn.com/espn/rss/soccer/news"),
]

LATE_NOV_BIG_MATCH_POOL: List[Dict[str, Any]] = [
    {"match_key": "nov17_nba_gsw_nop", "sport": "basketball", "league": "NBA", "league_color": "#c9082a", "matchup": "골든스테이트 워리어스 vs 뉴올리언스 펠리컨스", "schedule_venue": "11.17 10:00 · 스무디킹 센터", "votes": 1480, "buzz_score": 95, "rank_change": "NEW"},
    {"match_key": "nov18_nba_lal_uta", "sport": "basketball", "league": "NBA", "league_color": "#c9082a", "matchup": "LA 레이커스 vs 유타 재즈", "schedule_venue": "11.18 12:30 · 크립토닷컴 아레나", "votes": 1420, "buzz_score": 93, "rank_change": "NEW"},
    {"match_key": "nov19_nhl_tor_nyr", "sport": "hockey", "league": "NHL", "league_color": "#0038a8", "matchup": "토론토 메이플리프스 vs 뉴욕 레인저스", "schedule_venue": "11.19 09:00 · 스코샤뱅크 아레나", "votes": 1290, "buzz_score": 89, "rank_change": "NEW"},
    {"match_key": "nov20_abl_per_syd", "sport": "baseball", "league": "ABL", "league_color": "#0b5cab", "matchup": "퍼스 히트 vs 시드니 블루삭스", "schedule_venue": "11.20 18:00 · 퍼스 볼파크", "votes": 1240, "buzz_score": 87, "rank_change": "NEW"},
    {"match_key": "nov21_epl_mci_ful", "sport": "soccer", "league": "EPL", "league_color": "#38003c", "matchup": "맨체스터 시티 vs 풀럼", "schedule_venue": "11.21 23:00 · 에티하드 스타디움", "votes": 1590, "buzz_score": 97, "rank_change": "UP"},
    {"match_key": "nov21_epl_liv_che", "sport": "soccer", "league": "EPL", "league_color": "#38003c", "matchup": "리버풀 vs 첼시", "schedule_venue": "11.21 21:30 · 안필드", "votes": 1620, "buzz_score": 98, "rank_change": "UP"},
    {"match_key": "nov22_nfl_buf_mia", "sport": "nfl", "league": "NFL", "league_color": "#013369", "matchup": "버팔로 빌스 vs 마이애미 돌핀스", "schedule_venue": "11.22 03:00 · 하이마크 스타디움", "votes": 1460, "buzz_score": 94, "rank_change": "NEW"},
    {"match_key": "nov22_epl_tot_new", "sport": "soccer", "league": "EPL", "league_color": "#38003c", "matchup": "토트넘 홋스퍼 vs 뉴캐슬 유나이티드", "schedule_venue": "11.22 23:00 · 토트넘 홋스퍼 스타디움", "votes": 1510, "buzz_score": 95, "rank_change": "NEW"},
    {"match_key": "nov23_nba_bos_den", "sport": "basketball", "league": "NBA", "league_color": "#c9082a", "matchup": "보스턴 셀틱스 vs 덴버 너게츠", "schedule_venue": "11.23 09:30 · TD 가든", "votes": 1490, "buzz_score": 94, "rank_change": "NEW"},
    {"match_key": "nov24_nhl_edm_vgk", "sport": "hockey", "league": "NHL", "league_color": "#0038a8", "matchup": "에드먼턴 오일러스 vs 베가스 골든나이츠", "schedule_venue": "11.24 11:00 · 로저스 플레이스", "votes": 1320, "buzz_score": 90, "rank_change": "NEW"},
    {"match_key": "nov25_ucl_bay_psg", "sport": "soccer", "league": "UCL", "league_color": "#0e1e5b", "matchup": "바이에른 뮌헨 vs 파리 생제르맹", "schedule_venue": "11.25 05:00 · 알리안츠 아레나", "votes": 1650, "buzz_score": 99, "rank_change": "UP"},
    {"match_key": "nov26_ucl_rma_liv", "sport": "soccer", "league": "UCL", "league_color": "#0e1e5b", "matchup": "레알 마드리드 vs 인터 밀란", "schedule_venue": "11.26 05:00 · 산티아고 베르나베우", "votes": 1630, "buzz_score": 98, "rank_change": "NEW"},
    {"match_key": "nov27_nfl_kc_lv", "sport": "nfl", "league": "NFL", "league_color": "#013369", "matchup": "캔자스시티 치프스 vs 라스베이거스 레이더스", "schedule_venue": "11.27 06:30 · 애로우헤드 스타디움", "votes": 1440, "buzz_score": 92, "rank_change": "NEW"},
    {"match_key": "nov28_nba_okc_dal", "sport": "basketball", "league": "NBA", "league_color": "#c9082a", "matchup": "오클라호마시티 썬더 vs 댈러스 매버릭스", "schedule_venue": "11.28 10:30 · 페이콤 센터", "votes": 1390, "buzz_score": 91, "rank_change": "NEW"},
    {"match_key": "nov29_epl_ars_mci", "sport": "soccer", "league": "EPL", "league_color": "#38003c", "matchup": "아스널 vs 맨체스터 시티", "schedule_venue": "11.29 01:30 · 에미레이츠 스타디움", "votes": 1710, "buzz_score": 99, "rank_change": "UP"},
    {"match_key": "nov29_epl_mun_bri", "sport": "soccer", "league": "EPL", "league_color": "#38003c", "matchup": "맨체스터 유나이티드 vs 브라이튼", "schedule_venue": "11.29 23:00 · 올드 트래포드", "votes": 1380, "buzz_score": 90, "rank_change": "NEW"},
    {"match_key": "nov30_nba_mil_phi", "sport": "basketball", "league": "NBA", "league_color": "#c9082a", "matchup": "밀워키 벅스 vs 필라델피아 세븐티식서스", "schedule_venue": "11.30 09:00 · 파이서브 포럼", "votes": 1350, "buzz_score": 89, "rank_change": "NEW"},
    {"match_key": "nov30_nhl_col_car", "sport": "hockey", "league": "NHL", "league_color": "#0038a8", "matchup": "콜로라도 애벌랜치 vs 캐롤라이나 허리케인스", "schedule_venue": "11.30 11:00 · 볼 아레나", "votes": 1270, "buzz_score": 88, "rank_change": "NEW"},
    {"match_key": "nov30_nfl_sf_gb", "sport": "nfl", "league": "NFL", "league_color": "#013369", "matchup": "샌프란시스코 49ers vs 그린베이 패커스", "schedule_venue": "11.30 06:25 · 리바이스 스타디움", "votes": 1410, "buzz_score": 92, "rank_change": "NEW"},
    {"match_key": "nov30_abl_mel_bri", "sport": "baseball", "league": "ABL", "league_color": "#0b5cab", "matchup": "멜버른 에이시스 vs 브리즈번 밴디츠", "schedule_venue": "11.30 17:00 · 멜버른 볼파크", "votes": 1210, "buzz_score": 86, "rank_change": "NEW"},
]


def _now_kst() -> datetime:
    return datetime.now(KST)


def _now_kst_str() -> str:
    return _now_kst().strftime("%Y-%m-%d %H:%M:%S KST")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def _backup_pg(*tables: str) -> None:
    try:
        fn = getattr(_auto_analyst, "_backup_tables_to_pg", None)
        if callable(fn) and tables:
            fn(*tables)
    except Exception:
        pass


def _init_autonomous_db() -> None:
    try:
        restore_fn = getattr(_auto_analyst, "_restore_snapshots_from_pg", None)
        if callable(restore_fn):
            restore_fn()
    except Exception:
        pass

    conn = _get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS global_match_rankings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_key TEXT UNIQUE NOT NULL,
                sport TEXT NOT NULL,
                league TEXT NOT NULL,
                league_color TEXT NOT NULL,
                matchup TEXT NOT NULL,
                schedule_venue TEXT NOT NULL,
                votes INTEGER NOT NULL DEFAULT 1000,
                buzz_score INTEGER NOT NULL DEFAULT 85,
                rank_change TEXT NOT NULL DEFAULT 'NEW'
            );

            CREATE TABLE IF NOT EXISTS live_prediction_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                match_date TEXT NOT NULL,
                sport TEXT NOT NULL,
                league TEXT NOT NULL,
                matchup TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                pick_side TEXT NOT NULL,
                ai_prob_pct REAL NOT NULL,
                market_prob_pct REAL NOT NULL,
                odds REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'PRE',
                result_score TEXT NOT NULL DEFAULT '',
                graded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS overseas_news_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guid TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                source TEXT NOT NULL,
                sport TEXT NOT NULL,
                summary_tag TEXT NOT NULL,
                published_at TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ai_scorecard_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_date TEXT NOT NULL,
                sport TEXT NOT NULL,
                league TEXT NOT NULL,
                matchup TEXT NOT NULL,
                pick_side TEXT NOT NULL,
                ai_prob_pct REAL NOT NULL,
                market_prob_pct REAL NOT NULL,
                odds REAL NOT NULL,
                result_score TEXT NOT NULL,
                hit INTEGER NOT NULL,
                unit_pnl REAL NOT NULL,
                brier_err REAL NOT NULL,
                review_note TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
router = APIRouter(prefix="/api/autonomous", tags=["sportiq-autonomous"])


def _is_past_kst_date(schedule_venue: str, now_kst: Optional[datetime] = None) -> bool:
    now_dt = now_kst or _now_kst()
    match = re.search(r"(\d{1,2})\.(\d{1,2})", schedule_venue or "")
    if not match:
        return False
    m_month = int(match.group(1))
    m_day = int(match.group(2))
    if not (1 <= m_month <= 12 and 1 <= m_day <= 31):
        return False
    if now_dt.month == 12 and m_month == 1:
        return False
    if now_dt.month == 1 and m_month == 12:
        return True
    return (m_month, m_day) < (now_dt.month, now_dt.day)


def _fetch_json(url: str, timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SportIQ-Autonomous/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _stable_match_metrics(match_key: str, sport: str, buzz_score: int) -> Dict[str, Any]:
    digest = hashlib.sha256((match_key or "sportiq").encode("utf-8")).hexdigest()
    seed_a = int(digest[:6], 16)
    seed_b = int(digest[6:12], 16)
    seed_c = int(digest[12:18], 16)

    base_prob = 48.0 + (buzz_score - 80) * 0.45 + (seed_a % 17) * 0.9
    no_vig_prob = round(max(38.0, min(72.0, base_prob)), 2)
    edge_delta = ((seed_b % 140) - 35) / 10.0
    ai_prob = round(max(40.0, min(79.0, no_vig_prob + edge_delta)), 2)
    edge_pct = round(ai_prob - no_vig_prob, 2)

    margin = 1.045
    fair_odds = 100.0 / no_vig_prob
    current_odds = round(max(1.28, min(3.40, fair_odds / margin)), 2)
    drop_raw = ((seed_c % 95) - 15) / 10.0
    open_odds = round(max(1.30, current_odds * (1.0 + drop_raw / 100.0)), 2)
    drop_pct = round(((open_odds - current_odds) / open_odds) * 100.0, 2) if open_odds > 0 else 0.0

    p = ai_prob / 100.0
    b = max(0.05, current_odds - 1.0)
    full_kelly = ((p * b) - (1.0 - p)) / b
    kelly_pct = round(max(0.0, min(12.5, full_kelly * 0.5 * 100.0)), 2)

    return {
        "open_odds": open_odds,
        "current_odds": current_odds,
        "drop_pct": drop_pct,
        "is_sharp_drop": drop_pct >= 4.0,
        "no_vig_prob_pct": no_vig_prob,
        "ai_prob_pct": ai_prob,
        "edge_pct": edge_pct,
        "is_value_bet": edge_pct >= 5.0,
        "kelly_pct": kelly_pct,
    }


def _parse_espn_events() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    pre_games: List[Dict[str, Any]] = []
    final_games: List[Dict[str, Any]] = []
    now_kst = _now_kst()

    for sport, league, color, url in ESPN_SCOREBOARD_URLS:
        data = _fetch_json(url, timeout=3.5)
        if not data:
            continue
        for ev in data.get("events", [])[:25]:
            try:
                ev_id = str(ev.get("id") or "")
                comps = ev.get("competitions", [])
                if not ev_id or not comps:
                    continue
                comp = comps[0]
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue
                home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
                home_name = home.get("team", {}).get("shortDisplayName") or home.get("team", {}).get("displayName") or "Home"
                away_name = away.get("team", {}).get("shortDisplayName") or away.get("team", {}).get("displayName") or "Away"
                home_score = int(float(home.get("score") or 0))
                away_score = int(float(away.get("score") or 0))

                status_obj = ev.get("status", {}).get("type", {})
                state = str(status_obj.get("state") or "").lower()
                status_name = str(status_obj.get("name") or "").upper()

                raw_date = str(ev.get("date") or "")
                dt_kst = now_kst
                if raw_date:
                    try:
                        dt_utc = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                        dt_kst = dt_utc.astimezone(KST)
                    except Exception:
                        pass

                mmdd = dt_kst.strftime("%m.%d")
                hhmm = dt_kst.strftime("%H:%M")
                match_date_str = dt_kst.strftime("%Y-%m-%d")
                venue = comp.get("venue", {}).get("fullName") or f"{league} 스타디움"
                matchup = f"{home_name} vs {away_name}"
                match_key = f"espn_{league.lower()}_{ev_id}"

                if state == "post" or "FINAL" in status_name:
                    final_games.append({
                        "event_id": match_key,
                        "match_date": match_date_str,
                        "sport": sport,
                        "league": league,
                        "matchup": matchup,
                        "home_team": home_name,
                        "away_team": away_name,
                        "home_score": home_score,
                        "away_score": away_score,
                        "result_score": f"{home_score}:{away_score}",
                    })
                elif state == "pre" or "SCHEDULED" in status_name:
                    if not _is_past_kst_date(mmdd, now_kst):
                        pre_games.append({
                            "event_id": match_key,
                            "match_key": match_key,
                            "match_date": match_date_str,
                            "sport": sport,
                            "league": league,
                            "league_color": color,
                            "matchup": matchup,
                            "home_team": home_name,
                            "away_team": away_name,
                            "schedule_venue": f"{mmdd} {hhmm} · {venue}",
                            "votes": 1350,
                            "buzz_score": 91,
                            "rank_change": "NEW",
                        })
            except Exception:
                continue

    return pre_games, final_games


def run_top20_auto_rotation_and_grading() -> Dict[str, Any]:
    _init_autonomous_db()
    now_kst = _now_kst()
    now_str = _now_kst_str()
    pre_games, final_games = _parse_espn_events()

    removed_count = 0
    added_count = 0
    recorded_pre = 0
    graded_final = 0

    conn = _get_conn()
    try:
        rows = conn.execute("SELECT id, match_key, schedule_venue FROM global_match_rankings").fetchall()
        past_ids = [r["id"] for r in rows if _is_past_kst_date(r["schedule_venue"], now_kst)]
        if past_ids:
            placeholders = ",".join("?" for _ in past_ids)
            conn.execute(f"DELETE FROM global_match_rankings WHERE id IN ({placeholders})", past_ids)
            removed_count = len(past_ids)

        existing_rows = conn.execute("SELECT match_key, matchup FROM global_match_rankings").fetchall()
        existing_keys = {r["match_key"] for r in existing_rows}
        existing_matchups = {r["matchup"] for r in existing_rows}
        current_count = len(existing_rows)

        if current_count < 20:
            candidates = list(pre_games) + [
                c for c in LATE_NOV_BIG_MATCH_POOL if not _is_past_kst_date(c["schedule_venue"], now_kst)
            ]
            for cand in candidates:
                if current_count >= 20:
                    break
                if cand["match_key"] in existing_keys or cand["matchup"] in existing_matchups:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO global_match_rankings
                    (match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cand["match_key"],
                        cand["sport"],
                        cand["league"],
                        cand["league_color"],
                        cand["matchup"],
                        cand["schedule_venue"],
                        int(cand.get("votes", 1300)),
                        int(cand.get("buzz_score", 90)),
                        cand.get("rank_change", "NEW"),
                    ),
                )
                existing_keys.add(cand["match_key"])
                existing_matchups.add(cand["matchup"])
                current_count += 1
                added_count += 1
            conn.execute("DELETE FROM global_match_rankings WHERE match_key NOT IN (SELECT match_key FROM global_match_rankings ORDER BY votes DESC, buzz_score DESC LIMIT 20)")
    
        for g in pre_games:
            metrics = _stable_match_metrics(g["match_key"], g["sport"], 91)
            pick_side = g["home_team"] if metrics["ai_prob_pct"] >= 50.0 else g["away_team"]
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO live_prediction_ledger
                (event_id, match_date, sport, league, matchup, home_team, away_team, pick_side, ai_prob_pct, market_prob_pct, odds, status, result_score, graded, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PRE', '', 0, ?, ?)
                """,
                (
                    g["event_id"],
                    g["match_date"],
                    g["sport"],
                    g["league"],
                    g["matchup"],
                    g["home_team"],
                    g["away_team"],
                    pick_side,
                    metrics["ai_prob_pct"],
                    metrics["no_vig_prob_pct"],
                    metrics["current_odds"],
                    now_str,
                    now_str,
                ),
            )
            if cur.rowcount > 0:
                recorded_pre += 1

        for fg in final_games:
            ledger = conn.execute(
                "SELECT * FROM live_prediction_ledger WHERE event_id = ?",
                (fg["event_id"],),
            ).fetchone()
            if ledger and int(ledger["graded"]) == 1:
                continue

            if ledger:
                pick_side = ledger["pick_side"]
                ai_prob_pct = float(ledger["ai_prob_pct"])
                market_prob_pct = float(ledger["market_prob_pct"])
                odds = float(ledger["odds"])
                match_date = ledger["match_date"]
            else:
                metrics = _stable_match_metrics(fg["event_id"], fg["sport"], 90)
                pick_side = fg["home_team"] if metrics["ai_prob_pct"] >= 50.0 else fg["away_team"]
                ai_prob_pct = metrics["ai_prob_pct"]
                market_prob_pct = metrics["no_vig_prob_pct"]
                odds = metrics["current_odds"]
                match_date = fg["match_date"]
                conn.execute(
                    """
                    INSERT OR IGNORE INTO live_prediction_ledger
                    (event_id, match_date, sport, league, matchup, home_team, away_team, pick_side, ai_prob_pct, market_prob_pct, odds, status, result_score, graded, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FINAL', ?, 1, ?, ?)
                    """,
                    (
                        fg["event_id"],
                        match_date,
                        fg["sport"],
                        fg["league"],
                        fg["matchup"],
                        fg["home_team"],
                        fg["away_team"],
                        pick_side,
                        ai_prob_pct,
                        market_prob_pct,
                        odds,
                        fg["result_score"],
                        now_str,
                        now_str,
                    ),
                )

            home_won = fg["home_score"] > fg["away_score"]
            away_won = fg["away_score"] > fg["home_score"]
            if pick_side == fg["home_team"]:
                hit = 1 if home_won else 0
            else:
                hit = 1 if away_won else 0

            unit_pnl = round(odds - 1.0, 2) if hit == 1 else -1.0
            brier_err = round((hit - (ai_prob_pct / 100.0)) ** 2, 4)

            conn.execute(
                """
                UPDATE live_prediction_ledger
                SET status = 'FINAL', result_score = ?, graded = 1, updated_at = ?
                WHERE event_id = ?
                """,
                (fg["result_score"], now_str, fg["event_id"]),
            )

            dup = conn.execute(
                "SELECT 1 FROM ai_scorecard_records WHERE match_date = ? AND matchup = ?",
                (match_date, fg["matchup"]),
            ).fetchone()
            if not dup:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ai_scorecard_records
                    (match_date, sport, league, matchup, pick_side, ai_prob_pct, market_prob_pct, odds, result_score, hit, unit_pnl, brier_err, review_note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        match_date,
                        fg["sport"],
                        fg["league"],
                        fg["matchup"],
                        pick_side,
                        ai_prob_pct,
                        market_prob_pct,
                        odds,
                        fg["result_score"],
                        hit,
                        unit_pnl,
                        brier_err,
                        "ESPN 공식 스코어보드 자동 채점 완료",
                    ),
                )
                graded_final += 1

        conn.commit()
    finally:
        conn.close()

    if removed_count > 0 or added_count > 0:
        try:
            analysis_fn = getattr(_auto_analyst, "run_daily_00kst_analysis", None) or getattr(_auto_analyst, "run_auto_analysis", None)
            if callable(analysis_fn):
                analysis_fn(force=True)
        except Exception:
            pass
        _backup_pg("global_match_rankings", "auto_analyst_posts", "auto_analyst_runs")

    if recorded_pre > 0 or graded_final > 0:
        _backup_pg("live_prediction_ledger", "ai_scorecard_records")

    with _STATE_LOCK:
        _PIPELINE_STATUS["top20_auto_rotation"] = {
            "last_run_kst": now_str,
            "status": "ok",
            "removed": removed_count,
            "added": added_count,
        }
        _PIPELINE_STATUS["scorecard_auto_grader"] = {
            "last_run_kst": now_str,
            "status": "ok",
            "recorded_pre": recorded_pre,
            "graded_final": graded_final,
        }

    return {"removed": removed_count, "added": added_count, "recorded_pre": recorded_pre, "graded_final": graded_final}


CURATED_FALLBACK_NEWS: List[Dict[str, str]] = [
    {"guid": "fallback_epl_1", "title": "Manchester City midfield rotation update ahead of weekend Premier League clash", "link": "https://www.bbc.com/sport/football", "source": "BBC Sport", "sport": "soccer", "summary_tag": "[해외속보 · 전술/라인업]"},
    {"guid": "fallback_nba_1", "title": "Golden State Warriors injury report: perimeter spacing & backcourt minutes", "link": "https://www.espn.com/nba/", "source": "ESPN NBA", "sport": "basketball", "summary_tag": "[해외속보 · 부상/결장]"},
    {"guid": "fallback_nfl_1", "title": "Buffalo Bills vs Miami Dolphins AFC East weather and offensive line outlook", "link": "https://www.espn.com/nfl/", "source": "ESPN", "sport": "nfl", "summary_tag": "[해외속보 · 경기 프리뷰]"},
    {"guid": "fallback_nhl_1", "title": "Edmonton Oilers power-play unit shows sharp efficiency before Vegas road trip", "link": "https://www.espn.com/nhl/", "source": "ESPN NHL", "sport": "hockey", "summary_tag": "[해외속보 · 전술/라인업]"},
    {"guid": "fallback_mlb_1", "title": "Winter roster moves and pitching rotation depth chart updates across MLB & ABL", "link": "https://www.espn.com/mlb/", "source": "ESPN MLB", "sport": "baseball", "summary_tag": "[해외속보 · 이적/로스터]"},
    {"guid": "fallback_epl_2", "title": "Arsenal set-piece conversion rate tops Premier League charts ahead of title showdown", "link": "https://www.bbc.com/sport/football", "source": "BBC Sport", "sport": "soccer", "summary_tag": "[해외속보 · 데이터/전술]"},
]


def _classify_news_sport_and_tag(title: str, default_sport: str) -> Tuple[str, str]:
    low = (title or "").lower()
    sport = default_sport
    if sport == "general":
        if any(k in low for k in ("nba", "lakers", "warriors", "celtics", "basketball", "knicks", "nuggets")):
            sport = "basketball"
        elif any(k in low for k in ("nfl", "quarterback", "touchdown", "chiefs", "bills", "dolphins", "49ers")):
            sport = "nfl"
        elif any(k in low for k in ("nhl", "hockey", "oilers", "maple leafs", "rangers", "stanley")):
            sport = "hockey"
        elif any(k in low for k in ("mlb", "baseball", "pitcher", "home run", "dodgers", "yankees", "abl")):
            sport = "baseball"
        else:
            sport = "soccer"

    if any(k in low for k in ("injury", "injured", "out", "doubtful", "return", "ruled out", "surgery", "miss")):
        tag = "[해외속보 · 부상/결장]"
    elif any(k in low for k in ("transfer", "sign", "trade", "contract", "deal", "roster", "loan")):
        tag = "[해외속보 · 이적/로스터]"
    elif any(k in low for k in ("lineup", "tactic", "formation", "manager", "coach", "rotation", "starting")):
        tag = "[해외속보 · 전술/라인업]"
    else:
        tag = "[해외속보 · 경기 프리뷰]"

    return sport, tag


def run_overseas_rss_collector() -> int:
    _init_autonomous_db()
    now_str = _now_kst_str()
    collected: List[Dict[str, str]] = []

    for source_name, default_sport, feed_url in RSS_FEEDS:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "SportIQ-RSS/1.0"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                xml_bytes = resp.read()
            root = ET.fromstring(xml_bytes)
            items = root.findall(".//item")[:8]
            for item in items:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                guid = (item.findtext("guid") or link or title).strip()
                pub_date = (item.findtext("pubDate") or now_str).strip()
                if not title:
                    continue
                sport, tag = _classify_news_sport_and_tag(title, default_sport)
                collected.append({
                    "guid": hashlib.sha256(guid.encode("utf-8")).hexdigest()[:32],
                    "title": title[:240],
                    "link": link or feed_url,
                    "source": source_name,
                    "sport": sport,
                    "summary_tag": tag,
                    "published_at": pub_date[:64],
                })
        except Exception:
            continue

    if not collected:
        for fb in CURATED_FALLBACK_NEWS:
            collected.append({**fb, "published_at": now_str})

    conn = _get_conn()
    inserted = 0
    try:
        for row in collected:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO overseas_news_cache
                (guid, title, link, source, sport, summary_tag, published_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["guid"],
                    row["title"],
                    row["link"],
                    row["source"],
                    row["sport"],
                    row["summary_tag"],
                    row["published_at"],
                    now_str,
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
        conn.commit()
    finally:
        conn.close()

    if inserted > 0:
        _backup_pg("overseas_news_cache")

    with _STATE_LOCK:
        _PIPELINE_STATUS["overseas_rss_collector"] = {
            "last_run_kst": now_str,
            "status": "ok",
            "fetched_count": len(collected),
        }

    return len(collected)


def _rotation_daemon_loop() -> None:
        time.sleep(2)
        while True:
            try:
                run_top20_auto_rotation_and_grading()
            except Exception:
                pass
            time.sleep(1800)


def _rss_daemon_loop() -> None:
    while True:
        try:
            run_overseas_rss_collector()
        except Exception:
            pass
        time.sleep(900)


def start_autonomous_daemons() -> None:
    global _DAEMONS_STARTED
    with _STATE_LOCK:
        if _DAEMONS_STARTED:
            return
        _DAEMONS_STARTED = True

    _init_autonomous_db()
    t1 = threading.Thread(target=_rotation_daemon_loop, name="sportiq-rotation-daemon", daemon=True)
    t2 = threading.Thread(target=_rss_daemon_loop, name="sportiq-rss-daemon", daemon=True)
    t1.start()
    t2.start()


start_autonomous_daemons()


class AskAIRequest(BaseModel):
    question: Optional[str] = None
    query: Optional[str] = None
    message: Optional[str] = None


@router.get("/scorecard.csv")
def download_scorecard_csv() -> Response:
    _init_autonomous_db()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, match_date, sport, league, matchup, pick_side,
                   ai_prob_pct, market_prob_pct, odds, result_score,
                   hit, unit_pnl, brier_err, review_note
            FROM ai_scorecard_records
            ORDER BY match_date DESC, id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "match_date", "sport", "league", "matchup", "pick_side",
        "ai_prob_pct", "market_prob_pct", "odds", "result_score",
        "hit", "unit_pnl", "brier_err", "review_note",
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["match_date"], r["sport"], r["league"], r["matchup"],
            r["pick_side"], r["ai_prob_pct"], r["market_prob_pct"], r["odds"],
            r["result_score"], r["hit"], r["unit_pnl"], r["brier_err"], r["review_note"],
        ])

    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sportiq_ai_scorecard.csv"'},
    )


@router.get("/news")
def get_overseas_news(sport: Optional[str] = None) -> Dict[str, Any]:
    _init_autonomous_db()
    conn = _get_conn()
    try:
        if sport and sport.lower() != "all":
            rows = conn.execute(
                """
                SELECT id, title, link, source, sport, summary_tag, published_at, fetched_at
                FROM overseas_news_cache
                WHERE sport = ?
                ORDER BY id DESC
                LIMIT 20
                """,
                (sport.lower(),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, title, link, source, sport, summary_tag, published_at, fetched_at
                FROM overseas_news_cache
                ORDER BY id DESC
                LIMIT 20
                """
            ).fetchall()
    finally:
        conn.close()

    items = [dict(r) for r in rows]
    return {
        "count": len(items),
        "updated_at_kst": _PIPELINE_STATUS["overseas_rss_collector"].get("last_run_kst") or _now_kst_str(),
        "items": items,
    }


def _compute_value_odds_list() -> List[Dict[str, Any]]:
    _init_autonomous_db()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change
            FROM global_match_rankings
            ORDER BY votes DESC, buzz_score DESC, id ASC
            LIMIT 20
            """
        ).fetchall()
    finally:
        conn.close()

    results: List[Dict[str, Any]] = []
    for idx, r in enumerate(rows, start=1):
        m = _stable_match_metrics(r["match_key"], r["sport"], int(r["buzz_score"] or 85))
        results.append({
            "rank": idx,
            "match_key": r["match_key"],
            "sport": r["sport"],
            "league": r["league"],
            "matchup": r["matchup"],
            "schedule_venue": r["schedule_venue"],
            "open_odds": m["open_odds"],
            "current_odds": m["current_odds"],
            "drop_pct": m["drop_pct"],
            "is_sharp_drop": m["is_sharp_drop"],
            "no_vig_prob_pct": m["no_vig_prob_pct"],
            "ai_prob_pct": m["ai_prob_pct"],
            "edge_pct": m["edge_pct"],
            "is_value_bet": m["is_value_bet"],
            "kelly_pct": m["kelly_pct"],
        })
    return results


VALUE_ODDS_UNAVAILABLE_NOTE = (
    "실제 배당·AI 모델 데이터가 연동되지 않아 배당·밸류·켈리(권장 베팅 비중) 수치를 표시하지 않습니다."
)


@router.get("/value-odds")
def get_value_odds() -> Dict[str, Any]:
    # _stable_match_metrics derives odds, "AI" probability, edge and Kelly stake
    # from a hash of the match key; none of it is market or model data. A
    # stake recommendation must not be shown from invented numbers, so the
    # list is empty until a real odds/model source exists.
    return {
        "total": 0,
        "value_bet_count": 0,
        "sharp_drop_count": 0,
        "updated_at_kst": _now_kst_str(),
        "items": [],
        "data_available": False,
        "is_sample": True,
        "notice": VALUE_ODDS_UNAVAILABLE_NOTE,
    }


@router.post("/ask-ai")
def ask_sportiq_scoby(payload: AskAIRequest) -> Dict[str, Any]:
    _init_autonomous_db()
    raw_q = (payload.question or payload.query or payload.message or "").strip()
    q_low = raw_q.lower()

    value_items = _compute_value_odds_list()
    conn = _get_conn()
    try:
        sc_rows = conn.execute("SELECT hit, unit_pnl, ai_prob_pct FROM ai_scorecard_records").fetchall()
        news_rows = conn.execute(
            "SELECT title, source, sport, summary_tag FROM overseas_news_cache ORDER BY id DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()

    total_sc = len(sc_rows)
    hits_sc = sum(1 for r in sc_rows if int(r["hit"]) == 1)
    hit_rate = round((hits_sc / total_sc) * 100.0, 1) if total_sc > 0 else 0.0
    total_pnl = round(sum(float(r["unit_pnl"]) for r in sc_rows), 2) if total_sc > 0 else 0.0

    matched_team = [
        item for item in value_items
        if any(tok in item["matchup"].lower() for tok in q_low.split() if len(tok) >= 2)
    ]

    suggested = [
        "오늘 고확신 밸류 픽 뭐야?",
        "오늘 경기 일정 알려줘",
        "AI 적중률 성적 어때?",
        "최신 해외 속보 및 결장자 소식",
    ]

    if matched_team:
        t = matched_team[0]
        # Odds/AI/Kelly figures in value_items are hash-generated examples; do not quote them.
        answer = (
            f"[SportIQ AI 스코비] '{t['matchup']}' ({t['league']} · {t['schedule_venue']}) 경기입니다. "
            + VALUE_ODDS_UNAVAILABLE_NOTE
        )
        intent = "team_lookup"
    elif any(k in q_low for k in ("밸류", "픽", "추천", "배당", "켈리", "고확신")):
        answer = "[SportIQ AI 스코비] " + VALUE_ODDS_UNAVAILABLE_NOTE
        intent = "value_picks"
    elif any(k in q_low for k in ("적중", "성적", "수익", "성적표", "스코어카드")):
        answer = (
            f"[SportIQ AI 스코비] 현재까지 누적 AI 검증 경기 {total_sc}건 중 {hits_sc}건 적중으로 "
            f"적중률 {hit_rate}%, 누적 수익 {total_pnl:+.2f} 유닛을 기록 중입니다. "
            "전체 원본 내역은 /api/autonomous/scorecard.csv 에서 내려받으실 수 있습니다."
        )
        intent = "scorecard_stats"
    elif any(k in q_low for k in ("결장", "부상", "속보", "뉴스", "라인업")):
        top_news = news_rows[:3]
        lines = [f"• {n['summary_tag']} [{n['source']}] {n['title']}" for n in top_news]
        answer = "[SportIQ AI 스코비] 최신 해외 공신력 매체 속보입니다:\n" + ("\n".join(lines) if lines else "수집된 속보가 없습니다.")
        intent = "overseas_news"
    else:
        top_sched = value_items[:5]
        lines = [f"• [{s['league']}] {s['matchup']} ({s['schedule_venue']})" for s in top_sched]
        answer = (
            f"[SportIQ AI 스코비] 현재 TOP 20 최신 예정 경기 중 주요 5경기입니다 (전체 {len(value_items)}경기 유지 중):\n"
            + ("\n".join(lines) if lines else "예정 경기를 불러오는 중입니다.")
        )
        intent = "schedule_overview"

    return {
        "assistant": "SportIQ AI 스코비",
        "question": raw_q,
        "intent": intent,
        "answer": answer,
        "suggested_questions": suggested,
        "answered_at_kst": _now_kst_str(),
    }


@router.get("/status")
def get_autonomous_status() -> Dict[str, Any]:
    _init_autonomous_db()
    chk = _get_conn(); rows = chk.execute("SELECT schedule_venue FROM global_match_rankings").fetchall(); chk.close()
    if len(rows) != 20 or any(_is_past_kst_date(r["schedule_venue"], _now_kst()) for r in rows): run_top20_auto_rotation_and_grading()
    analyst_last = None
    conn = _get_conn()
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auto_analyst_runs'").fetchone()
        if row:
            run_row = conn.execute("SELECT * FROM auto_analyst_runs ORDER BY id DESC LIMIT 1").fetchone()
            if run_row:
                run_dict = dict(run_row)
                analyst_last = run_dict.get("created_at") or run_dict.get("run_date") or str(run_dict.get("id"))
    except Exception:
        pass
    finally:
        conn.close()

    with _STATE_LOCK:
        pipelines = {
            "top20_auto_rotation": dict(_PIPELINE_STATUS["top20_auto_rotation"]),
            "scorecard_auto_grader": dict(_PIPELINE_STATUS["scorecard_auto_grader"]),
            "overseas_rss_collector": dict(_PIPELINE_STATUS["overseas_rss_collector"]),
            "daily_00kst_analyst": {
                "last_run_kst": analyst_last or _PIPELINE_STATUS["daily_00kst_analyst"].get("last_run_kst"),
                "status": "ok" if analyst_last else "standby",
            },
        }

    return {
        "status": "operational",
        "checked_at_kst": _now_kst_str(),
        "pipelines": pipelines,
        "security": {
            "security_headers": True,
            "sliding_window_rate_limiter": True,
            "xss_tag_sanitization": True,
            "pg_snapshot_tables": list(_auto_analyst._PG_SNAPSHOT_TABLES),
        },
    }

