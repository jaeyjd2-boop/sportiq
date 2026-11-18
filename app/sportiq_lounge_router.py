import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.sqlite_guard import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DEFAULT_TIMEOUT_SEC,
    install_sqlite_guard,
    managed_sqlite_conn,
    run_with_db_retry,
)
from app.sportiq_member_upgrade import inject_upgrade_into_html, upgrade_router
import app.sportiq_member_upgrade as _smu

_SORT_DEDUP_APPLIED = False

_FIXED_SORT_STRIP_FN = r"""function isElementHiddenOrCollapsed(el) {
    if (!el) return true;
    if (el.id === "proTickerList" || el.id === "dcTickerControls") return true;
    if (el.style && el.style.display === "none") return true;
    if (el.classList && (el.classList.contains("hidden") || el.classList.contains("sq-match-sort-bar"))) return true;
    if (typeof window.getComputedStyle === "function") {
      try {
        var cs = window.getComputedStyle(el);
        if (cs && (cs.display === "none" || cs.visibility === "hidden")) return true;
      } catch (e) {}
    }
    if (typeof el.getClientRects === "function" && el.getClientRects().length === 0) return true;
    return false;
  }

  function countDirectMatchCardChildren(el) {
    if (!el || !el.children || el.children.length < 2 || el.children.length > 500) return 0;
    if (
      el.id === "proLiveTickerBar" ||
      el.id === "proTickerList" ||
      el.id === "dcTickerControls" ||
      el.id === "feedList" ||
      el.id === "dcLauncherGrid" ||
      el.id === "globalTop10Grid"
    ) {
      return 0;
    }
    if (isElementHiddenOrCollapsed(el)) return 0;

    var rawKids = Array.prototype.slice.call(el.children);
    var cardKids = [];
    for (var i = 0; i < rawKids.length; i++) {
      var ch = rawKids[i];
      if (isElementHiddenOrCollapsed(ch)) continue;
      cardKids.push(ch);
    }
    if (cardKids.length < 2) return 0;

    var matchLikeCount = 0;
    for (var j = 0; j < cardKids.length; j++) {
      var c = cardKids[j];
      var t = c.textContent || "";
      if (
        (parseMatchEpochFromText(t) != null || t.indexOf("최근 시즌 기록") !== -1) &&
        /Scheduled|예정|종료|Final|AET|LIVE|배당|스코어|vs/i.test(t)
      ) {
        matchLikeCount++;
      }
    }
    if (matchLikeCount < 2 || matchLikeCount < Math.ceil(cardKids.length * 0.4)) return 0;
    return matchLikeCount;
  }

  function hasDescendantMatchStrip(container) {
    if (!container || !container.querySelectorAll) return false;
    if (container.id !== "dcLiveTickerTrack" && container.querySelector("#dcLiveTickerTrack")) {
      return true;
    }
    var subEls = container.querySelectorAll("div, section, ul");
    for (var i = 0; i < subEls.length; i++) {
      if (subEls[i] !== container && countDirectMatchCardChildren(subEls[i]) >= 2) {
        return true;
      }
    }
    return false;
  }

  function cleanupDuplicateMatchSortBars() {
    var tickerWrap = document.getElementById("proLiveTickerBar");
    if (tickerWrap) {
      var prev = tickerWrap.previousElementSibling;
      while (prev && prev.classList && prev.classList.contains("sq-match-sort-bar")) {
        var toDrop = prev;
        prev = prev.previousElementSibling;
        if (toDrop.parentNode) toDrop.parentNode.removeChild(toDrop);
      }
      if (tickerWrap.__sqToolbarEl) tickerWrap.__sqToolbarEl = null;

      var liveTrack = document.getElementById("dcLiveTickerTrack");
      var innerBars = tickerWrap.querySelectorAll(".sq-match-sort-bar");
      if (liveTrack && innerBars.length > 0) {
        var keeper = liveTrack.__sqToolbarEl || innerBars[0];
        for (var k = 0; k < innerBars.length; k++) {
          if (innerBars[k] !== keeper && innerBars[k].parentNode) {
            innerBars[k].parentNode.removeChild(innerBars[k]);
          }
        }
        if (keeper && keeper.nextElementSibling !== liveTrack && liveTrack.parentNode) {
          liveTrack.parentNode.insertBefore(keeper, liveTrack);
        }
        liveTrack.__sqToolbarEl = keeper;
      }
    }
  }

  function sortRenderedMatchCardStrips(forceResort) {
    cleanupDuplicateMatchSortBars();

    document.querySelectorAll("div, section, ul").forEach(function (container) {
      if (countDirectMatchCardChildren(container) < 2) return;
      if (hasDescendantMatchStrip(container)) return;

      var kids = Array.prototype.slice.call(container.children).filter(function (ch) {
        return !isElementHiddenOrCollapsed(ch);
      });
      if (kids.length < 2) return;

      bindSmoothHorizontalControls(container);
      mountSortToggleAboveStrip(container);

      var scroller = resolveActualScrollViewport(container);
      if (container.__sqToolbarEl && scroller) {
        updateNavButtonEdgeStates(container.__sqToolbarEl, scroller);
      }

      if (!forceResort && isStripUserInteracting(container)) return;

      var firstTxt = (kids[0].textContent || "").slice(0, 48);
      var lastTxt = (kids[kids.length - 1].textContent || "").slice(0, 48);
      var sig = matchSortMode + "|" + kids.length + "|" + firstTxt + "|" + lastTxt;
      if (!forceResort && container.__sqSortedSig === sig) return;

      var decorated = kids.map(function (el, idx) {
        var t = el.textContent || "";
        var isSeedArch = el.getAttribute("data-sq-seed-archive") === "1" || t.indexOf("최근 시즌 기록") !== -1;
        var ep = isSeedArch ? null : parseMatchEpochFromText(t);
        var ord = isSeedArch ? { bucket: 8, key: idx } : computeMatchOrderTuple(t, ep, idx);
        var isMy = /★\s*MY\b/.test(t) ? 0 : 1;
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
      if (changed) {
        var prevScrollLeft = scroller ? (scroller.scrollLeft || 0) : (container.scrollLeft || 0);
        sorted.forEach(function (item) { container.appendChild(item.el); });
        if (scroller) scroller.scrollLeft = forceResort ? 0 : prevScrollLeft;
      }
      var newKids = Array.prototype.slice.call(container.children).filter(function (ch) {
        return !isElementHiddenOrCollapsed(ch);
      });
      container.__sqSortedSig = matchSortMode + "|" + newKids.length + "|" +
        ((newKids[0] && newKids[0].textContent) || "").slice(0, 48) + "|" +
        ((newKids[newKids.length - 1] && newKids[newKids.length - 1].textContent) || "").slice(0, 48);
      if (container.__sqToolbarEl && scroller) {
        updateNavButtonEdgeStates(container.__sqToolbarEl, scroller);
      }
    });

    cleanupDuplicateMatchSortBars();
  }"""


