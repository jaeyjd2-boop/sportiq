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


@router.get("/{match_id}/forecast")
def forecast(match_id: int):
    """Experimental pre-match Poisson forecast using only earlier stored results."""
    from math import exp, factorial
    with engine.connect() as c:
        match = c.execute(text("""
            SELECT id, home_team_id, away_team_id, start_time, status
            FROM matches WHERE id=:id
        """), {"id": match_id}).mappings().first()
        if not match:
            raise HTTPException(404, "match not found")
        if not match["home_team_id"] or not match["away_team_id"]:
            raise HTTPException(422, "team identifiers unavailable")
        rows = c.execute(text("""
            SELECT home_team_id, away_team_id, home_score, away_score, start_time
            FROM matches
            WHERE start_time < :kickoff AND id <> :id
              AND status IN ('FT','AET','PEN')
              AND home_score IS NOT NULL AND away_score IS NOT NULL
              AND (home_team_id IN (:home,:away) OR away_team_id IN (:home,:away))
            ORDER BY start_time DESC LIMIT 500
        """), {"id":match_id,"kickoff":match["start_time"],
               "home":match["home_team_id"],"away":match["away_team_id"]}).mappings().all()
    def recent(team):
        return [r for r in rows if r["home_team_id"] == team or r["away_team_id"] == team][:5]
    h = recent(match["home_team_id"])
    a = recent(match["away_team_id"])
    if len(h) < 3 or len(a) < 3:
        return {"match_id": match_id, "available": False,
                "reason": "At least three completed prior matches per team are required",
                "samples": {"home":len(h),"away":len(a)}}
    def averages(games, team):
        scored = sum(float(r["home_score"] if r["home_team_id"] == team else r["away_score"]) for r in games)
        conceded = sum(float(r["away_score"] if r["home_team_id"] == team else r["home_score"]) for r in games)
        return scored/len(games), conceded/len(games)
    hf, ha = averages(h, match["home_team_id"])
    af, aa = averages(a, match["away_team_id"])
    # Shrink noisy five-match rates toward a neutral 1.3 goals per match.
    home_lambda = min(4.0, max(0.2, 0.65*(hf+aa)/2 + 0.35*1.3))
    away_lambda = min(4.0, max(0.2, 0.65*(af+ha)/2 + 0.35*1.3))
    def poisson(k, rate):
        return exp(-rate)*rate**k/factorial(k)
    # Include all scores 0..15; normalize the negligible truncated tail.
    matrix = [(i,j,poisson(i,home_lambda)*poisson(j,away_lambda))
              for i in range(16) for j in range(16)]
    total = sum(p for _,_,p in matrix)
    probs = {
        "home": sum(p for i,j,p in matrix if i>j)/total,
        "draw": sum(p for i,j,p in matrix if i==j)/total,
        "away": sum(p for i,j,p in matrix if i<j)/total,
    }
    score = max(matrix, key=lambda item:item[2])
    return {"match_id":match_id,"available":True,"model_version":"poisson-form-v0.1",
            "probabilities":{k:round(v,4) for k,v in probs.items()},
            "expected_goals":{"home":round(home_lambda,2),"away":round(away_lambda,2)},
            "most_likely_score":{"home":score[0],"away":score[1]},
            "samples":{"home":len(h),"away":len(a)},
            "disclaimer":"Experimental estimate, not calibrated or validated; historical data may be incomplete."}


@router.get("/forecasts/upcoming")
def upcoming_forecasts(league_provider_id: str = "fd:2013", limit: int = 10):
    """Small, bounded preview for the WordPress match board."""
    from fastapi import Query
    if not 1 <= limit <= 20:
        raise HTTPException(422, "limit must be between 1 and 20")
    with engine.connect() as c:
        fixtures = c.execute(text("""
            SELECT m.id, m.start_time, ht.name AS home_team, at.name AS away_team
            FROM matches m
            JOIN leagues l ON l.id = m.league_id
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            WHERE l.provider_league_id = :league
              AND m.start_time > NOW()
              AND m.status NOT IN ('FT','AET','PEN')
            ORDER BY m.start_time, m.id LIMIT :limit
        """), {"league":league_provider_id, "limit":limit}).mappings().all()
    items = []
    for fixture in fixtures:
        result = forecast(fixture["id"])
        items.append({"match":dict(fixture), "forecast":result})
    return {"league_provider_id":league_provider_id, "items":items,
            "total":len(items), "note":"Experimental predictions; not validated or calibrated."}
