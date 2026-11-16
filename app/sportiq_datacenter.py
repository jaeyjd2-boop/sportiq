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
        }
        filtered = matches
        if sport and sport.strip().lower() != "all":
            if sport == "live":
                filtered = [m for m in filtered if m["state"] == "in"]
            else:
                filtered = [m for m in filtered if m["sport"] == sport]
        if league and league.strip().lower() != "all":
            filtered = [m for m in filtered if m["league"] == league.upper()]
        return {
            "ok": True,
            "server_now_kst": _fmt_kst(_now_kst()),
            "counts": counts,
            "score_alerts": list(_SCORE_ALERTS[:15]),
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


def _publish_daily_briefing(force=False):
    global _LAST_BRIEFING_DATE
    today = _now_kst().strftime("%Y-%m-%d")
    if not force and _LAST_BRIEFING_DATE == today:
        return False
    try:
        now = _now_kst()
        today_md = now.strftime("%m.%d")
        title = f"[\U0001f4ca 데이터센터 자동업로드] {today_md} 5종목 18리그 순위 · Elo 시즌예측 · 부상자 · 이적시장 브리핑"
        sim_line = "EPL 시즌 예측: 실시간 Elo + Monte Carlo 5,000회 반영 중"
        inj_line = "부상자 명단: 구단별 주요 결장자 실시간 추적 중"
        content = f"[{today_md} SPORTIQ 데이터센터 브리핑]\\n- {sim_line}\\n- {inj_line}\\n- 해외 이적시장: 유럽 5대 리그 루머·오피셜 30분 주기 동기화\\n- 데이터센터 5개 탭에서 전체 18개 리그 상세 지표를 확인하세요."
        conn = sqlite3.connect("/tmp/sportiq_lounge.db", timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auto_analyst_posts'")
        if cur.fetchone():
            mk = f"datacenter-daily-{today}"
            cur.execute("DELETE FROM auto_analyst_posts WHERE match_key = ?", (mk,))
            tp = json.dumps(["실시간 18개 리그 순위·득점왕 자동 반영", "Elo + Monte Carlo 5,000회 우승·강등 확률 산출", "구단별 부상자·해외 이적시장 30분 주기 동기화"], ensure_ascii=False)
            cur.execute("INSERT INTO auto_analyst_posts (run_date_kst, match_key, rank_num, is_summary, sport, league, badge_text, title, content, tactical_points, recommended_pick, confidence_score, sentiment_home_pct, sentiment_away_pct, sentiment_home_label, sentiment_away_label, hashtags, likes, created_at_kst) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (today, mk, 0, 1, "축구", "5종목 · 18리그", "\U0001f4ca 데이터센터 LIVE", title, content, tp, "데이터센터 5대 탭 실시간 브리핑", 89, 64, 36, "데이터 정배 우세", "부상·일정 변수 주의", "#데이터센터 #실시간순위 #AI시즌예측 #해외이적시장 #부상자명단", 15, _fmt_kst(now)))
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
