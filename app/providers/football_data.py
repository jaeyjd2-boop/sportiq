"""football-data.org v4 adapter returning the collector's normalized fixture shape."""
import httpx
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

    def fixtures(self, day):
        data = self.get("/matches", {"dateFrom": day, "dateTo": day})
        return [self._normalize(x) for x in data.get("matches", [])
                if x.get("id") is not None and
                (x.get("homeTeam") or {}).get("id") is not None and
                (x.get("awayTeam") or {}).get("id") is not None]

    def live(self):
        data = self.get("/matches", {"status": "LIVE"})
        return [self._normalize(x) for x in data.get("matches", [])
                if x.get("id") is not None]

    def close(self):
        self.client.close()
