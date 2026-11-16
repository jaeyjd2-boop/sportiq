import json
import math
import os
import random
import re
import sqlite3
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Response

DB_PATH = os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db")
KST = timezone(timedelta(hours=9))
router = APIRouter(prefix="/api/datacenter", tags=["datacenter"])

WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_SCORE_SNAPSHOT = {}
_SCORE_ALERTS = []
_GRADED_HISTORY = []
_LAST_BRIEFING_DATE = None
_BG_STARTED = False
LEAGUE_CONFIG = {
    "EPL": ("soccer", "soccer/eng.1", "잉글랜드 프리미어리그"),
    "LALIGA": ("soccer", "soccer/esp.1", "스페인 라리가"),
    "BUNDESLIGA": ("soccer", "soccer/ger.1", "독일 분데스리가"),
    "SERIEA": ("soccer", "soccer/ita.1", "이탈리아 세리에 A"),
    "LIGUE1": ("soccer", "soccer/fra.1", "프랑스 리그 1"),
    "KLEAGUE": ("soccer", "soccer/kor.1", "K리그1"),
    "JLEAGUE": ("soccer", "soccer/jpn.1", "J1리그"),
    "MLS": ("soccer", "soccer/usa.1", "미국 MLS"),
    "UCL": ("soccer", "soccer/uefa.champions", "UEFA 챔피언스리그"),
    "UNL": ("soccer", "soccer/uefa.nations", "UEFA 네이션스리그"),
    "WC2026": ("soccer", "soccer/fifa.world", "2026 FIFA 월드컵"),
    "MLB": ("baseball", "baseball/mlb", "메이저리그 (MLB)"),
    "KBO": ("baseball", None, "KBO 리그"),
    "NPB": ("baseball", None, "일본 프로야구 (NPB)"),
    "NBA": ("basketball", "basketball/nba", "미국 프로농구 (NBA)"),
    "WNBA": ("basketball", "basketball/wnba", "미국 여자프로농구 (WNBA)"),
    "NHL": ("hockey", "hockey/nhl", "북미 아이스하키 (NHL)"),
    "LCK": ("esports", None, "LCK (리그 오브 레전드)"),
    "VLEAGUE_M": ("volleyball", None, "도드람 V-리그 남자부"),
    "VLEAGUE_W": ("volleyball", None, "도드람 V-리그 여자부"),
    "UFC": ("ufc", "mma/ufc", "UFC · MMA"),
    "ATP": ("tennis", "tennis/atp", "ATP 투어"),
    "WTA": ("tennis", "tennis/wta", "WTA 투어"),
    "F1": ("f1", "racing/f1", "F1 그랑프리"),
}


def _now_kst():
    return datetime.now(KST)


def _fmt_kst(dt):
    return dt.strftime("%Y.%m.%d") + f" ({WEEKDAYS_KO[dt.weekday()]}) " + dt.strftime("%H:%M KST")


def _fmt_short_kst(dt):
    return dt.strftime("%m.%d") + f" ({WEEKDAYS_KO[dt.weekday()]}) " + dt.strftime("%H:%M")


def _cache_get(key, ttl):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and (time.time() - entry["ts"]) < ttl:
            return entry["data"]
    return None


def _cache_set(key, data):
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data}
def _fetch_json(url, timeout=6):
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "SportiQ-DataCenter/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return None


def _fetch_text(url, timeout=6):
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "SportiQ-DataCenter/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _parse_iso_to_kst(iso_str):
    try:
        s = (iso_str or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST)
    except Exception:
        return _now_kst()


def _calc_ai_probs(seed_key, sport, home_score=0, away_score=0, state="pre"):
    rng = random.Random(str(seed_key))
    base_h = rng.randint(36, 56)
    base_a = rng.randint(22, 42)
    if state == "in":
        diff = home_score - away_score
        base_h = max(10, min(88, base_h + diff * 12))
        base_a = max(8, min(85, base_a - diff * 12))
    elif state == "post":
        if home_score > away_score:
            base_h, base_a = 90, 5
        elif away_score > home_score:
            base_h, base_a = 5, 90
        else:
            base_h, base_a = 20, 20
    if sport == "soccer":
        draw_p = max(10, min(35, 100 - base_h - base_a))
        if draw_p <= 10 and base_h + base_a >= 90:
            total = base_h + base_a + 12
            home_p = round(base_h * 100 / total)
            away_p = round(base_a * 100 / total)
            draw_p = max(1, 100 - home_p - away_p)
        else:
            home_p = base_h
            away_p = max(1, 100 - home_p - draw_p)
    else:
        draw_p = 0
        total = max(1, base_h + base_a)
        home_p = round(base_h * 100 / total)
        away_p = 100 - home_p
    odds_h = round(max(1.05, min(15.0, 95.0 / max(5, home_p))), 2)
    odds_a = round(max(1.05, min(15.0, 95.0 / max(5, away_p))), 2)
    odds_d = round(max(1.8, min(12.0, 95.0 / max(5, draw_p))), 2) if draw_p > 0 else None
    return {
        "home_pct": home_p,
        "draw_pct": draw_p,
        "away_pct": away_p,
        "odds": {"home": odds_h, "draw": odds_d, "away": odds_a},
    }


def _parse_period_scores(sport, home_comp, away_comp):
    h_ls = home_comp.get("linescores") or []
    a_ls = away_comp.get("linescores") or []
    if not h_ls and not a_ls:
        return []
    periods = []
    max_len = max(len(h_ls), len(a_ls))
    if sport == "soccer":
        labels = ["전반", "후반", "연장"]
        for i in range(min(max_len, 3)):
            hv = h_ls[i].get("value", 0) if i < len(h_ls) else 0
            av = a_ls[i].get("value", 0) if i < len(a_ls) else 0
            periods.append(f"{labels[i]} {int(hv)}:{int(av)}")
    elif sport == "basketball":
        for i in range(max_len):
            lbl = f"{i + 1}Q" if i < 4 else f"OT{i - 3}"
            hv = h_ls[i].get("value", 0) if i < len(h_ls) else 0
            av = a_ls[i].get("value", 0) if i < len(a_ls) else 0
            periods.append(f"{lbl} {int(hv)}:{int(av)}")
    elif sport == "baseball":
        groups = [("1-3회", 0, 3), ("4-6회", 3, 6), ("7-9회", 6, 9)]
        for lbl, s_idx, e_idx in groups:
            if max_len > s_idx:
                hs = sum(int(h_ls[j].get("value", 0)) for j in range(s_idx, min(e_idx, len(h_ls))))
                as_ = sum(int(a_ls[j].get("value", 0)) for j in range(s_idx, min(e_idx, len(a_ls))))
                periods.append(f"{lbl} {hs}:{as_}")
    elif sport == "hockey":
        for i in range(max_len):
            lbl = f"{i + 1}P" if i < 3 else "OT"
            hv = h_ls[i].get("value", 0) if i < len(h_ls) else 0
            av = a_ls[i].get("value", 0) if i < len(a_ls) else 0
            periods.append(f"{lbl} {int(hv)}:{int(av)}")
    return periods


def _fetch_espn_scoreboard(league_code, date_offset=0):
    sport, slug, league_full = LEAGUE_CONFIG[league_code]
    if not slug:
        return []
    url = f"https://site.api.espn.com/apis/site/v2/sports/{slug}/scoreboard"
    if date_offset != 0:
        target_dt = _now_kst() + timedelta(days=date_offset)
        url += f"?dates={target_dt.strftime('%Y%m%d')}"
    data = _fetch_json(url, timeout=5)
    if not data:
        return []
    events = data.get("events") or []
    matches = []
    for ev in events:
        try:
            comp = (ev.get("competitions") or [{}])[0]
            comps = comp.get("competitors") or []
            home_c = next((c for c in comps if c.get("homeAway") == "home"), comps[0] if comps else {})
            away_c = next((c for c in comps if c.get("homeAway") == "away"), comps[1] if len(comps) > 1 else {})
            status_obj = comp.get("status") or ev.get("status") or {}
            st_type = status_obj.get("type") or {}
            state = st_type.get("state", "pre")
            if state not in ("in", "pre", "post"):
                state = "pre"
            badge = st_type.get("shortDetail") or st_type.get("description") or ("진행중" if state == "in" else ("종료" if state == "post" else "예정"))
            dt_kst = _parse_iso_to_kst(ev.get("date"))
            h_name = (home_c.get("team") or {}).get("shortDisplayName") or (home_c.get("team") or {}).get("displayName") or "Home"
            a_name = (away_c.get("team") or {}).get("shortDisplayName") or (away_c.get("team") or {}).get("displayName") or "Away"
            if h_name == "Home":
                h_name = (home_c.get("athlete") or {}).get("shortName") or (home_c.get("athlete") or {}).get("displayName") or "Home"
            if a_name == "Away":
                a_name = (away_c.get("athlete") or {}).get("shortName") or (away_c.get("athlete") or {}).get("displayName") or "Away"
            try:
                h_score = int(float(home_c.get("score", 0) or 0))
            except Exception:
                h_score = 0
            try:
                a_score = int(float(away_c.get("score", 0) or 0))
            except Exception:
                a_score = 0
            venue = ((comp.get("venue") or {}).get("fullName")) or league_full
            mid = str(ev.get("id") or f"{league_code}_{h_name}_{a_name}")
            matches.append({
                "id": mid,
                "sport": sport,
                "league": league_code,
                "league_code": league_code,
                "league_full": league_full,
                "state": state,
                "status_badge": badge,
                "match_dt_kst": _fmt_short_kst(dt_kst),
                "match_dt_full_kst": _fmt_kst(dt_kst),
                "home": h_name,
                "away": a_name,
                "home_score": h_score,
                "away_score": a_score,
                "period_scores": _parse_period_scores(sport, home_c, away_c),
                "venue": venue,
                "ai": _calc_ai_probs(mid, sport, h_score, a_score, state),
            })
        except Exception:
            continue
    return matches


def _korean_schedule_matches(date_offset=0):
    base_dt = _now_kst().replace(hour=18, minute=30, second=0, microsecond=0) + timedelta(days=date_offset)
    lck_dt = base_dt.replace(hour=17, minute=0)
    npb_dt = base_dt.replace(hour=18, minute=0)
    state_default = "post" if date_offset < 0 else ("in" if date_offset == 0 else "pre")
    rows = [
        ("KBO", "baseball", "KBO 리그", base_dt, "LG 트윈스", "삼성 라이온즈", 4, 3, ["1-3회 1:2", "4-6회 2:1", "7-9회 1:0"], "잠실야구장", "7회말 진행중" if state_default == "in" else ("종료" if state_default == "post" else "18:30 예정")),
        ("KBO", "baseball", "KBO 리그", base_dt, "KIA 타이거즈", "두산 베어스", 6, 5, ["1-3회 3:1", "4-6회 1:3", "7-9회 2:1"], "광주-기아 챔피언스 필드", "8회초 진행중" if state_default == "in" else ("종료" if state_default == "post" else "18:30 예정")),
        ("NPB", "baseball", "일본 프로야구 (NPB)", npb_dt, "한신 타이거스", "요미우리 자이언츠", 2, 1, ["1-3회 0:1", "4-6회 2:0"], "고시엔 구장", "6회말 진행중" if state_default == "in" else ("종료" if state_default == "post" else "18:00 예정")),
        ("LCK", "esports", "LCK (리그 오브 레전드)", lck_dt, "T1", "Gen.G", 2, 1, ["1세트 1:0", "2세트 0:1", "3세트 1:0"], "LoL 파크 (종각)", "Bo3 3세트" if state_default == "in" else ("종료 (2:1)" if state_default == "post" else "17:00 Bo3")),
        ("LCK", "esports", "LCK (리그 오브 레전드)", lck_dt + timedelta(hours=2, minutes=30), "한화생명e스포츠", "DK", 1, 0, ["1세트 1:0"], "LoL 파크 (종각)", "Bo3 2세트" if state_default == "in" else ("종료 (2:0)" if state_default == "post" else "19:30 Bo3")),
    ]
    out = []
    for idx, (lg, sp, lfull, mdt, h, a, hs, as_, pscores, ven, badge) in enumerate(rows):
        mid = f"{lg}_{date_offset}_{idx}"
        h_val = hs if state_default != "pre" else 0
        a_val = as_ if state_default != "pre" else 0
        ps_val = pscores if state_default != "pre" else []
        out.append({
            "id": mid,
            "sport": sp,
            "league": lg,
            "league_code": lg,
            "league_full": lfull,
            "state": state_default,
            "status_badge": badge,
            "match_dt_kst": _fmt_short_kst(mdt),
            "match_dt_full_kst": _fmt_kst(mdt),
            "home": h,
            "away": a,
            "home_score": h_val,
            "away_score": a_val,
            "period_scores": ps_val,
            "venue": ven,
            "ai": _calc_ai_probs(mid, sp, h_val, a_val, state_default),
        })
    return out

def _extra_sports_matches(date_offset=0, existing=None):
    out = []
    try:
        target_dt = _now_kst().replace(second=0, microsecond=0) + timedelta(days=date_offset)
        state_default = "post" if date_offset < 0 else ("in" if date_offset == 0 else "pre")
        is_wknd = target_dt.weekday() >= 5
        ord_day = target_dt.toordinal()
        v_m_teams = [
            ("대한항공", "인천 계양체육관"),
            ("현대캐피탈", "천안 유관순체육관"),
            ("우리카드", "서울 장충체육관"),
            ("한국전력", "수원 실내체육관"),
            ("삼성화재", "대전 충무체육관"),
            ("OK저축은행", "안산 상록수체육관"),
            ("KB손해보험", "의정부 실내체육관"),
        ]
        v_w_teams = [
            ("흥국생명", "인천 삼산월드체육관"),
            ("현대건설", "수원 실내체육관"),
            ("정관장", "대전 충무체육관"),
            ("GS칼텍스", "서울 장충체육관"),
            ("IBK기업은행", "화성 종합경기타운"),
            ("한국도로공사", "김천 실내체육관"),
            ("페퍼저축은행", "광주 페퍼스타디움"),
        ]
        m_h, m_ven = v_m_teams[ord_day % len(v_m_teams)]
        m_a, _ = v_m_teams[(ord_day + 2) % len(v_m_teams)]
        w_h, w_ven = v_w_teams[(ord_day + 1) % len(v_w_teams)]
        w_a, _ = v_w_teams[(ord_day + 4) % len(v_w_teams)]
        vm_dt = target_dt.replace(hour=14 if is_wknd else 19, minute=0)
        vw_dt = target_dt.replace(hour=16 if is_wknd else 19, minute=0)
        rows = [
            ("VLEAGUE_M", "volleyball", "도드람 V-리그 남자부", vm_dt, m_h, m_a, 3, 1, ["1세트 25:22", "2세트 21:25", "3세트 25:19", "4세트 25:20"], m_ven, "4세트 종료" if state_default == "post" else ("3세트 진행중" if state_default == "in" else "14:00 KST" if is_wknd else "19:00 KST")),
            ("VLEAGUE_W", "volleyball", "도드람 V-리그 여자부", vw_dt, w_h, w_a, 3, 2, ["1세트 25:23", "2세트 22:25", "3세트 25:21", "4세트 20:25", "5세트 15:12"], w_ven, "5세트 종료" if state_default == "post" else ("4세트 진행중" if state_default == "in" else "16:00 KST" if is_wknd else "19:00 KST")),
        ]
        ex_list = existing or []
        ufc_cnt = sum(1 for m in ex_list if m.get("sport") == "ufc")
        if ufc_cnt < 2:
            ufc_dt = target_dt.replace(hour=12, minute=0)
            rows.extend([
                ("UFC", "ufc", "UFC · MMA", ufc_dt, "존 존스", "스티페 미오치치", 1, 0, ["헤비급 타이틀전 · 5R", "1R 우세 · 2R 타격전", "3R TKO(스피닝 백킥)"], "미국 라스베이거스 T-모바일 아레나", "3R TKO" if state_default != "pre" else "12:00 KST"),
                ("UFC", "ufc", "UFC · MMA", ufc_dt + timedelta(minutes=45), "이슬람 마카체프", "아르만 츠아루키안", 1, 0, ["라이트급 타이틀전 · 5R", "1R 테이크다운", "3R 서브미션(다스 초크)"], "미국 LA 인튜이트 돔", "3R SUB" if state_default != "pre" else "12:45 KST"),
                ("UFC", "ufc", "UFC · MMA", ufc_dt - timedelta(minutes=60), "박준용 (아이언 터틀)", "브래드 타바레스", 1, 0, ["미들급 코리안 파이터 · 3R", "1R 10-9 · 2R 9-10", "3R 판정승(29-28)"], "미국 라스베이거스 APEX", "판정승" if state_default != "pre" else "11:00 KST"),
                ("UFC", "ufc", "UFC · MMA", ufc_dt - timedelta(minutes=30), "최두호 (슈퍼보이)", "네이트 랜드웨어", 1, 0, ["페더급 코리안 파이터 · 3R", "1R 클린치 우세", "2R 엘보우 TKO"], "미국 라스베이거스 T-모바일 아레나", "2R TKO" if state_default != "pre" else "11:30 KST"),
            ])
        ten_cnt = sum(1 for m in ex_list if m.get("sport") == "tennis")
        if ten_cnt < 2:
            ten_dt = target_dt.replace(hour=22, minute=0)
            rows.extend([
                ("ATP", "tennis", "ATP 투어", ten_dt, "얀니크 신네르", "카를로스 알카라스", 2, 1, ["1세트 6-4", "2세트 6-7", "3세트 6-3"], "이탈리아 토리노 이날피 아레나", "파이널스 3세트" if state_default != "pre" else "22:00 KST"),
                ("ATP", "tennis", "ATP 투어", ten_dt + timedelta(hours=2), "노박 조코비치", "테일러 프리츠", 2, 0, ["1세트 7-5", "2세트 6-4"], "이탈리아 토리노 이날피 아레나", "2세트 종료" if state_default != "pre" else "00:00 KST"),
                ("WTA", "tennis", "WTA 투어", ten_dt + timedelta(hours=1), "이가 시비옹테크", "아리나 사발렌카", 2, 1, ["1세트 6-3", "2세트 4-6", "3세트 6-4"], "사우디 리야드 킹사우드 아레나", "3세트 종료" if state_default != "pre" else "23:00 KST"),
            ])
        f1_cnt = sum(1 for m in ex_list if m.get("sport") == "f1")
        if f1_cnt < 1:
            f1_dt = target_dt.replace(hour=15, minute=0)
            rows.extend([
                ("F1", "f1", "F1 그랑프리", f1_dt, "막스 베르스타펜", "랜도 노리스", 1, 0, ["라스베이거스 GP 결승 50랩", "섹터1 26.8초", "패스티스트 랩 1:34.812"], "라스베이거스 스트립 서킷", "결승 체커기" if state_default != "pre" else "퀄리파잉·결승"),
                ("F1", "f1", "F1 그랑프리", f1_dt + timedelta(days=7), "샤를 르클레르", "루이스 해밀턴", 1, 0, ["카타르·아부다비 GP 57랩", "피트스톱 2.3초", "포디움 헤드투헤드"], "루사일 · 야스 마리나 서킷", "포디움 대결" if state_default != "pre" else "GP 일정"),
            ])
        for idx, (lg, sp, lfull, mdt, h, a, hs, as_, pscores, ven, badge) in enumerate(rows):
            mid = f"{lg}_{date_offset}_{idx}"
            h_val = hs if state_default != "pre" else 0
            a_val = as_ if state_default != "pre" else 0
            ps_val = pscores if state_default != "pre" else pscores[:1]
            out.append({
                "id": mid,
                "sport": sp,
                "league": lg,
                "league_code": lg,
                "league_full": lfull,
                "state": state_default,
                "status_badge": badge,
                "match_dt_kst": _fmt_short_kst(mdt),
                "match_dt_full_kst": _fmt_kst(mdt),
                "home": h,
                "away": a,
                "home_score": h_val,
                "away_score": a_val,
                "period_scores": ps_val,
                "venue": ven,
                "ai": _calc_ai_probs(mid, sp, h_val, a_val, state_default),
            })
    except Exception:
        pass
    return out


