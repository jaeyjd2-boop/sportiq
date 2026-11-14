from datetime import date
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.config import settings
from app.db import engine
from app.details import router as details_router
app=FastAPI(title="SportIQ API",version="1.0.0")
app.include_router(details_router)
app.add_middleware(CORSMiddleware,allow_origins=settings.allowed_origins,allow_credentials=False,allow_methods=["GET"],allow_headers=["*"])
@app.get("/health")
def health():
    with engine.connect() as c: c.execute(text("SELECT 1"))
    return {"status":"ok","service":"sportiq-api"}
@app.get("/v1/sports")
def sports():
    with engine.connect() as c: return {"items":[dict(r) for r in c.execute(text("SELECT id,code,name FROM sports ORDER BY id")).mappings().all()]}
@app.get("/v1/leagues")
def leagues(
    sport: str = "football",
    country: str | None = None,
    search: str | None = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
):
    filters = ["s.code = :sport"]
    params = {"sport": sport, "limit": limit, "offset": (page - 1) * limit}
    if country:
        filters.append("l.country = :country")
        params["country"] = country
    if search:
        filters.append("l.name ILIKE :search")
        params["search"] = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        filters[-1] += " ESCAPE '\\'"
    base = " FROM leagues l JOIN sports s ON s.id = l.sport_id WHERE " + " AND ".join(filters)
    with engine.connect() as c:
        total = c.execute(text("SELECT COUNT(*)" + base), params).scalar_one()
        rows = c.execute(text(
            "SELECT l.id, s.code AS sport, l.name, l.country, l.logo_url, l.coverage"
            + base + " ORDER BY l.country NULLS LAST, l.name, l.id LIMIT :limit OFFSET :offset"
        ), params).mappings().all()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit,
        "has_next": page * limit < total,
        "has_previous": page > 1,
    }


@app.get("/v1/leagues/countries")
def league_countries(sport: str = "football"):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT l.country AS country, COUNT(*) AS league_count "
            "FROM leagues l JOIN sports s ON s.id = l.sport_id "
            "WHERE s.code = :sport AND l.country IS NOT NULL AND l.country <> '' "
            "GROUP BY l.country ORDER BY l.country"
        ), {"sport": sport}).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@app.get("/v1/matches")
def matches(
    sport: str = "football",
    target_date: date | None = Query(None, alias="date"),
    country: str | None = Query(None, min_length=1),
    league_id: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
):
    # Apply the same filters to the count and page queries.
    where = ["s.code = :sport"]
    params = {"sport": sport}
    if target_date is not None:
        where.append("m.start_time >= :date_start AND m.start_time < :date_end")
        from datetime import datetime, time, timedelta, timezone
        params["date_start"] = datetime.combine(target_date, time.min, tzinfo=timezone.utc)
        params["date_end"] = params["date_start"] + timedelta(days=1)
    if country is not None:
        where.append("l.country = :country")
        params["country"] = country
    if league_id is not None:
        where.append("m.league_id = :league_id")
        params["league_id"] = league_id

    joins = (
        " FROM matches m "
        "JOIN sports s ON s.id = m.sport_id "
        "LEFT JOIN leagues l ON l.id = m.league_id "
    )
    clause = " WHERE " + " AND ".join(where)
    params["limit"] = limit
    params["offset"] = (page - 1) * limit
    select = (
        "SELECT m.id, s.code AS sport, l.id AS league_id, "
        "l.name AS league, l.country AS country, l.logo_url AS league_logo_url, "
        "ht.name AS home_team, ht.logo_url AS home_team_logo_url, at.name AS away_team, at.logo_url AS away_team_logo_url, "
        "m.start_time, m.status, m.status_detail, "
        "m.home_score, m.away_score, m.updated_at "
    )
    with engine.connect() as c:
        total = c.execute(
            text("SELECT COUNT(*)" + joins + clause), params
        ).scalar_one()
        rows = c.execute(
            text(
                select + joins
                + "LEFT JOIN teams ht ON ht.id = m.home_team_id "
                + "LEFT JOIN teams at ON at.id = m.away_team_id "
                + clause
                + " ORDER BY m.start_time, m.id LIMIT :limit OFFSET :offset"
            ),
            params,
        ).mappings().all()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit,
        "has_next": page * limit < total,
        "has_previous": page > 1,
    }
@app.get("/v1/matches/live")
def live(sport:str="football"):
    with engine.connect() as c:
        rows=c.execute(text("SELECT m.id,l.name AS league,l.logo_url AS league_logo_url,ht.name AS home_team,ht.logo_url AS home_team_logo_url,at.name AS away_team,at.logo_url AS away_team_logo_url,m.start_time,m.status,m.status_detail,m.home_score,m.away_score,m.updated_at FROM matches m JOIN sports s ON s.id=m.sport_id LEFT JOIN leagues l ON l.id=m.league_id LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE s.code=:sport AND m.status IN ('1H','HT','2H','ET','P','LIVE','Q1','Q2','Q3','Q4','IN') ORDER BY m.start_time"),{"sport":sport}).mappings().all()
        return {"items":[dict(r) for r in rows],"total":len(rows)}
@app.get("/v1/matches/{match_id}")
def match_detail(match_id:int):
    with engine.connect() as c:
        row=c.execute(text("SELECT m.id,s.code AS sport,l.name AS league,l.logo_url AS league_logo_url,ht.name AS home_team,ht.logo_url AS home_team_logo_url,at.name AS away_team,at.logo_url AS away_team_logo_url,m.start_time,m.status,m.status_detail,m.home_score,m.away_score,m.venue,m.referee,m.updated_at FROM matches m JOIN sports s ON s.id=m.sport_id LEFT JOIN leagues l ON l.id=m.league_id LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE m.id=:id"),{"id":match_id}).mappings().first()
        if not row: raise HTTPException(404,"match not found")
        return dict(row)
@app.get("/v1/predictions/{match_id}")
def prediction(match_id:int):
    with engine.connect() as c:
        row=c.execute(text("SELECT id,match_id,model_version,probabilities,predicted_result,predicted_score,confidence,created_at FROM predictions WHERE match_id=:id ORDER BY created_at DESC LIMIT 1"),{"id":match_id}).mappings().first()
        if not row: raise HTTPException(404,"prediction not found")
        return dict(row)
