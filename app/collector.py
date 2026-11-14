import json
from datetime import date
from sqlalchemy import text
from app.db import engine
from app.providers.football_data import FootballDataClient
def collect_leagues():
    client=FootballDataClient()
    try:
        items=client.leagues()
        with engine.begin() as c:
            sid=c.execute(text("SELECT id FROM sports WHERE code='football'")).scalar_one()
            for x in items:
                l=x.get("league") or {}; country=(x.get("country") or {}).get("name")
                c.execute(text("""INSERT INTO leagues(sport_id,provider_league_id,name,country,logo_url,coverage) VALUES(:sid,:ext,:name,:country,:logo,CAST(:coverage AS JSONB)) ON CONFLICT(sport_id,provider_league_id) DO UPDATE SET name=EXCLUDED.name,country=EXCLUDED.country,logo_url=EXCLUDED.logo_url,coverage=EXCLUDED.coverage"""),{"sid":sid,"ext":str(l.get("id")),"name":l.get("name") or "Unknown","country":country,"logo":l.get("logo"),"coverage":json.dumps((x.get("seasons") or [{}])[0].get("coverage",{}))})
        return len(items)
    finally: client.close()
def collect_fixtures(day:date, end_day=None):
    client=FootballDataClient()
    try: items=client.fixtures_range(day.isoformat(), (end_day or day).isoformat())
    finally: client.close()
    return store_fixtures(items)


def store_fixtures(items, chunk_size=200):
    """Persist fixtures with cached league/team lookups and batched match upserts.

    Each chunk commits atomically. Invalid records are rejected rather than
    inventing provider IDs; any failed chunk raises for caller retry.
    """
    if not isinstance(chunk_size, int) or chunk_size < 1 or chunk_size > 1000:
        raise ValueError("chunk_size must be between 1 and 1000")
    if not items:
        return 0

    # Last occurrence wins; avoid PostgreSQL ON CONFLICT affecting one row twice.
    unique = {}
    for x in items:
        fixture = x.get("fixture") or {}
        fid = fixture.get("id")
        if fid is None:
            raise ValueError("fixture missing provider ID")
        unique[str(fid)] = x

    league_sql = text("INSERT INTO leagues(sport_id,provider_league_id,name,country) VALUES(:sid,:ext,:name,:country) ON CONFLICT(sport_id,provider_league_id) DO UPDATE SET name=EXCLUDED.name RETURNING id")
    team_sql = text("INSERT INTO teams(sport_id,provider_team_id,name,logo_url) VALUES(:sid,:ext,:name,:logo) ON CONFLICT(sport_id,provider_team_id) DO UPDATE SET name=EXCLUDED.name,logo_url=EXCLUDED.logo_url RETURNING id")
    match_sql = text("""INSERT INTO matches(sport_id,league_id,provider_match_id,home_team_id,away_team_id,start_time,status,status_detail,home_score,away_score,venue,referee,raw_payload,updated_at)
        VALUES(:sid,:lid,:ext,:home,:away,:start,:status,:detail,:hs,:ascore,:venue,:ref,CAST(:raw AS JSONB),NOW())
        ON CONFLICT(sport_id,provider_match_id) DO UPDATE SET
        league_id=EXCLUDED.league_id,home_team_id=EXCLUDED.home_team_id,
        away_team_id=EXCLUDED.away_team_id,start_time=EXCLUDED.start_time,
        status=EXCLUDED.status,status_detail=EXCLUDED.status_detail,
        home_score=EXCLUDED.home_score,away_score=EXCLUDED.away_score,
        raw_payload=EXCLUDED.raw_payload,updated_at=NOW()""")

    entries = list(unique.values())
    count = 0
    with engine.connect() as conn:
        sid = conn.execute(text("SELECT id FROM sports WHERE code='football'")).scalar_one()
    for offset in range(0, len(entries), chunk_size):
        batch = entries[offset:offset + chunk_size]
        with engine.begin() as conn:
            league_ids = {}
            team_ids = {}
            match_rows = []
            for x in batch:
                f = x.get("fixture") or {}
                l = x.get("league") or {}
                teams = x.get("teams") or {}
                goals = x.get("goals") or {}
                league_external = l.get("id")
                home = teams.get("home") or {}
                away = teams.get("away") or {}
                if league_external is None or home.get("id") is None or away.get("id") is None or not f.get("date"):
                    raise ValueError("fixture missing league, team, or date")
                league_key = str(league_external)
                if league_key not in league_ids:
                    league_ids[league_key] = conn.execute(league_sql, {
                        "sid": sid, "ext": league_key, "name": l.get("name") or "Unknown",
                        "country": (x.get("country") or {}).get("name")
                    }).scalar_one()
                tids = []
                for team in (home, away):
                    key = str(team["id"])
                    if key not in team_ids:
                        team_ids[key] = conn.execute(team_sql, {
                            "sid": sid, "ext": key, "name": team.get("name") or "Unknown",
                            "logo": team.get("logo")
                        }).scalar_one()
                    tids.append(team_ids[key])
                venue = f.get("venue") or {}
                state = f.get("status") or {}
                match_rows.append({
                    "sid": sid, "lid": league_ids[league_key], "ext": str(f["id"]),
                    "home": tids[0], "away": tids[1], "start": f["date"],
                    "status": state.get("short", "NS"), "detail": state.get("long"),
                    "hs": goals.get("home"), "ascore": goals.get("away"),
                    "venue": venue.get("name"), "ref": f.get("referee"),
                    "raw": json.dumps(x, ensure_ascii=False),
                })
            # SQLAlchemy/psycopg2 executemany batches repeated match INSERTs.
            conn.execute(match_sql, match_rows)
        count += len(batch)
    return count

