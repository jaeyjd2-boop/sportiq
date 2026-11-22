from datetime import date
import time
from collections import defaultdict, deque
from fastapi import Request
from fastapi.responses import JSONResponse
from app.sportiq_autonomous_sec import router as autonomous_router
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.config import settings
from app.db import engine
from app.details import router as details_router
from app.match_status import live_cutoff, with_stale_flag
from app.diagnostics import router as diagnostics_router
from app.security.wp_bridge_router import router as wp_bridge_router
from app.scheduler import web_backfill_lifespan
from app.sportiq_lounge_router import router as lounge_router
from app.sportiq_auto_analyst import router as auto_analyst_router
from app.sportiq_issues_ranking import router as issues_ranking_router
from app.sportiq_pro_lab import router as pro_lab_router
from app.sportiq_datacenter import router as datacenter_router
app=FastAPI(title="SportIQ API",version="1.0.0",lifespan=web_backfill_lifespan)
app.include_router(details_router)
app.include_router(diagnostics_router)
app.include_router(wp_bridge_router)
app.include_router(auto_analyst_router)
app.include_router(lounge_router)
app.include_router(issues_ranking_router)
app.include_router(pro_lab_router)
app.include_router(autonomous_router)
app.include_router(datacenter_router)
app.add_middleware(CORSMiddleware,allow_origins=list(dict.fromkeys([*settings.allowed_origins,"https://billnojay.com","https://www.billnojay.com","http://billnojay.com","http://www.billnojay.com"])),allow_credentials=False,allow_methods=["GET","POST","PUT","DELETE","OPTIONS"],allow_headers=["*"])
_RATE_LIMIT_PATHS = {
    "/api/pro-lab/tactics",
    "/api/shorts/submit",
    "/api/auto-analyst/run-now",
    "/api/partner-inquiries",
    "/api/posts",
}
_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LIMIT_WINDOW_SEC = 60.0
_RATE_LIMIT_MAX_REQ = 8


@app.middleware("http")
async def sportiq_security_and_ratelimit_middleware(request: Request, call_next):
    if request.method == "POST" and request.url.path in _RATE_LIMIT_PATHS:
        fwd = request.headers.get("x-forwarded-for", "")
        client_ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
        now_ts = time.monotonic()
        bucket = _RATE_LIMIT_BUCKETS[f"{client_ip}:{request.url.path}"]
        while bucket and (now_ts - bucket[0]) > _RATE_LIMIT_WINDOW_SEC:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT_MAX_REQ:
            resp_429 = JSONResponse(
                status_code=429,
                content={"detail": "요청이 너무 빠릅니다. 잠시 후 다시 시도해 주세요."},
            )
            resp_429.headers["X-Content-Type-Options"] = "nosniff"
            resp_429.headers["X-Frame-Options"] = "SAMEORIGIN"
            resp_429.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            resp_429.headers["X-XSS-Protection"] = "1; mode=block"
            resp_429.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
            return resp_429
        bucket.append(now_ts)

    response = await call_next(request)
    # JSON is UTF-8, but some clients (e.g. Windows PowerShell 5.1) fall back
    # to Latin-1 without an explicit charset and show "MÃ¼nchen".
    if response.headers.get("content-type") == "application/json":
        response.headers["content-type"] = "application/json; charset=utf-8"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


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
        "items": [with_stale_flag(row) for row in rows],
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
        rows=c.execute(text("SELECT m.id,l.name AS league,l.logo_url AS league_logo_url,ht.name AS home_team,ht.logo_url AS home_team_logo_url,at.name AS away_team,at.logo_url AS away_team_logo_url,m.start_time,m.status,m.status_detail,m.home_score,m.away_score,m.updated_at FROM matches m JOIN sports s ON s.id=m.sport_id LEFT JOIN leagues l ON l.id=m.league_id LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE s.code=:sport AND m.status IN ('1H','HT','2H','ET','P','LIVE','Q1','Q2','Q3','Q4','IN') AND m.start_time >= :cutoff ORDER BY m.start_time"),{"sport":sport,"cutoff":live_cutoff()}).mappings().all()
        # Rows still marked live 4h+ after kickoff are stale provider data, not live games.
        return {"items":[with_stale_flag(r) for r in rows],"total":len(rows)}
@app.get("/v1/matches/{match_id}")
def match_detail(match_id:int):
    with engine.connect() as c:
        row=c.execute(text("SELECT m.id,s.code AS sport,l.name AS league,l.logo_url AS league_logo_url,ht.name AS home_team,ht.logo_url AS home_team_logo_url,at.name AS away_team,at.logo_url AS away_team_logo_url,m.start_time,m.status,m.status_detail,m.home_score,m.away_score,m.venue,m.referee,m.updated_at FROM matches m JOIN sports s ON s.id=m.sport_id LEFT JOIN leagues l ON l.id=m.league_id LEFT JOIN teams ht ON ht.id=m.home_team_id LEFT JOIN teams at ON at.id=m.away_team_id WHERE m.id=:id"),{"id":match_id}).mappings().first()
        if not row: raise HTTPException(404,"match not found")
        return with_stale_flag(row)
