"""Links from SportIQ to jjangdoltv.com, pointed at league pages once they exist.

* Every link to jjangdoltv.com carries rel="nofollow sponsored noopener": they
  are traffic buttons, not editorial links, and pass no ranking signal.
* A league link goes to {BASE}/sports/{slug} (slug as in
  sportiq_seo.STANDINGS_LEAGUES, e.g. epl, mlb, nba) only when that page is
  listed in jjangdoltv's sitemap.xml and answers 200. Otherwise it goes to the
  jjangdoltv home page. As of 2026-11-25 no /sports/{slug} page exists, so all
  links go to the home page until jjangdoltv adds them.
* The check runs in the web process every 12 hours (first run 3 minutes after
  start). The result is kept in the lounge SQLite table jjangdol_link_state
  (also in the PG snapshot list), so a redeploy starts from the last result.
* Failures never flip links: if the sitemap cannot be read, the previous result
  stays; a league whose page check fails on the network keeps its old value.
  Only a definite answer (listed + 200, or not listed / 404) changes a league.
* Runs on Render (RENDER env var) or with JJANGDOL_LINKS_ENABLED=1;
  JJANGDOL_LINKS_ENABLED=0 turns it off. JJANGDOL_BASE_URL overrides the domain.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, Iterable, List, Optional, Set

from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger("sportiq.jjangdol")

BASE = (os.environ.get("JJANGDOL_BASE_URL") or "https://jjangdoltv.com").strip().rstrip("/")
ROOT_URL = BASE + "/"
REL = "nofollow sponsored noopener"
USER_AGENT = "Mozilla/5.0 (compatible; SportIQ-linkcheck/1.0)"
TABLE = "jjangdol_link_state"
CHECK_EVERY_SECONDS = 12 * 3600
MAX_CHILD_SITEMAPS = 10

router = APIRouter(tags=["jjangdol-links"])
_STATE: Dict[str, object] = {"leagues": {}, "checked_at": 0.0}
_LOCK = threading.Lock()
_READY = [False]
_LOADED = [False]
_STARTED = [False]


def league_slugs() -> List[str]:
    from app.sportiq_seo import STANDINGS_LEAGUES

    return list(STANDINGS_LEAGUES)


def league_page(slug: str) -> str:
    return f"{BASE}/sports/{slug}"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _get(url: str, timeout: float = 15.0) -> tuple:
    """(status, body). status 0 means no answer (network, DNS, timeout)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read(5_000_000)
    except urllib.error.HTTPError as e:
        return int(e.code), b""
    except Exception as e:
        log.warning("jjangdol fetch %s failed: %s", url, e)
        return 0, b""


_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def sitemap_urls() -> Optional[Set[str]]:
    """Every <loc> in {BASE}/sitemap.xml (one level of sitemap index followed).

    None when the sitemap could not be read: callers keep the previous result."""
    status, body = _get(BASE + "/sitemap.xml")
    if status != 200:
        return None
    text = body.decode("utf-8", "replace")
    locs = [u.strip() for u in _LOC.findall(text)]
    if "<sitemapindex" in text[:2000].lower():
        out: Set[str] = set()
        for child in locs[:MAX_CHILD_SITEMAPS]:
            st, b = _get(child)
            if st != 200:
                return None
            out.update(u.strip() for u in _LOC.findall(b.decode("utf-8", "replace")))
        return out
    return set(locs)


def _norm(url: str) -> str:
    return url.split("#", 1)[0].split("?", 1)[0].rstrip("/")


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------
def check(previous: Optional[Dict[str, str]] = None,
          slugs: Optional[Iterable[str]] = None) -> Optional[Dict[str, str]]:
    """Return {slug: league page URL} for the leagues with a live page.

    None when the sitemap could not be read (keep the old result)."""
    previous = dict(previous or {})
    listed = sitemap_urls()
    if listed is None:
        return None
    listed_norm = {_norm(u) for u in listed}
    out: Dict[str, str] = {}
    for slug in (slugs if slugs is not None else league_slugs()):
        page = league_page(slug)
        if _norm(page) not in listed_norm:
            continue                      # not listed: home page
        status, _ = _get(page)
        if status == 200:
            out[slug] = page
        elif status == 0 or status == 429 or status >= 500:
            if slug in previous:          # no clear answer: keep what we had
                out[slug] = previous[slug]
    return out


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def _conn():
    from app import sportiq_auto_analyst as aa

    return aa._get_conn()