def collect_live():
    # 라이브 전용 피드에서 반환된 fixture의 날짜를 모아 해당 날짜의 최신 일정을 재수집
    client=FootballDataClient()
    try: live=client.live()
    finally: client.close()
    days=sorted({str(((x.get("fixture") or {}).get("date") or ""))[:10] for x in live if (x.get("fixture") or {}).get("date")})
    return sum(collect_fixtures(date.fromisoformat(d)) for d in days)

def refresh_forecast_results(days_back=2):
    """Refresh recent Brazil Serie A results independently of the LIVE feed.

    One competition-scoped request avoids a costly all-league scan and
    catches matches that finish between regular fixture collection runs.
    """
    from datetime import datetime, timedelta, timezone
    if not isinstance(days_back, int) or not 1 <= days_back <= 3:
        raise ValueError("days_back must be 1..3")
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)
    client = FootballDataClient()
    try:
        items = client.competition_fixtures_range("BSA", start.isoformat(), today.isoformat())
    finally:
        client.close()
    return store_fixtures(items) if items else 0


def collect_match_details(max_fixtures=2):
    """Quota-conscious enrichment of live matches; no historical backfill."""
    # football-data.org free tier does not provide fixture statistics or events.
    return 0
    client=FootballDataClient()
    count=0
    try:
        with engine.connect() as c:
            fixtures=c.execute(text("""SELECT m.id,m.provider_match_id FROM matches m JOIN sports s ON s.id=m.sport_id WHERE s.code='football' AND m.status IN ('1H','HT','2H','ET','P','LIVE') AND m.provider_match_id IS NOT NULL ORDER BY m.updated_at DESC LIMIT :limit"""),{"limit":max_fixtures}).mappings().all()
        for fixture in fixtures:
            fid=fixture["provider_match_id"]
            try:
                stats=client.fixture_statistics(fid)
                events=client.fixture_events(fid)
            except Exception:
                import logging
                logging.getLogger("sportiq-worker").exception("details fixture %s unavailable",fid)
                continue
            with engine.begin() as c:
                for item in stats:
                    team=(item.get("team") or {})
                    provider_id=team.get("id")
                    if provider_id is None: continue
                    team_id=c.execute(text("""SELECT t.id FROM teams t JOIN sports s ON s.id=t.sport_id WHERE s.code='football' AND t.provider_team_id=:pid"""),{"pid":str(provider_id)}).scalar()
                    if not team_id: continue
                    values={x.get("type"):x.get("value") for x in item.get("statistics",[]) if x.get("type")}
                    c.execute(text("""INSERT INTO match_team_stats(match_id,team_id,statistics,updated_at) VALUES(:mid,:tid,CAST(:stats AS JSONB),NOW()) ON CONFLICT(match_id,team_id) DO UPDATE SET statistics=EXCLUDED.statistics,updated_at=NOW()"""),{"mid":fixture["id"],"tid":team_id,"stats":json.dumps(values,ensure_ascii=False)})
                c.execute(text("DELETE FROM match_events WHERE match_id=:mid"),{"mid":fixture["id"]})
                for event in events:
                    tm=event.get("time") or {}; player=event.get("player") or {}
                    c.execute(text("""INSERT INTO match_events(match_id,event_type,event_time,player_name,detail,payload) VALUES(:mid,:kind,:clock,:player,:detail,CAST(:payload AS JSONB))"""),{"mid":fixture["id"],"kind":event.get("type"),"clock":str(tm.get("elapsed") or "") + ("+"+str(tm["extra"]) if tm.get("extra") else ""),"player":player.get("name"),"detail":event.get("detail"),"payload":json.dumps(event,ensure_ascii=False)})
            count+=1
        return count
    finally:
        client.close()


def collect_historical(days_back=30, window_days=7):
    """Bounded historical backfill; explicit invocation only, not an automatic job."""
    from datetime import timedelta, timezone, datetime
    if not isinstance(days_back, int) or not 1 <= days_back <= 90:
        raise ValueError("days_back must be between 1 and 90")
    if not isinstance(window_days, int) or not 1 <= window_days <= 14:
        raise ValueError("window_days must be between 1 and 14")
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    first = yesterday - timedelta(days=days_back - 1)
    client = FootballDataClient()
    saved = 0
    try:
        start = first
        while start <= yesterday:
            end = min(start + timedelta(days=window_days - 1), yesterday)
            fixtures = client.fixtures_range(start.isoformat(), end.isoformat())
            historical = [item for item in fixtures
                          if (item.get("fixture") or {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
                          and (item.get("goals") or {}).get("home") is not None
                          and (item.get("goals") or {}).get("away") is not None]
            saved += store_fixtures(historical)
            start = end + timedelta(days=1)
    finally:
        client.close()
    return saved