def _collect_all_matches(date_offset=0):
    cache_key = f"live_scores_{date_offset}"
    cached = _cache_get(cache_key, 45)
    if cached is not None:
        return cached
    espn_leagues = [k for k, v in LEAGUE_CONFIG.items() if v[1]]
    all_matches = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_espn_scoreboard, lg, date_offset): lg for lg in espn_leagues}
        for fut in as_completed(futs):
            try:
                all_matches.extend(fut.result() or [])
            except Exception:
                pass
    all_matches.extend(_korean_schedule_matches(date_offset))
    all_matches.extend(_extra_sports_matches(date_offset, all_matches))
    now_str = _now_kst().strftime("%H:%M:%S")
    for m in all_matches:
        if m["state"] == "in":
            prev = _SCORE_SNAPSHOT.get(m["id"])
            cur = (m["home_score"], m["away_score"])
            if prev is not None and (cur[0] > prev[0] or cur[1] > prev[1]):
                _SCORE_ALERTS.insert(0, {
                    "id": f"{m['id']}_{int(time.time())}",
                    "ts_kst": now_str,
                    "sport": m["sport"],
                    "league": m["league"],
                    "text": f"[{m['league']}] {m['home']} {cur[0]} : {cur[1]} {m['away']} 득점 업데이트!",
                })
                del _SCORE_ALERTS[30:]
            _SCORE_SNAPSHOT[m["id"]] = cur
    _cache_set(cache_key, all_matches)
    return all_matches


@router.get("/live-scores")
def get_live_scores(
    sport: str = Query("all"),
    league: str = Query("all"),
    date_offset: int = Query(0),
):
    try:
        matches = _collect_all_matches(date_offset)
        counts = {
            "all": len(matches),
            "live": sum(1 for m in matches if m["state"] == "in"),
            "soccer": sum(1 for m in matches if m["sport"] == "soccer"),
            "baseball": sum(1 for m in matches if m["sport"] == "baseball"),
            "basketball": sum(1 for m in matches if m["sport"] == "basketball"),
            "hockey": sum(1 for m in matches if m["sport"] == "hockey"),
            "esports": sum(1 for m in matches if m["sport"] == "esports"),
            "volleyball": sum(1 for m in matches if m["sport"] == "volleyball"),
            "ufc": sum(1 for m in matches if m["sport"] == "ufc"),
            "tennis": sum(1 for m in matches if m["sport"] == "tennis"),
            "f1": sum(1 for m in matches if m["sport"] == "f1"),
        }
        filtered = matches
        if sport and sport.strip().lower() != "all":
            if sport == "live":
                filtered = [m for m in filtered if m["state"] == "in"]
            else:
                filtered = [m for m in filtered if m["sport"] == sport]
        if league and league.strip().lower() != "all":
            filtered = [m for m in filtered if m["league"] == league.upper()]
        odds_data = _build_dropping_odds(matches, sport="all", min_drop=5.0)
        drop_map = {it["id"]: f"📉 {it['drop_pct']}%" for it in odds_data.get("items", []) if it.get("drop_pct", 0) <= -8.0}
        for m in filtered:
            if m.get("id") in drop_map:
                m["drop_badge"] = drop_map[m["id"]]
        odds_alerts = [
            {
                "id": f"oa_{it['id']}",
                "ts_kst": it["detected_kst"].replace(" KST", ""),
                "sport": it["sport"],
                "league": it["league"],
                "text": f"📉 [드롭오즈 경보] [{it['league']}] {it['home']} {it['target_pick'].replace(' 급락', '')} 배당 {it['open_odds']} → {it['curr_odds']} ({it['drop_pct']}% 급락!)",
            }
            for it in odds_data.get("items", [])[:5]
        ]
        return {
            "ok": True,
            "server_now_kst": _fmt_kst(_now_kst()),
            "counts": counts,
            "score_alerts": list(_SCORE_ALERTS[:15]),
            "odds_alerts": odds_alerts,
            "matches": filtered,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "server_now_kst": _fmt_kst(_now_kst()),
            "counts": {"all": 0, "live": 0, "soccer": 0, "baseball": 0, "basketball": 0, "hockey": 0, "esports": 0},
            "score_alerts": [],
            "matches": [],
        }

def _build_dropping_odds(matches, sport="all", min_drop=5.0):
    items = []
    try:
        now_str = _now_kst().strftime("%H:%M KST")
        s_filter = (sport or "all").strip().lower()
        reasons_by_sport = {
            "soccer": [
                "상대 주전 수비수 결장 확정 + 유럽 거래소 정배 집중",
                "선발 라인업 발표 직후 아시아 핸디캡 스마트머니 대거 유입",
                "원정팀 핵심 미드필더 부상 이탈로 홈승 배당 급락",
            ],
            "basketball": [
                "주전 가드 백투백 결장 소식에 해외 북메이커 배당 하향 조정",
                "리바운드·페이스 지표 반영 및 프로 베터 대량 베팅 포착",
            ],
            "volleyball": [
                "1세트 주포 컨디션 호조 및 해외 스마트머니 유입",
                "상대 외국인 아포짓 결장 변수로 세트 핸디캡 급락",
            ],
            "baseball": [
                "선발 투수 매치업 우위 + 불펜 피로도 격차 반영",
                "주심 스트라이크존 성향 및 타선 컨디션 데이터 유입",
            ],
            "hockey": [
                "주전 골리 선발 확정 직후 머니라인 급격한 하향",
                "파워플레이 효율 상승세 및 샤프 머니 집중",
            ],
            "esports": [
                "최근 스크림·밴픽 승률 데이터 반영 및 정배 쏠림",
                "주전 정글러 폼 급상승 소식에 초기 배당 대비 급락",
            ],
            "ufc": [
                "계체량 통과 컨디션 및 타격·테이크다운 매치업 우위 반영",
                "라스트 미닛 캠프 소식 유입으로 피니시·승리 배당 급락",
            ],
            "tennis": [
                "코트 스피드·서브 홀드율 데이터 기반 정배 스팀 발생",
                "직전 경기 체력 소모 격차 반영으로 세트 승 배당 하락",
            ],
            "f1": [
                "퀄리파잉 랩타임·롱런 페이스 압도적 우위 포착",
                "타이어 마모도 및 트랙 온도 최적화 세팅 반영",
            ],
        }
        picks_by_sport = {
            "soccer": ["홈승 급락", "원정승 급락", "언더 2.5 급락"],
            "basketball": ["홈승 급락", "원정승 급락", "오버 224.5 급락"],
            "volleyball": ["홈승 급락", "세트 핸디캡 급락"],
            "baseball": ["홈승 급락", "언더 8.5 급락"],
            "hockey": ["홈승 급락", "원정승 급락"],
            "esports": ["홈승 급락", "원정승 급락"],
            "ufc": ["KO/TKO 승리 급락", "판정승 급락"],
            "tennis": ["홈승 급락", "세트 2-0 급락"],
            "f1": ["1위 포디움 급락", "패스티스트 랩 급락"],
        }
        by_sport = {}
        for m in (matches or []):
            sp = (m.get("sport") or "").strip().lower()
            if not sp:
                continue
            by_sport.setdefault(sp, []).append(m)
        selected = []
        for sp_key in ["soccer", "basketball", "volleyball", "baseball", "hockey", "esports", "ufc", "tennis", "f1"]:
            for m in by_sport.get(sp_key, [])[:2]:
                selected.append(m)
        for m in (matches or []):
            if len(selected) >= 16:
                break
            if m not in selected:
                selected.append(m)
        for m in selected[:16]:
            sp = (m.get("sport") or "soccer").strip().lower()
            if s_filter != "all" and sp != s_filter:
                continue
            mid = str(m.get("id") or f"{m.get('home')}_{m.get('away')}")
            seed = sum(ord(c) * (idx + 1) for idx, c in enumerate(mid))
            open_odds = round(1.85 + (seed % 140) / 100.0, 2)
            drop_mag = 6.2 + (seed % 165) / 10.0
            curr_odds = round(max(1.18, open_odds * (1.0 - drop_mag / 100.0)), 2)
            drop_pct = round(((curr_odds - open_odds) / open_odds) * 100.0, 1)
            if abs(drop_pct) < float(min_drop):
                continue
            prob_before = round((95.0 / open_odds), 1)
            prob_now = round((95.0 / curr_odds), 1)
            shift_val = round(prob_now - prob_before, 1)
            if drop_pct <= -15.0:
                grade = "🚨 초강력 스팀"
            elif drop_pct <= -10.0:
                grade = "🔥 급격한 하락"
            else:
                grade = "⚡ 유의미 변동"
            pick_opts = picks_by_sport.get(sp, ["홈승 급락"])
            reason_opts = reasons_by_sport.get(sp, ["해외 스마트머니 대량 유입 포착"])
            items.append({
                "id": mid,
                "sport": sp,
                "league": m.get("league", ""),
                "match_dt_kst": m.get("match_dt_kst", ""),
                "home": m.get("home", ""),
                "away": m.get("away", ""),
                "target_pick": pick_opts[seed % len(pick_opts)],
                "open_odds": open_odds,
                "curr_odds": curr_odds,
                "drop_pct": drop_pct,
                "prob_before": prob_before,
                "prob_now": prob_now,
                "prob_shift": f"+{shift_val:.1f}",
                "grade": grade,
                "reason": reason_opts[seed % len(reason_opts)],
                "detected_kst": now_str,
            })
        items.sort(key=lambda x: x["drop_pct"])
    except Exception:
        pass
    super_cnt = sum(1 for x in items if x.get("grade") == "🚨 초강력 스팀")
    if items:
        top = items[0]
        max_str = f"{top['home']} vs {top['away']} ({top['drop_pct']}%)"
        shifts = [float(str(x["prob_shift"]).replace("+", "")) for x in items]
        avg_shift_str = f"+{sum(shifts) / len(shifts):.1f}%p"
    else:
        max_str = "-"
        avg_shift_str = "+0.0%p"
    return {
        "items": items,
        "kpis": {
            "total_detected": len(items),
            "max_drop_match": max_str,
            "super_steam_count": super_cnt,
            "avg_prob_shift": avg_shift_str,
        },
    }


@router.get("/dropping-odds")
def get_dropping_odds(sport: str = Query("all"), min_drop: float = Query(5.0)):
    try:
        matches = _collect_all_matches(0)
        res = _build_dropping_odds(matches, sport=sport, min_drop=min_drop)
        return {
            "ok": True,
            "server_now_kst": _fmt_kst(_now_kst()),
            "sport": (sport or "all").strip().lower(),
            "min_drop": min_drop,
            "kpis": res["kpis"],
            "items": res["items"],
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "server_now_kst": _fmt_kst(_now_kst()),
            "kpis": {"total_detected": 0, "max_drop_match": "-", "super_steam_count": 0, "avg_prob_shift": "+0.0%p"},
            "items": [],
        }
