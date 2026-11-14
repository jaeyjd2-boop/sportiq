from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from app.db import engine

router = APIRouter(prefix="/v1/matches", tags=["analysis"])

@router.get("/{match_id}/analysis")
def analysis(match_id: int):
    with engine.connect() as c:
        m = c.execute(text("""SELECT m.id,m.home_team_id,m.away_team_id,m.start_time,m.status,m.status_detail,m.home_score,m.away_score,m.updated_at,l.name AS league,l.logo_url AS league_logo_url,ht.name AS home_team,ht.logo_url AS home_team_logo_url,at.name AS away_team,at.logo_url AS away_team_logo_url FROM matches m LEFT JOIN leagues l ON l.id=m.league_id LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE m.id=:id"""),{"id":match_id}).mappings().first()
        if not m: raise HTTPException(404,"match not found")
        recent_sql = text("""SELECT m.id,m.start_time,m.status,m.home_score,m.away_score,m.home_team_id,m.away_team_id,ht.name AS home_team,at.name AS away_team FROM matches m LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE (m.home_team_id=:team OR m.away_team_id=:team) AND m.status IN ('FT','AET','PEN') AND m.id<>:id ORDER BY m.start_time DESC,m.id DESC LIMIT 5""")
        home = c.execute(recent_sql,{"team":m["home_team_id"],"id":match_id}).mappings().all() if m["home_team_id"] else []
        away = c.execute(recent_sql,{"team":m["away_team_id"],"id":match_id}).mappings().all() if m["away_team_id"] else []
        h2h = c.execute(text("""SELECT m.id,m.start_time,m.status,m.home_score,m.away_score,ht.name AS home_team,at.name AS away_team FROM matches m LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE ((m.home_team_id=:h AND m.away_team_id=:a) OR (m.home_team_id=:a AND m.away_team_id=:h)) AND m.status IN ('FT','AET','PEN') AND m.id<>:id ORDER BY m.start_time DESC,m.id DESC LIMIT 10"""),{"h":m["home_team_id"],"a":m["away_team_id"],"id":match_id}).mappings().all() if m["home_team_id"] and m["away_team_id"] else []
        stats = c.execute(text("SELECT t.name AS team,ts.statistics,ts.updated_at FROM match_team_stats ts JOIN teams t ON t.id=ts.team_id WHERE ts.match_id=:id"),{"id":match_id}).mappings().all()
        events = c.execute(text("SELECT event_type,event_time,player_name,detail FROM match_events WHERE match_id=:id ORDER BY id LIMIT 100"),{"id":match_id}).mappings().all()
    def form(rows, team):
        w=d=l=gf=ga=0
        for r in rows:
            if r["home_score"] is None or r["away_score"] is None: continue
            own=int(r["home_score"] if r["home_team_id"]==team else r["away_score"])
            opp=int(r["away_score"] if r["home_team_id"]==team else r["home_score"])
            gf+=own;ga+=opp
            if own>opp: w+=1
            elif own==opp: d+=1
            else: l+=1
        played=w+d+l
        return {"played":played,"wins":w,"draws":d,"losses":l,"goals_for":gf,"goals_against":ga,"points_per_game":round((3*w+d)/played,2) if played else None}
    return {"match":dict(m),"home":{"recent":[dict(x) for x in home],"form":form(home,m["home_team_id"])},"away":{"recent":[dict(x) for x in away],"form":form(away,m["away_team_id"])},"head_to_head":[dict(x) for x in h2h],"live_statistics":[dict(x) for x in stats],"events":[dict(x) for x in events],"statistics_available":bool(stats),"note":"Only stored data is shown; unavailable statistics are not estimated."}
