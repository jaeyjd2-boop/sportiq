"""Store immutable pre-kickoff forecasts and score finished matches."""
import json
from app.forecast_metrics import score_probabilities
from sqlalchemy import text
from app.db import engine
from app.details import forecast

MODEL = "poisson-form-v0.1"

def snapshot_forecasts(limit=30):
    with engine.connect() as c:
        ids=c.execute(text("""SELECT m.id FROM matches m JOIN leagues l ON l.id=m.league_id
          WHERE l.provider_league_id='fd:2013' AND m.start_time>NOW() AND m.status='NS'
          AND NOT EXISTS (SELECT 1 FROM predictions p WHERE p.match_id=m.id AND p.model_version=:model)
          ORDER BY m.start_time LIMIT :limit"""),{"model":MODEL,"limit":limit}).scalars().all()
    saved=0
    for mid in ids:
        result=forecast(mid)
        if not result.get("available"): continue
        probs=result["probabilities"]
        with engine.begin() as c:
            inserted=c.execute(text("""INSERT INTO predictions(match_id,model_version,probabilities,predicted_result,predicted_score,confidence)
              SELECT m.id,:model,CAST(:probs AS JSONB),:winner,CAST(:score AS JSONB),:confidence
              FROM matches m WHERE m.id=:mid AND m.start_time>NOW() AND m.status='NS'
              AND NOT EXISTS(SELECT 1 FROM predictions p WHERE p.match_id=m.id AND p.model_version=:model)
              RETURNING id"""),{"mid":mid,"model":MODEL,"probs":json.dumps(probs),
              "winner":max(probs,key=probs.get),"score":json.dumps(result["most_likely_score"]),
              "confidence":max(probs.values())}).scalar()
            saved+=int(inserted is not None)
    return saved

def evaluate_forecasts(limit=100):
    with engine.connect() as c:
        rows=c.execute(text("""SELECT p.id,p.probabilities,m.home_score,m.away_score
          FROM predictions p JOIN matches m ON m.id=p.match_id
          WHERE p.model_version=:model AND p.created_at<m.start_time AND m.status='FT'
          AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
          AND NOT EXISTS(SELECT 1 FROM prediction_results r WHERE r.prediction_id=p.id)
          ORDER BY m.start_time LIMIT :limit"""),{"model":MODEL,"limit":limit}).mappings().all()
    saved=0
    for r in rows:
        actual="home" if r["home_score"]>r["away_score"] else "away" if r["home_score"]<r["away_score"] else "draw"
        try:
            brier,loss=score_probabilities(r["probabilities"],actual)
        except ValueError:
            continue
        with engine.begin() as c:
            inserted=c.execute(text("""INSERT INTO prediction_results(prediction_id,actual_result,actual_score,brier_score,log_loss)
              SELECT p.id,:actual,CAST(:score AS JSONB),:brier,:loss
              FROM predictions p JOIN matches m ON m.id=p.match_id
              WHERE p.id=:id AND p.created_at<m.start_time AND m.status='FT'
              AND NOT EXISTS(SELECT 1 FROM prediction_results r WHERE r.prediction_id=p.id)
              RETURNING id"""),{"id":r["id"],"actual":actual,"score":json.dumps({"home":float(r["home_score"]),"away":float(r["away_score"])}),"brier":brier,"loss":loss}).scalar()
            saved+=int(inserted is not None)
    return saved