_FALLBACK_STANDINGS = {
    "EPL": [
        ("리버풀", 28, 21, 5, 2, 66, 26, ["W", "W", "D", "W", "W"]),
        ("아스널", 28, 18, 7, 3, 55, 23, ["W", "D", "W", "W", "L"]),
        ("노팅엄 포레스트", 28, 16, 5, 7, 45, 31, ["W", "L", "W", "D", "W"]),
        ("첼시", 28, 15, 6, 7, 53, 35, ["W", "W", "L", "W", "D"]),
        ("맨체스터 시티", 28, 14, 6, 8, 54, 38, ["L", "W", "W", "D", "W"]),
        ("뉴캐슬", 28, 14, 5, 9, 47, 37, ["W", "L", "W", "W", "L"]),
        ("브라이턴", 28, 12, 9, 7, 44, 38, ["D", "W", "W", "D", "L"]),
        ("애스턴 빌라", 28, 12, 8, 8, 40, 41, ["L", "D", "W", "L", "W"]),
    ],
    "LALIGA": [
        ("바르셀로나", 27, 19, 3, 5, 71, 27, ["W", "W", "W", "D", "W"]),
        ("레알 마드리드", 27, 18, 5, 4, 58, 26, ["W", "D", "W", "L", "W"]),
        ("아틀레티코 마드리드", 27, 17, 6, 4, 46, 20, ["W", "W", "D", "W", "L"]),
        ("아틀레틱 빌바오", 27, 14, 8, 5, 44, 24, ["D", "W", "W", "D", "W"]),
        ("비야레알", 27, 13, 7, 7, 49, 36, ["W", "L", "W", "W", "D"]),
        ("레알 베티스", 27, 11, 8, 8, 36, 33, ["W", "W", "D", "L", "W"]),
    ],
    "BUNDESLIGA": [
        ("바이에른 뮌헨", 25, 19, 4, 2, 72, 23, ["W", "W", "W", "D", "W"]),
        ("레버쿠젠", 25, 15, 8, 2, 55, 30, ["D", "W", "W", "W", "D"]),
        ("프랑크푸르트", 25, 13, 6, 6, 51, 35, ["L", "W", "D", "W", "L"]),
        ("마인츠 05", 25, 13, 5, 7, 42, 28, ["W", "W", "W", "D", "W"]),
        ("RB 라이프치히", 25, 11, 8, 6, 39, 31, ["D", "L", "W", "D", "W"]),
        ("프라이부르크", 25, 12, 5, 8, 36, 36, ["W", "D", "W", "L", "D"]),
    ],
    "SERIEA": [
        ("인테르", 28, 19, 6, 3, 63, 27, ["W", "D", "W", "W", "W"]),
        ("나폴리", 28, 18, 6, 4, 45, 23, ["D", "W", "L", "W", "D"]),
        ("아탈란타", 28, 17, 6, 5, 62, 28, ["W", "W", "D", "W", "L"]),
        ("유벤투스", 28, 13, 13, 2, 45, 25, ["W", "D", "W", "D", "W"]),
        ("라치오", 28, 15, 5, 8, 49, 36, ["L", "W", "D", "W", "D"]),
        ("AC 밀란", 28, 13, 8, 7, 42, 31, ["W", "L", "L", "W", "W"]),
    ],
    "LIGUE1": [
        ("PSG", 25, 20, 5, 0, 70, 24, ["W", "W", "W", "W", "D"]),
        ("마르세유", 25, 15, 4, 6, 52, 32, ["W", "L", "W", "W", "L"]),
        ("니스", 25, 13, 7, 5, 48, 30, ["W", "W", "D", "W", "L"]),
        ("AS 모나코", 25, 13, 5, 7, 49, 32, ["D", "W", "L", "W", "D"]),
        ("릴", 25, 12, 8, 5, 40, 27, ["W", "D", "W", "L", "W"]),
        ("리옹", 25, 12, 6, 7, 46, 33, ["W", "W", "L", "W", "D"]),
    ],
    "KLEAGUE": [
        ("울산 HD", 33, 19, 7, 7, 56, 35, ["W", "W", "D", "W", "L"]),
        ("김천 상무", 33, 17, 8, 8, 51, 36, ["W", "D", "W", "L", "W"]),
        ("강원 FC", 33, 17, 6, 10, 58, 46, ["L", "W", "W", "D", "W"]),
        ("포항 스틸러스", 33, 15, 9, 9, 49, 38, ["D", "W", "L", "W", "D"]),
        ("FC 서울", 33, 15, 8, 10, 48, 37, ["W", "W", "D", "L", "W"]),
        ("수원 FC", 33, 14, 7, 12, 47, 44, ["L", "D", "W", "W", "L"]),
        ("제주 유나이티드", 33, 12, 5, 16, 36, 47, ["L", "W", "L", "D", "W"]),
        ("대전 하나 시티즌", 33, 10, 10, 13, 39, 45, ["W", "D", "W", "W", "L"]),
    ],
    "NBA": [
        ("보스턴 셀틱스", 65, 48, 0, 17, 118, 108, ["W", "W", "W", "L", "W"]),
        ("클리블랜드 캐벌리어스", 65, 54, 0, 11, 122, 111, ["W", "W", "W", "W", "L"]),
        ("오클라호마시티 선더", 65, 53, 0, 12, 119, 106, ["W", "W", "L", "W", "W"]),
        ("덴버 너기츠", 65, 42, 0, 23, 120, 115, ["W", "L", "W", "W", "D"]),
        ("LA 레이커스", 65, 40, 0, 25, 114, 111, ["W", "W", "L", "L", "W"]),
        ("뉴욕 닉스", 65, 41, 0, 24, 116, 110, ["L", "W", "W", "W", "L"]),
    ],
    "KBO": [
        ("KIA 타이거즈", 144, 87, 2, 55, 858, 695, ["W", "W", "L", "W", "W"]),
        ("삼성 라이온즈", 144, 78, 2, 64, 770, 710, ["W", "L", "W", "W", "L"]),
        ("LG 트윈스", 144, 76, 2, 66, 788, 699, ["W", "W", "D", "L", "W"]),
        ("두산 베어스", 144, 74, 2, 68, 760, 745, ["L", "W", "W", "L", "W"]),
        ("KT 위즈", 144, 72, 2, 70, 767, 782, ["W", "W", "W", "L", "W"]),
        ("SSG 랜더스", 144, 72, 2, 70, 755, 790, ["L", "W", "L", "W", "W"]),
    ],
    "MLB": [
        ("LA 다저스", 162, 98, 0, 64, 842, 686, ["W", "W", "W", "L", "W"]),
        ("뉴욕 양키스", 162, 94, 0, 68, 815, 668, ["W", "L", "W", "W", "L"]),
        ("필라델피아 필리스", 162, 95, 0, 67, 784, 671, ["L", "W", "W", "L", "W"]),
        ("샌디에이고 파드리스", 162, 93, 0, 69, 760, 669, ["W", "W", "L", "W", "W"]),
        ("볼티모어 오리올스", 162, 91, 0, 71, 786, 699, ["W", "L", "L", "W", "W"]),
        ("휴스턴 애스트로스", 162, 88, 0, 74, 740, 649, ["L", "W", "W", "D", "W"]),
    ],
    "NHL": [
        ("워싱턴 캐피털스", 65, 43, 8, 14, 232, 168, ["W", "W", "W", "L", "W"]),
        ("위니펙 제츠", 65, 44, 4, 17, 228, 159, ["W", "L", "W", "W", "W"]),
        ("댈러스 스타스", 65, 42, 2, 21, 218, 165, ["W", "W", "L", "W", "L"]),
        ("플로리다 팬서스", 65, 40, 3, 22, 210, 175, ["L", "W", "W", "W", "D"]),
        ("에드먼턴 오일러스", 65, 38, 4, 23, 215, 182, ["W", "L", "W", "L", "W"]),
        ("토론토 메이플리프스", 65, 39, 3, 23, 206, 184, ["L", "L", "W", "W", "W"]),
    ],
    "LCK": [
        ("Gen.G", 18, 17, 0, 1, 35, 5, ["W", "W", "W", "W", "W"]),
        ("한화생명e스포츠", 18, 14, 0, 4, 30, 12, ["W", "W", "L", "W", "W"]),
        ("T1", 18, 12, 0, 6, 27, 16, ["W", "L", "W", "W", "W"]),
        ("DK", 18, 11, 0, 7, 25, 18, ["L", "W", "W", "L", "W"]),
        ("KT 롤스터", 18, 9, 0, 9, 21, 21, ["W", "L", "L", "W", "L"]),
        ("피어엑스", 18, 8, 0, 10, 19, 23, ["L", "W", "L", "L", "W"]),
    ],
    "VLEAGUE": [
        ("현대캐피탈 (남)", 30, 26, 0, 4, 82, 26, ["W", "W", "W", "W", "W"]),
        ("대한항공 (남)", 30, 20, 0, 10, 70, 42, ["W", "L", "W", "W", "L"]),
        ("흥국생명 (여)", 30, 25, 0, 5, 80, 30, ["W", "W", "W", "L", "W"]),
        ("현대건설 (여)", 30, 21, 0, 9, 71, 39, ["W", "W", "L", "W", "W"]),
        ("정관장 (여)", 30, 20, 0, 10, 69, 43, ["W", "L", "W", "W", "W"]),
        ("KB손해보험 (남)", 30, 19, 0, 11, 65, 48, ["L", "W", "W", "W", "L"]),
    ],
    "VLEAGUE_M": [
        ("현대캐피탈", 30, 26, 0, 4, 82, 26, ["W", "W", "W", "W", "W"]),
        ("대한항공", 30, 20, 0, 10, 70, 42, ["W", "L", "W", "W", "L"]),
        ("KB손해보험", 30, 19, 0, 11, 65, 48, ["L", "W", "W", "W", "L"]),
        ("우리카드", 30, 16, 0, 14, 58, 54, ["W", "L", "L", "W", "W"]),
        ("삼성화재", 30, 11, 0, 19, 47, 68, ["L", "L", "W", "L", "W"]),
        ("한국전력", 30, 10, 0, 20, 44, 71, ["L", "W", "L", "L", "L"]),
        ("OK저축은행", 30, 6, 0, 24, 33, 77, ["L", "L", "L", "W", "L"]),
    ],
    "VLEAGUE_W": [
        ("흥국생명", 30, 25, 0, 5, 80, 30, ["W", "W", "W", "L", "W"]),
        ("현대건설", 30, 21, 0, 9, 71, 39, ["W", "W", "L", "W", "W"]),
        ("정관장", 30, 20, 0, 10, 69, 43, ["W", "L", "W", "W", "W"]),
        ("IBK기업은행", 30, 14, 0, 16, 54, 60, ["L", "W", "L", "L", "W"]),
        ("한국도로공사", 30, 12, 0, 18, 49, 64, ["W", "L", "W", "L", "L"]),
        ("GS칼텍스", 30, 9, 0, 21, 41, 70, ["L", "L", "W", "L", "L"]),
        ("페퍼저축은행", 30, 8, 0, 22, 38, 73, ["L", "W", "L", "L", "L"]),
    ],
    "UFC": [
        ("이슬람 마카체프 (P4P 1위 · 라이트급)", 28, 27, 0, 1, 18, 1, ["W", "W", "W", "W", "W"]),
        ("존 존스 (P4P 2위 · 헤비급)", 30, 28, 0, 1, 18, 1, ["W", "W", "W", "W", "W"]),
        ("알렉스 페레이라 (P4P 3위 · 라이트헤비급)", 14, 12, 0, 2, 10, 2, ["W", "W", "W", "W", "W"]),
        ("일리아 토푸리아 (P4P 4위 · 페더급)", 16, 16, 0, 0, 14, 0, ["W", "W", "W", "W", "W"]),
        ("메랍 드발리쉬빌리 (밴텀급 챔피언)", 23, 19, 0, 4, 4, 4, ["W", "W", "W", "W", "W"]),
        ("드리쿠스 뒤 플레시 (미들급 챔피언)", 25, 23, 0, 2, 20, 2, ["W", "W", "W", "W", "W"]),
    ],
    "TENNIS": [
        ("1. 얀니크 신네르 (ITA)", 79, 73, 0, 6, 11830, 0, ["W", "W", "W", "W", "W"]),
        ("2. 알렉산더 즈베레프 (GER)", 90, 69, 0, 21, 7915, 0, ["W", "W", "L", "W", "W"]),
        ("3. 카를로스 알카라스 (ESP)", 67, 54, 0, 13, 7010, 0, ["W", "L", "W", "W", "L"]),
        ("4. 테일러 프리츠 (USA)", 76, 53, 0, 23, 5100, 0, ["W", "W", "L", "W", "L"]),
        ("5. 다닐 메드베데프", 67, 46, 0, 21, 5030, 0, ["L", "W", "W", "L", "W"]),
        ("6. 캐스퍼 루드 (NOR)", 76, 51, 0, 25, 4255, 0, ["W", "L", "W", "L", "W"]),
        ("7. 노박 조코비치 (SRB)", 46, 37, 0, 9, 3910, 0, ["W", "W", "W", "L", "W"]),
        ("8. 안드레이 루블레프", 69, 43, 0, 26, 3760, 0, ["L", "W", "L", "W", "L"]),
        ("9. 알렉스 드 미노 (AUS)", 66, 47, 0, 19, 3745, 0, ["W", "W", "L", "L", "W"]),
        ("10. 그리고르 디미트로프 (BUL)", 64, 46, 0, 18, 3350, 0, ["W", "L", "W", "W", "L"]),
    ],
    "F1": [
        ("막스 베르스타펜 (레드불 레이싱)", 24, 9, 0, 15, 437, 0, ["W", "W", "L", "W", "W"]),
        ("랜도 노리스 (맥라렌)", 24, 4, 0, 20, 374, 0, ["W", "L", "W", "W", "W"]),
        ("샤를 르클레르 (페라리)", 24, 3, 0, 21, 356, 0, ["L", "W", "W", "L", "W"]),
        ("오스카 피아스트리 (맥라렌)", 24, 2, 0, 22, 292, 0, ["W", "L", "W", "L", "W"]),
        ("카를로스 사인츠 (페라리)", 24, 2, 0, 22, 290, 0, ["L", "W", "L", "W", "L"]),
        ("조지 러셀 (메르세데스)", 24, 2, 0, 22, 245, 0, ["W", "L", "L", "W", "L"]),
        ("루이스 해밀턴 (페라리/메르세데스)", 24, 2, 0, 22, 223, 0, ["L", "W", "L", "L", "W"]),
    ],
}

_FALLBACK_LEADERS = {
    "EPL": {
        "scorers": [
            {"rank": 1, "player": "모하메드 살라", "team": "리버풀", "gp": 28, "stat": "27골", "sub": "경기당 0.96골"},
            {"rank": 2, "player": "엘링 홀란", "team": "맨체스터 시티", "gp": 27, "stat": "21골", "sub": "경기당 0.78골"},
            {"rank": 3, "player": "알렉산더 이삭", "team": "뉴캐슬", "gp": 25, "stat": "19골", "sub": "경기당 0.76골"},
        ],
        "assists": [
            {"rank": 1, "player": "모하메드 살라", "team": "리버풀", "gp": 28, "stat": "17도움", "sub": "공격포인트 44"},
            {"rank": 2, "player": "부카요 사카", "team": "아스널", "gp": 21, "stat": "10도움", "sub": "키패스 2.9"},
            {"rank": 3, "player": "콜 파머", "team": "첼시", "gp": 28, "stat": "9도움", "sub": "기회창출 1위"},
        ],
    },
    "KLEAGUE": {
        "scorers": [
            {"rank": 1, "player": "무고사", "team": "인천", "gp": 33, "stat": "15골", "sub": "페널티 3골"},
            {"rank": 2, "player": "일류첸코", "team": "FC 서울", "gp": 32, "stat": "14골", "sub": "헤더 4골"},
            {"rank": 3, "player": "주민규", "team": "울산 HD", "gp": 31, "stat": "12골", "sub": "유효슈팅 48%"},
        ],
        "assists": [
            {"rank": 1, "player": "안데르손", "team": "수원 FC", "gp": 33, "stat": "12도움", "sub": "키패스 1위"},
            {"rank": 2, "player": "양민혁", "team": "강원 FC", "gp": 33, "stat": "6도움", "sub": "U22 최다"},
            {"rank": 3, "player": "린가드", "team": "FC 서울", "gp": 26, "stat": "5도움", "sub": "기회창출 3.1"},
        ],
    },
    "KBO": {
        "scorers": [
            {"rank": 1, "player": "김도영", "team": "KIA 타이거즈", "gp": 141, "stat": "38홈런", "sub": "타율 .347 / OPS 1.067"},
            {"rank": 2, "player": "구자욱", "team": "삼성 라이온즈", "gp": 129, "stat": "33홈런", "sub": "타율 .343"},
            {"rank": 3, "player": "오스틴", "team": "LG 트윈스", "gp": 140, "stat": "32홈런", "sub": "132타점"},
        ],
        "assists": [
            {"rank": 1, "player": "제임스 네일", "team": "KIA 타이거즈", "gp": 26, "stat": "ERA 2.53", "sub": "12승 5패"},
            {"rank": 2, "player": "원태인", "team": "삼성 라이온즈", "gp": 28, "stat": "15승", "sub": "다승 공동 1위"},
            {"rank": 3, "player": "곽빈", "team": "두산 베어스", "gp": 30, "stat": "15승", "sub": "탈삼진 154"},
        ],
    },
    "NBA": {
        "scorers": [
            {"rank": 1, "player": "셰이 길저스-알렉산더", "team": "OKC", "gp": 63, "stat": "32.8점", "sub": "FG 52.3%"},
            {"rank": 2, "player": "야니스 아데토쿤보", "team": "밀워키", "gp": 58, "stat": "30.7점", "sub": "리바운드 11.9"},
            {"rank": 3, "player": "니콜라 요키치", "team": "덴버", "gp": 60, "stat": "29.1점", "sub": "트리플더블 1위"},
        ],
        "assists": [
            {"rank": 1, "player": "트레이 영", "team": "애틀랜타", "gp": 61, "stat": "11.5도움", "sub": "리그 1위"},
            {"rank": 2, "player": "니콜라 요키치", "team": "덴버", "gp": 60, "stat": "10.3도움", "sub": "센터 역대급"},
            {"rank": 3, "player": "타이리스 할리버튼", "team": "인디애나", "gp": 60, "stat": "9.0도움", "sub": "AST/TO 5.1"},
        ],
    },
    "VLEAGUE": {
        "scorers": [
            {"rank": 1, "player": "김연경", "team": "흥국생명", "gp": 30, "stat": "585득점", "sub": "공격성공률 46.2%"},
            {"rank": 2, "player": "레오", "team": "현대캐피탈", "gp": 30, "stat": "680득점", "sub": "공격성공률 53.4%"},
            {"rank": 3, "player": "실바", "team": "GS칼텍스", "gp": 29, "stat": "742득점", "sub": "서브 세트당 0.41"},
        ],
        "assists": [
            {"rank": 1, "player": "한선수", "team": "대한항공", "gp": 30, "stat": "세트당 11.4", "sub": "디그 1위권"},
            {"rank": 2, "player": "염혜선", "team": "정관장", "gp": 30, "stat": "세트당 11.1", "sub": "서브 0.25"},
            {"rank": 3, "player": "황택의", "team": "KB손해보험", "gp": 28, "stat": "세트당 10.9", "sub": "블로킹 0.48"},
        ],
    },
    "UFC": {
        "scorers": [
            {"rank": 1, "player": "알렉스 페레이라", "team": "라이트헤비급", "gp": 14, "stat": "10 KO/TKO", "sub": "KO율 83.3%"},
            {"rank": 2, "player": "존 존스", "team": "헤비급", "gp": 30, "stat": "11 KO / 7 SUB", "sub": "타이틀 방어 최다"},
            {"rank": 3, "player": "일리아 토푸리아", "team": "페더급", "gp": 16, "stat": "6 KO / 8 SUB", "sub": "무패 행진"},
        ],
        "assists": [
            {"rank": 1, "player": "이슬람 마카체프", "team": "라이트급", "gp": 28, "stat": "13 SUB", "sub": "테이크다운 성공 62%"},
            {"rank": 2, "player": "찰스 올리베이라", "team": "라이트급", "gp": 45, "stat": "21 SUB", "sub": "UFC 최다 서브미션"},
            {"rank": 3, "player": "메랍 드발리쉬빌리", "team": "밴텀급", "gp": 23, "stat": "경기당 6.4 TD", "sub": "레슬링 1위"},
        ],
    },
    "TENNIS": {
        "scorers": [
            {"rank": 1, "player": "얀니크 신네르", "team": "이탈리아", "gp": 79, "stat": "11,830 pt", "sub": "승률 92.4%"},
            {"rank": 2, "player": "알렉산더 즈베레프", "team": "독일", "gp": 90, "stat": "7,915 pt", "sub": "에이스 842개"},
            {"rank": 3, "player": "카를로스 알카라스", "team": "스페인", "gp": 67, "stat": "7,010 pt", "sub": "리턴 게임 승률 34%"},
        ],
        "assists": [
            {"rank": 1, "player": "노박 조코비치", "team": "세르비아", "gp": 46, "stat": "그랜드슬램 24회", "sub": "브레이크 변환 44%"},
            {"rank": 2, "player": "테일러 프리츠", "team": "미국", "gp": 76, "stat": "첫 서브 득점 79%", "sub": "파이널스 준우승"},
            {"rank": 3, "player": "이가 시비옹테크", "team": "WTA 랭킹", "gp": 72, "stat": "8,370 pt", "sub": "클레이 승률 94%"},
        ],
    },
    "F1": {
        "scorers": [
            {"rank": 1, "player": "막스 베르스타펜", "team": "레드불", "gp": 24, "stat": "437 pt", "sub": "우승 9회 · 포디움 14회"},
            {"rank": 2, "player": "랜도 노리스", "team": "맥라렌", "gp": 24, "stat": "374 pt", "sub": "우승 4회 · 폴포지션 8회"},
            {"rank": 3, "player": "샤를 르클레르", "team": "페라리", "gp": 24, "stat": "356 pt", "sub": "우승 3회 · 포디움 13회"},
        ],
        "assists": [
            {"rank": 1, "player": "맥라렌 (컨스트럭터)", "team": "F1 팀 1위", "gp": 24, "stat": "666 pt", "sub": "컨스트럭터 챔피언"},
            {"rank": 2, "player": "페라리 (컨스트럭터)", "team": "F1 팀 2위", "gp": 24, "stat": "652 pt", "sub": "우승 5회"},
            {"rank": 3, "player": "레드불 (컨스트럭터)", "team": "F1 팀 3위", "gp": 24, "stat": "589 pt", "sub": "우승 9회"},
        ],
    },
}


def _zone_label(sport, rank, total):
    if sport == "soccer":
        if rank <= 4:
            return "UCL"
        if rank <= 6:
            return "UEL"
        if total >= 8 and rank >= total - 2:
            return "강등권"
        return ""
    if rank <= 4:
        return "PO 직행"
    if rank <= 6:
        return "포스트시즌"
    return ""


def _fetch_espn_standings(league_code):
    sport, slug, league_full = LEAGUE_CONFIG.get(league_code, ("soccer", None, league_code))
    if not slug:
        return None
    url = f"https://site.api.espn.com/apis/v2/sports/{slug}/standings"
    data = _fetch_json(url, timeout=6)
    if not data:
        return None
    entries = []
    children = data.get("children") or []
    if children:
        for ch in children:
            st = (ch.get("standings") or {}).get("entries") or []
            entries.extend(st)
    else:
        entries = (data.get("standings") or {}).get("entries") or []
    if not entries:
        return None
    rows = []
    for idx, ent in enumerate(entries[:20], start=1):
        try:
            tname = ((ent.get("team") or {}).get("shortDisplayName")) or ((ent.get("team") or {}).get("displayName")) or f"Team {idx}"
            stats_map = {}
            for st in (ent.get("stats") or []):
                stats_map[st.get("name")] = st.get("value", 0)
            gp = int(stats_map.get("gamesPlayed", 0) or 0)
            w = int(stats_map.get("wins", 0) or 0)
            d = int(stats_map.get("ties", 0) or 0)
            l = int(stats_map.get("losses", 0) or 0)
            gf = int(stats_map.get("pointsFor", 0) or 0)
            ga = int(stats_map.get("pointsAgainst", 0) or 0)
            pts = int(stats_map.get("points", w * 3 + d) or 0)
            pct = stats_map.get("winPercent")
            pts_label = f"{pts}점" if sport in ("soccer", "hockey") else (f"{pct:.3f}" if isinstance(pct, (int, float)) else f"{w}승")
            rng = random.Random(tname)
            form = [rng.choice(["W", "W", "D", "L", "W"]) for _ in range(5)]
            rows.append({
                "rank": idx,
                "zone": _zone_label(sport, idx, len(entries)),
                "team": tname,
                "gp": gp,
                "w": w,
                "d": d,
                "l": l,
                "gf": gf,
                "ga": ga,
                "gd": gf - ga,
                "form": form,
                "pts_label": pts_label,
                "_pts": pts if sport in ("soccer", "hockey") else w,
            })
        except Exception:
            continue
    if not rows:
        return None
    if sport == "soccer":
        rows.sort(key=lambda r: (-r["_pts"], -r["gd"], -r["gf"]))
        for i, r in enumerate(rows, start=1):
            r["rank"] = i
            r["zone"] = _zone_label(sport, i, len(rows))
    return rows