def _ensure_upgrade_js_dedup_patched() -> bool:
    global _SORT_DEDUP_APPLIED
    js_raw = getattr(_smu, "UPGRADE_JS", "")
    if not isinstance(js_raw, str) or not js_raw:
        return False
    if "cleanupDuplicateMatchSortBars" in js_raw:
        _SORT_DEDUP_APPLIED = True
        return True
    pattern = re.compile(
        r"function\s+sortRenderedMatchCardStrips\s*\(\s*forceResort\s*\)\s*\{.*?(?=\n\s*function\s+mountSortToggleAboveStrip\s*\()",
        flags=re.DOTALL,
    )
    patched, n = pattern.subn(lambda _m: _FIXED_SORT_STRIP_FN, js_raw, count=1)
    if n > 0:
        _smu.UPGRADE_JS = patched
        _SORT_DEDUP_APPLIED = True
        return True
    return False


_ensure_upgrade_js_dedup_patched()


install_sqlite_guard()

router = APIRouter()
router.include_router(upgrade_router)

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "sportiq_community.html")
DB_PATH = os.environ.get("SPORTIQ_LOUNGE_DB", "/tmp/sportiq_lounge.db")


def _to_epoch_ts(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.timestamp()
    except Exception:
        return None


def _format_cheer_time(val: Any) -> str:
    ts = _to_epoch_ts(val)
    if ts is not None:
        try:
            return datetime.fromtimestamp(ts, tz=KST).strftime("%H:%M:%S")
        except (OSError, OverflowError, ValueError):
            pass
    s = str(val or "").strip()
    if len(s) >= 8 and ":" in s:
        for token in s.split():
            if token.count(":") >= 1 and token[0].isdigit():
                return token[:8]
    return datetime.now(KST).strftime("%H:%M:%S")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=DEFAULT_TIMEOUT_SEC, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _db_session(write: bool = False):
    return managed_sqlite_conn(DB_PATH, write=write, row_factory=sqlite3.Row)


def _init_db() -> None:
    def _op() -> None:
        with _db_session(write=False) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS presence_sessions (
                    session_id TEXT PRIMARY KEY,
                    role TEXT DEFAULT 'GUEST',
                    visit_date TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_hashtags (
                    tag TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS partner_inquiries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company TEXT NOT NULL,
                    contact_name TEXT NOT NULL,
                    contact_info TEXT NOT NULL,
                    tier TEXT DEFAULT 'STANDARD',
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cheers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    role TEXT DEFAULT 'GUEST',
                    team TEXT DEFAULT 'ALL',
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS post_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT NOT NULL,
                    session_id TEXT DEFAULT '',
                    reason TEXT DEFAULT '부적절한 게시글',
                    created_at REAL NOT NULL
                );
CREATE TABLE IF NOT EXISTS global_match_rankings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key TEXT UNIQUE NOT NULL,
    sport TEXT NOT NULL,
    league TEXT NOT NULL,
    league_color TEXT NOT NULL,
    matchup TEXT NOT NULL,
    schedule_venue TEXT NOT NULL,
    votes INTEGER NOT NULL DEFAULT 0,
    buzz_score INTEGER NOT NULL DEFAULT 80,
    rank_change TEXT NOT NULL DEFAULT '-'
);
INSERT OR IGNORE INTO global_match_rankings(match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change) VALUES
('epl-liv-mun', 'football', 'EPL', '#38003c', '리버풀 vs 맨체스터 유나이티드', '11.22 · 안필드 노스웨스트 더비', 2840, 99, '-'), ('unl-esp-eng', 'football', '네이션스리그 A', '#003399', '스페인 vs 잉글랜드', '11.16 · 매치데이 6 빅매치', 2610, 97, '▲1'),
('ucl-ars-bvb', 'football', '챔피언스리그', '#0e1e5b', '아스널 vs 보루시아 도르트문트', '11.25 · 매치데이 5', 2390, 95, '▲2'), ('nba-bos-dal', 'basketball', 'NBA', '#c9082a', '보스턴 셀틱스 vs 댈러스 매버릭스', '11.16 · TD 가든', 2150, 93, '▼1'), ('epl-new-ars', 'football', 'EPL', '#38003c', '뉴캐슬 유나이티드 vs 아스널', '11.21 · 세인트 제임스 파크', 1980, 91, '▲1'),
('nba-phx-lal', 'basketball', 'NBA', '#c9082a', '피닉스 선즈 vs LA 레이커스', '11.16 · 서부 라이벌전', 1840, 89, '-'), ('laliga-bar-vil', 'football', '라리가', '#ee8707', 'FC 바르셀로나 vs 비야레알', '11.22 · 스포티파이 캄 노우', 1690, 87, 'NEW'), ('nfl-det-ne', 'nfl', 'NFL', '#013369', '디트로이트 라이온스 vs 뉴잉글랜드 패트리어츠', '11.15 · 뮌헨 인터내셔널 게임', 1540, 85, '▲2'),
('laliga-rma-cel', 'football', '라리가', '#ee8707', '레알 마드리드 vs 셀타 비고', '11.22 · 산티아고 베르나베우', 1420, 83, '▼1'), ('nba-den-phx', 'basketball', 'NBA', '#c9082a', '덴버 너기츠 vs 피닉스 선즈', '11.17 · 볼 아레나', 1290, 81, 'NEW');
UPDATE global_match_rankings SET votes = 2980 WHERE match_key = 'epl-liv-mun' AND votes < 2980; INSERT OR IGNORE INTO global_match_rankings(match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change) VALUES ('apbc-kor-jpn', 'baseball', 'APBC 야구 결승', '#0047a0', '대한민국 vs 일본', '11.15 · 도쿄돔 결승 한일전', 2890, 98, 'HOT'), ('nhl-fla-edm', 'hockey', 'NHL 하키', '#0284c7', '플로리다 팬서스 vs 에드먼턴 오일러스', '11.17 · 스탠리컵 리매치', 2040, 92, 'NEW'), ('nhl-chi-tor', 'hockey', 'NHL 하키', '#0284c7', '시카고 블랙호크스 vs 토론토 메이플리프스', '11.17 · 오리지널 6 매치', 1760, 88, 'NEW'), ('apbc-tpe-aus', 'baseball', 'APBC 야구', '#0047a0', '대만 vs 호주', '11.15 · 도쿄돔 3·4위 결정전', 1610, 86, 'NEW'), ('seriea-ata-int', 'football', '세리에A', '#008fd7', '아탈란타 vs 인터 밀란', '11.22 · 베르가모 상위권 격돌', 1480, 84, 'NEW'), ('nhl-nyr-mtl', 'hockey', 'NHL 하키', '#0284c7', '뉴욕 레인저스 vs 몬트리올 캐나디언스', '11.16 · 매디슨 스퀘어 가든', 1360, 82, 'NEW'), ('lidom-lic-esc', 'baseball', 'LIDOM 야구', '#0047a0', '티그레스 델 리세이 vs 에스코히도', '11.15 · 도미니카 윈터리그 더비', 1210, 79, 'NEW'), ('ucl-lil-bay', 'football', '챔피언스리그', '#0e1e5b', '릴 OSC vs 바이에른 뮌헨', '11.25 · 매치데이 5', 1150, 78, '▲1'), ('nhl-tbl-edm', 'hockey', 'NHL 하키', '#0284c7', '탬파베이 라이트닝 vs 에드먼턴 오일러스', '11.19 · 아말리 아레나', 1090, 77, 'NEW'), ('abl-ade-bri', 'baseball', 'ABL 프로야구', '#0047a0', '애들레이드 자이언츠 vs 브리즈번 밴디츠', '11.20 · 호주리그 개막전', 1020, 76, 'NEW');            """
            )
            row = conn.execute("SELECT COUNT(*) AS cnt FROM daily_hashtags").fetchone()
            if row["cnt"] == 0:
                now = time.time()
                seeds = [
                    ("#UCL결승", 142),
                    ("#손흥민", 128),
                    ("#EPL전술", 96),
                    ("#KBO리그", 84),
                    ("#NBA플레이오프", 79),
                    ("#xG데이터", 63),
                    ("#T1월즈", 58),
                ]
                conn.executemany(
                    "INSERT OR IGNORE INTO daily_hashtags(tag, count, updated_at) VALUES (?, ?, ?)",
                    [(t, c, now) for t, c in seeds],
                )
            crow = conn.execute("SELECT COUNT(*) AS cnt FROM cheers").fetchone()
            if crow["cnt"] == 0:
                now = time.time()
                cseeds = [
                    ("seed-1", "데이터헌터", "PRO", "토트넘", "오늘 후반전 하프스페이스 침투 xG 수치가 심상치 않네요!", now - 180),
                    ("seed-2", "야구덕후K", "MEMBER", "KIA", "선발 투수 릴리스 포인트 안정적이라 6이닝 무실점 기대합니다.", now - 90),
                    ("seed-3", "클러치타임", "GUEST", "NBA", "4쿼터 클러치 수비 로테이션이 승부를 가를 듯합니다!", now - 30),
                ]
                conn.executemany(
                    "INSERT INTO cheers(session_id, author, role, team, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    cseeds,
                )

    run_with_db_retry(_op)


_init_db()


# --- Stale-While-Revalidate (SWR) & Background Pre-Warmer for DataCenter Tabs ---
_DC_SWR_CACHE: Dict[int, tuple[float, list]] = {}
_DC_SWR_REFRESHING: set[int] = set()
_DC_SWR_LOCK = threading.Lock()
_DC_SWR_INSTALLED = False


def install_datacenter_swr_accelerator(dc_mod: Any) -> bool:
    global _DC_SWR_INSTALLED
    if getattr(dc_mod, "__swr_accelerated__", False):
        return True
    orig_collect = getattr(dc_mod, "_collect_all_matches", None)
    if not callable(orig_collect):
        return False

    def _fresh_ttl(offset: int) -> float:
        return 45.0 if offset == 0 else 600.0

    def _refresh_in_bg(offset: int) -> None:
        with _DC_SWR_LOCK:
            if offset in _DC_SWR_REFRESHING:
                return
            _DC_SWR_REFRESHING.add(offset)

        def _job() -> None:
            try:
                data = orig_collect(offset)
                if isinstance(data, list):
                    with _DC_SWR_LOCK:
                        _DC_SWR_CACHE[offset] = (time.monotonic(), data)
            except Exception:
                pass
            finally:
                with _DC_SWR_LOCK:
                    _DC_SWR_REFRESHING.discard(offset)

        threading.Thread(target=_job, daemon=True, name=f"dc-swr-{offset}").start()

    def _swr_collect(date_offset: int = 0):
        try:
            offset = int(date_offset)
        except (TypeError, ValueError):
            offset = 0
        now_m = time.monotonic()
        with _DC_SWR_LOCK:
            entry = _DC_SWR_CACHE.get(offset)
        if entry is not None:
            ts, cached_list = entry
            if (now_m - ts) <= _fresh_ttl(offset):
                return cached_list
            _refresh_in_bg(offset)
            return cached_list

        data = orig_collect(offset)
        if isinstance(data, list):
            with _DC_SWR_LOCK:
                _DC_SWR_CACHE[offset] = (time.monotonic(), data)
        return data

    dc_mod._collect_all_matches = _swr_collect
    dc_mod.__swr_accelerated__ = True
    _DC_SWR_INSTALLED = True
    return True


def _datacenter_prewarm_daemon() -> None:
    time.sleep(1.5)
    dc_mod = None
    for _ in range(20):
        try:
            from app import sportiq_datacenter as _dc
            if install_datacenter_swr_accelerator(_dc):
                dc_mod = _dc
                break
        except Exception:
            pass
        time.sleep(1.0)
    if dc_mod is None:
        return

    tick = 0
    while True:
        try:
            with _DC_SWR_LOCK:
                e0 = _DC_SWR_CACHE.get(0)
            if e0 is None:
                dc_mod._collect_all_matches(0)
            elif (time.monotonic() - e0[0]) >= 32.0:
                with _DC_SWR_LOCK:
                    _DC_SWR_CACHE[0] = (time.monotonic() - 46.0, e0[1])
                dc_mod._collect_all_matches(0)

            if tick % 15 == 0:
                with _DC_SWR_LOCK:
                    em1 = _DC_SWR_CACHE.get(-1)
                if em1 is None:
                    dc_mod._collect_all_matches(-1)
                else:
                    with _DC_SWR_LOCK:
                        _DC_SWR_CACHE[-1] = (time.monotonic() - 601.0, em1[1])
                    dc_mod._collect_all_matches(-1)
        except Exception:
            pass
        tick += 1
        time.sleep(35.0)


threading.Thread(target=_datacenter_prewarm_daemon, daemon=True, name="sportiq-dc-prewarm").start()


class PresencePingIn(BaseModel):
    session_id: str = "anon"
    role: Optional[str] = "GUEST"


class HashtagIn(BaseModel):
    tag: str


class PartnerInquiryIn(BaseModel):
    company: str
    contact_name: str
    contact_info: str
    tier: Optional[str] = "PREMIUM_SPONSOR"
    message: str


class CheerIn(BaseModel):
    session_id: str = "anon"
    author: str = "익명팬"
    role: Optional[str] = "GUEST"
    team: Optional[str] = "ALL"
    message: str


class ReportIn(BaseModel):
    session_id: Optional[str] = ""
    reason: Optional[str] = "신고 접수"


@router.get("/", response_class=HTMLResponse)
def serve_lounge_home():
    if os.path.exists(HTML_PATH):
        try:
            _ensure_upgrade_js_dedup_patched()
            with open(HTML_PATH, "r", encoding="utf-8") as f:
                raw_html = f.read()
            rendered = inject_upgrade_into_html(raw_html)
            rendered = re.sub(
                r'(/api/member/p0p1-upgrade\.js\?v=)(\d+)',
                lambda m: m.group(1) + str(int(m.group(2)) + 100),
                rendered,
            )
            return HTMLResponse(rendered, media_type="text/html; charset=utf-8")
        except Exception:
            return FileResponse(HTML_PATH, media_type="text/html; charset=utf-8")
    return HTMLResponse("<h1>SportIQ Lounge</h1><p>HTML file missing.</p>", status_code=200)


@router.get("/api/lounge/health")
def lounge_health():
    def _op() -> None:
        with _db_session(write=False) as conn:
            conn.execute("SELECT 1").fetchone()

    run_with_db_retry(_op)
    return {
        "status": "ok",
        "service": "sportiq-lounge",
        "storage": "sqlite-wal",
        "busy_timeout_ms": DEFAULT_BUSY_TIMEOUT_MS,
        "datacenter_swr_active": _DC_SWR_INSTALLED,
        "member_upgrade_v1": True,
        "match_sort_bar_dedup": _SORT_DEDUP_APPLIED,
    }


def _presence_stats(conn: sqlite3.Connection) -> dict:
    now = time.time()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    active = conn.execute(
        "SELECT COUNT(*) AS cnt FROM presence_sessions WHERE last_seen >= ?", (now - 120,)
    ).fetchone()["cnt"]
    today_cnt = conn.execute(
        "SELECT COUNT(*) AS cnt FROM presence_sessions WHERE visit_date = ?", (today,)
    ).fetchone()["cnt"]
    return {"active_now": max(active, 1), "today_visitors": max(today_cnt, 1)}


@router.post("/api/presence/ping")
def presence_ping(payload: PresencePingIn):
    now = time.time()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    sid = (payload.session_id or "anon").strip()[:64]
    role = (payload.role or "GUEST").strip()[:24]

    def _op() -> dict:
        with _db_session(write=True) as conn:
            conn.execute(
                """
                INSERT INTO presence_sessions(session_id, role, visit_date, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    role = excluded.role,
                    visit_date = excluded.visit_date,
                    last_seen = excluded.last_seen
                """,
                (sid, role, today, now, now),
            )
            return _presence_stats(conn)

    stats = run_with_db_retry(_op)
    return {"ok": True, **stats}


@router.get("/api/presence")
def get_presence():
    def _op() -> dict:
        with _db_session(write=False) as conn:
            return _presence_stats(conn)

    return run_with_db_retry(_op)


@router.get("/api/hashtags/daily")
def get_daily_hashtags():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT tag, count, updated_at FROM daily_hashtags ORDER BY count DESC, updated_at DESC LIMIT 24"
            ).fetchall()
            return [dict(r) for r in rows]

    return {"hashtags": run_with_db_retry(_op)}


@router.post("/api/hashtags/register")
def register_hashtag(payload: HashtagIn):
    raw = (payload.tag or "").strip().replace(" ", "")
    if not raw:
        raise HTTPException(status_code=400, detail="해시태그를 입력해 주세요.")
    tag = raw if raw.startswith("#") else f"#{raw}"
    if len(tag) < 2 or len(tag) > 32:
        raise HTTPException(status_code=400, detail="해시태그는 1~30자 이내로 입력해 주세요.")
    now = time.time()

    def _op() -> list[dict]:
        with _db_session(write=True) as conn:
            conn.execute(
                """
                INSERT INTO daily_hashtags(tag, count, updated_at) VALUES (?, 1, ?)
                ON CONFLICT(tag) DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
                """,
                (tag, now),
            )
            rows = conn.execute(
                "SELECT tag, count, updated_at FROM daily_hashtags ORDER BY count DESC, updated_at DESC LIMIT 24"
            ).fetchall()
            return [dict(r) for r in rows]

    hashtags = run_with_db_retry(_op)
    try:
        from app.sportiq_auto_analyst import _backup_tables_to_pg
        _backup_tables_to_pg("daily_hashtags")
    except Exception:
        pass
    return {"ok": True, "tag": tag, "hashtags": hashtags}


def _mask_contact(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if "@" in s:
        local, domain = s.split("@", 1)
        return f"{local[:2]}***@{domain}"
    if len(s) <= 6:
        return s[:2] + "*****"
    return f"{s[:3]}****{s[-3:]}"


@router.get("/api/partner-inquiries")
def list_partner_inquiries():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT id, company, contact_name, contact_info, tier, message, created_at FROM partner_inquiries ORDER BY id DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]

    rows = run_with_db_retry(_op)
    items = []
    for d in rows:
        d["contact_info"] = _mask_contact(d.get("contact_info", ""))
        items.append(d)
    return {"inquiries": items}


@router.post("/api/partner-inquiries")
def create_partner_inquiry(payload: PartnerInquiryIn):
    _c = payload.contact_info.strip()
    if not payload.company.strip() or not payload.contact_name.strip() or not payload.contact_info.strip():
        raise HTTPException(status_code=400, detail="업체명, 담당자명, 연락처를 모두 입력해 주세요.")
    if not (("@" in _c and "." in _c.split("@", 1)[-1]) or sum(ch.isdigit() for ch in _c) >= 8):
        raise HTTPException(status_code=400, detail="연락처는 유효한 이메일 또는 전화번호 형식으로 입력해 주세요.")
    now = time.time()

    def _op() -> int:
        with _db_session(write=True) as conn:
            cur = conn.execute(
                "INSERT INTO partner_inquiries(company, contact_name, contact_info, tier, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    payload.company.strip()[:80],
                    payload.contact_name.strip()[:60],
                    payload.contact_info.strip()[:100],
                    (payload.tier or "STANDARD").strip()[:40],
                    (payload.message or "").strip()[:1000],
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    new_id = run_with_db_retry(_op)
    try:
        from app.sportiq_auto_analyst import _backup_tables_to_pg
        _backup_tables_to_pg("partner_inquiries")
    except Exception:
        pass
    return {"ok": True, "id": new_id, "message": "파트너 입점 및 제휴 문의가 정상 접수되었습니다."}


@router.get("/api/cheers")
def list_cheers():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT id, author, role, team, message, created_at FROM cheers ORDER BY id DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]

    rows = run_with_db_retry(_op)
    result = []
    for r in rows:
        dt = _format_cheer_time(r.get("created_at"))
        ts = _to_epoch_ts(r.get("created_at"))
        result.append({**r, "created_at": ts if ts is not None else r.get("created_at"), "time_label": dt})
    return {"cheers": result}


@router.post("/api/cheers")
def post_cheer(payload: CheerIn, request: Request):
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="응원 메시지를 입력해 주세요.")
    sid = (payload.session_id or (request.client.host if request.client else "anon")).strip()[:64]
    now = time.time()
    author = (payload.author or "익명팬").strip()[:30]
    role = (payload.role or "GUEST").strip()[:20]
    team = (payload.team or "ALL").strip()[:30]
    clean_msg = msg[:300]

    def _op() -> int:
        with _db_session(write=True) as conn:
            last = conn.execute(
                "SELECT created_at FROM cheers WHERE session_id = ? ORDER BY id DESC LIMIT 1", (sid,)
            ).fetchone()
            last_ts = _to_epoch_ts(last["created_at"]) if last else None
            if last_ts is not None and (now - last_ts) < 5.0:
                remain = max(1, int(round(5.0 - (now - last_ts))))
                raise HTTPException(status_code=429, detail=f"도배 방지를 위해 {remain}초 후 다시 전송할 수 있습니다.")
            cur = conn.execute(
                "INSERT INTO cheers(session_id, author, role, team, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, author, role, team, clean_msg, now),
            )
            return int(cur.lastrowid or 0)

    new_id = run_with_db_retry(_op)
    dt = datetime.fromtimestamp(now, tz=KST).strftime("%H:%M:%S")
    return {
        "ok": True,
        "cheer": {
            "id": new_id,
            "author": author,
            "role": role,
            "team": team,
            "message": clean_msg,
            "created_at": now,
            "time_label": dt,
        },
    }


@router.post("/api/posts/{post_id}/report")
def report_post(post_id: str, payload: Optional[ReportIn] = None):
    now = time.time()
    sid = (payload.session_id if payload else "") or ""
    reason = (payload.reason if payload else "") or "커뮤니티 가이드라인 위반 신고"

    def _op() -> None:
        with _db_session(write=True) as conn:
            conn.execute(
                "INSERT INTO post_reports(post_id, session_id, reason, created_at) VALUES (?, ?, ?, ?)",
                (post_id[:64], sid[:64], reason[:200], now),
            )

    run_with_db_retry(_op)
    return {"ok": True, "post_id": post_id, "reported": True}


@router.get("/api/rankings/global-matches")
def get_global_matches():
    def _op() -> list[dict]:
        with _db_session(write=False) as conn:
            rows = conn.execute(
                "SELECT id, match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change FROM global_match_rankings ORDER BY votes DESC, buzz_score DESC, id ASC"
            ).fetchall()
            return [
                {
                    "rank": i + 1,
                    "id": r["id"],
                    "match_key": r["match_key"],
                    "sport": r["sport"],
                    "league": r["league"],
                    "league_color": r["league_color"],
                    "matchup": r["matchup"],
                    "schedule_venue": r["schedule_venue"],
                    "votes": r["votes"],
                    "buzz_score": r["buzz_score"],
                    "rank_change": r["rank_change"],
                }
                for i, r in enumerate(rows)
            ]

    return {"matches": run_with_db_retry(_op)}


@router.post("/api/rankings/global-matches/{match_id}/vote")
def vote_global_match(match_id: int):
    def _op() -> tuple[Optional[dict], list[dict]]:
        with _db_session(write=True) as conn:
            cur = conn.execute("UPDATE global_match_rankings SET votes = votes + 1 WHERE id = ?", (match_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="매치를 찾을 수 없습니다.")
            rows = conn.execute(
                "SELECT id, match_key, sport, league, league_color, matchup, schedule_venue, votes, buzz_score, rank_change FROM global_match_rankings ORDER BY votes DESC, buzz_score DESC, id ASC"
            ).fetchall()
            items = [
                {
                    "rank": i + 1,
                    "id": r["id"],
                    "match_key": r["match_key"],
                    "sport": r["sport"],
                    "league": r["league"],
                    "league_color": r["league_color"],
                    "matchup": r["matchup"],
                    "schedule_venue": r["schedule_venue"],
                    "votes": r["votes"],
                    "buzz_score": r["buzz_score"],
                    "rank_change": r["rank_change"],
                }
                for i, r in enumerate(rows)
            ]
            voted = next((m for m in items if m["id"] == match_id), None)
            return voted, items

    voted, items = run_with_db_retry(_op)
    try:
        from app.sportiq_auto_analyst import _backup_tables_to_pg
        _backup_tables_to_pg("global_match_rankings")
    except Exception:
        pass
    return {"ok": True, "match": voted, "matches": items}
