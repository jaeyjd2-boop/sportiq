"""Non-football collection: ESPN first, API-SPORTS for a sport once its key is set.

One source per sport. A sport uses API-SPORTS when API_SPORTS_KEY is set
and the sport is supported/selected (see providers.api_sports.active_sports);
otherwise ESPN. The two never run for the same sport at the same time.

Switching source without duplicates ("adoption"): provider ids differ
(espn:... vs as:...), so before storing a batch from the new source, every
incoming match is compared with existing rows of the *other* source for the
same sport: same normalized home and away names and kickoff within 3 hours.
A match found that way gets its provider_match_id rewritten to the new id,
and a team found by exact normalized name gets its provider_team_id
rewritten. The normal upsert then updates that same row, so history and
team ids (used by /analysis) carry over. Names that differ between providers
are not merged; those matches stay as separate rows under the old source.

Korean leagues: ESPN has no KBO, KBL or V-League. With the key set, the
sports in API_SPORTS_KOREA (that are not fully on API-SPORTS) also get their
South Korean games from API-SPORTS (same /games?date= request, filtered by
country), stored as as:... rows next to the ESPN rows of that sport. No
adoption runs for them: ESPN never has those games.

Statuses and scores are always the provider's values.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, text

from app.collector import store_fixtures
from app.config import settings
from app.db import engine
from app.providers import api_sports, espn

log = logging.getLogger("sportiq-multisport")

LIVE_STATUSES = ("LIVE", "HT", "1H", "2H", "ET", "BT", "P")
FINAL_STATUSES = ("FT", "AET", "PEN", "AWD", "CANC", "PST", "ABD")
ADOPT_WINDOW = timedelta(hours=3)


def source_for(sport):
    return "api_sports" if sport in api_sports.active_sports() else "espn"


def window_dates(now=None):
    """Yesterday .. today + FIXTURE_LOOKAHEAD_DAYS - 1 (UTC)."""
    today = (now or datetime.now(timezone.utc)).date()
    return today - timedelta(days=1), today + timedelta(days=max(1, settings.fixture_lookahead_days) - 1)


def _ts(dt):
    """UTC ISO-8601 text: PostgreSQL casts it for timestamptz comparisons and
    SQLite (local runs) compares it with the stored ISO strings."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _norm(name):
    return re.sub(r"[^0-9a-z가-힣]", "", str(name or "").lower())


def _parse_when(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def adopt_existing(items, sport, new_prefix):
    """Re-key rows of the other source that are the same match/team (see module doc)."""
    old_prefix = "espn:" if new_prefix == "as:" else "as:"
    if not items:
        return 0
    adopted = 0
    with engine.begin() as c:
        sid = c.execute(text("SELECT id FROM sports WHERE code=:s"), {"s": sport}).scalar()
        if sid is None:
            return 0
        teams = c.execute(text("SELECT id, name, provider_team_id FROM teams WHERE sport_id=:sid"),
                          {"sid": sid}).mappings().all()
        old_teams = {}
        known_ids = {str(t["provider_team_id"]) for t in teams}
        for t in teams:
            if str(t["provider_team_id"]).startswith(old_prefix):
                old_teams.setdefault(_norm(t["name"]), []).append(t)
        for x in items:
            for side in ("home", "away"):
                tm = x["teams"][side]
                if tm["id"] in known_ids:
                    continue
                cands = old_teams.get(_norm(tm["name"])) or []
                if len(cands) == 1:
                    c.execute(text("UPDATE teams SET provider_team_id=:new WHERE id=:id"),
                              {"new": tm["id"], "id": cands[0]["id"]})
                    known_ids.add(tm["id"])
                    old_teams.pop(_norm(tm["name"]), None)
        times = [t for t in (_parse_when(x["fixture"]["date"]) for x in items) if t]
        if not times:
            return 0
        rows = c.execute(text("""
            SELECT m.id, m.provider_match_id, m.start_time, ht.name AS home, at.name AS away
            FROM matches m LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id
            WHERE m.sport_id=:sid AND m.provider_match_id LIKE :old
              AND m.start_time >= :lo AND m.start_time <= :hi
        """), {"sid": sid, "old": old_prefix + "%", "lo": _ts(min(times) - ADOPT_WINDOW),
               "hi": _ts(max(times) + ADOPT_WINDOW)}).mappings().all()
        existing_new = {r[0] for r in c.execute(text(
            "SELECT provider_match_id FROM matches WHERE sport_id=:sid AND provider_match_id LIKE :new"),
            {"sid": sid, "new": new_prefix + "%"}).all()}
        used = set()
        for x in items:
            new_id = x["fixture"]["id"]
            if new_id in existing_new:
                continue
            when = _parse_when(x["fixture"]["date"])
            h, a = _norm(x["teams"]["home"]["name"]), _norm(x["teams"]["away"]["name"])
            for r in rows:
                if r["id"] in used:
                    continue
                st = _parse_when(r["start_time"])
                if st is None or when is None or abs(st - when) > ADOPT_WINDOW:
                    continue
                if _norm(r["home"]) == h and _norm(r["away"]) == a:
                    c.execute(text("UPDATE matches SET provider_match_id=:new WHERE id=:id"),
                              {"new": new_id, "id": r["id"]})
                    used.add(r["id"])
                    existing_new.add(new_id)
                    adopted += 1
                    break
    return adopted


def _store(items, sport, prefix, adopt=True):
    good = [x for x in items if x and x["fixture"]["id"].startswith(prefix)]
    if not good:
        return 0
    if adopt:
        adopt_existing(good, sport, prefix)
    return store_fixtures(good, sport_code=sport)


def collect_schedule(espn_client=None, as_client=None, now=None):
    """Yesterday..lookahead for every non-football sport from its source."""
    first, last = window_dates(now)
    result = {}
    own_espn = espn_client is None
    espn_client = espn_client or espn.EspnClient()
    try:
        for league in espn.LEAGUES:
            if source_for(league.sport) != "espn":
                continue
            items = espn_client.league_window(league, first, last)
            result[league.key] = _store(items, league.sport, "espn:")
        # API-SPORTS costs quota per date: whole window every 6 h, today otherwise.
        now_dt = now or datetime.now(timezone.utc)
        full = now_dt.hour % 6 == 0
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)] if full else [now_dt.date()]
        result.update(_collect_api_sports_days(as_client, days))
        result.update(_collect_api_sports_days(as_client, days, api_sports.korea_sports(), korea_only=True))
    finally:
        if own_espn:
            espn_client.close()
    return result