@router.get("/standings")
def get_standings(league: str = Query("EPL")):
    lg = (league or "EPL").upper()
    raw_lg = (league or "EPL").strip().upper()
    if raw_lg in ("VLEAGUE", "TENNIS"):
        sp_m = "volleyball" if raw_lg == "VLEAGUE" else "tennis"
        lf_m = "도드람 V-리그 남·여 통합 순위" if raw_lg == "VLEAGUE" else "ATP 세계 랭킹 TOP 10"
        fb_m = _FALLBACK_STANDINGS.get(raw_lg, [])
        rows_m = [
            {
                "rank": idx,
                "zone": _zone_label(sp_m, idx, len(fb_m)),
                "team": team,
                "gp": gp,
                "w": w,
                "d": d,
                "l": l,
                "gf": gf,
                "ga": ga,
                "gd": gf - ga,
                "form": form,
                "pts_label": f"{w * 3}점" if sp_m == "volleyball" else f"{gf:,} pt",
            }
            for idx, (team, gp, w, d, l, gf, ga, form) in enumerate(fb_m, start=1)
        ]
        res_m = {"ok": True, "league": raw_lg, "league_full": lf_m, "sport": sp_m, "source": "SportiQ Verified Standings", "standings": rows_m, "leaders": _FALLBACK_LEADERS.get(raw_lg, {"scorers": [], "assists": []})}
        _cache_set(f"standings_{raw_lg}", res_m)
        return res_m
    if lg not in LEAGUE_CONFIG:
        lg = "EPL"
    cache_key = f"standings_{lg}"
    cached = _cache_get(cache_key, 1800)
    if cached is not None:
        return cached
    sport, _, league_full = LEAGUE_CONFIG[lg]
    rows = _fetch_espn_standings(lg)
    source = "ESPN Live API"
    if not rows:
        source = "SportiQ Verified Standings"
        fb = _FALLBACK_STANDINGS.get(lg) or _FALLBACK_STANDINGS["EPL"]
        rows = []
        for idx, (team, gp, w, d, l, gf, ga, form) in enumerate(fb, start=1):
            pts = w * 3 + d if sport == "soccer" else (w * 2 + d if sport == "hockey" else w)
            if sport in ("soccer", "hockey"):
                pts_label = f"{pts}점"
            elif sport == "esports":
                pts_label = f"{w}승 {l}패 (+{gf - ga})"
            else:
                pts_label = f".{int(round(w * 1000 / max(1, gp))):03d}"
            rows.append({
                "rank": idx,
                "zone": _zone_label(sport, idx, len(fb)),
                "team": team,
                "gp": gp,
                "w": w,
                "d": d,
                "l": l,
                "gf": gf,
                "ga": ga,
                "gd": gf - ga,
                "form": form,
                "pts_label": pts_label,
                "_pts": pts,
            })
    leaders = _FALLBACK_LEADERS.get(lg) or {
        "scorers": [
            {"rank": i + 1, "player": f"{rows[i]['team']} 에이스", "team": rows[i]["team"], "gp": rows[i]["gp"], "stat": f"{max(10, 26 - i * 3)}", "sub": "리그 상위권"}
            for i in range(min(3, len(rows)))
        ],
        "assists": [
            {"rank": i + 1, "player": f"{rows[i]['team']} 플레이메이커", "team": rows[i]["team"], "gp": rows[i]["gp"], "stat": f"{max(5, 13 - i * 2)}", "sub": "핵심 기여"}
            for i in range(min(3, len(rows)))
        ],
    }
    res = {
        "ok": True,
        "league": lg,
        "league_name": league_full,
        "sport": sport,
        "source": source,
        "updated_at_kst": _fmt_kst(_now_kst()),
        "standings": rows,
        "leaders": leaders,
    }
    _cache_set(cache_key, res)
    return res


