# SportIQ backend
FastAPI + PostgreSQL + API-Football + WordPress connector.

## Render
This repository contains a Render Blueprint at `render.yaml` defining a Docker web service, background collector worker, and PostgreSQL database. The Blueprint uses paid Starter compute for the web and worker plus a Basic PostgreSQL database. Review current prices in Render before applying.
Set `API_FOOTBALL_KEY` as a secret environment variable on both services. Do not commit API keys or `.env`.
The API initializes the schema on startup. Health check: `/health`.
## Local
Copy `.env.example` to `.env`, set API_FOOTBALL_KEY, then run `docker compose up --build`.
## API
- GET /health
- GET /v1/sports
- GET /v1/leagues?sport=football
- GET /v1/matches?sport=football&date=YYYY-MM-DD
- GET /v1/matches/live?sport=football
- GET /v1/matches/{id}
- GET /v1/predictions/{match_id}
## Scope
The initial provider adapter collects football data through API-Football. The other seven sports are seeded in the database but do not yet have provider adapters. Prediction API reads saved prediction records; no trained prediction model is included.