def _collect_api_sports_days(as_client, days, sports=None, korea_only=False):
    """sports=None means every sport fully on API-SPORTS; an empty set means none."""
    sports = sorted(api_sports.active_sports() if sports is None else sports)
    if not sports:
        return {}
    own = as_client is None
    as_client = as_client or api_sports.ApiSportsClient(engine)
    out = {}
    try:
        for sport in sports:
            total = 0
            for d in days:
                try:
                    total += _store(as_client.games(sport, d.isoformat(), korea_only=korea_only),
                                    sport, "as:", adopt=not korea_only)
                except api_sports.BudgetExhausted as exc:
                    log.warning("api-sports budget: %s", exc)
                    break
            out[("as-kr:" if korea_only else "as:") + sport] = total
    finally:
        if own:
            as_client.close()
    return out


def sports_needing_live_poll(now=None):
    """Non-football sports with a match live now or starting within 15 min."""
    now = now or datetime.now(timezone.utc)
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT DISTINCT s.code AS sport, l.provider_league_id AS league
            FROM matches m JOIN sports s ON s.id=m.sport_id JOIN leagues l ON l.id=m.league_id
            WHERE s.code <> 'football'
              AND (m.provider_match_id LIKE 'espn:%' OR m.provider_match_id LIKE 'as:%')
              AND ((m.status IN :live AND m.start_time >= :old)
                   OR (m.status NOT IN :final AND m.start_time BETWEEN :lo AND :hi))
        """).bindparams(bindparam("live", expanding=True), bindparam("final", expanding=True)), {
            "live": list(LIVE_STATUSES), "final": list(FINAL_STATUSES),
            "old": _ts(now - timedelta(hours=12)), "lo": _ts(now - timedelta(hours=6)), "hi": _ts(now + timedelta(minutes=15)),
        }).mappings().all()
    return {(r["sport"], str(r["league"])) for r in rows}


def collect_live(espn_client=None, as_client=None, now=None):
    """Every 10 min: refetch only leagues that have live/imminent matches (today ±1)."""
    now = now or datetime.now(timezone.utc)
    targets = sports_needing_live_poll(now)
    if not targets:
        return {}
    today = now.date()
    days = [today - timedelta(days=1), today]
    result = {}
    espn_keys = {lg.removeprefix("espn:") for sp, lg in targets if lg.startswith("espn:") and source_for(sp) == "espn"}
    if espn_keys:
        own = espn_client is None
        espn_client = espn_client or espn.EspnClient()
        try:
            for league in espn.LEAGUES:
                if league.key in espn_keys:
                    # league_window adds the previous US date for date-based leagues.
                    items = espn_client.league_window(league, today, today)
                    result[league.key] = _store(items, league.sport, "espn:")
        finally:
            if own:
                espn_client.close()
    as_sports = {sp for sp, lg in targets if source_for(sp) == "api_sports"}
    if as_sports:
        result.update(_collect_api_sports_days(as_client, [today], as_sports))
    kr_sports = {sp for sp, lg in targets if lg.startswith("as:")} & api_sports.korea_sports()
    if kr_sports:
        result.update(_collect_api_sports_days(as_client, [today], kr_sports, korea_only=True))
    return result
