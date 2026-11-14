"""Read-only football history coverage diagnostics."""
from fastapi import APIRouter, Query
from sqlalchemy import text
from app.db import engine

router = APIRouter(prefix="/v1/diagnostics", tags=["diagnostics"])

@router.get("/football-history")
def football_history(limit: int = Query(200, ge=1, le=2000)):
    """Coverage of upcoming fixtures by exact stored team IDs; no cross-provider guessing."""
    sql = text("""
        WITH upcoming AS (
            SELECT m.id,m.start_time,m.home_team_id,m.away_team_id,
                   ht.provider_team_id AS home_provider_id,
                   at.provider_team_id AS away_provider_id
            FROM matches m
            JOIN sports s ON s.id=m.sport_id
            LEFT JOIN teams ht ON ht.id=m.home_team_id
            LEFT JOIN teams at ON at.id=m.away_team_id
            WHERE s.code='football' AND m.start_time > NOW()
              AND m.status NOT IN ('FT','AET','PEN')
            ORDER BY m.start_time,m.id LIMIT :limit
        ), coverage AS (
            SELECT u.*,
              (SELECT COUNT(*) FROM matches p WHERE p.start_time<u.start_time
                AND p.status IN ('FT','AET','PEN')
                AND p.home_score IS NOT NULL AND p.away_score IS NOT NULL
                AND (p.home_team_id=u.home_team_id OR p.away_team_id=u.home_team_id)) AS home_history,
              (SELECT COUNT(*) FROM matches p WHERE p.start_time<u.start_time
                AND p.status IN ('FT','AET','PEN')
                AND p.home_score IS NOT NULL AND p.away_score IS NOT NULL
                AND (p.home_team_id=u.away_team_id OR p.away_team_id=u.away_team_id)) AS away_history
            FROM upcoming u
        )
        SELECT CASE WHEN home_provider_id LIKE 'fd:%' AND away_provider_id LIKE 'fd:%'
                    THEN 'football-data.org'
                    WHEN home_provider_id IS NULL OR away_provider_id IS NULL
                    THEN 'missing-team-id'
                    WHEN home_provider_id NOT LIKE 'fd:%' AND away_provider_id NOT LIKE 'fd:%'
                    THEN 'other-or-legacy'
                    ELSE 'mixed' END AS provider_group,
               COUNT(*) AS upcoming_matches,
               COUNT(*) FILTER (WHERE home_history >= 1) AS home_with_history,
               COUNT(*) FILTER (WHERE away_history >= 1) AS away_with_history,
               COUNT(*) FILTER (WHERE home_history >= 3 AND away_history >= 3) AS forecast_eligible,
               COUNT(*) FILTER (WHERE home_history >= 5 AND away_history >= 5) AS full_five_match_history
        FROM coverage GROUP BY 1 ORDER BY 1
    """)
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql,{"limit":limit}).mappings().all()]
    return {"sample_limit":limit,
            "total_sampled":sum(int(r["upcoming_matches"]) for r in rows),
            "forecast_eligible":sum(int(r["forecast_eligible"]) for r in rows),
            "groups":rows,
            "note":"Provider groups are inferred from stored ID prefixes; no team IDs are merged."}