@app.get("/v1/predictions/{match_id}")
def prediction(match_id:int):
    with engine.connect() as c:
        row=c.execute(text("SELECT id,match_id,model_version,probabilities,predicted_result,predicted_score,confidence,created_at FROM predictions WHERE match_id=:id ORDER BY created_at DESC LIMIT 1"),{"id":match_id}).mappings().first()
        if not row: raise HTTPException(404,"prediction not found")
        return dict(row)


@app.get("/v1/operations/collector-status")
def collector_status():
    """Public sanitized summary. Historical failures do not mask later recovery."""
    from datetime import datetime, timezone
    with engine.connect() as c:
        exists = c.execute(text("SELECT to_regclass('public.collector_runs') IS NOT NULL")).scalar()
        if not exists:
            return {"status": "not_initialized", "fixtures_stale": True,
                    "failed_last_24h": 0, "unresolved_failures": 0,
                    "last_fixtures_success": None, "jobs": []}
        rows = c.execute(text("""
            SELECT DISTINCT ON (job_name) job_name, status, result_count, finished_at
            FROM collector_runs ORDER BY job_name, finished_at DESC, id DESC
        """)).mappings().all()
        failures = c.execute(text("""
            SELECT COUNT(*) FROM collector_runs
            WHERE status='failed' AND finished_at >= NOW() - INTERVAL '24 hours'
        """)).scalar_one()
        latest = c.execute(text("""
            SELECT MAX(finished_at) FROM collector_runs
            WHERE job_name='fixtures' AND status='success'
        """)).scalar()
    stale = latest is None or (datetime.now(timezone.utc) - latest).total_seconds() > 15 * 3600
    unresolved = sum(row["status"] in ("failed", "skipped") for row in rows)
    return {
        "status": "degraded" if stale or unresolved else "ok",
        "fixtures_stale": stale,
        "last_fixtures_success": latest.isoformat() if latest else None,
        "failed_last_24h": failures,
        "unresolved_failures": unresolved,
        "jobs": [
            {"job": row["job_name"], "status": row["status"],
             "result_count": row["result_count"],
             "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None}
            for row in rows
        ],
    }


@app.get("/v1/operations/dashboard", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
def operations_dashboard():
    """Read-only operational dashboard. No credentials or error payloads."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SportIQ 수집 모니터링</title>
<style>body{font-family:system-ui,sans-serif;max-width:850px;margin:3rem auto;padding:0 1rem;background:#f8fafc;color:#0f172a}
h1{font-size:1.6rem}.panel{background:white;border:1px solid #e2e8f0;border-radius:12px;padding:1.2rem;margin:1rem 0}
.ok{color:#15803d}.bad{color:#b91c1c}.muted{color:#64748b}table{width:100%;border-collapse:collapse}
td,th{padding:.7rem;text-align:left;border-bottom:1px solid #e2e8f0}button{padding:.6rem 1rem;cursor:pointer}</style>
</head><body><h1>SportIQ 수집 모니터링</h1><p class="muted">자동 갱신: 60초</p>
<div class="panel"><h2 id="overall">확인 중…</h2><p id="details"></p></div>
<div class="panel"><h2>최근 작업</h2><table><thead><tr><th>작업</th><th>상태</th><th>처리 건수</th><th>완료 시각 (UTC)</th></tr></thead><tbody id="jobs"></tbody></table></div>
<button onclick="refresh()">새로고침</button>
<script>
async function refresh(){
 const overall=document.getElementById('overall'), details=document.getElementById('details'), body=document.getElementById('jobs');
 try {
  const r=await fetch('/v1/operations/collector-status',{cache:'no-store'});
  if(!r.ok) throw Error('HTTP '+r.status);
  const data=await r.json(); overall.textContent=data.status==='ok'?'정상 운영':data.status==='degraded'?'점검 필요':'초기화 대기';
  overall.className=data.status==='ok'?'ok':'bad';
  details.textContent='미복구 실패: '+data.unresolved_failures+'건 | 최근 24시간 실패 기록: '+data.failed_last_24h+'건 | 경기 일정 갱신 지연: '+(data.fixtures_stale?'예':'아니요')+' | 마지막 일정 수집 성공: '+(data.last_fixtures_success??'없음');
  body.replaceChildren();
  for(const j of data.jobs){
   const tr=document.createElement('tr');
   for(const v of [j.job,j.status,j.result_count??'-',j.finished_at??'-']){
    const td=document.createElement('td');td.textContent=String(v);tr.appendChild(td);
   }
   body.appendChild(tr);
  }
 }catch(e){overall.textContent='상태 조회 실패';overall.className='bad';details.textContent=String(e);}
}
refresh();setInterval(refresh,60000);
</script></body></html>""")


@app.get("/v1/forecast-performance")
def forecast_performance(league_provider_id: str = "fd:2013"):
    """Only pre-kickoff snapshots scored against final results."""
    with engine.connect() as c:
        row=c.execute(text("""
            SELECT COUNT(r.id) AS evaluated,
                   ROUND(AVG(r.brier_score)::numeric, 4) AS mean_brier,
                   ROUND(AVG(r.log_loss)::numeric, 4) AS mean_log_loss,
                   COUNT(r.id) FILTER(WHERE p.predicted_result=r.actual_result) AS correct
            FROM predictions p JOIN matches m ON m.id=p.match_id
            JOIN leagues l ON l.id=m.league_id
            LEFT JOIN prediction_results r ON r.prediction_id=p.id
            WHERE l.provider_league_id=:league AND p.model_version='poisson-form-v0.1'
              AND p.created_at<m.start_time
        """),{"league":league_provider_id}).mappings().one()
        pending=c.execute(text("""SELECT COUNT(*) FROM predictions p JOIN matches m ON m.id=p.match_id
          JOIN leagues l ON l.id=m.league_id WHERE l.provider_league_id=:league
          AND p.model_version='poisson-form-v0.1' AND p.created_at<m.start_time
          AND NOT EXISTS(SELECT 1 FROM prediction_results r WHERE r.prediction_id=p.id)"""),{"league":league_provider_id}).scalar_one()
    return {"league_provider_id":league_provider_id,"model_version":"poisson-form-v0.1",
            "evaluated":row["evaluated"],"pending":pending,"correct":row["correct"],
            "accuracy":round(row["correct"]/row["evaluated"],4) if row["evaluated"] else None,
            "mean_brier":float(row["mean_brier"]) if row["mean_brier"] is not None else None,
            "mean_log_loss":float(row["mean_log_loss"]) if row["mean_log_loss"] is not None else None,
            "note":"Only pre-kickoff stored predictions; no accuracy claims before sufficient samples."}


@app.get("/v1/forecast-performance/leagues")
def forecast_performance_leagues():
    """Comparable pre-kickoff performance by league; no ranking for tiny samples."""
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT l.provider_league_id, l.name AS league_name,
                   COUNT(r.id) AS evaluated,
                   COUNT(*) FILTER (WHERE r.id IS NULL) AS pending,
                   COUNT(r.id) FILTER (WHERE p.predicted_result = r.actual_result) AS correct,
                   ROUND(AVG(r.brier_score)::numeric, 4) AS mean_brier,
                   ROUND(AVG(r.log_loss)::numeric, 4) AS mean_log_loss
            FROM predictions p
            JOIN matches m ON m.id=p.match_id
            JOIN leagues l ON l.id=m.league_id
            LEFT JOIN prediction_results r ON r.prediction_id=p.id
            WHERE p.model_version='poisson-form-v0.1'
              AND p.created_at < m.start_time
            GROUP BY l.provider_league_id, l.name
            ORDER BY evaluated DESC, l.name
        """)).mappings().all()
    return {"model_version":"poisson-form-v0.1","leagues":[{
        "league_provider_id":r["provider_league_id"],"league_name":r["league_name"],
        "evaluated":r["evaluated"],"pending":r["pending"],"correct":r["correct"],
        "accuracy":round(r["correct"]/r["evaluated"],4) if r["evaluated"] else None,
        "mean_brier":float(r["mean_brier"]) if r["mean_brier"] is not None else None,
        "mean_log_loss":float(r["mean_log_loss"]) if r["mean_log_loss"] is not None else None,
        "sample_sufficient":r["evaluated"]>=30,
    } for r in rows]}


@app.get("/v1/forecast-performance/trend")
def forecast_performance_trend(league_provider_id: str = "fd:2013"):
    """Daily cumulative scoring trend for pre-kickoff predictions only."""
    with engine.connect() as c:
        rows=c.execute(text("""
            SELECT r.evaluated_at::date AS evaluation_date,
                   COUNT(*) AS evaluated,
                   COUNT(*) FILTER (WHERE p.predicted_result=r.actual_result) AS correct,
                   SUM(r.brier_score) AS brier_total,
                   SUM(r.log_loss) AS log_loss_total
            FROM prediction_results r
            JOIN predictions p ON p.id=r.prediction_id
            JOIN matches m ON m.id=p.match_id
            JOIN leagues l ON l.id=m.league_id
            WHERE l.provider_league_id=:league
              AND p.model_version='poisson-form-v0.1'
              AND p.created_at < m.start_time
            GROUP BY r.evaluated_at::date
            ORDER BY evaluation_date
        """),{"league":league_provider_id}).mappings().all()
    total=correct=0
    brier=loss=0.0
    points=[]
    for r in rows:
        total+=r["evaluated"]
        correct+=r["correct"]
        brier+=float(r["brier_total"])
        loss+=float(r["log_loss_total"])
        points.append({"date":r["evaluation_date"].isoformat(),
                       "evaluated":total,"accuracy":round(correct/total,4),
                       "mean_brier":round(brier/total,4),
                       "mean_log_loss":round(loss/total,4),
                       "sample_sufficient":total>=30})
    return {"league_provider_id":league_provider_id,
            "model_version":"poisson-form-v0.1","points":points,
            "note":"Cumulative metrics by evaluation date; 30 samples is a display threshold, not statistical proof."}
