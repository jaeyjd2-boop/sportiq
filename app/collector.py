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
    count=0
    with engine.begin() as c:
        sid=c.execute(text("SELECT id FROM sports WHERE code='football'")).scalar_one()
        for x in items:
            f=x.get("fixture") or {}; l=x.get("league") or {}; teams=x.get("teams") or {}; goals=x.get("goals") or {}
            lid=c.execute(text("INSERT INTO leagues(sport_id,provider_league_id,name,country) VALUES(:sid,:ext,:name,:country) ON CONFLICT(sport_id,provider_league_id) DO UPDATE SET name=EXCLUDED.name RETURNING id"),{"sid":sid,"ext":str(l.get("id")),"name":l.get("name") or "Unknown","country":(x.get("country") or {}).get("name")}).scalar_one()
            tids=[]
            for t in [teams.get("home") or {},teams.get("away") or {}]:
                tids.append(c.execute(text("INSERT INTO teams(sport_id,provider_team_id,name,logo_url) VALUES(:sid,:ext,:name,:logo) ON CONFLICT(sport_id,provider_team_id) DO UPDATE SET name=EXCLUDED.name,logo_url=EXCLUDED.logo_url RETURNING id"),{"sid":sid,"ext":str(t.get("id")),"name":t.get("name") or "Unknown","logo":t.get("logo")}).scalar_one())
            venue=f.get("venue") or {}; status=f.get("status") or {}
            c.execute(text("""INSERT INTO matches(sport_id,league_id,provider_match_id,home_team_id,away_team_id,start_time,status,status_detail,home_score,away_score,venue,referee,raw_payload,updated_at) VALUES(:sid,:lid,:ext,:home,:away,:start,:status,:detail,:hs,:ascore,:venue,:ref,CAST(:raw AS JSONB),NOW()) ON CONFLICT(sport_id,provider_match_id) DO UPDATE SET league_id=EXCLUDED.league_id,home_team_id=EXCLUDED.home_team_id,away_team_id=EXCLUDED.away_team_id,start_time=EXCLUDED.start_time,status=EXCLUDED.status,status_detail=EXCLUDED.status_detail,home_score=EXCLUDED.home_score,away_score=EXCLUDED.away_score,raw_payload=EXCLUDED.raw_payload,updated_at=NOW()"""),{"sid":sid,"lid":lid,"ext":str(f.get("id")),"home":tids[0],"away":tids[1],"start":f.get("date"),"status":status.get("short","NS"),"detail":status.get("long"),"hs":goals.get("home"),"ascore":goals.get("away"),"venue":venue.get("name"),"ref":f.get("referee"),"raw":json.dumps(x,ensure_ascii=False)})
            count+=1
    return count
def collect_live():
    # 라이브 전용 피드에서 반환된 fixture의 날짜를 모아 해당 날짜의 최신 일정을 재수집
    client=FootballDataClient()
    try: live=client.live()
    finally: client.close()
    days=sorted({str(((x.get("fixture") or {}).get("date") or ""))[:10] for x in live if (x.get("fixture") or {}).get("date")})
    return sum(collect_fixtures(date.fromisoformat(d)) for d in days)

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
