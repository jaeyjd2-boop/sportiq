"""football-data.org v4 adapter returning the collector's normalized fixture shape."""
import logging
import httpx

log = logging.getLogger('sportiq-football-data')
from app.config import settings

class FootballDataError(RuntimeError):
    pass

class FootballDataClient:
    def __init__(self):
        if not settings.football_data_token:
            raise FootballDataError("FOOTBALL_DATA_TOKEN is not configured")
        self.client = httpx.Client(
            base_url="https://api.football-data.org/v4",
            headers={"X-Auth-Token": settings.football_data_token},
            timeout=20,
        )

    def get(self, path, params=None):
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def leagues(self):
        data = self.get("/competitions")
        return [
            {"league": {"id": "fd:" + str(x["id"]), "name": x["name"], "logo": x.get("emblem")},
             "country": {"name": (x.get("area") or {}).get("name")},
             "seasons": [{"coverage": {"provider": "football-data.org", "code": x.get("code")}}]}
            for x in data.get("competitions", []) if x.get("id") is not None
        ]

    def _normalize(self, x):
        competition = x.get("competition") or {}
        area = x.get("area") or {}
        home, away = x.get("homeTeam") or {}, x.get("awayTeam") or {}
        score = x.get("score") or {}
        full = score.get("fullTime") or {}
        current = score.get("regularTime") or {}
        status = x.get("status") or "SCHEDULED"
        short = {
            "SCHEDULED": "NS", "TIMED": "NS", "IN_PLAY": "LIVE",
            "LIVE": "LIVE", "PAUSED": "HT", "FINISHED": "FT",
            "POSTPONED": "PST", "SUSPENDED": "SUSP", "CANCELLED": "CANC",
            "AWARDED": "AWD",
        }.get(status, status[:40])
        goals = full if status in ("FINISHED", "AWARDED") else current
        if not any(v is not None for v in goals.values()):
            goals = full
        def team(t):
            identifier = t.get("id")
            return {"id": "fd:" + str(identifier) if identifier is not None else None,
                    "name": t.get("name") or t.get("shortName") or "Unknown",
                    "logo": t.get("crest")}
        return {
            "fixture": {"id": "fd:" + str(x["id"]), "date": x.get("utcDate"),
                        "status": {"short": short, "long": status},
                        "venue": {"name": x.get("venue")}, "referee": None},
            "league": {"id": "fd:" + str(competition.get("id")),
                       "name": competition.get("name") or "Unknown",
                       "logo": competition.get("emblem")},
            "country": {"name": area.get("name")},
            "teams": {"home": team(home), "away": team(away)},
            "goals": {"home": goals.get("home"), "away": goals.get("away")},
            "source": "football-data.org",
        }

    def fixtures_range(self, start_day, end_day):
        # Global /matches can return an empty set for restricted subscriptions.
        # Fetch the subscribed competitions individually, using a bounded list.
        competitions = ("PL", "PD", "BL1", "SA", "FL1", "CL")
        results = []
        seen = set()
        for code in competitions:
            try:
                data = self.get(f"/competitions/{code}/matches",
                                {"dateFrom": start_day, "dateTo": end_day})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 404):
                    log.warning("competition %s unavailable: HTTP %s", code, exc.response.status_code)
                    continue
                raise
            matches = data.get("matches", [])
            missing = sum(1 for x in matches if
                          (x.get("homeTeam") or {}).get("id") is None or
                          (x.get("awayTeam") or {}).get("id") is None)
            log.info("competition %s raw=%d missing_team_ids=%d", code, len(matches), missing)
            for x in matches:
                if x.get("id") is None or x["id"] in seen:
                    continue
                if (x.get("homeTeam") or {}).get("id") is None or (x.get("awayTeam") or {}).get("id") is None:
                    continue
                seen.add(x["id"])
                results.append(self._normalize(x))
        log.info("fixture range %s..%s normalized=%d", start_day, end_day, len(results))
        return results

    def fixtures(self, day):
        return self.fixtures_range(day, day)

    def live(self):
        data = self.get("/matches", {"status": "LIVE"})
        return [self._normalize(x) for x in data.get("matches", [])
                if x.get("id") is not None]

    def close(self):
        self.client.close()