def _ensure_table() -> None:
    if _READY[0]:
        return
    with _conn() as c:
        c.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at REAL NOT NULL)")
    try:
        from app import sportiq_auto_analyst as aa
        tables = aa._PG_SNAPSHOT_TABLES
        if TABLE not in tables:
            aa._PG_SNAPSHOT_TABLES = (list(tables) + [TABLE]) if isinstance(tables, list) else tuple(tables) + (TABLE,)
        aa._restore_snapshots_from_pg()
    except Exception:
        pass
    _READY[0] = True


def _load() -> None:
    if _LOADED[0]:
        return
    _LOADED[0] = True
    try:
        _ensure_table()
        with _conn() as c:
            row = c.execute(f"SELECT v FROM {TABLE} WHERE k = 'state'").fetchone()
        if row:
            data = json.loads(row[0])
            leagues = {str(k): str(v) for k, v in (data.get("leagues") or {}).items()
                       if isinstance(v, str) and v.startswith(BASE + "/")}
            with _LOCK:
                _STATE["leagues"] = leagues
                _STATE["checked_at"] = float(data.get("checked_at") or 0)
    except Exception as e:
        log.warning("jjangdol state load failed: %s", e)


def _save(leagues: Dict[str, str], checked_at: float) -> None:
    with _LOCK:
        _STATE["leagues"] = dict(leagues)
        _STATE["checked_at"] = checked_at
    try:
        _ensure_table()
        with _conn() as c:
            c.execute(f"INSERT OR REPLACE INTO {TABLE} (k, v, updated_at) VALUES ('state', ?, ?)",
                      (json.dumps({"leagues": leagues, "checked_at": checked_at}), checked_at))
        from app import sportiq_auto_analyst as aa
        aa._backup_tables_to_pg(TABLE)
    except Exception as e:
        log.warning("jjangdol state save failed: %s", e)


def current() -> Dict[str, str]:
    _load()
    with _LOCK:
        return dict(_STATE["leagues"])  # type: ignore[arg-type]


def url_for(slug: Optional[str]) -> str:
    """League page when it exists, else the jjangdoltv home page."""
    if slug:
        hit = current().get(slug)
        if hit:
            return hit
    return ROOT_URL


def run_check() -> Optional[Dict[str, str]]:
    """One check; stores and returns the new map, or None when it kept the old one."""
    try:
        before = current()
        new = check(before)
        if new is None:
            log.warning("jjangdol sitemap unreadable; keeping %d league links", len(before))
            return None
        if new != before:
            log.info("jjangdol league links: %s -> %s", sorted(before), sorted(new))
        _save(new, time.time())
        return new
    except Exception as e:
        log.warning("jjangdol link check failed: %s", e)
        return None


# --------------------------------------------------------------------------
# API and job
# --------------------------------------------------------------------------
@router.get("/api/jjangdol/links")
def jjangdol_links():
    with _LOCK:
        checked = float(_STATE["checked_at"] or 0)  # type: ignore[arg-type]
    leagues = current()
    return JSONResponse(
        {"root": ROOT_URL, "rel": REL, "leagues": leagues,
         "checked_at": int(checked) if checked else None},
        headers={"Cache-Control": "public, max-age=600"},
    )


def enabled() -> bool:
    flag = (os.environ.get("JJANGDOL_LINKS_ENABLED") or "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    return flag in ("1", "true", "yes") or bool(os.environ.get("RENDER"))


def _loop() -> None:
    time.sleep(180)
    while True:
        run_check()
        time.sleep(CHECK_EVERY_SECONDS)


@router.on_event("startup")
def _start_jjangdol_job() -> None:
    if not enabled() or _STARTED[0]:
        return
    _STARTED[0] = True
    threading.Thread(target=_loop, name="jjangdol-links", daemon=True).start()