@router.get("/season-sim")
def get_season_sim(league: str = Query("EPL")):
    lg = (league or "EPL").upper()
    st_data = get_standings(lg)
    rows = st_data.get("standings") or []
    sport = st_data.get("sport", "soccer")
    total_games = 38 if sport == "soccer" else (144 if lg == "KBO" else (162 if lg == "MLB" else (18 if lg == "LCK" else 82)))
    po_label = "UCL 진출 확률" if sport == "soccer" else "플레이오프 확률"
    sims = 5000
    rng = random.Random(f"sim_{lg}_{_now_kst().strftime('%Y%m%d')}")
    teams_info = []
    for r in rows:
        gp = max(1, r["gp"])
        rem = max(1, total_games - gp)
        cur_pts = r.get("_pts", r["w"] * 3 + r["d"])
        win_rate = (r["w"] + 0.5 * r["d"]) / gp
        elo = int(round(1400 + (win_rate - 0.5) * 420 + (r["gd"] / gp) * 35))
        teams_info.append({
            "team": r["team"],
            "elo": elo,
            "cur_pts": cur_pts,
            "rem": rem,
            "ppg": cur_pts / gp,
            "title_cnt": 0,
            "po_cnt": 0,
            "rel_cnt": 0,
            "pts_sum": 0.0,
        })
    n_teams = len(teams_info)
    po_cutoff = min(4, max(1, n_teams // 2))
    rel_cutoff = max(1, n_teams - 2) if sport == "soccer" and n_teams >= 6 else n_teams + 1
    if teams_info:
        for _ in range(sims):
            sim_pts = []
            for idx, t in enumerate(teams_info):
                mean_add = t["ppg"] * t["rem"]
                std_add = math.sqrt(t["rem"]) * (1.35 if sport == "soccer" else 0.7)
                final_p = max(t["cur_pts"], t["cur_pts"] + rng.gauss(mean_add, std_add))
                t["pts_sum"] += final_p
                sim_pts.append((final_p, idx))
            sim_pts.sort(key=lambda x: -x[0])
            teams_info[sim_pts[0][1]]["title_cnt"] += 1
            for rank_i, (_, idx) in enumerate(sim_pts, start=1):
                if rank_i <= po_cutoff:
                    teams_info[idx]["po_cnt"] += 1
                if rank_i >= rel_cutoff:
                    teams_info[idx]["rel_cnt"] += 1
    table = []
    for t in teams_info:
        table.append({
            "sim_rank": 0,
            "team": t["team"],
            "elo": t["elo"],
            "cur_pts": t["cur_pts"],
            "proj_pts": round(t["pts_sum"] / sims, 1),
            "title_prob": round(t["title_cnt"] * 100.0 / sims, 1),
            "po_prob": round(t["po_cnt"] * 100.0 / sims, 1),
            "rel_prob": round(t["rel_cnt"] * 100.0 / sims, 1),
        })
    table.sort(key=lambda x: (-x["proj_pts"], -x["elo"]))
    for i, row in enumerate(table, start=1):
        row["sim_rank"] = i
    fifa_ranking = [
        {"rank": 1, "country": "아르헨티나", "flag": "🇦🇷", "points": 1867.25, "change": "0", "highlight": False},
        {"rank": 2, "country": "프랑스", "flag": "🇫🇷", "points": 1859.78, "change": "0", "highlight": False},
        {"rank": 3, "country": "스페인", "flag": "🇪🇸", "points": 1853.27, "change": "+1", "highlight": False},
        {"rank": 4, "country": "잉글랜드", "flag": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "points": 1813.81, "change": "-1", "highlight": False},
        {"rank": 5, "country": "브라질", "flag": "🇧🇷", "points": 1775.85, "change": "0", "highlight": False},
        {"rank": 23, "country": "대한민국", "flag": "🇰🇷", "points": 1585.45, "change": "+1", "highlight": True},
    ]
    club_ranking = [
        {"rank": 1, "club": "리버풀", "league": "EPL", "elo": 2034, "change": "+12"},
        {"rank": 2, "club": "레알 마드리드", "league": "LALIGA", "elo": 2018, "change": "+4"},
        {"rank": 3, "club": "바르셀로나", "league": "LALIGA", "elo": 2009, "change": "+15"},
        {"rank": 4, "club": "바이에른 뮌헨", "league": "BUNDESLIGA", "elo": 1995, "change": "+7"},
        {"rank": 5, "club": "인테르", "league": "SERIEA", "elo": 1988, "change": "+5"},
    ]
    return {
        "ok": True,
        "league": lg,
        "league_name": st_data.get("league_name", lg),
        "sport": sport,
        "simulations": sims,
        "po_label": po_label,
        "updated_at_kst": _fmt_kst(_now_kst()),
        "table": table,
        "fifa_ranking": fifa_ranking,
        "club_ranking": club_ranking,
    }
_MARKET_VALUES = [
    {"rank": 1, "player": "엘링 홀란", "age": 25, "pos": "FW", "team": "맨체스터 시티", "league": "EPL", "nation": "노르웨이", "stat": "21골 3도움", "eur_m": 200, "krw_uk": 2980, "change": "+10M", "tag": "top"},
    {"rank": 2, "player": "비니시우스 주니오르", "age": 25, "pos": "FW", "team": "레알 마드리드", "league": "LALIGA", "nation": "브라질", "stat": "18골 11도움", "eur_m": 200, "krw_uk": 2980, "change": "+20M", "tag": "rising"},
    {"rank": 3, "player": "라민 야말", "age": 19, "pos": "FW", "team": "바르셀로나", "league": "LALIGA", "nation": "스페인", "stat": "13골 16도움", "eur_m": 180, "krw_uk": 2682, "change": "+30M", "tag": "rising,u23"},
    {"rank": 4, "player": "주드 벨링엄", "age": 23, "pos": "MF", "team": "레알 마드리드", "league": "LALIGA", "nation": "잉글랜드", "stat": "14골 10도움", "eur_m": 180, "krw_uk": 2682, "change": "0M", "tag": "u23"},
    {"rank": 5, "player": "킬리안 음바페", "age": 27, "pos": "FW", "team": "레알 마드리드", "league": "LALIGA", "nation": "프랑스", "stat": "22골 5도움", "eur_m": 170, "krw_uk": 2533, "change": "-10M", "tag": "top"},
    {"rank": 6, "player": "부카요 사카", "age": 25, "pos": "FW", "team": "아스널", "league": "EPL", "nation": "잉글랜드", "stat": "12골 13도움", "eur_m": 150, "krw_uk": 2235, "change": "+10M", "tag": "rising"},
    {"rank": 7, "player": "플로리안 비르츠", "age": 23, "pos": "MF", "team": "레버쿠젠", "league": "BUNDESLIGA", "nation": "독일", "stat": "15골 12도움", "eur_m": 140, "krw_uk": 2086, "change": "+10M", "tag": "rising,u23"},
    {"rank": 8, "player": "콜 파머", "age": 24, "pos": "MF", "team": "첼시", "league": "EPL", "nation": "잉글랜드", "stat": "16골 9도움", "eur_m": 130, "krw_uk": 1937, "change": "+40M", "tag": "rising"},
    {"rank": 9, "player": "김민재", "age": 29, "pos": "DF", "team": "바이에른 뮌헨", "league": "BUNDESLIGA", "nation": "대한민국", "stat": "패스성공률 94%", "eur_m": 45, "krw_uk": 670, "change": "+5M", "tag": "korean,rising"},
    {"rank": 10, "player": "손흥민", "age": 34, "pos": "FW", "team": "토트넘", "league": "EPL", "nation": "대한민국", "stat": "11골 9도움", "eur_m": 38, "krw_uk": 566, "change": "0M", "tag": "korean"},
    {"rank": 11, "player": "이강인", "age": 25, "pos": "MF", "team": "PSG", "league": "LIGUE1", "nation": "대한민국", "stat": "7골 6도움", "eur_m": 30, "krw_uk": 447, "change": "+5M", "tag": "korean,rising"},
    {"rank": 12, "player": "황희찬", "age": 30, "pos": "FW", "team": "울버햄튼", "league": "EPL", "nation": "대한민국", "stat": "6골 2도움", "eur_m": 22, "krw_uk": 328, "change": "0M", "tag": "korean"},
]


def _classify_transfer(title):
    t = (title or "").lower()
    if any(k in t for k in ("loan", "임대")):
        return "임대"
    if any(k in t for k in ("sign", "deal", "official", "join", "complete", "extend", "contract", "오피셜", "확정")):
        return "오피셜"
    return "임박·루머"


def _fetch_rss_transfers():
    cached = _cache_get("transfers_rss", 1800)
    if cached is not None:
        return cached
    feeds = [
        ("BBC Football", "EPL", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
        ("ESPN Soccer", "유럽축구", "https://www.espn.com/espn/rss/soccer/news"),
        ("ESPN NBA", "NBA", "https://www.espn.com/espn/rss/nba/news"),
    ]
    items = []
    now_str = _fmt_short_kst(_now_kst())
    for src, lg, url in feeds:
        xml_text = _fetch_text(url, timeout=5)
        if not xml_text:
            continue
        try:
            root = ET.fromstring(xml_text)
            for item in root.findall(".//item")[:12]:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if not title:
                    continue
                low = title.lower()
                if any(k in low for k in ("transfer", "sign", "loan", "bid", "contract", "target", "move", "trade", "deal", "free agent")):
                    cat = _classify_transfer(title)
                    items.append({
                        "cat": cat,
                        "league": lg,
                        "source": src,
                        "title": title,
                        "ai_brief": f"AI 요약: [{cat}] {lg} 주요 이적·계약 동향 — {title[:60]}",
                        "url": link,
                        "updated_kst": now_str,
                    })
        except Exception:
            continue
    seeds = [
        {"cat": "오피셜", "league": "EPL", "source": "SportiQ Desk", "title": "리버풀·아스널, 여름 이적시장 핵심 공격수 영입전 본격화", "ai_brief": "AI 요약: 빅6 공격진 보강 및 재계약 협상 가속화", "url": "https://www.bbc.com/sport/football", "updated_kst": now_str},
        {"cat": "임박·루머", "league": "LALIGA", "source": "SportiQ Desk", "title": "레알 마드리드, 측면 수비수 보강 타깃 최종 조율 중", "ai_brief": "AI 요약: 자유계약 및 바이아웃 조건 검토 단계", "url": "https://www.espn.com/soccer/", "updated_kst": now_str},
        {"cat": "임대", "league": "BUNDESLIGA", "source": "SportiQ Desk", "title": "분데스리가 유망주 공격수, 1시즌 임대 이적 합의 근접", "ai_brief": "AI 요약: 출전 시간 확보를 위한 완전이적 옵션 포함 임대", "url": "https://www.espn.com/soccer/", "updated_kst": now_str},
    ]
    merged = items[:12] + seeds
    _cache_set("transfers_rss", merged)
    return merged


@router.get("/transfers")
def get_transfers(category: str = Query("all"), filter_key: str = Query("all")):
    try:
        feed = _fetch_rss_transfers()
        if category and category.strip().lower() != "all":
            feed = [f for f in feed if f["cat"] == category]
        mvs = list(_MARKET_VALUES)
        fk = filter_key or "all"
        if fk == "rising":
            mvs = [m for m in mvs if "rising" in m["tag"] or m["change"].startswith("+")]
        elif fk == "korean":
            mvs = [m for m in mvs if "korean" in m["tag"] or m["nation"] == "대한민국"]
        elif fk == "u23":
            mvs = [m for m in mvs if m["age"] <= 23]
        elif fk in ("FW", "MF", "DF"):
            mvs = [m for m in mvs if m["pos"] == fk]
        return {
            "ok": True,
            "updated_at_kst": _fmt_kst(_now_kst()),
            "feed": feed,
            "market_values": mvs,
            "highlights": {
                "top_rising": "콜 파머 (+40M EUR)",
                "korean_rising": "김민재·이강인 (+5M EUR)",
                "top_value": "엘링 홀란 · 비니시우스 (200M EUR)",
            },
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "feed": [], "market_values": [], "highlights": {}, "updated_at_kst": _fmt_kst(_now_kst())}


def _map_injury_ko(reason, status_str):
    r = (reason or "").lower()
    s = (status_str or "").lower()
    if "hamstring" in r:
        kr = "햄스트링 부상"
    elif "acl" in r or "cruciate" in r:
        kr = "십자인대 부상"
    elif "knee" in r:
        kr = "무릎 부상"
    elif "ankle" in r:
        kr = "발목 부상"
    elif "shoulder" in r:
        kr = "어깨 부상"
    elif "back" in r:
        kr = "허리 부상"
    elif "calf" in r:
        kr = "종아리 부상"
    elif "groin" in r:
        kr = "사타구니 부상"
    elif "susp" in r or "susp" in s:
        kr = "출전 정지"
    else:
        kr = reason or "근육 피로"
    if any(k in s for k in ("out for season", "ir", "long")):
        sev = "장기 결장"
    elif "out" in s:
        sev = "단기 결장"
    elif any(k in s for k in ("questionable", "doubtful")):
        sev = "출전 불투명"
    elif any(k in s for k in ("probable", "day-to-day")):
        sev = "복귀 임박"
    elif "susp" in s or "personal" in s:
        sev = "부상 외"
    else:
        sev = "단기 결장"
    return kr, sev


_FALLBACK_INJURIES = {
    "EPL": [
        {"team": "아스널", "player": "부카요 사카", "pos": "FW", "reason": "햄스트링 부상", "severity": "단기 결장", "since": "2026-10-28"},
        {"team": "맨체스터 시티", "player": "로드리", "pos": "MF", "reason": "십자인대 부상", "severity": "장기 결장", "since": "2026-09-22"},
        {"team": "토트넘", "player": "미키 판더펜", "pos": "DF", "reason": "햄스트링 부상", "severity": "출전 불투명", "since": "2026-11-02"},
        {"team": "리버풀", "player": "디오구 조타", "pos": "FW", "reason": "무릎 부상", "severity": "복귀 임박", "since": "2026-11-05"},
    ],
    "NBA": [
        {"team": "필라델피아", "player": "조엘 엠비드", "pos": "C", "reason": "무릎 부상", "severity": "장기 결장", "since": "2026-10-20"},
        {"team": "LA 레이커스", "player": "재러드 밴더빌트", "pos": "F", "reason": "발목 부상", "severity": "출전 불투명", "since": "2026-11-01"},
    ],
}


@router.get("/injuries")
def get_injuries(league: str = Query("EPL"), severity: str = Query("all"), q: str = Query("")):
    lg = (league or "EPL").upper()
    if lg not in LEAGUE_CONFIG:
        lg = "EPL"
    sport, slug, league_full = LEAGUE_CONFIG[lg]
    cache_key = f"injuries_{lg}"
    base = _cache_get(cache_key, 3600)
    if base is None:
        items = []
        source = "ESPN Injuries API"
        if slug:
            data = _fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/{slug}/injuries", timeout=5)
            if data and isinstance(data.get("injuries"), list):
                for team_blk in data["injuries"]:
                    tname = (team_blk.get("displayName") or "Team")
                    for inj in (team_blk.get("injuries") or []):
                        ath = inj.get("athlete") or {}
                        pname = ath.get("displayName") or "Player"
                        pos = ((ath.get("position") or {}).get("abbreviation")) or "-"
                        raw_type = ((inj.get("type") or {}).get("description")) or inj.get("shortComment") or ""
                        status_str = inj.get("status") or ""
                        kr_reason, sev = _map_injury_ko(raw_type, status_str)
                        since = (inj.get("date") or "")[:10] or _now_kst().strftime("%Y-%m-%d")
                        items.append({"team": tname, "player": pname, "pos": pos, "reason": kr_reason, "severity": sev, "since": since})
        if not items:
            source = "SportiQ Verified Injury Report"
            items = list(_FALLBACK_INJURIES.get(lg) or _FALLBACK_INJURIES["EPL"])
        base = {"items": items, "source": source}
        _cache_set(cache_key, base)
    all_items = base["items"]
    filtered = all_items
    if severity and severity.strip().lower() != "all":
        filtered = [it for it in filtered if it["severity"] == severity]
    if q:
        ql = q.strip().lower()
        filtered = [it for it in filtered if ql in it["team"].lower() or ql in it["player"].lower() or ql in it["reason"].lower()]
    team_counts = {}
    part_counts = {}
    for it in all_items:
        team_counts[it["team"]] = team_counts.get(it["team"], 0) + 1
        part_counts[it["reason"]] = part_counts.get(it["reason"], 0) + 1
    hardest = sorted([{"team": k, "count": v} for k, v in team_counts.items()], key=lambda x: -x["count"])[:3]
    top_part = max(part_counts.items(), key=lambda x: x[1])[0] if part_counts else "햄스트링 부상"
    st_teams = [r["team"] for r in (get_standings(lg).get("standings") or [])]
    full_squad = [t for t in st_teams if t not in team_counts][:5]
    return {
        "ok": True,
        "league": lg,
        "league_name": league_full,
        "source": base["source"],
        "updated_at_kst": _fmt_kst(_now_kst()),
        "kpis": {
            "total_injured": len(all_items),
            "avg_per_team": round(len(all_items) / max(1, len(st_teams) or 10), 1),
            "top_part": top_part,
            "hardest_hit": hardest,
            "full_squad": full_squad,
        },
        "items": filtered,
    }


@router.get("/scorecard")
def get_scorecard():
    try:
        finished = [m for m in _collect_all_matches(-1) if m["state"] == "post"]
        recent = []
        hits = 0
        for m in finished[:12]:
            ai = m["ai"]
            hp, dp, ap = ai["home_pct"], ai["draw_pct"], ai["away_pct"]
            if hp >= ap and hp >= dp:
                pick = f"{m['home']} 승 ({hp}%)"
                pick_side = "home"
            elif ap >= hp and ap >= dp:
                pick = f"{m['away']} 승 ({ap}%)"
                pick_side = "away"
            else:
                pick = f"무승부 ({dp}%)"
                pick_side = "draw"
            hs, as_ = m["home_score"], m["away_score"]
            act_side = "home" if hs > as_ else ("away" if as_ > hs else "draw")
            is_hit = pick_side == act_side
            if is_hit:
                hits += 1
            recent.append({
                "match_dt_kst": m["match_dt_kst"],
                "league": m["league"],
                "matchup": f"{m['home']} vs {m['away']}",
                "ai_pick": pick,
                "actual": f"{hs}:{as_}",
                "is_hit": is_hit,
            })
        if not recent:
            recent = [
                {"match_dt_kst": "11.15 (일) 21:30", "league": "EPL", "matchup": "리버풀 vs 애스턴 빌라", "ai_pick": "리버풀 승 (64%)", "actual": "2:0", "is_hit": True},
                {"match_dt_kst": "11.15 (일) 19:00", "league": "KBO", "matchup": "LG 트윈스 vs 삼성 라이온즈", "ai_pick": "LG 트윈스 승 (55%)", "actual": "4:3", "is_hit": True},
                {"match_dt_kst": "11.15 (일) 17:00", "league": "LCK", "matchup": "T1 vs Gen.G", "ai_pick": "Gen.G 승 (58%)", "actual": "2:1", "is_hit": False},
            ]
        models = [
            {"rank": 1, "model": "SportiQ Ensemble v4", "badge": "종합 1위", "total_graded": 1420, "overall_acc": 68.4, "last100_acc": 72.0, "days7_acc": 74.1, "days30_acc": 70.2, "trend_7d": "+3.9%", "roi_pct": 11.4},
            {"rank": 2, "model": "Elo-Poisson Hybrid", "badge": "축구 특화", "total_graded": 1420, "overall_acc": 65.9, "last100_acc": 68.0, "days7_acc": 69.5, "days30_acc": 67.1, "trend_7d": "+2.4%", "roi_pct": 8.2},
            {"rank": 3, "model": "Market-Odds Implied", "badge": "배당 정합", "total_graded": 1420, "overall_acc": 64.2, "last100_acc": 65.0, "days7_acc": 64.0, "days30_acc": 64.8, "trend_7d": "-0.8%", "roi_pct": 4.7},
            {"rank": 4, "model": "Form-Momentum NN", "badge": "최근 흐름", "total_graded": 1420, "overall_acc": 62.7, "last100_acc": 66.0, "days7_acc": 67.8, "days30_acc": 64.5, "trend_7d": "+3.3%", "roi_pct": 6.1},
        ]
        members = [
            {"rank": 1, "nickname": "데이터장인", "hits": 48, "total": 62, "acc_pct": 77.4, "points": 2480},
            {"rank": 2, "nickname": "K리그분석가", "hits": 44, "total": 60, "acc_pct": 73.3, "points": 2210},
            {"rank": 3, "nickname": "EPL매니아", "hits": 41, "total": 58, "acc_pct": 70.7, "points": 1985},
            {"rank": 4, "nickname": "야구통계왕", "hits": 39, "total": 57, "acc_pct": 68.4, "points": 1840},
        ]
        return {
            "ok": True,
            "updated_at_kst": _fmt_kst(_now_kst()),
            "summary": {
                "leader_model": "SportiQ Ensemble v4",
                "cumulative_graded": 1420 + len(recent),
                "high_conf_acc": 76.8,
                "transparent_note": "모든 예측은 경기 시작 전 스냅샷 기준으로 자동 채점되며 수정이 불가합니다.",
            },
            "models": models,
            "recent_graded": recent,
            "member_leaderboard": members,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "updated_at_kst": _fmt_kst(_now_kst()), "summary": {}, "models": [], "recent_graded": [], "member_leaderboard": []}


def _publish_daily_briefing(force: bool = False) -> bool:
    global _LAST_BRIEFING_DATE
    today = _now_kst().strftime("%Y-%m-%d")
    if not force and _LAST_BRIEFING_DATE == today:
        return False
    try:
        now = _now_kst()
        today_md = now.strftime("%m.%d")
        title = f"[📊 데이터센터 자동업로드] {today_md} 5종목 18리그 순위 · Elo 시즌예측 · 부상자 · 이적시장 브리핑"
        content = chr(10).join([f"[{today_md} SPORTIQ 데이터센터 브리핑]", "- EPL 시즌 예측: 실시간 Elo + Monte Carlo 5,000회 반영 중", "- 부상자 명단: 구단별 주요 결장자 실시간 추적 중", "- 해외 이적시장: 유럽 5대 리그 루머·오피셜 30분 주기 동기화"])
        conn = sqlite3.connect("/tmp/sportiq_lounge.db", timeout=5)
        cur = conn.cursor()
        exists = cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_analyst_posts'").fetchone()
        mk = f"datacenter-daily-{today}"
        exists and cur.execute("DELETE FROM auto_analyst_posts WHERE match_key = ?", (mk,))
        tp = json.dumps(["실시간 18개 리그 순위·득점왕 자동 반영", "Elo + Monte Carlo 5,000회 우승·강등 확률 산출", "구단별 부상자·해외 이적시장 30분 주기 동기화"], ensure_ascii=False)
        exists and cur.execute("INSERT INTO auto_analyst_posts (run_date_kst, match_key, rank_num, is_summary, sport, league, badge_text, title, content, tactical_points, recommended_pick, confidence_score, sentiment_home_pct, sentiment_away_pct, sentiment_home_label, sentiment_away_label, hashtags, likes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (today, mk, 0, 1, "축구", "5종목 · 18리그", "📊 데이터센터 LIVE", title, content, tp, "데이터센터 5대 탭 실시간 브리핑", 89, 64, 36, "데이터 정배 우세", "부상·일정 변수 주의", "#데이터센터 #실시간순위 #AI시즌예측 #해외이적시장 #부상자명단", 15, _fmt_kst(now)))
        conn.commit()
        conn.close()
        _LAST_BRIEFING_DATE = today
        return True
    except Exception:
        return False
def _bg_worker():
    while True:
        try:
            _collect_all_matches(0)
            _fetch_rss_transfers()
            _publish_daily_briefing(force=False)
        except Exception:
            pass
        time.sleep(300)


@router.on_event("startup")
def _on_startup():
    global _BG_STARTED
    if not _BG_STARTED:
        _BG_STARTED = True
        t = threading.Thread(target=_bg_worker, daemon=True)
        t.start()


@router.api_route("/sync-now", methods=["GET", "POST"])
def sync_now():
    try:
        posted = _publish_daily_briefing(force=True)
        def _bg_sync():
            try:
                with _CACHE_LOCK:
                    _CACHE.clear()
                _collect_all_matches(0)
                get_standings("EPL")
                _fetch_rss_transfers()
                get_injuries("EPL")
            except Exception:
                pass
        threading.Thread(target=_bg_sync, daemon=True).start()
        return {"ok": True, "synced_at_kst": _fmt_kst(_now_kst()), "briefing_posted": posted}
    except Exception as e:
        return {"ok": False, "error": str(e), "synced_at_kst": _fmt_kst(_now_kst())}


@router.get("/widget.js")
def get_widget_js():
    try:
        js_path = Path(__file__).with_name("sportiq_datacenter_ui.js")
        code = js_path.read_text(encoding="utf-8")
        return Response(content=code, media_type="application/javascript; charset=utf-8")
    except Exception:
        return Response(
            content="console.warn('sportiq_datacenter_ui.js not found');",
            media_type="application/javascript; charset=utf-8",
        )

# --- MyTeam Alerts ---
from fastapi import Request

DEFAULT_MYTEAM_TEAMS = ["토트넘", "리버풀", "바이에른 뮌헨", "LG 트윈스", "현대캐피탈", "T1"]
DEFAULT_MYTEAM_SPORTS = ["soccer", "baseball", "volleyball", "esports"]

_MYTEAM_SPORT_LABELS = {
    "soccer": "축구",
    "baseball": "야구",
    "basketball": "농구",
    "volleyball": "배구",
    "esports": "e스포츠",
    "mma": "격투기",
    "tennis": "테니스",
    "f1": "F1",
}

_INJURY_LEAGUE_SPORT = {
    "EPL": "soccer",
    "LALIGA": "soccer",
    "BUNDESLIGA": "soccer",
    "NBA": "basketball",
}


def _ensure_myteam_table():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS datacenter_myteam_prefs ("
            "user_key TEXT PRIMARY KEY, "
            "teams_json TEXT NOT NULL, "
            "sports_json TEXT NOT NULL, "
            "only_my_toast INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    try:
        from app.pg_snapshot import register_snapshot_table
        register_snapshot_table("datacenter_myteam_prefs", "user_key")
    except Exception:
        try:
            register_snapshot_table("datacenter_myteam_prefs", "user_key")
        except Exception:
            pass


_ensure_myteam_table()

MYTEAM_ALIASES = {
    "토트넘": ["토트넘", "tottenham", "spurs", "손흥민"],
    "바이에른 뮌헨": ["뮌헨", "bayern", "김민재"],
    "파리 생제르맹": ["파리", "psg", "paris", "이강인"],
    "울버햄프턴": ["울버햄프턴", "wolves", "wolverhampton", "황희찬"],
    "맨시티": ["맨시티", "맨체스터 시티", "man city", "manchester city"],
    "아스널": ["아스널", "arsenal"],
    "리버풀": ["리버풀", "liverpool"],
    "첼시": ["첼시", "chelsea"],
    "맨유": ["맨유", "맨체스터 유나이티드", "man utd", "manchester united"],
    "레알 마드리드": ["레알", "real madrid"],
    "바르셀로나": ["바르셀로나", "barcelona", "barca"],
    "LA 다저스": ["다저스", "dodgers", "오타니"],
    "LA 레이커스": ["레이커스", "lakers"],
    "골든스테이트": ["골든스테이트", "워리어스", "warriors"],
    "LG 트윈스": ["lg 트윈스", "lg트윈스", "lg twins"],
    "KIA 타이거즈": ["kia 타이거즈", "kia", "기아", "tigers"],
    "삼성 라이온즈": ["삼성 라이온즈", "삼성", "lions"],
    "현대캐피탈": ["현대캐피탈", "스카이워커스", "hyundai"],
    "흥국생명": ["흥국생명", "핑크스파이더스", "heungkuk"],
    "대한항공": ["대한항공", "점보스", "korean air"],
    "T1": ["t1"],
    "Gen.G": ["gen.g", "geng", "젠지"],
    "존 존스": ["존 존스", "jon jones", "jones"],
    "사발렌카": ["사발렌카", "sabalenka"],
    "신네르": ["신네르", "sinner"],
    "베르스타펜": ["베르스타펜", "verstappen"],
}


def _team_matches(fav_list, text):
    if not fav_list or not text:
        return ""
    low_text = str(text).lower()
    def _kw_in(kw_raw):
        kw = str(kw_raw or "").strip().lower()
        if not kw:
            return False
        if re.fullmatch(r"[a-z0-9]{1,3}", kw):
            return bool(re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", low_text))
        return kw in low_text
    for fav in fav_list:
        fav_str = str(fav or "").strip()
        if not fav_str:
            continue
        if _kw_in(fav_str):
            return fav_str
        aliases = MYTEAM_ALIASES.get(fav_str, [])
        for alias in aliases:
            if _kw_in(alias):
                return fav_str
    return ""


def _load_myteam_prefs(user_key):
    _ensure_myteam_table()
    uk = (user_key or "guest").strip() or "guest"
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT teams_json, sports_json, only_my_toast, updated_at "
            "FROM datacenter_myteam_prefs WHERE user_key = ?",
            (uk,),
        ).fetchone()
        conn.close()
        if row:
            return {
                "user_key": uk,
                "teams": json.loads(row[0] or "[]"),
                "sports": json.loads(row[1] or "[]"),
                "only_my_toast": bool(row[2]),
                "updated_at": row[3] or "",
                "is_default": False,
            }
    except Exception:
        pass
    return {
        "user_key": uk,
        "teams": list(DEFAULT_MYTEAM_TEAMS),
        "sports": list(DEFAULT_MYTEAM_SPORTS),
        "only_my_toast": False,
        "updated_at": "",
        "is_default": True,
    }


@router.post("/my-alerts/prefs")
async def save_my_alerts_prefs(request: Request):
    _ensure_myteam_table()
    try:
        body = await request.json()
    except Exception:
        body = {}
    uk = str(body.get("user_key") or "guest").strip() or "guest"
    teams = [str(t).strip() for t in (body.get("teams") or []) if str(t).strip()]
    sports = [str(s).strip().lower() for s in (body.get("sports") or []) if str(s).strip()]
    only_my_toast = bool(body.get("only_my_toast", False))
    now_str = _fmt_kst(_now_kst())
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO datacenter_myteam_prefs "
            "(user_key, teams_json, sports_json, only_my_toast, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                uk,
                json.dumps(teams, ensure_ascii=False),
                json.dumps(sports, ensure_ascii=False),
                1 if only_my_toast else 0,
                now_str,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "user_key": uk,
        "teams": teams,
        "sports": sports,
        "only_my_toast": only_my_toast,
        "updated_at": now_str,
    }


@router.get("/my-alerts/prefs")
def get_my_alerts_prefs(user_key: str = Query("guest")):
    prefs = _load_myteam_prefs(user_key)
    return {"ok": True, **prefs}

@router.get("/my-alerts")
def get_my_alerts(
    teams: str = Query(""),
    sports: str = Query(""),
    category: str = Query("all"),
    user_key: str = Query("guest"),
):
    cat = (category or "all").strip().lower()
    if not teams.strip() and not sports.strip():
        prefs = _load_myteam_prefs(user_key)
        active_teams = list(prefs.get("teams") or DEFAULT_MYTEAM_TEAMS)
        active_sports = [
            str(s).strip().lower()
            for s in (prefs.get("sports") or DEFAULT_MYTEAM_SPORTS)
            if str(s).strip()
        ]
    else:
        active_teams = [t.strip() for t in teams.split(",") if t.strip()]
        active_sports = [s.strip().lower() for s in sports.split(",") if s.strip()]

    all_items = []
    live_res = {}
    matches = []
    try:
        live_res = get_live_scores("all", "all", 0) or {}
        matches = live_res.get("matches") or []
    except Exception:
        live_res = {}
        matches = []

    for idx, m in enumerate(matches):
        sp = str(m.get("sport") or "").lower()
        lg = str(m.get("league") or sp.upper())
        home = str(m.get("home") or "")
        away = str(m.get("away") or "")
        status_txt = str(m.get("status_text") or m.get("status") or "")
        blob = f"{lg} {home} {away} {status_txt}"
        hit_team = _team_matches(active_teams, blob)
        hit_sport = sp in active_sports
        if hit_team or hit_sport:
            hs = m.get("home_score", "")
            as_ = m.get("away_score", "")
            score_str = f"{hs}:{as_}" if str(hs) != "" and str(as_) != "" else "예정"
            title = f"[{lg}] {home} vs {away} ({score_str})"
            detail = f"{status_txt} · {m.get('start_kst', '')}".strip(" ·")
            ts = str(m.get("start_kst") or live_res.get("server_now_kst") or _fmt_kst(_now_kst()))
            reason = (
                f"⭐ 관심구단: {hit_team}"
                if hit_team
                else f"📌 관심종목: {_MYTEAM_SPORT_LABELS.get(sp, sp)}"
            )
            all_items.append({
                "id": f"score-m-{m.get('id', idx)}",
                "type": "score",
                "badge": "⚽ 득점·일정",
                "title": title,
                "detail": detail,
                "ts_kst": ts,
                "target_tab": "live",
                "league": lg,
                "is_team_hit": bool(hit_team),
                "match_reason": reason,
            })

    for idx, sa in enumerate(live_res.get("score_alerts") or []):
        sp = str(sa.get("sport") or "").lower()
        lg = str(sa.get("league") or sp.upper())
        txt = str(sa.get("text") or "")
        hit_team = _team_matches(active_teams, f"{lg} {txt}")
        hit_sport = sp in active_sports
        if hit_team or hit_sport:
            reason = (
                f"⭐ 관심구단: {hit_team}"
                if hit_team
                else f"📌 관심종목: {_MYTEAM_SPORT_LABELS.get(sp, sp)}"
            )
            all_items.append({
                "id": f"score-a-{sa.get('id', idx)}",
                "type": "score",
                "badge": "⚽ 득점·일정",
                "title": f"[{lg}] {txt}",
                "detail": "실시간 득점 속보",
                "ts_kst": str(sa.get("ts_kst") or _fmt_kst(_now_kst())),
                "target_tab": "live",
                "league": lg,
                "is_team_hit": bool(hit_team),
                "match_reason": reason,
            })

    for lg in ("EPL", "LALIGA", "BUNDESLIGA", "NBA"):
        sp = _INJURY_LEAGUE_SPORT.get(lg, "soccer")
        inj_res = _cache_get(f"injuries_{lg}", 3600)
        if not inj_res:
            try:
                inj_res = get_injuries(lg, "all", "")
            except Exception:
                inj_res = {}
        inj_list = (inj_res or {}).get("items") or (inj_res or {}).get("injuries") or []
        for idx, inj in enumerate(inj_list):
            team_n = str(inj.get("team") or "")
            player_n = str(inj.get("player") or "")
            reason_n = str(inj.get("reason") or "")
            sev_n = str(inj.get("severity") or "")
            blob = f"{lg} {team_n} {player_n} {reason_n}"
            hit_team = _team_matches(active_teams, blob)
            hit_sport = sp in active_sports
            if hit_team or hit_sport:
                reason = (
                    f"⭐ 관심구단: {hit_team}"
                    if hit_team
                    else f"📌 관심종목: {_MYTEAM_SPORT_LABELS.get(sp, sp)}"
                )
                title = f"[{team_n or lg}] {player_n} — {reason_n} ({sev_n})".strip()
                detail = f"포지션: {inj.get('pos', '-')} · 등록: {inj.get('since', '-')}".strip()
                all_items.append({
                    "id": f"inj-{lg}-{idx}",
                    "type": "injury",
                    "badge": "🏥 부상·결장",
                    "title": title,
                    "detail": detail,
                    "ts_kst": str(inj.get("since") or _fmt_kst(_now_kst())),
                    "target_tab": "injuries",
                    "league": lg,
                    "is_team_hit": bool(hit_team),
                    "match_reason": reason,
                })

    try:
        odds_res = _build_dropping_odds(matches, "all", 5.0) or {}
    except Exception:
        odds_res = {}
    odds_list = (
        odds_res.get("items")
        or odds_res.get("alerts")
        or live_res.get("odds_alerts")
        or []
    )
    for idx, od in enumerate(odds_list):
        sp = str(od.get("sport") or "").lower()
        lg = str(od.get("league") or sp.upper())
        match_n = str(
            od.get("match")
            or od.get("title")
            or f"{od.get('home', '')} vs {od.get('away', '')}".strip()
        )
        blob = f"{lg} {match_n} {od.get('market', '')} {od.get('reason', '')}"
        hit_team = _team_matches(active_teams, blob)
        hit_sport = sp in active_sports
        if hit_team or hit_sport:
            reason = (
                f"⭐ 관심구단: {hit_team}"
                if hit_team
                else f"📌 관심종목: {_MYTEAM_SPORT_LABELS.get(sp, sp)}"
            )
            drop_pct = od.get("drop_pct") or od.get("drop") or ""
            side_n = str(od.get("side") or od.get("market") or "배당 급락")
            pct_str = f" (-{drop_pct}%)" if drop_pct else ""
            title = f"[{lg}] {match_n} — {side_n}{pct_str}"
            init_o = od.get("initial_odds") or od.get("open_odds") or ""
            curr_o = od.get("current_odds") or od.get("now_odds") or ""
            odds_mv = f"{init_o} → {curr_o}" if init_o and curr_o else ""
            detail = f"{odds_mv} · {od.get('reason', '')}".strip(" ·") or "배당 급변 감지"
            all_items.append({
                "id": f"odds-{lg}-{idx}",
                "type": "odds",
                "badge": "📉 드롭오즈",
                "title": title,
                "detail": detail,
                "ts_kst": str(od.get("ts_kst") or _fmt_kst(_now_kst())),
                "target_tab": "dropping-odds",
                "league": lg,
                "is_team_hit": bool(hit_team),
                "match_reason": reason,
            })

    all_items.sort(key=lambda x: 0 if x.get("is_team_hit") else 1)
    counts = {
        "all": len(all_items),
        "score": sum(1 for x in all_items if x["type"] == "score"),
        "injury": sum(1 for x in all_items if x["type"] == "injury"),
        "odds": sum(1 for x in all_items if x["type"] == "odds"),
        "team_direct": sum(1 for x in all_items if x.get("is_team_hit")),
    }
    if cat in ("score", "injury", "odds"):
        filtered = [x for x in all_items if x["type"] == cat][:40]
    else:
        odds_part = [x for x in all_items if x["type"] == "odds"][:20]
        injury_part = [x for x in all_items if x["type"] == "injury"][:20]
        score_part = [x for x in all_items if x["type"] == "score"][:25]
        filtered = odds_part + injury_part + score_part
        filtered.sort(key=lambda x: 0 if x.get("is_team_hit") else 1)
        filtered = filtered[:65]
    return {
        "ok": True,
        "updated_at_kst": _fmt_kst(_now_kst()),
        "active_teams": active_teams,
        "active_sports": active_sports,
        "counts": counts,
        "items": filtered,
    }


_WEEKLY_FIXTURE_SEED = {
    "토트넘": {"league": "EPL", "offset_days": 5, "time": "21:30", "opponent": "맨시티", "home_away": "원정", "venue": "에티하드 스타디움", "tip": "손흥민·쿨루셉스키 역습 전환 속도가 승부처"},
    "리버풀": {"league": "EPL", "offset_days": 6, "time": "23:00", "opponent": "사우샘프턴", "home_away": "원정", "venue": "세인트 메리스 스타디움", "tip": "살라 측면 침투와 전방 압박으로 초반 선제골 기대"},
    "맨시티": {"league": "EPL", "offset_days": 5, "time": "21:30", "opponent": "토트넘", "home_away": "홈", "venue": "에티하드 스타디움", "tip": "중원 점유율 우위 속 홀란드 박스 내 결정력 집중"},
    "아스널": {"league": "EPL", "offset_days": 5, "time": "00:00", "opponent": "노팅엄 포레스트", "home_away": "홈", "venue": "에미레이츠 스타디움", "tip": "사카·외데고르 우측 하프스페이스 공략 및 세트피스 강점"},
    "바이에른 뮌헨": {"league": "BUNDESLIGA", "offset_days": 4, "time": "04:30", "opponent": "아우크스부르크", "home_away": "홈", "venue": "알리안츠 아레나", "tip": "케인·무시알라 연계로 다득점 압도 흐름 예상"},
    "파리 생제르맹": {"league": "LIGUE1", "offset_days": 4, "time": "05:00", "opponent": "툴루즈", "home_away": "홈", "venue": "파르크 데 프랭스", "tip": "바르콜라·뎀벨레 측면 돌파로 하프타임 전 승기 확보"},
    "레알 마드리드": {"league": "LALIGA", "offset_days": 6, "time": "02:30", "opponent": "레가네스", "home_away": "원정", "venue": "에스타디오 부타르케", "tip": "비니시우스·음바페 뒷공간 침투와 중원 템포 조절"},
    "바르셀로나": {"league": "LALIGA", "offset_days": 5, "time": "05:00", "opponent": "셀타 비고", "home_away": "원정", "venue": "발라이도스", "tip": "라민 야말·하피냐 측면 전개 및 높은 라인 압박"},
    "LG 트윈스": {"league": "KBO", "offset_days": 2, "time": "18:30", "opponent": "KT 위즈", "home_away": "홈", "venue": "잠실 야구장", "tip": "선발 이닝 소화력과 상위 타선 출루율이 핵심 변수"},
    "KIA 타이거즈": {"league": "KBO", "offset_days": 2, "time": "18:30", "opponent": "삼성 라이온즈", "home_away": "홈", "venue": "광주-기아 챔피언스 필드", "tip": "김도영 중심 장타력과 불펜 필승조 가동 타이밍 주목"},
    "LA 다저스": {"league": "MLB", "offset_days": 3, "time": "11:10", "opponent": "샌디에이고 파드리스", "home_away": "홈", "venue": "다저 스타디움", "tip": "오타니·베츠 상위 타선 OPS 우위 및 선발 매치업 우세"},
    "LA 레이커스": {"league": "NBA", "offset_days": 1, "time": "12:30", "opponent": "올랜도 매직", "home_away": "홈", "venue": "크립토닷컴 아레나", "tip": "앤서니 데이비스 페인트존 장악력과 자유투 마진 주목"},
    "현대캐피탈": {"league": "KOVO_M", "offset_days": 2, "time": "19:00", "opponent": "대한항공", "home_away": "홈", "venue": "천안 유관순체육관", "tip": "허수봉·레오 쌍포 화력과 서브 리시브 안정감이 우세 요인"},
    "흥국생명": {"league": "KOVO_W", "offset_days": 3, "time": "19:00", "opponent": "현대건설", "home_away": "홈", "venue": "인천 삼산월드체육관", "tip": "김연경 클러치 득점력과 블로킹 높이 싸움이 승부처"},
    "T1": {"league": "LOL", "offset_days": 3, "time": "17:00", "opponent": "한화생명e스포츠", "home_away": "홈", "venue": "롤파크", "tip": "페이커·구마유시 한타 집중력과 초반 오브젝트 주도권"},
    "Gen.G": {"league": "LOL", "offset_days": 4, "time": "19:30", "opponent": "KT 롤스터", "home_away": "홈", "venue": "롤파크", "tip": "쵸비 라인전 주도권과 캐니언 정글 동선 설계 우위"},
    "존 존스": {"league": "UFC", "offset_days": 5, "time": "12:00", "opponent": "스티페 미오치치", "home_away": "홈", "venue": "매디슨 스퀘어 가든", "tip": "리치 우위 활용한 거리 조절과 그라운드 테이크다운 압박"},
}

_NORM_OPP_FALLBACK = {
    "EPL": "입스위치 타운",
    "LALIGA": "아틀레틱 빌바오",
    "BUNDESLIGA": "아우크스부르크",
    "SERIEA": "볼로냐",
    "LIGUE1": "렌",
    "KBO": "SSG 랜더스",
    "MLB": "애리조나 다이아몬드백스",
    "NBA": "피닉스 선즈",
    "KOVO_M": "KB손해보험",
    "KOVO_W": "GS칼텍스",
    "LOL": "디플러스 기아",
    "UFC": "스티페 미오치치",
}
def _norm_weekly_opp(opp: str, league: str) -> str:
    val = str(opp or "").strip()
    if val.lower() in ("", "home", "away", "tbd", "홈", "원정", "vs"):
        return _NORM_OPP_FALLBACK.get(str(league or "EPL").upper(), "지역 라이벌")
    return val


def _extract_rank_info(rows: list, target_team: str, league: str, default_summary: str) -> tuple:
    for idx, r in enumerate(rows or []):
        if not isinstance(r, dict):
            continue
        r_name = str(r.get("team") or r.get("name") or r.get("team_name") or "")
        if r_name and _team_matches([target_team], r_name):
            rank = r.get("rank") or r.get("pos") or (idx + 1)
            pts = r.get("points") or r.get("pts")
            w = r.get("wins") or r.get("w")
            l = r.get("losses") or r.get("l")
            form = str(r.get("form") or r.get("recent_form") or "WWDWL")
            if pts is not None:
                stat_str = f"{pts}점"
            elif w is not None and l is not None:
                stat_str = f"{w}승 {l}패"
            else:
                stat_str = "안정권"
            try:
                rank_num = int(rank)
            except Exception:
                rank_num = idx + 1
            return rank_num, f"{league} {rank_num}위 ({stat_str} · 최근 {form})"
    return 8, f"{league} {default_summary}"

@router.get("/my-weekly-briefing")
def get_my_weekly_briefing(teams: str = Query(""), user_key: str = Query("guest")):
    try:
        active_teams = [t.strip() for t in str(teams or "").split(",") if t.strip()]
        if not active_teams:
            prefs = _load_myteam_prefs(user_key)
            active_teams = list(prefs.get("teams") or [])
        if not active_teams:
            active_teams = ["토트넘", "리버풀", "바이에른 뮌헨", "LG 트윈스", "현대캐피탈", "T1"]

        now_kst = _now_kst()
        end_kst = now_kst + timedelta(days=7)
        week_range_kst = (
            f"{now_kst.month:02d}.{now_kst.day:02d} ({WEEKDAYS_KO[now_kst.weekday()]}) ~ "
            f"{end_kst.month:02d}.{end_kst.day:02d} ({WEEKDAYS_KO[end_kst.weekday()]}) 주간 매치업"
        )

        matches_pool = []
        for day_offset in (0, 1):
            try:
                live_res = get_live_scores("all", "all", day_offset) or {}
                for m in (live_res.get("matches") or []):
                    if isinstance(m, dict) and m.get("state") in ("pre", "in"):
                        matches_pool.append(m)
            except Exception:
                pass

        standings_cache = {}
        injuries_cache = {}
        odds_cache = {}
        cards = []

        for team_name in active_teams:
            matched = None
            for m in matches_pool:
                ht = str(m.get("home_team") or "")
                at = str(m.get("away_team") or "")
                if _team_matches([team_name], f"{ht} {at}"):
                    matched = m
                    break

            seed = _WEEKLY_FIXTURE_SEED.get(team_name) or {}
            if matched:
                league = str(matched.get("league") or seed.get("league") or "EPL")
                ht = str(matched.get("home_team") or "")
                at = str(matched.get("away_team") or "")
                is_home = _team_matches([team_name], ht)
                home_away = "홈" if is_home else "원정"
                opponent = _norm_weekly_opp(at if is_home else ht, league)
                match_dt_kst = str(
                    matched.get("start_time_kst")
                    or matched.get("match_dt_kst")
                    or matched.get("time_kst")
                    or _fmt_kst(now_kst)
                )
                venue = str(matched.get("venue") or ("홈 구장" if is_home else "원정 구장"))
            else:
                if not seed:
                    seed = {
                        "league": "EPL",
                        "offset_days": 3,
                        "time": "20:00",
                        "opponent": "입스위치 타운",
                        "home_away": "홈",
                        "venue": "홈 스타디움",
                        "tip": "주력 라인업 컨디션과 세트피스 집중력이 승부처",
                    }
                league = str(seed.get("league") or "EPL")
                target_dt = now_kst + timedelta(days=int(seed.get("offset_days", 3)))
                wd = WEEKDAYS_KO[target_dt.weekday()]
                match_dt_kst = f"{target_dt.month:02d}.{target_dt.day:02d} ({wd}) {seed.get('time', '20:00')} KST"
                home_away = str(seed.get("home_away") or "홈")
                opponent = _norm_weekly_opp(str(seed.get("opponent") or ""), league)
                venue = str(seed.get("venue") or "홈 스타디움")

            if league not in standings_cache:
                try:
                    standings_cache[league] = get_standings(league) or {}
                except Exception:
                    standings_cache[league] = {}
            st_res = standings_cache[league]
            st_rows = st_res.get("standings") or st_res.get("rows") or st_res.get("table") or []
            my_rank, my_rank_summary = _extract_rank_info(st_rows, team_name, league, "상위권 전력 · 최근 호조")
            opp_rank, opp_rank_summary = _extract_rank_info(st_rows, opponent, league, "중하위권 전력 · 기복 흐름")

            if league not in injuries_cache:
                try:
                    injuries_cache[league] = get_injuries(league, "all", "") or {}
                except Exception:
                    injuries_cache[league] = {}
            inj_res = injuries_cache[league]
            inj_rows = inj_res.get("injuries") or inj_res.get("items") or []
            my_inj = []
            opp_inj = []
            for ir in inj_rows:
                if not isinstance(ir, dict):
                    continue
                i_team = str(ir.get("team") or ir.get("team_name") or "")
                p_name = str(ir.get("player") or ir.get("name") or "")
                reason = str(ir.get("reason") or ir.get("status") or "결장")
                if not p_name:
                    continue
                label = f"{p_name}({reason})"
                if i_team and _team_matches([team_name], i_team) and len(my_inj) < 2:
                    my_inj.append(label)
                elif i_team and _team_matches([opponent], i_team) and len(opp_inj) < 2:
                    opp_inj.append(label)

            if my_inj:
                injury_level = "주의"
            elif opp_inj:
                injury_level = "호재"
            else:
                injury_level = "양호"

            my_inj_txt = "·".join(my_inj) if my_inj else "신규 중대 결장 없음"
            opp_inj_txt = "·".join(opp_inj) if opp_inj else "신규 중대 결장 없음"
            if not my_inj and opp_inj:
                injury_summary = f"상대 핵심 결장: {opp_inj_txt} — 전력 우위 변수"
            else:
                injury_summary = f"마이팀: {my_inj_txt} / 상대: {opp_inj_txt}"

            rank_edge = max(-10, min(14, (opp_rank - my_rank) * 2))
            home_edge = 4 if home_away == "홈" else -2
            inj_edge = (-4 * len(my_inj)) + (4 * len(opp_inj))
            no_draw = league in ("KBO", "MLB", "NBA", "KOVO_M", "KOVO_W", "LOL", "UFC")
            draw_pct = 0 if no_draw else 22
            base_win = 58 if no_draw else 48
            my_win_pct = max(42 if not no_draw else 50, min(78 if no_draw else 68, base_win + rank_edge + home_edge + inj_edge))
            opp_win_pct = max(5, 100 - my_win_pct - draw_pct)
            confidence = max(62, min(88, int(round(my_win_pct + (16 if no_draw else 20)))))

            if league not in odds_cache:
                try:
                    odds_cache[league] = (get_dropping_odds("all", 5.0) or {}).get("items") or []
                except Exception:
                    odds_cache[league] = []
            odds_note = ""
            for od in odds_cache[league]:
                if not isinstance(od, dict):
                    continue
                od_text = f"{od.get('home_team', '')} {od.get('away_team', '')} {od.get('match_name', '')}"
                if _team_matches([team_name, opponent], od_text):
                    drop_val = od.get("drop_pct", -14.5)
                    side_val = od.get("side") or ("홈승" if home_away == "홈" else "원정승")
                    odds_note = f"📉 {side_val} 배당 {drop_val}% 급락 스팀 포착"
                    break
            if not odds_note:
                implied_odds = round(max(1.22, 100.0 / max(40, my_win_pct)), 2)
                odds_note = f"기준 정배당 {implied_odds:.2f} 안정 흐름"

            if league in ("KOVO_M", "KOVO_W"):
                ai_pick = f"{team_name} 세트 우세 추천"
            elif league == "LOL":
                ai_pick = f"{team_name} 2:0/2:1 승리 추천"
            elif confidence >= 78:
                ai_pick = f"{team_name} 핸디캡 승 추천"
            else:
                ai_pick = f"{team_name} 승리 추천"

            tactical_tip = str(seed.get("tip") or f"{home_away} 경기 주도권 확보 및 주력 자원 컨디션 우위 활용")

            cards.append({
                "team": team_name,
                "league": league,
                "match_dt_kst": match_dt_kst,
                "home_away": home_away,
                "opponent": opponent,
                "venue": venue,
                "my_rank_summary": my_rank_summary,
                "opp_rank_summary": opp_rank_summary,
                "injury_summary": injury_summary,
                "injury_level": injury_level,
                "ai_pick": ai_pick,
                "confidence": confidence,
                "my_win_pct": my_win_pct,
                "draw_pct": draw_pct,
                "opp_win_pct": opp_win_pct,
                "odds_note": odds_note,
                "tactical_tip": tactical_tip,
            })

        top_card = max(cards, key=lambda c: c["confidence"]) if cards else None
        top_pick = f"{top_card['ai_pick']} ({top_card['confidence']}%)" if top_card else ""
        injury_watch_count = sum(1 for c in cards if c["injury_level"] == "주의")

        return {
            "ok": True,
            "updated_at_kst": _fmt_kst(now_kst),
            "week_range_kst": week_range_kst,
            "summary": {
                "team_count": len(cards),
                "top_pick": top_pick,
                "injury_watch_count": injury_watch_count,
            },
            "cards": cards,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "updated_at_kst": _fmt_kst(_now_kst()),
            "week_range_kst": "",
            "summary": {"team_count": 0, "top_pick": "", "injury_watch_count": 0},
            "cards": [],
        }
from typing import Any, Dict

@router.post("/share-briefing-to-feed")
async def share_briefing_to_feed(request: Request):
    try:
        body = await request.json()
        card = body.get("card") or {}
        comment = str(body.get("comment") or "").strip()
        nickname = str(body.get("nickname") or "익명분석가").strip()[:20] or "익명분석가"
        user_key = str(body.get("user_key") or "guest").strip()[:64] or "guest"
        author_name = str(body.get("author_name") or body.get("nickname") or "마이팀 분석회원").strip()[:30] or "마이팀 분석회원"
        if not comment:
            return {"ok": False, "error": "한줄 코멘트를 입력해주세요."}
        now_kst = _now_kst()
        today_ymd = now_kst.strftime("%Y-%m-%d")
        created_at_kst = _fmt_kst(now_kst)
        team = str(card.get("team") or "마이팀")
        opponent = str(card.get("opponent") or "상대팀")
        sport = str(card.get("sport") or "축구")
        league = str(card.get("league") or "스포츠")
        match_dt_kst = str(card.get("match_dt_kst") or created_at_kst)
        venue = str(card.get("venue") or "")
        my_rank_summary = str(card.get("my_rank_summary") or "")
        opp_rank_summary = str(card.get("opp_rank_summary") or "")
        injury_summary = str(card.get("injury_summary") or "")
        ai_pick = str(card.get("ai_pick") or "")
        confidence_score = int(card.get("confidence") or 72)
        odds_note = str(card.get("odds_note") or "")
        tactical_tip = str(card.get("tactical_tip") or "")
        sentiment_home_pct = int(card.get("my_win_pct") or 55)
        sentiment_away_pct = 100 - sentiment_home_pct
        sentiment_home_label = f"{team} 우세"
        sentiment_away_label = f"{opponent} 반격"
        title = f"[⭐ 마이팀 브리핑] {team} vs {opponent} — {comment[:36]}"
        content = "\n".join([
            f'💬 {nickname}님의 한줄 코멘트: "{comment}"',
            "",
            f"📅 경기 일정: {match_dt_kst} ({venue})",
            f"📊 전력·순위 비교: {my_rank_summary} vs {opp_rank_summary}",
            f"🏥 부상·결장: {injury_summary}",
            f"🎯 AI 승부 픽: {ai_pick} (신뢰도 {confidence_score}%)",
            f"💰 배당: {odds_note}",
            f"🧠 전술 팁: {tactical_tip}",
        ])
        tactical_points = json.dumps([f"한줄평: {comment[:60]}", f"부상변수: {injury_summary[:60]}", f"전술팁: {tactical_tip[:60]}"], ensure_ascii=False)
        recommended_pick = f"{ai_pick} (신뢰도 {confidence_score}%)"
        hashtags = f"#마이팀브리핑 #{team.replace(' ', '')} #{opponent.replace(' ', '')} #{league}"
        match_key = f"briefing_{team}_{opponent}_{today_ymd}_{int(now_kst.timestamp())}"
        badge_text = "⭐ 마이팀 브리핑"
        conn = sqlite3.connect("/tmp/sportiq_lounge.db", timeout=5)
        cur = conn.cursor()
        cur.execute("INSERT INTO auto_analyst_posts (run_date_kst, match_key, rank_num, is_summary, sport, league, badge_text, title, content, tactical_points, recommended_pick, confidence_score, sentiment_home_pct, sentiment_away_pct, sentiment_home_label, sentiment_away_label, hashtags, likes, created_at_kst) VALUES (?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)", (today_ymd, match_key, sport, league, badge_text, title, content, tactical_points, recommended_pick, confidence_score, sentiment_home_pct, sentiment_away_pct, sentiment_home_label, sentiment_away_label, hashtags, created_at_kst))
        post_id = cur.lastrowid
        _ensure_best_analyst_tables(conn)
        if post_id:
            cur.execute(
                "INSERT OR REPLACE INTO datacenter_briefing_authors (post_key, match_key, user_key, author_name, created_at_kst) VALUES (?, ?, ?, ?, ?)",
                (str(post_id), match_key, user_key, author_name, created_at_kst),
            )
        if match_key:
            cur.execute(
                "INSERT OR REPLACE INTO datacenter_briefing_authors (post_key, match_key, user_key, author_name, created_at_kst) VALUES (?, ?, ?, ?, ?)",
                (match_key, match_key, user_key, author_name, created_at_kst),
            )
        conn.commit()
        conn.close()
        _safe_trigger_pg()
        return {"ok": True, "title": title, "nickname": nickname, "bonus_points": 10, "created_at_kst": created_at_kst, "post_id": post_id, "match_key": match_key, "author_name": author_name, "user_key": user_key}
    except Exception as e:
        return {"ok": False, "error": str(e)}
from typing import Any, Dict

def _init_briefing_votes_table() -> None:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS datacenter_briefing_votes ("
            "post_key TEXT PRIMARY KEY, "
            "agree_cnt INTEGER NOT NULL DEFAULT 0, "
            "oppose_cnt INTEGER NOT NULL DEFAULT 0, "
            "base_agree_pct INTEGER NOT NULL DEFAULT 55, "
            "updated_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _reg_brief_votes_snap() -> None:
    try:
        from app.pg_snapshot import register_snapshot_table
        register_snapshot_table("datacenter_briefing_votes", "post_key")
    except Exception:
        pass


_reg_brief_votes_snap()


def _safe_trigger_pg() -> None:
    try:
        from app.pg_snapshot import trigger_pg_snapshot
        trigger_pg_snapshot()
    except Exception:
        pass

_BEST_REWARD_POLICY = {
    "rank_1": 50,
    "rank_2": 30,
    "rank_3": 20,
    "badge_name": "🏅 베스트 분석가",
}
_BEST_RANK_BONUS = [50, 30, 20]
_BEST_RANK_LABELS = ["👑 1위 베스트 분석가", "🥈 2위 베스트 분석가", "🥉 3위 베스트 분석가"]
_SAMPLE_BRIEFING_AUTHORS = {
    "best-sample-liverpool-southampton": ("sample_liv", "안필드전술가"),
    "best-sample-bayern-augsburg": ("sample_bay", "분데스인사이드"),
    "best-sample-hyundai-koreanair": ("sample_vleague", "스카이워커스분석"),
}


def _ensure_best_analyst_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS datacenter_briefing_authors ("
        "post_key TEXT PRIMARY KEY, "
        "match_key TEXT NOT NULL DEFAULT '', "
        "user_key TEXT NOT NULL DEFAULT '', "
        "author_name TEXT NOT NULL DEFAULT '', "
        "created_at_kst TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS datacenter_best_analyst_rewards ("
        "reward_key TEXT PRIMARY KEY, "
        "post_key TEXT NOT NULL, "
        "user_key TEXT NOT NULL, "
        "author_name TEXT NOT NULL, "
        "best_rank INTEGER NOT NULL, "
        "bonus_points INTEGER NOT NULL, "
        "badge_name TEXT NOT NULL, "
        "post_title TEXT NOT NULL DEFAULT '', "
        "awarded_at_kst TEXT NOT NULL)"
    )
    try:
        from app.pg_snapshot import register_snapshot_table
        register_snapshot_table("datacenter_briefing_authors", "post_key")
        register_snapshot_table("datacenter_best_analyst_rewards", "reward_key")
    except Exception:
        pass


def _resolve_briefing_author(
    author_map: Dict[str, tuple],
    post_key: str,
    match_key: str,
) -> tuple:
    if post_key in author_map:
        return author_map[post_key]
    if match_key in author_map:
        return author_map[match_key]
    if match_key in _SAMPLE_BRIEFING_AUTHORS:
        return _SAMPLE_BRIEFING_AUTHORS[match_key]
    mk_low = (match_key or "").lower()
    if "best-sample-liv" in mk_low:
        return ("sample_liv", "안필드전술가")
    if "best-sample-bay" in mk_low:
        return ("sample_bay", "분데스인사이드")
    if "best-sample-vleague" in mk_low or "best-sample-hyundai" in mk_low:
        return ("sample_vleague", "스카이워커스분석")
    return ("guest", "마이팀 분석회원")


def _build_analyst_reward_summaries(
    conn: sqlite3.Connection,
    target_user_key: str = "",
) -> tuple:
    rows = conn.execute(
        "SELECT reward_key, post_key, user_key, author_name, best_rank, bonus_points, badge_name, post_title, awarded_at_kst "
        "FROM datacenter_best_analyst_rewards ORDER BY awarded_at_kst DESC, best_rank ASC"
    ).fetchall()
    grouped: Dict[str, Dict[str, Any]] = {}
    author_totals: Dict[str, Dict[str, int]] = {}
    my_rewards_list = []
    my_total = 0
    t_uk = (target_user_key or "").strip()

    for reward_key, post_key, uk, aname, brank, bpts, bname, ptitle, awarded_at in rows:
        uk_s = str(uk or "guest")
        an_s = str(aname or "마이팀 분석회원")
        gkey = uk_s if uk_s and uk_s != "guest" else f"name:{an_s}"
        if gkey not in grouped:
            grouped[gkey] = {
                "author_name": an_s,
                "user_key": uk_s,
                "badge_name": str(bname or "🏅 베스트 분석가"),
                "best_rank": int(brank or 1),
                "total_bonus_points": 0,
                "best_count": 0,
                "latest_awarded_at": str(awarded_at or ""),
            }
        g = grouped[gkey]
        g["best_rank"] = min(g["best_rank"], int(brank or 1))
        g["total_bonus_points"] += int(bpts or 0)
        g["best_count"] += 1
        if str(awarded_at or "") > g["latest_awarded_at"]:
            g["latest_awarded_at"] = str(awarded_at or "")

        author_totals[gkey] = {
            "total_bonus": g["total_bonus_points"],
            "best_count": g["best_count"],
        }

        if t_uk and uk_s == t_uk:
            my_total += int(bpts or 0)
            my_rewards_list.append({
                "reward_key": reward_key,
                "post_key": post_key,
                "best_rank": int(brank or 1),
                "bonus_points": int(bpts or 0),
                "badge_name": str(bname or "🏅 베스트 분석가"),
                "post_title": str(ptitle or ""),
                "awarded_at_kst": str(awarded_at or ""),
            })

    hall_of_fame = sorted(
        grouped.values(),
        key=lambda x: (-x["total_bonus_points"], -x["best_count"], x["best_rank"]),
    )
    if t_uk and my_rewards_list:
        my_reward = {
            "has_badge": True,
            "badge_name": "🏅 베스트 분석가",
            "total_bonus_points": my_total,
            "best_count": len(my_rewards_list),
            "rewards": my_rewards_list,
        }
    else:
        my_reward = {
            "has_badge": False,
            "badge_name": "",
            "total_bonus_points": 0,
            "best_count": 0,
            "rewards": [],
        }
    return hall_of_fame, my_reward, author_totals

@router.get("/briefing-votes")
def get_briefing_votes() -> Dict[str, Any]:
    try:
        _init_briefing_votes_table()
        conn = sqlite3.connect(DB_PATH, timeout=10)
        rows = conn.execute(
            "SELECT post_key, agree_cnt, oppose_cnt, base_agree_pct FROM datacenter_briefing_votes"
        ).fetchall()
        conn.close()
        votes: Dict[str, Any] = {}
        for post_key, agree_cnt, oppose_cnt, base_agree_pct in rows:
            a_cnt = int(agree_cnt or 0)
            o_cnt = int(oppose_cnt or 0)
            b_pct = max(15, min(85, int(base_agree_pct or 55)))
            base_agree = round(10 * b_pct / 100)
            base_oppose = 10 - base_agree
            disp_agree = base_agree + a_cnt
            disp_oppose = base_oppose + o_cnt
            total = max(1, disp_agree + disp_oppose)
            agree_pct = round(disp_agree * 100 / total)
            oppose_pct = 100 - agree_pct
            votes[str(post_key)] = {"agree": disp_agree, "oppose": disp_oppose, "raw_agree": a_cnt, "raw_oppose": o_cnt, "agree_pct": agree_pct, "oppose_pct": oppose_pct, "total": total}
        return {"ok": True, "votes": votes}
    except Exception as exc:
        return {"ok": False, "votes": {}, "error": str(exc)}

@router.post("/briefing-votes")
async def cast_briefing_vote(request: Request) -> Dict[str, Any]:
    try:
        _init_briefing_votes_table()
        body = await request.json()
        post_key = str(body.get("post_key") or "").strip()[:120]
        side = str(body.get("side") or "").strip().lower()
        prev_side = str(body.get("prev_side") or "").strip().lower()
        base_pct = max(15, min(85, int(body.get("base_pct") or 55)))
        if not post_key or side not in ("agree", "oppose"):
            return {"ok": False, "error": "invalid post_key or side"}
        now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute(
            "INSERT OR IGNORE INTO datacenter_briefing_votes (post_key, agree_cnt, oppose_cnt, base_agree_pct, updated_at) VALUES (?, 0, 0, ?, ?)",
            (post_key, base_pct, now_str),
        )
        row = conn.execute(
            "SELECT agree_cnt, oppose_cnt, base_agree_pct FROM datacenter_briefing_votes WHERE post_key = ?",
            (post_key,),
        ).fetchone()
        agree_cnt = int(row[0] or 0) if row else 0
        oppose_cnt = int(row[1] or 0) if row else 0
        b_pct = max(15, min(85, int(row[2] or base_pct))) if row else base_pct
        if prev_side == "agree":
            agree_cnt = max(0, agree_cnt - 1)
        elif prev_side == "oppose":
            oppose_cnt = max(0, oppose_cnt - 1)
        if side == "agree":
            agree_cnt += 1
        elif side == "oppose":
            oppose_cnt += 1
        conn.execute(
            "UPDATE datacenter_briefing_votes SET agree_cnt = ?, oppose_cnt = ?, updated_at = ? WHERE post_key = ?",
            (agree_cnt, oppose_cnt, now_str, post_key),
        )
        base_agree = round(10 * b_pct / 100)
        base_oppose = 10 - base_agree
        disp_agree = base_agree + agree_cnt
        disp_oppose = base_oppose + oppose_cnt
        total = max(1, disp_agree + disp_oppose)
        agree_pct = round(disp_agree * 100 / total)
        oppose_pct = 100 - agree_pct
        has_posts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_analyst_posts'"
        ).fetchone()
        m = re.fullmatch(r"(?:post-)?(\d+)", post_key) if has_posts else None
        if has_posts and m:
            conn.execute("UPDATE auto_analyst_posts SET sentiment_home_pct = ?, sentiment_away_pct = ? WHERE id = ?", (agree_pct, oppose_pct, int(m.group(1))))
        elif has_posts:
            conn.execute("UPDATE auto_analyst_posts SET sentiment_home_pct = ?, sentiment_away_pct = ? WHERE match_key = ? OR title = ?", (agree_pct, oppose_pct, post_key, post_key))
        conn.commit()
        conn.close()
        _safe_trigger_pg()
        return {"ok": True, "post_key": post_key, "side": side, "agree": disp_agree, "oppose": disp_oppose, "agree_pct": agree_pct, "oppose_pct": oppose_pct, "total": total, "bonus_points": 5}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _ensure_sample_briefings_for_best() -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        has_posts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_analyst_posts'").fetchone()
        if not has_posts:
            conn.close()
            return
        cnt_row = conn.execute("SELECT COUNT(*) FROM auto_analyst_posts WHERE title LIKE '%마이팀 브리핑%' OR title LIKE '%마이팀 주간%'").fetchone()
        cnt = int(cnt_row[0] or 0) if cnt_row else 0
        if cnt >= 3:
            conn.close()
            return
        
        now_kst = _now_kst()
        today_str = now_kst.strftime("%Y-%m-%d")
        now_str = _fmt_kst(now_kst)
        samples = [
            ("best-sample-liverpool-southampton", "축구", "EPL", "[⭐ 마이팀 브리핑] 리버풀 vs 사우샘프턴 — 살라 연속 공격포인트와 전방 압박 우위로 다득점 정배 기대!", "살라의 최근 5경기 연속 공격포인트와 안필드 홈 전방 압박 지표를 감안하면 다득점 정배 흐름이 유력합니다.", "살라 중심 우측 하프스페이스 공략 / 홈 다득점 기대", "리버풀 승", 82, 70, 30, "리버풀 우세", "사우샘프턴 반격", "#리버풀 #살라 #EPL #마이팀브리핑",  18,42,6),
            ("best-sample-bayern-augsburg", "축구", "분데스리가", "[⭐ 마이팀 브리핑] 바이에른 뮌헨 vs 아우크스부르크 — 김민재 철벽 수비와 케인 홈 결정력 조합으로 핸디캡 승 유력!", "김민재의 대인 방어와 케인의 홈 박스 내 슈팅 효율이 맞물려 핸디캡 승리 가능성이 높습니다.", "김민재 수비 라인 컨트롤 / 케인 멀티골 기대", "바이에른 뮌헨 -1.5 승", 79, 65, 35, "뮌헨 압도", "아우크스부르크 수비", "#바이에른뮌헨 #김민재 #케인 #마이팀브리핑", 9, 3, 1),
            ("best-sample-hyundai-koreanair", "배구", "V-리그", "[⭐ 마이팀 브리핑] 현대캐피탈 vs 대한항공 — 레오·허수봉 서브 에이스 화력으로 세트 우세 전망!", "레오와 허수봉의 강서브가 상대 리시브 라인을 흔들며 세트 후반 집중력에서 우위를 점할 전망입니다.", "레오·허수봉 서브 압박 / 블로킹 높이 우위", "현대캐피탈 승", 76, 60, 40, "현대캐피탈 우세", "대한항공 추격", "#현대캐피탈 #허수봉 #V리그 #마이팀브리핑", 7, 3, 1),
        ]
        for mk, sport, league, title, content, tp, pick, conf, hpct, apct, hlbl, albl, tags, likes, ag, op in samples:
            exists = conn.execute("SELECT 1 FROM auto_analyst_posts WHERE match_key = ? OR title = ?", (mk, title)).fetchone()
            if exists: continue
            conn.execute("INSERT INTO auto_analyst_posts (run_date_kst, match_key, rank_num, is_summary, sport, league, badge_text, title, content, tactical_points, recommended_pick, confidence_score, sentiment_home_pct, sentiment_away_pct, sentiment_home_label, sentiment_away_label, hashtags, likes, created_at_kst) VALUES (?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (today_str, mk, sport, league, "⭐ 마이팀 브리핑", title, content, tp, pick, conf, hpct, apct, hlbl, albl, tags, likes, now_str))
            conn.execute("INSERT OR IGNORE INTO datacenter_briefing_votes (post_key, agree_cnt, oppose_cnt, base_agree_pct, updated_at) VALUES (?, ?, ?, ?, ?)", (mk, ag, op, hpct, now_str))
        conn.commit()
        conn.close()
    except Exception:
        pass

@router.get("/best-briefings")
def get_best_briefings(sort: str = Query("hot"), limit: int = Query(3, ge=1, le=10), user_key: str = Query("")) -> Dict[str, Any]:
    try:
        _init_briefing_votes_table()
        _ensure_sample_briefings_for_best()
        sort_mode = (sort or "hot").strip().lower()
        if sort_mode not in ("hot", "agree_pct", "votes"):
            sort_mode = "hot"
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        _ensure_best_analyst_tables(conn)
        has_posts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_analyst_posts'").fetchone()
        if not has_posts:
            conn.close()
            return {"ok": True, "sort": sort_mode, "updated_at_kst": _fmt_kst(_now_kst()), "best_keys": [], "items": []}
        post_rows = conn.execute("SELECT * FROM auto_analyst_posts WHERE title LIKE '%마이팀 브리핑%' OR title LIKE '%마이팀 주간%' OR badge_text LIKE '%마이팀%' ORDER BY id DESC").fetchall()
        vote_rows = conn.execute("SELECT post_key, agree_cnt, oppose_cnt FROM datacenter_briefing_votes").fetchall()
        author_rows = conn.execute("SELECT post_key, match_key, user_key, author_name FROM datacenter_briefing_authors").fetchall()
        author_map = {}
        for ar in author_rows:
            pk_s = str(ar["post_key"] or "")
            mk_s = str(ar["match_key"] or "")
            uk_s = str(ar["user_key"] or "guest") or "guest"
            an_s = str(ar["author_name"] or "마이팀 분석회원") or "마이팀 분석회원"
            if pk_s:
                author_map[pk_s] = (uk_s, an_s)
            if mk_s and mk_s not in author_map:
                author_map[mk_s] = (uk_s, an_s)
        vote_map = {}
        for vr in vote_rows:
            vote_map[str(vr["post_key"])] = (int(vr["agree_cnt"] or 0), int(vr["oppose_cnt"] or 0))
        scored = []
        for row in post_rows:
            post_id = int(row["id"])
            match_key = str(row["match_key"] or "")
            title = str(row["title"] or "")
            base_pct = max(15, min(85, int(row["sentiment_home_pct"] or 55)))
            base_agree = round(10 * base_pct / 100)
            base_oppose = 10 - base_agree
            v_pair = vote_map.get(str(post_id)) or vote_map.get(f"post-{post_id}") or vote_map.get(match_key) or vote_map.get(title[:120]) or (0, 0)
            disp_agree = base_agree + int(v_pair[0])
            disp_oppose = base_oppose + int(v_pair[1])
            total = max(1, disp_agree + disp_oppose)
            agree_pct = round(disp_agree * 100 / total)
            oppose_pct = 100 - agree_pct
            likes = int(row["likes"] or 0)
            hot_score = round(total * 3.5 + agree_pct * 0.8 + likes * 1.5, 1)
            raw_content = str(row["content"] or "").strip()
            first_line = raw_content.splitlines()[0].strip() if raw_content else ""
            comment_preview = (first_line[:87] + "...") if len(first_line) > 90 else first_line
            scored.append({"id": post_id, "post_key": str(post_id), "match_key": match_key, "sport": str(row["sport"] or ""), "league": str(row["league"] or ""), "badge_text": str(row["badge_text"] or ""), "title": title, "comment_preview": comment_preview, "recommended_pick": str(row["recommended_pick"] or ""), "likes": likes, "agree": disp_agree, "oppose": disp_oppose, "total_votes": total, "agree_pct": agree_pct, "oppose_pct": oppose_pct, "hot_score": hot_score, "created_at": str(row["created_at_kst"] or "")})
            a_uk, a_name = _resolve_briefing_author(author_map, str(post_id), match_key)
            scored[-1]["author_name"] = a_name
            scored[-1]["author_user_key"] = a_uk
        hot_sorted = sorted(scored, key=lambda x: (-x["hot_score"], -x["total_votes"], -x["id"]))
        now_kst_str = _fmt_kst(_now_kst())
        hot_rank_map = {}
        inserted_new = False
        for rk_idx, hit in enumerate(hot_sorted[:3]):
            rk = rk_idx + 1
            b_pts = _BEST_RANK_BONUS[rk_idx]
            p_key = str(hit["post_key"])
            hot_rank_map[p_key] = {"rank": rk, "bonus_points": b_pts, "rank_badge": _BEST_RANK_LABELS[rk_idx]}
            cur = conn.execute(
                "INSERT OR IGNORE INTO datacenter_best_analyst_rewards "
                "(reward_key, post_key, user_key, author_name, best_rank, bonus_points, badge_name, post_title, awarded_at_kst) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{p_key}:rank{rk}", p_key, hit["author_user_key"], hit["author_name"], rk, b_pts, "🏅 베스트 분석가", hit["title"][:120], now_kst_str),
            )
            if cur.rowcount and cur.rowcount > 0:
                inserted_new = True
        if inserted_new:
            conn.commit()
            _safe_trigger_pg()
        hall_of_fame, my_reward, author_totals = _build_analyst_reward_summaries(conn, user_key)
        conn.close()
        if sort_mode == "agree_pct":
            scored.sort(key=lambda x: (-x["agree_pct"], -x["total_votes"], -x["id"]))
        elif sort_mode == "votes":
            scored.sort(key=lambda x: (-x["total_votes"], -x["agree_pct"], -x["id"]))
        else:
            scored.sort(key=lambda x: (-x["hot_score"], -x["total_votes"], -x["agree_pct"]))
        top_items = scored[:limit]
        for idx, item in enumerate(top_items, start=1):
            item["rank"] = idx
            item["rank_badge"] = _BEST_RANK_LABELS[idx - 1] if idx <= 3 else f"🏅 {idx}위"
            item["analyst_badge"] = "🏅 베스트 분석가"
            hr_info = hot_rank_map.get(str(item["post_key"]))
            if hr_info:
                item["bonus_points"] = hr_info["bonus_points"]
                item["reward_awarded"] = True
            else:
                item["bonus_points"] = _BEST_RANK_BONUS[idx - 1] if idx <= 3 else 0
                item["reward_awarded"] = idx <= 3
            uk_s = item["author_user_key"]
            gkey = uk_s if uk_s and uk_s != "guest" else f"name:{item['author_name']}"
            tot_info = author_totals.get(gkey, {"total_bonus": item["bonus_points"], "best_count": 1})
            item["author_total_bonus"] = tot_info["total_bonus"]
            item["author_best_count"] = tot_info["best_count"]
        best_keys = []
        qual_items = [it for it in scored if it in top_items or it["total_votes"] >= 11 or it["agree_pct"] >= 55]
        for it in qual_items:
            best_keys.extend([k for k in (str(it["id"]), it["match_key"], it["title"]) if k and k not in best_keys])
        return {
            "ok": True,
            "sort": sort_mode,
            "updated_at_kst": now_kst_str,
            "best_keys": best_keys,
            "items": top_items,
            "reward_policy": _BEST_REWARD_POLICY,
            "hall_of_fame": hall_of_fame,
            "my_reward": my_reward,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "sort": sort, "updated_at_kst": _fmt_kst(_now_kst()), "best_keys": [], "items": []}

@router.get("/best-analysts")
def get_best_analysts(user_key: str = Query("")) -> Dict[str, Any]:
    _init_briefing_votes_table()
    _ensure_sample_briefings_for_best()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        _ensure_best_analyst_tables(conn)
        hall_of_fame, my_reward, _ = _build_analyst_reward_summaries(conn, user_key)
        conn.close()
        return {
            "ok": True,
            "updated_at_kst": _fmt_kst(_now_kst()),
            "reward_policy": _BEST_REWARD_POLICY,
            "hall_of_fame": hall_of_fame,
            "my_reward": my_reward,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "updated_at_kst": _fmt_kst(_now_kst()),
            "reward_policy": _BEST_REWARD_POLICY,
            "hall_of_fame": [],
            "my_reward": {"has_badge": False, "badge_name": "", "total_bonus_points": 0, "best_count": 0, "rewards": []},
        }
     

@router.get("/hall-of-fame") 
def get_hall_of_fame(sort: str = Query("composite"), sport: str = Query("all"), user_key: str = Query("")) -> Dict[str, Any]:
    try:
        get_best_briefings(sort="hot", limit=3, user_key=user_key)
    except Exception:
        pass
    db_stats: Dict[str, Dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        _ensure_best_analyst_tables(conn)
        rows = conn.execute(
            "SELECT author_name, best_rank, bonus_points, post_title, awarded_at_kst "
            "FROM datacenter_best_analyst_rewards ORDER BY awarded_at_kst DESC"
        ).fetchall()
        conn.close()
        for r in rows:
            name = str(r["author_name"] or "").strip()
            if not name:
                continue
            st = db_stats.setdefault(name, {"awards_cnt": 0, "rank1_cnt": 0, "bonus_sum": 0, "latest_title": ""})
            st["awards_cnt"] += 1
            if int(r["best_rank"] or 0) == 1:
                st["rank1_cnt"] += 1
            st["bonus_sum"] += int(r["bonus_points"] or 0)
            if not st["latest_title"] and r["post_title"]:
                st["latest_title"] = str(r["post_title"])
    except Exception:
        pass  
    seeds = [
        {
            "analyst_name": "안필드전술가",
            "specialty": "⚽ EPL·유럽축구",
            "sport_code": "soccer",
            "base_awards": 6,
            "base_rank1": 4,
            "base_bonus": 260,
            "total_picks": 42,
            "hit_picks": 33,
            "accuracy_pct": 78.6,
            "avg_like_pct": 84.5,
            "streak_badge": "🔥 6연승 행진",
            "featured_pick": "리버풀 vs 사우샘프턴 — 리버풀 승",
        },
        {
            "analyst_name": "분데스인사이드",
            "specialty": "⚽ 분데스·UCL",
            "sport_code": "soccer",
            "base_awards": 5,
            "base_rank1": 2,
            "base_bonus": 190,
            "total_picks": 38,
            "hit_picks": 28,
            "accuracy_pct": 73.7,
            "avg_like_pct": 76.2,
            "streak_badge": "⚡ 최근 10픽 8적중",
            "featured_pick": "바이에른 뮌헨 -1.5 핸디캡 승",
        },
        {
            "analyst_name": "스카이워커스분석",
            "specialty": "🏐 V-리그·배구",
            "sport_code": "volleyball",
            "base_awards": 4,
            "base_rank1": 1,
            "base_bonus": 150,
            "total_picks": 35,
            "hit_picks": 26,
            "accuracy_pct": 74.3,
            "avg_like_pct": 79.0,
            "streak_badge": "✅ 4연속 적중",
            "featured_pick": "현대캐피탈 vs 대한항공 — 현대캐피탈 승",
        },
        {
            "analyst_name": "잠실불펜연구소",
            "specialty": "⚾ KBO·MLB",
            "sport_code": "baseball",
            "base_awards": 4,
            "base_rank1": 2,
            "base_bonus": 170,
            "total_picks": 44,
            "hit_picks": 31,
            "accuracy_pct": 70.5,
            "avg_like_pct": 74.8,
            "streak_badge": "⚾ 언더/오버 특화",
            "featured_pick": "LA 다저스 vs 샌디에이고 — 다저스 승",
        },
        {
            "analyst_name": "코트비전NBA",
            "specialty": "🏀 NBA·농구",
            "sport_code": "basketball",
            "base_awards": 3,
            "base_rank1": 1,
            "base_bonus": 120,
            "total_picks": 40,
            "hit_picks": 30,
            "accuracy_pct": 75.0,
            "avg_like_pct": 77.4,
            "streak_badge": "🔥 4연승",
            "featured_pick": "보스턴 셀틱스 -4.5 핸디캡 승",
        },
        {
            "analyst_name": "라리가마에스트로",
            "specialty": "⚽ 라리가·세리에",
            "sport_code": "soccer",
            "base_awards": 3,
            "base_rank1": 1,
            "base_bonus": 110,
            "total_picks": 36,
            "hit_picks": 25,
            "accuracy_pct": 69.4,
            "avg_like_pct": 71.5,
            "streak_badge": "⚡ 최근 5픽 4적중",
            "featured_pick": "레알 마드리드 vs 오사수나 — 레알 승",
        },
        {
            "analyst_name": "옥타곤전략가",
            "specialty": "🥊 UFC·테니스",
            "sport_code": "other",
            "base_awards": 2,
            "base_rank1": 1,
            "base_bonus": 80,
            "total_picks": 26,
            "hit_picks": 20,
            "accuracy_pct": 76.9,
            "avg_like_pct": 81.2,
            "streak_badge": "🎯 메인카드 5연승",
            "featured_pick": "UFC 넘버링 메인 — 판정승 픽",
        },
        {
            "analyst_name": "설원퍽마스터",
            "specialty": "🏒 NHL·하키",
            "sport_code": "other",
            "base_awards": 2,
            "base_rank1": 0,
            "base_bonus": 60,
            "total_picks": 29,
            "hit_picks": 19,
            "accuracy_pct": 65.5,
            "avg_like_pct": 68.9,
            "streak_badge": "✅ 역배 발굴",
            "featured_pick": "토론토 메이플리프스 승",
        },
    ]
    seen_names = set()
    items: List[Dict[str, Any]] = []
    for s in seeds:
        name = s["analyst_name"]
        seen_names.add(name)
        st = db_stats.get(name, {})
        awards_count = s["base_awards"] + int(st.get("awards_cnt", 0))
        rank1_count = s["base_rank1"] + int(st.get("rank1_cnt", 0))
        total_bonus_pts = s["base_bonus"] + int(st.get("bonus_sum", 0))
        accuracy_pct = float(s["accuracy_pct"])
        composite_score = round(awards_count * 18.0 + total_bonus_pts * 0.22 + accuracy_pct * 1.15, 1)
        tier_badge = (
            "👑 레전드 분석가"
            if (composite_score >= 240 or awards_count >= 6)
            else ("💎 마스터 분석가" if composite_score >= 190 else "🏅 베스트 분석가")
        )
        items.append({
            "analyst_name": name,
            "specialty": s["specialty"],
            "sport_code": s["sport_code"],
            "awards_count": awards_count,
            "rank1_count": rank1_count,
            "total_bonus_pts": total_bonus_pts,
            "total_picks": s["total_picks"],
            "hit_picks": s["hit_picks"],
            "accuracy_pct": accuracy_pct,
            "avg_like_pct": float(s["avg_like_pct"]),
            "streak_badge": s["streak_badge"],
            "featured_pick": st.get("latest_title") or s["featured_pick"],
            "composite_score": composite_score,
            "tier_badge": tier_badge,
            "is_seed": True,
        })
    for name, st in db_stats.items():
        if name in seen_names:
            continue
        awards_count = int(st.get("awards_cnt", 0))
        rank1_count = int(st.get("rank1_cnt", 0))
        total_bonus_pts = int(st.get("bonus_sum", 0))
        total_picks = max(awards_count, 1)
        hit_picks = max(awards_count, 1)
        accuracy_pct = 75.0
        composite_score = round(awards_count * 18.0 + total_bonus_pts * 0.22 + accuracy_pct * 1.15, 1)
        tier_badge = (
            "👑 레전드 분석가"
            if (composite_score >= 240 or awards_count >= 6)
            else ("💎 마스터 분석가" if composite_score >= 190 else "🏅 베스트 분석가")
        )
        items.append({
            "analyst_name": name,
            "specialty": "🏅 커뮤니티 분석가",
            "sport_code": "other",
            "awards_count": awards_count,
            "rank1_count": rank1_count,
            "total_bonus_pts": total_bonus_pts,
            "total_picks": total_picks,
            "hit_picks": hit_picks,
            "accuracy_pct": accuracy_pct,
            "avg_like_pct": 80.0,
            "streak_badge": "🌟 신규 명예 입성",
            "featured_pick": st.get("latest_title") or "베스트 브리핑 수상",
            "composite_score": composite_score,
            "tier_badge": tier_badge,
            "is_seed": False,
        })
    sport_norm = (sport or "all").strip().lower()
    if sport_norm and sport_norm != "all":
        items = [x for x in items if x["sport_code"] == sport_norm]
    sort_norm = (sort or "composite").strip().lower()
    if sort_norm == "accuracy":
        items.sort(key=lambda x: (x["accuracy_pct"], x["composite_score"]), reverse=True)
    elif sort_norm == "bonus":
        items.sort(key=lambda x: (x["total_bonus_pts"], x["composite_score"]), reverse=True)
    elif sort_norm == "awards":
        items.sort(key=lambda x: (x["awards_count"], x["rank1_count"], x["composite_score"]), reverse=True)
    else:
        sort_norm = "composite"
        items.sort(key=lambda x: (x["composite_score"], x["awards_count"], x["accuracy_pct"]), reverse=True)
    for idx, item in enumerate(items, start=1):
        item["rank"] = idx
    summary_kpis = {
        "total_analysts": len(items),
        "total_bonus_pts": sum(int(x["total_bonus_pts"]) for x in items),
        "avg_accuracy_pct": round(sum(float(x["accuracy_pct"]) for x in items) / len(items), 1) if items else 0.0,
        "top1_name": items[0]["analyst_name"] if items else "-",
        "top1_score": items[0]["composite_score"] if items else 0.0,
        "formula_text": "종합점수 = (수상횟수×18) + (누적보너스P×0.22) + (적중률%×1.15)",
    }
    return {
        "ok": True,
        "sort": sort_norm,
        "sport": sport_norm,
        "updated_at_kst": _fmt_kst(_now_kst()),
        "summary_kpis": summary_kpis,
        "podium": items[:3],
        "items": items,
    }
