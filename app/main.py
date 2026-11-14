from datetime import date
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.config import settings
from app.db import engine
app=FastAPI(title="SportIQ API",version="1.0.0")
app.add_middleware(CORSMiddleware,allow_origins=settings.allowed_origins,allow_credentials=False,allow_methods=["GET"],allow_headers=["*"])
@app.get("/health")
def health():
    with engine.connect() as c: c.execute(text("SELECT 1"))
    return {"status":"ok","service":"sportiq-api"}
@app.get("/v1/sports")
def sports():
    with engine.connect() as c: return {"items":[dict(r) for r in c.execute(text("SELECT id,code,name FROM sports ORDER BY id")).mappings().all()]}
@app.get("/v1/leagues")
def leagues(sport:str="football",country:str|None=None,limit:int=Query(100,ge=1,le=500)):
    q="SELECT l.id,s.code AS sport,l.name,l.country,l.logo_url,l.coverage FROM leagues l JOIN sports s ON s.id=l.sport_id WHERE s.code=:sport"
    p={"sport":sport,"limit":limit}
    if country: q+=" AND l.country=:country"; p["country"]=country
    with engine.connect() as c:
        rows=c.execute(text(q+" ORDER BY l.country,l.name LIMIT :limit"),p).mappings().all()
        return {"items":[dict(r) for r in rows],"total":len(rows)}
@app.get("/v1/matches")
def matches(sport:str="football",target_date:date|None=Query(None,alias="date"),limit:int=Query(100,ge=1,le=500)):
    q="SELECT m.id,s.code AS sport,l.name AS league,ht.name AS home_team,at.name AS away_team,m.start_time,m.status,m.status_detail,m.home_score,m.away_score,m.updated_at FROM matches m JOIN sports s ON s.id=m.sport_id LEFT JOIN leagues l ON l.id=m.league_id LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE s.code=:sport"
    p={"sport":sport,"limit":limit}
    if target_date: q+=" AND m.start_time::date=:target_date"; p["target_date"]=target_date
    with engine.connect() as c:
        rows=c.execute(text(q+" ORDER BY m.start_time LIMIT :limit"),p).mappings().all()
        return {"items":[dict(r) for r in rows],"total":len(rows)}
@app.get("/v1/matches/live")
def live(sport:str="football"):
    with engine.connect() as c:
        rows=c.execute(text("SELECT m.id,l.name AS league,ht.name AS home_team,at.name AS away_team,m.start_time,m.status,m.status_detail,m.home_score,m.away_score,m.updated_at FROM matches m JOIN sports s ON s.id=m.sport_id LEFT JOIN leagues l ON l.id=m.league_id LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE s.code=:sport AND m.status IN ('1H','HT','2H','ET','P','LIVE','Q1','Q2','Q3','Q4','IN') ORDER BY m.start_time"),{"sport":sport}).mappings().all()
        return {"items":[dict(r) for r in rows],"total":len(rows)}
@app.get("/v1/matches/{match_id}")
def match_detail(match_id:int):
    with engine.connect() as c:
        row=c.execute(text("SELECT m.id,s.code AS sport,l.name AS league,ht.name AS home_team,at.name AS away_team,m.start_time,m.status,m.status_detail,m.home_score,m.away_score,m.venue,m.referee,m.updated_at FROM matches m JOIN sports s ON s.id=m.sport_id LEFT JOIN leagues l ON l.id=m.league_id LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE m.id=:id"),{"id":match_id}).mappings().first()
        if not row: raise HTTPException(404,"match not found")
        return dict(row)
@app.get("/v1/predictions/{match_id}")
def prediction(match_id:int):
    with engine.connect() as c:
        row=c.execute(text("SELECT id,match_id,model_version,probabilities,predicted_result,predicted_score,confidence,created_at FROM predictions WHERE match_id=:id ORDER BY created_at DESC LIMIT 1"),{"id":match_id}).mappings().first()
        if not row: raise HTTPException(404,"prediction not found")
        return dict(row)
