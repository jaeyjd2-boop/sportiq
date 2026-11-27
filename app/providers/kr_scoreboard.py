"""Korean leagues from billnojay's live scoreboard, in the collector's fixture shape.

ESPN has no K League, V-League, WKBL, KBL or KBO, and API-SPORTS needs a paid
key. billnojay's lounge scoreboard (/api/member/scoreboard-v30, this same
service) already gathers those games from the Korean portals with Korean
team names. This adapter reads that public endpoint over HTTP (the worker
does not import the lounge module) and turns its Korean items into fixtures,
so they are stored as normal matches: ids, results history, form, h2h.

Ids are stable without a provider id: "kr:" + hash of league code, KST date
and both team names. League names are stored in English (K League 1,
V-League, ...) with country "South Korea" so downstream league maps work;
team names stay Korean as the portals give them. Team logos are the official
emblems published by kleague.com, kovo.co.kr and wkbl.or.kr.

The scoreboard's "ai" percentages are name-hash samples and are ignored.
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("sportiq-kr-scoreboard")

DEFAULT_URL = "https://billnojay.com/api/member/scoreboard-v30"
TIMEOUT = 30.0
KST = timezone(timedelta(hours=9))

# league_code -> (sport, English league name)
LEAGUES = {
    "KLEAGUE": ("football", "K League 1"),
    "KLEAGUE2": ("football", "K League 2"),
    "KBO": ("baseball", "KBO League"),
    "KBL": ("basketball", "KBL"),
    "WKBL": ("basketball", "WKBL"),
    "VLEAGUE_M": ("volleyball", "V-League"),
    "VLEAGUE_W": ("volleyball", "V-League Women"),
}
SPORTS = sorted({s for s, _ in LEAGUES.values()})
COUNTRY = "South Korea"

_K = "https://www.kleague.com/assets/images/emblem/emblem_%s.png"
_V = "https://cdn.kovo.co.kr/emblems/%s.svg"
_W = "https://www.wkbl.or.kr/static/images/team/teamlogo_%s.png"
# Korean team name as the scoreboard writes it -> official emblem (read from each league site, 2026-11-27)
LOGOS = {
    "football": {k: _K % v for k, v in {
        "FC 서울": "K09", "울산 HD": "K01", "대전 하나 시티즌": "K10", "인천 유나이티드": "K18", "강원 FC": "K21",
        "전북 현대": "K05", "제주 SK": "K04", "FC 안양": "K27", "포항 스틸러스": "K03", "김천 상무": "K35",
        "부천 FC 1995": "K26", "광주 FC": "K22", "수원 삼성": "K02", "수원 FC": "K29", "대구 FC": "K17",
        "서울 이랜드": "K31", "부산 아이파크": "K06", "화성 FC": "K39", "충남 아산": "K34", "김포 FC": "K36",
        "경남 FC": "K20", "천안 시티": "K38", "성남 FC": "K08", "파주 프런티어": "K40", "용인 FC": "K42",
        "충북 청주": "K37", "전남 드래곤즈": "K07", "김해 FC 2008": "K41", "안산 그리너스": "K32",
    }.items()},
    "volleyball": {k: _V % v for k, v in {
        "대한항공": "jumbos", "현대캐피탈": "skywalkers", "우리카드": "wooriwon", "KB손해보험": "stars",
        "한국전력": "vixtorm", "OK저축은행": "okman", "삼성화재": "bluefangs", "GS칼텍스": "kixx",
        "한국도로공사": "hipass", "현대건설": "hillstate", "흥국생명": "pinkspiders", "IBK기업은행": "altos",
        "정관장": "redsparks", "SOOP 수퍼스": "soopers",
    }.items()},
    "basketball": {k: _W % v for k, v in {
        "KB스타즈": "01", "삼성생명": "03", "우리은행": "05", "신한은행": "07", "하나은행": "09", "BNK썸": "11",
    }.items()},
}

_WHEN = re.compile(r"(\d{4})\.(\d{2})\.(\d{2}).*?(\d{1,2}):(\d{2})")


def _status(item):
    badge = str(item.get("status_badge") or "")
    if "연기" in badge:
        return "PST", badge
    if "취소" in badge:
        return "CANC", badge
    state = item.get("state")
    if state == "post":
        return "FT", badge or "종료"
    if state in ("in", "live"):
        return "LIVE", badge or "진행 중"
    return "NS", badge or "예정"


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _key(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def normalize(item):
    """One scoreboard item -> (sport, fixture), or None for non-Korean or incomplete items."""
    if not isinstance(item, dict):
        return None
    code = item.get("league_code")
    if code not in LEAGUES:
        return None
    home, away = str(item.get("home") or "").strip(), str(item.get("away") or "").strip()
    t = _WHEN.search(str(item.get("match_dt_full_kst") or ""))
    if not (home and away and t):
        return None
    y, mo, d, hh, mm = (int(x) for x in t.groups())
    try:
        start = datetime(y, mo, d, hh, mm, tzinfo=KST).astimezone(timezone.utc)
    except ValueError:
        return None
    sport, league_name = LEAGUES[code]
    short, detail = _status(item)
    scored = short in ("FT", "LIVE")
    day = f"{y:04d}{mo:02d}{d:02d}"
    logos = LOGOS.get(sport, {})
    return sport, {
        "fixture": {"id": "kr:" + _key(code, day, home, away), "date": start.isoformat(),
                    "status": {"short": short, "long": detail[:100]},
                    "venue": {"name": (item.get("venue") or None)}, "referee": None},
        "league": {"id": "kr:" + code, "name": league_name, "logo": None},
        "country": {"name": COUNTRY},
        "teams": {"home": {"id": f"kr:{code}:{_key(home)}", "name": home, "logo": logos.get(home)},
                  "away": {"id": f"kr:{code}:{_key(away)}", "name": away, "logo": logos.get(away)}},
        "goals": {"home": _int(item.get("home_score")) if scored else None,
                  "away": _int(item.get("away_score")) if scored else None},
        "source": "billnojay",
    }


class ScoreboardClient:
    def __init__(self, url=DEFAULT_URL, transport=None):
        self.url = url
        self.client = httpx.Client(timeout=TIMEOUT, transport=transport,
                                   headers={"User-Agent": "sportiq-worker/kr-scoreboard"})

    def day(self, offset):
        """Korean fixtures for one KST day offset (-5..5), grouped by sport. Failures give {}."""
        try:
            r = self.client.get(self.url, params={"date_offset": int(offset)})
            r.raise_for_status()
            items = (r.json() or {}).get("items") or []
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("kr scoreboard offset %s failed: %s", offset, exc)
            return {}
        out = {}
        for it in items:
            hit = normalize(it)
            if hit:
                out.setdefault(hit[0], []).append(hit[1])
        return out

    def window(self, offsets):
        out = {}
        for off in offsets:
            for sport, items in self.day(off).items():
                out.setdefault(sport, []).extend(items)
        return out

    def close(self):
        self.client.close()
