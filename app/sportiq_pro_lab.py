import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import sportiq_auto_analyst as _analyst

router = APIRouter(prefix="/api/pro-lab", tags=["pro-lab"])

DB_PATH = os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db")
KST = timezone(timedelta(hours=9))

for _tbl in ("ai_scorecard_records", "saved_tactics"):
    if _tbl not in _analyst._PG_SNAPSHOT_TABLES:
        _analyst._PG_SNAPSHOT_TABLES = _analyst._PG_SNAPSHOT_TABLES + (_tbl,)

ESPN_ENDPOINTS = (
    ("EPL", "soccer", "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"),
    ("UCL", "soccer", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard"),
    ("UNL", "soccer", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.nations/scoreboard"),
    ("NBA", "basketball", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"),
    ("NHL", "hockey", "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"),
    ("NFL", "football", "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"),
)

_TICKER_LOCK = threading.Lock()
_TICKER_CACHE: Dict[str, Any] = {
    "items": [],
    "updated_at_kst": "",
    "last_fetch_ts": 0.0,
}
_WORKER_STARTED = False
_DB_INITIALIZED = False


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _now_kst_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


_KO_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _format_dt_kst(dt_kst: datetime) -> tuple:
    wd = _KO_WEEKDAYS[dt_kst.weekday()]
    short_str = f"{dt_kst.month:02d}.{dt_kst.day:02d} ({wd}) {dt_kst.hour:02d}:{dt_kst.minute:02d}"
    full_str = f"{dt_kst.year:04d}.{short_str} KST"
    return short_str, full_str


def _now_kst_display() -> str:
    _, full_str = _format_dt_kst(datetime.now(KST))
    return full_str


def _parse_iso_to_kst(iso_str: str) -> tuple:
    raw = str(iso_str or "").strip()
    if not raw:
        return "", ""
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_kst = dt.astimezone(KST)
        return _format_dt_kst(dt_kst)
    except Exception:
        return "", ""


def _fallback_match_dt_kst(schedule_venue: str, sport: str, league: str, match_id: str) -> tuple:
    text = str(schedule_venue or "")
    now_kst = datetime.now(KST)
    year = 2026
    month = None
    day = None
    m_date = re.search(r"([0-9]{1,2}) *[./-] *([0-9]{1,2})", text)
    if not m_date:
        m_date = re.search(r"([0-9]{1,2}) *월 *([0-9]{1,2}) *일", text)
    if m_date:
        try:
            cand_m = int(m_date.group(1))
            cand_d = int(m_date.group(2))
            datetime(year, cand_m, cand_d, tzinfo=KST)
            month, day = cand_m, cand_d
        except Exception:
            month, day = None, None
    hh = None
    mm = None
    m_time = re.search(r"([0-9]{1,2}) *: *([0-9]{2})", text)
    if m_time:
        try:
            cand_h = int(m_time.group(1))
            cand_min = int(m_time.group(2))
            if 0 <= cand_h <= 23 and 0 <= cand_min <= 59:
                hh, mm = cand_h, cand_min
        except Exception:
            hh, mm = None, None
    seed_val = 0
    for ch in str(match_id or "0"):
        seed_val = (seed_val * 31 + ord(ch)) % 100000
    if hh is None or mm is None:
        sp = str(sport or "").lower()
        lg = str(league or "").upper()
        if sp == "soccer" or lg in ("EPL", "UCL", "UNL"):
            slots = ((21, 30), (22, 0), (23, 0), (0, 30), (1, 30), (4, 0))
        elif sp == "basketball" or lg == "NBA":
            slots = ((9, 0), (9, 30), (10, 0), (11, 30), (12, 0))
        elif sp == "hockey" or lg == "NHL":
            slots = ((9, 0), (9, 30), (10, 0), (11, 0))
        elif sp == "football" or lg == "NFL":
            slots = ((3, 0), (6, 25), (10, 15))
        else:
            slots = ((19, 0), (20, 0), (21, 0))
        hh, mm = slots[seed_val % len(slots)]
    if month is None or day is None:
        base_dt = now_kst + timedelta(days=(seed_val % 5) + 1)
        dt_kst = datetime(base_dt.year, base_dt.month, base_dt.day, hh, mm, tzinfo=KST)
    else:
        dt_kst = datetime(year, month, day, hh, mm, tzinfo=KST)
    return _format_dt_kst(dt_kst)


def _split_teams(matchup: str) -> tuple:
    text = str(matchup or "").strip()
    for sep in (" vs ", " VS ", " v ", " - "):
        if sep in text:
            parts = text.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return text or "Home", "Away"


def _seed_hash(key: str) -> int:
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


# 초기 시드로 넣은 예시 적중 기록(실제 예측 결과 아님). 응답에서 is_sample로 표시한다.
_SEED_SCORECARD_KEYS = frozenset({
    ('2026-11-15', '리버풀 vs 아스톤 빌라'),
    ('2026-11-15', '바르셀로나 vs 바이에른 뮌헨'),
    ('2026-11-14', '보스턴 셀틱스 vs 밀워키 벅스'),
    ('2026-11-14', 'LA 레이커스 vs 피닉스 선즈'),
    ('2026-11-13', 'LA 다저스 vs 뉴욕 양키스'),
    ('2026-11-13', 'KIA 타이거즈 vs 삼성 라이온즈'),
    ('2026-11-12', '에드먼턴 오일러스 vs 댈러스 스타즈'),
    ('2026-11-12', '아스널 vs 첼시'),
    ('2026-11-11', '덴버 너게츠 vs 오클라호마시티'),
    ('2026-11-11', '플로리다 팬서스 vs 뉴욕 레인저스'),
    ('2026-11-10', '레알 마드리드 vs AC 밀란'),
    ('2026-11-10', '휴스턴 애스트로스 vs 텍사스 레인저스'),
})


def _init_pro_lab_db() -> None:
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    conn = _get_conn()
    try:
        conn.executescript("""
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

            CREATE TABLE IF NOT EXISTS saved_tactics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                sport TEXT NOT NULL,
                formation TEXT NOT NULL,
                author TEXT NOT NULL,
                memo TEXT NOT NULL,
                players_json TEXT NOT NULL,
                likes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)
        conn.commit()
    finally:
        conn.close()

    try:
        _analyst._restore_snapshots_from_pg()
    except Exception:
        pass

    conn = _get_conn()
    try:
        sc_count = conn.execute("SELECT COUNT(*) FROM ai_scorecard_records").fetchone()[0]
        if sc_count == 0:
            conn.executescript("""
                INSERT INTO ai_scorecard_records (
                    match_date, sport, league, matchup, pick_side,
                    ai_prob_pct, market_prob_pct, odds, result_score,
                    hit, unit_pnl, brier_err, review_note
                ) VALUES
                ('2026-11-15', '축구', 'EPL', '리버풀 vs 아스톤 빌라', '리버풀 승', 64.2, 55.1, 1.72, '2-0', 1, 0.72, 0.128, '홈 하이프레싱 xG 2.41 우위 적중'),
                ('2026-11-15', '축구', 'UCL', '바르셀로나 vs 바이에른 뮌헨', '바르셀로나 승', 49.5, 40.8, 2.35, '3-1', 1, 1.35, 0.255, '시장 과소평가 밸류 픽(+8.7%p) 적중'),
                ('2026-11-14', '농구', 'NBA', '보스턴 셀틱스 vs 밀워키 벅스', '보스턴 승', 67.0, 58.4, 1.65, '119-108', 1, 0.65, 0.109, '5-Out 스페이싱 3점 효율 우위 적중'),
                ('2026-11-14', '농구', 'NBA', 'LA 레이커스 vs 피닉스 선즈', '피닉스 승', 54.0, 48.0, 2.02, '112-109', 0, -1.00, 0.292, '4쿼터 클러치 턴오버 변수로 미적중'),
                ('2026-11-13', '야구', 'MLB', 'LA 다저스 vs 뉴욕 양키스', '다저스 승', 58.6, 50.2, 1.88, '5-3', 1, 0.88, 0.171, '불펜 WHIP 격차 반영 밸류 픽 적중'),
                ('2026-11-13', '야구', 'KBO', 'KIA 타이거즈 vs 삼성 라이온즈', 'KIA 승', 61.5, 56.0, 1.70, '4-6', 0, -1.00, 0.378, '선발 조기 강판 이후 역전 허용 미적중'),
                ('2026-11-12', '하키', 'NHL', '에드먼턴 오일러스 vs 댈러스 스타즈', '에드먼턴 승', 56.8, 47.9, 2.05, '4-2', 1, 1.05, 0.187, '파워플레이 득점률 격차 적중'),
                ('2026-11-12', '축구', 'EPL', '아스널 vs 첼시', '무승부', 31.2, 26.5, 3.45, '1-1', 1, 2.45, 0.473, '원정 로우블록 접전 모델 포착 적중'),
                ('2026-11-11', '농구', 'NBA', '덴버 너게츠 vs 오클라호마시티', 'OKC 승', 55.4, 46.8, 2.08, '104-111', 1, 1.08, 0.199, '수비 레이팅 우위 밸류 픽(+8.6%p) 적중'),
                ('2026-11-11', '하키', 'NHL', '플로리다 팬서스 vs 뉴욕 레인저스', '플로리다 승', 53.0, 49.5, 1.95, '2-3', 0, -1.00, 0.281, '골리 세이브율 급등 변수로 미적중'),
                ('2026-11-10', '축구', 'UCL', '레알 마드리드 vs AC 밀란', '레알 마드리드 승', 68.4, 62.0, 1.54, '2-1', 1, 0.54, 0.100, '후반 전환 속도 우위 정배 적중'),
                ('2026-11-10', '야구', 'MLB', '휴스턴 애스트로스 vs 텍사스 레인저스', '휴스턴 승', 57.2, 51.0, 1.90, '2-4', 0, -1.00, 0.327, '득점권 잔루 누적으로 미적중');
            """)
            conn.commit()
            try:
                _analyst._backup_tables_to_pg("ai_scorecard_records")
            except Exception:
                pass

        tc_count = conn.execute("SELECT COUNT(*) FROM saved_tactics").fetchone()[0]
        if tc_count == 0:
            conn.executescript("""
                INSERT INTO saved_tactics (
                    title, sport, formation, author, memo, players_json, likes, created_at
                ) VALUES
                (
                    '4-3-3 안필드 하이프레싱',
                    '축구',
                    '4-3-3',
                    'Coach_KloppLab',
                    '전방 스리톱이 상대 센터백 빌드업을 측면으로 유도하고, 8번 미드필더 두 명이 하프스페이스 세컨볼을 즉시 탈취하는 고강도 압박 구조입니다.',
                    '[{"name":"알리송","role":"GK","x":50,"y":90},{"name":"아놀드","role":"RB","x":84,"y":72},{"name":"코나테","role":"RCB","x":63,"y":76},{"name":"반 다이크","role":"LCB","x":37,"y":76},{"name":"로버트슨","role":"LB","x":16,"y":72},{"name":"흐라번베르흐","role":"CDM","x":50,"y":58},{"name":"맥 알리스터","role":"LCM","x":34,"y":46},{"name":"소보슬라이","role":"RCM","x":66,"y":46},{"name":"살라","role":"RW","x":82,"y":24},{"name":"누녜스","role":"ST","x":50,"y":18},{"name":"디아스","role":"LW","x":18,"y":24}]',
                    42,
                    '2026-11-15 18:00:00 KST'
                ),
                (
                    '3-4-2-1 포체티노 역습 블록',
                    '축구',
                    '3-4-2-1',
                    'Tactic_Master',
                    '수비 시 5-4-1 두 줄 블록으로 중앙 채널을 봉쇄한 뒤, 볼 탈취 즉시 양 윙백과 두 명의 10번이 수직 전환을 전개하는 역습 특화 전술입니다.',
                    '[{"name":"비카리오","role":"GK","x":50,"y":90},{"name":"로메로","role":"RCB","x":70,"y":76},{"name":"다이어","role":"CCB","x":50,"y":78},{"name":"판 더 펜","role":"LCB","x":30,"y":76},{"name":"포로","role":"RWB","x":88,"y":54},{"name":"비수마","role":"RCM","x":60,"y":56},{"name":"벤탄쿠르","role":"LCM","x":40,"y":56},{"name":"우도기","role":"LWB","x":12,"y":54},{"name":"쿨루셉스키","role":"RAM","x":66,"y":34},{"name":"매디슨","role":"LAM","x":34,"y":34},{"name":"손흥민","role":"ST","x":50,"y":18}]',
                    35,
                    '2026-11-15 19:30:00 KST'
                ),
                (
                    '보스턴 5-Out 스위치 공략',
                    '농구',
                    '5-Out',
                    'NBA_Spacing',
                    '5명 전원 3점 라인 밖에 배치해 페인트존 헬프 수비를 제거하고, 탑-윙 픽앤롤 이후 미스매치 아이솔레이션 및 코너 킥아웃을 노립니다.',
                    '[{"name":"할리데이","role":"PG","x":50,"y":26},{"name":"화이트","role":"SG","x":80,"y":34},{"name":"브라운","role":"SF","x":20,"y":34},{"name":"테이텀","role":"PF","x":84,"y":74},{"name":"포르징기스","role":"C","x":16,"y":74}]',
                    29,
                    '2026-11-15 21:00:00 KST'
                );
            """)
            conn.commit()
            try:
                _analyst._backup_tables_to_pg("saved_tactics")
            except Exception:
                pass
    finally:
        conn.close()

    _DB_INITIALIZED = True


def _build_match_model(
    match_key: str,
    sport: str,
    league: str,
    home: str,
    away: str,
    votes: int = 100,
    buzz_score: float = 80.0,
    pred_home: int = 0,
    pred_draw: int = 0,
    pred_away: int = 0,
) -> Dict[str, Any]:
    h = _seed_hash(f"{match_key}:{home}:{away}")
    is_soccer = sport in ("축구", "soccer")

    base_home = 38.0 + (h % 26)
    if is_soccer:
        ai_draw = round(20.0 + ((h >> 4) % 12), 1)
        remain = 100.0 - ai_draw
        ai_home = round(remain * (base_home / 100.0), 1)
        ai_away = round(100.0 - ai_home - ai_draw, 1)
    else:
        ai_draw = 0.0
        ai_home = round(36.0 + (h % 31), 1)
        ai_away = round(100.0 - ai_home, 1)

    total_preds = pred_home + pred_draw + pred_away
    if total_preds >= 5:
        mkt_home = round(pred_home * 100.0 / total_preds, 1)
        mkt_draw = round(pred_draw * 100.0 / total_preds, 1) if is_soccer else 0.0
        mkt_away = round(100.0 - mkt_home - mkt_draw, 1)
    else:
        shift = ((h >> 8) % 19) - 9
        mkt_home = round(max(18.0, min(78.0, ai_home - shift)), 1)
        if is_soccer:
            mkt_draw = round(max(14.0, min(34.0, ai_draw + (((h >> 12) % 5) - 2))), 1)
            mkt_away = round(100.0 - mkt_home - mkt_draw, 1)
        else:
            mkt_draw = 0.0
            mkt_away = round(100.0 - mkt_home, 1)

    edge_home = round(ai_home - mkt_home, 1)
    edge_away = round(ai_away - mkt_away, 1)
    edge_pct = edge_home if abs(edge_home) >= abs(edge_away) else edge_away
    value_badge = "💎 AI 밸류 포착" if edge_pct >= 8.4 else ""

    forms_pool = ("W-W-D-W-W", "W-D-W-L-W", "W-W-L-W-D", "L-W-W-D-W", "D-W-L-W-W")
    home_form = forms_pool[h % len(forms_pool)]
    away_form = forms_pool[(h >> 3) % len(forms_pool)]

    if is_soccer:
        expected_metric = f"xG {home} {1.25 + (h % 14) * 0.1:.2f} : {0.95 + ((h >> 5) % 12) * 0.1:.2f} {away}"
    elif sport in ("농구", "basketball"):
        expected_metric = f"예상 ORtg {112 + (h % 9)} vs {108 + ((h >> 5) % 9)}"
    elif sport in ("하키", "hockey"):
        expected_metric = f"예상 xGF {2.8 + (h % 9) * 0.1:.1f} vs {2.4 + ((h >> 5) % 9) * 0.1:.1f}"
    else:
        expected_metric = f"파워 인덱스 {(buzz_score or 82.0):.1f} / 화제성 {votes}"

    h2h_wins = 2 + (h % 3)
    h2h_losses = 5 - h2h_wins - (1 if is_soccer and (h % 2 == 0) else 0)
    h2h_draws = 5 - h2h_wins - max(0, h2h_losses)
    if is_soccer:
        h2h_summary = f"최근 5전 {home} {h2h_wins}승 {h2h_draws}무 {max(0, h2h_losses)}패"
    else:
        h2h_summary = f"최근 5전 {home} {h2h_wins}승 {5 - h2h_wins}패"

    injury_pool = (
        f"{home} 주전 라인업 정상 가동 · {away} 로테이션 체력 변수 점검",
        f"{away} 핵심 수비 자원 출전 의심(GTD) · 세트피스/수비 전환 주의",
        f"{home} 측면 자원 경미한 피로 누적 · 후반 교체 카드 비중 확대 예상",
        "양 팀 주요 핵심 스쿼드 정상 출전 유력 · 라인업 확정 직전 변동 체크",
    )
    risk_pool = (
        "초반 15분 전방 압박 강도에 따른 실점 리스크",
        "원정팀 역습 전환 속도 대비 뒷공간 노출 주의",
        "백투백 일정 및 이동 피로도에 따른 후반 집중력 변수",
        "배당 대비 팬 쏠림 현상 관찰 — 라이브 오즈 변동 확인 권장",
    )

    return {
        "ai_home_pct": ai_home,
        "ai_draw_pct": ai_draw,
        "ai_away_pct": ai_away,
        "mkt_home_pct": mkt_home,
        "mkt_draw_pct": mkt_draw,
        "mkt_away_pct": mkt_away,
        "edge_pct": edge_pct,
        "value_badge": value_badge,
        "injury_note": injury_pool[h % len(injury_pool)],
        "risk_note": risk_pool[(h >> 6) % len(risk_pool)],
        "quick_stats": {
            "recent_form": f"{home} ({home_form}) vs {away} ({away_form})",
            "expected_metric": expected_metric,
            "h2h_summary": h2h_summary,
        },
    }


def _fetch_espn_scoreboard() -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    for league_code, sport_code, url in ESPN_ENDPOINTS:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "SportIQ-ProLab/1.0"},
            )
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw)
        except Exception:
            continue

        events = data.get("events") or []
        for ev in events[:5]:
            try:
                comps = ev.get("competitions") or []
                if not comps:
                    continue
                comp = comps[0]
                competitors = comp.get("competitors") or []
                home_obj = None
                away_obj = None
                for c in competitors:
                    if c.get("homeAway") == "home":
                        home_obj = c
                    elif c.get("homeAway") == "away":
                        away_obj = c
                if not home_obj or not away_obj:
                    continue

                home_name = (
                    (home_obj.get("team") or {}).get("shortDisplayName")
                    or (home_obj.get("team") or {}).get("displayName")
                    or "Home"
                )
                away_name = (
                    (away_obj.get("team") or {}).get("shortDisplayName")
                    or (away_obj.get("team") or {}).get("displayName")
                    or "Away"
                )
                home_score = str(home_obj.get("score") or "0")
                away_score = str(away_obj.get("score") or "0")

                status_obj = (comp.get("status") or {}).get("type") or {}
                state = str(status_obj.get("state") or "").lower()
                is_live = state == "in"
                status_text = str(
                    status_obj.get("shortDetail")
                    or status_obj.get("description")
                    or "예정"
                )
                match_dt_kst, match_dt_full_kst = _parse_iso_to_kst(
                    ev.get("date") or comp.get("date") or ""
                )
                if not match_dt_kst:
                    match_dt_kst, match_dt_full_kst = _format_dt_kst(datetime.now(KST))
                if state not in ("in", "post"):
                    st_upper = status_text.upper()
                    if (
                        not status_text
                        or any(
                            tok in st_upper
                            for tok in ("EST", "EDT", "CST", "CDT", "PST", "PDT", "UTC", "GMT", " AM", " PM")
                        )
                    ):
                        status_text = f"{match_dt_kst} KST 예정"
                match_id = f"espn_{league_code.lower()}_{ev.get('id', '')}"
                model = _build_match_model(
                    match_key=match_id,
                    sport=sport_code,
                    league=league_code,
                    home=home_name,
                    away=away_name,
                )
                collected.append({
                    "match_id": match_id,
                    "league": league_code,
                    "home": home_name,
                    "away": away_name,
                    "home_score": home_score,
                    "away_score": away_score,
                    "status_text": status_text,
                    "match_dt_kst": match_dt_kst,
                    "match_dt_full_kst": match_dt_full_kst,
                    "is_live": is_live,
                    "ai_home_pct": model["ai_home_pct"],
                    "ai_draw_pct": model["ai_draw_pct"],
                    "ai_away_pct": model["ai_away_pct"],
                    "value_badge": model["value_badge"],
                    "quick_stats": model["quick_stats"],
                })
            except Exception:
                continue
    return collected


def _load_rankings_fallback(limit: int = 20) -> List[Dict[str, Any]]:
    _init_pro_lab_db()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, match_key, sport, league, matchup, schedule_venue, votes, buzz_score
            FROM global_match_rankings
            ORDER BY votes DESC, buzz_score DESC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    fallback_items: List[Dict[str, Any]] = []
    for r in rows:
        home, away = _split_teams(r["matchup"])
        mkey = r["match_key"] or f"rank_{r['id']}"
        model = _build_match_model(
            match_key=mkey,
            sport=r["sport"] or "축구",
            league=r["league"] or "GLOBAL",
            home=home,
            away=away,
            votes=int(r["votes"] or 100),
            buzz_score=float(r["buzz_score"] or 80.0),
        )
        match_dt_kst, match_dt_full_kst = _fallback_match_dt_kst(
            r["schedule_venue"] or "",
            r["sport"] or "축구",
            r["league"] or "GLOBAL",
            mkey,
        )
        fallback_items.append({
            "match_id": mkey,
            "league": r["league"] or "GLOBAL",
            "home": home,
            "away": away,
            "home_score": "-",
            "away_score": "-",
            "status_text": r["schedule_venue"] or "매치 프리뷰",
            "match_dt_kst": match_dt_kst,
            "match_dt_full_kst": match_dt_full_kst,
            "is_live": False,
            "ai_home_pct": model["ai_home_pct"],
            "ai_draw_pct": model["ai_draw_pct"],
            "ai_away_pct": model["ai_away_pct"],
            "value_badge": model["value_badge"],
            "quick_stats": model["quick_stats"],
        })
    return fallback_items


def _refresh_ticker_cache() -> None:
    espn_items = _fetch_espn_scoreboard()
    merged: List[Dict[str, Any]] = list(espn_items)
    seen_pairs = {(it["home"], it["away"]) for it in merged}

    if len(merged) < 12:
        for fb in _load_rankings_fallback(limit=20):
            if (fb["home"], fb["away"]) not in seen_pairs:
                merged.append(fb)
                seen_pairs.add((fb["home"], fb["away"]))
            if len(merged) >= 20:
                break

    merged = merged[:20]
    with _TICKER_LOCK:
        _TICKER_CACHE["items"] = merged
        _TICKER_CACHE["updated_at_kst"] = _now_kst_str()
        _TICKER_CACHE["last_fetch_ts"] = time.time()


def _ticker_worker_loop() -> None:
    while True:
        try:
            _refresh_ticker_cache()
        except Exception:
            pass
        time.sleep(60)


def _ensure_ticker_worker() -> None:
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        _WORKER_STARTED = True
        t = threading.Thread(target=_ticker_worker_loop, daemon=True)
        t.start()


@router.get("/live-ticker")
def get_live_ticker() -> Dict[str, Any]:
    _init_pro_lab_db()
    _ensure_ticker_worker()
    with _TICKER_LOCK:
        has_items = len(_TICKER_CACHE["items"]) > 0
        age = time.time() - float(_TICKER_CACHE["last_fetch_ts"] or 0.0)
    if not has_items or age > 120.0:
        _refresh_ticker_cache()
    with _TICKER_LOCK:
        return {
            "count": len(_TICKER_CACHE["items"]),
            "updated_at_kst": _TICKER_CACHE["updated_at_kst"] or _now_kst_str(),
            "server_now_kst": _now_kst_display(),
            "items": list(_TICKER_CACHE["items"]),
        }


@router.get("/match-insights")
def get_match_insights() -> Dict[str, Any]:
    _init_pro_lab_db()
    conn = _get_conn()
    try:
        rankings = conn.execute(
            """
            SELECT id, match_key, sport, league, league_color, matchup,
                   schedule_venue, votes, buzz_score, rank_change
            FROM global_match_rankings
            ORDER BY votes DESC, buzz_score DESC, id ASC
            LIMIT 20
            """
        ).fetchall()

        pred_map: Dict[str, Dict[str, int]] = {}
        try:
            pred_rows = conn.execute(
                """
                SELECT match_key, side, COUNT(*) AS cnt
                FROM match_predictions
                GROUP BY match_key, side
                """
            ).fetchall()
            for pr in pred_rows:
                mk = str(pr["match_key"] or "")
                side = str(pr["side"] or "").lower()
                bucket = pred_map.setdefault(mk, {"home": 0, "draw": 0, "away": 0})
                if side in ("home", "1", "h"):
                    bucket["home"] += int(pr["cnt"])
                elif side in ("draw", "x", "d"):
                    bucket["draw"] += int(pr["cnt"])
                elif side in ("away", "2", "a"):
                    bucket["away"] += int(pr["cnt"])
        except Exception:
            pred_map = {}
    finally:
        conn.close()

    insights: List[Dict[str, Any]] = []
    for idx, r in enumerate(rankings, start=1):
        home, away = _split_teams(r["matchup"])
        mkey = str(r["match_key"] or f"rank_{r['id']}")
        pcounts = pred_map.get(mkey, {"home": 0, "draw": 0, "away": 0})
        model = _build_match_model(
            match_key=mkey,
            sport=r["sport"] or "축구",
            league=r["league"] or "GLOBAL",
            home=home,
            away=away,
            votes=int(r["votes"] or 100),
            buzz_score=float(r["buzz_score"] or 80.0),
            pred_home=pcounts["home"],
            pred_draw=pcounts["draw"],
            pred_away=pcounts["away"],
        )
        insights.append({
            "rank": idx,
            "match_key": mkey,
            "sport": r["sport"],
            "league": r["league"],
            "league_color": r["league_color"],
            "matchup": r["matchup"],
            "home": home,
            "away": away,
            "schedule_venue": r["schedule_venue"],
            "votes": int(r["votes"] or 0),
            "buzz_score": float(r["buzz_score"] or 0.0),
            "ai_home_pct": model["ai_home_pct"],
            "ai_draw_pct": model["ai_draw_pct"],
            "ai_away_pct": model["ai_away_pct"],
            "mkt_home_pct": model["mkt_home_pct"],
            "mkt_draw_pct": model["mkt_draw_pct"],
            "mkt_away_pct": model["mkt_away_pct"],
            "edge_pct": model["edge_pct"],
            "value_badge": model["value_badge"],
            "injury_note": model["injury_note"],
            "risk_note": model["risk_note"],
            "quick_stats": model["quick_stats"],
            # AI·시장 확률과 부상·리스크 메모는 경기 키 해시로 만든 추정 예시값이다(실제 모델/시장 데이터 아님).
            "is_sample": True,
        })

    return {
        "count": len(insights),
        "updated_at_kst": _now_kst_str(),
        "items": insights,
        "is_sample": True,
        "sample_note": "AI/시장 확률·부상 메모는 실제 데이터가 아닌 추정 예시값입니다.",
    }


@router.get("/ai-scorecard")
def get_ai_scorecard() -> Dict[str, Any]:
    _init_pro_lab_db()
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

    records = [dict(r) for r in rows]
    for r in records:
        # No real prediction model exists yet: rows are either the initial seed
        # examples or picks the autonomous grader made from a match-key hash.
        is_seed = (str(r.get("match_date") or ""), str(r.get("matchup") or "")) in _SEED_SCORECARD_KEYS
        r["is_sample"] = True
        r["sample_kind"] = "seed" if is_seed else "hash_pick"
    real_records = [r for r in records if not r["is_sample"]]
    real_hits = sum(1 for r in real_records if int(r["hit"]) == 1)
    total = len(records)
    hits = sum(1 for r in records if int(r["hit"]) == 1)
    accuracy_pct = round((hits * 100.0 / total), 1) if total else 0.0
    total_pnl = sum(float(r["unit_pnl"]) for r in records)
    roi_pct = round((total_pnl * 100.0 / total), 1) if total else 0.0
    avg_brier = round(sum(float(r["brier_err"]) for r in records) / total, 3) if total else 0.0

    by_sport: Dict[str, Dict[str, Any]] = {}
    for r in records:
        sp = r["sport"]
        entry = by_sport.setdefault(sp, {"sport": sp, "total": 0, "hits": 0, "unit_pnl": 0.0})
        entry["total"] += 1
        entry["hits"] += 1 if int(r["hit"]) == 1 else 0
        entry["unit_pnl"] = round(entry["unit_pnl"] + float(r["unit_pnl"]), 2)

    sport_summary = []
    for sp, info in by_sport.items():
        info["accuracy_pct"] = round(info["hits"] * 100.0 / info["total"], 1) if info["total"] else 0.0
        sport_summary.append(info)

    return {
        "total_matches": total,
        "hits": hits,
        "misses": total - hits,
        "accuracy_pct": accuracy_pct,
        "roi_pct": roi_pct,
        "total_unit_pnl": round(total_pnl, 2),
        "avg_brier": avg_brier,
        "brier_note": f"평균 브라이어 오차 {avg_brier:.3f} (0.250 이하 양호 보정 구간)",
        "sport_summary": sport_summary,
        "records": records,
        # 위 합계에는 예시(시드) 기록이 포함될 수 있다. 실제 채점 기록만의 수치는 real_* 키를 본다.
        "includes_sample_records": any(r["is_sample"] for r in records),
        "sample_records_count": sum(1 for r in records if r["is_sample"]),
        "real_total_matches": len(real_records),
        "real_hits": real_hits,
        "real_accuracy_pct": round(real_hits * 100.0 / len(real_records), 1) if real_records else None,
        "verified_records_available": bool(real_records),
        "sample_note": "검증된 AI 예측 기록이 아직 없습니다. 아래 기록과 적중률·수익률은 예시이며 실제 실적이 아닙니다.",
    }


class TacticCreateRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=120)
    sport: str = Field(..., min_length=1, max_length=40)
    formation: str = Field(..., min_length=1, max_length=40)
    author: str = Field(default="SportIQ_Member", max_length=60)
    memo: str = Field(default="", max_length=1000)
    players: List[Dict[str, Any]] = Field(default_factory=list)


def _fetch_tactics_list(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, title, sport, formation, author, memo, players_json, likes, created_at
        FROM saved_tactics
        ORDER BY id DESC
        """
    ).fetchall()
    result: List[Dict[str, Any]] = []
    for r in rows:
        try:
            players = json.loads(r["players_json"] or "[]")
        except Exception:
            players = []
        result.append({
            "id": r["id"],
            "title": r["title"],
            "sport": r["sport"],
            "formation": r["formation"],
            "author": r["author"],
            "memo": r["memo"],
            "players": players,
            "likes": int(r["likes"] or 0),
            "created_at": r["created_at"],
        })
    return result


@router.get("/tactics")
def get_tactics() -> Dict[str, Any]:
    _init_pro_lab_db()
    conn = _get_conn()
    try:
        items = _fetch_tactics_list(conn)
    finally:
        conn.close()
    return {
        "count": len(items),
        "items": items,
    }


@router.post("/tactics")
def create_tactic(payload: TacticCreateRequest) -> Dict[str, Any]:
    _init_pro_lab_db()
    title = payload.title.strip().replace("<", "").replace(">", "")
    if not title:
        raise HTTPException(status_code=400, detail="전술 제목을 입력해 주세요.")

    players_json = json.dumps(payload.players, ensure_ascii=False)
    created_at = _now_kst_str()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO saved_tactics (
                title, sport, formation, author, memo, players_json, likes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                title,
                payload.sport.strip(),
                payload.formation.strip(),
               (payload.author or "SportIQ_Member").strip().replace("<", "").replace(">", ""), 
                payload.memo.strip().replace("<", "").replace(">", ""),
                players_json,
                created_at,
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
        try:
            _analyst._backup_tables_to_pg("saved_tactics")
        except Exception:
            pass
        items = _fetch_tactics_list(conn)
    finally:
        conn.close()

    return {
        "status": "ok",
        "saved_id": new_id,
        "count": len(items),
        "items": items,
    }


# 조회(GET) 요청이 처음 들어올 때 DB를 쓰지 않도록, 표 생성·시드를 모듈 로드 시 한 번 실행한다.
try:
    _init_pro_lab_db()
except Exception:
    pass
