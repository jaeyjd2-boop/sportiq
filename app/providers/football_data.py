"""football-data.org v4 adapter returning the collector's normalized fixture shape."""
import logging
import threading
import time
import httpx

log = logging.getLogger('sportiq-football-data')
from app.config import settings

class FootballDataError(RuntimeError):
    pass

# The free tier allows 10 requests/minute per token. Worker jobs run in
# parallel threads and each creates its own client, so the spacing must be
# shared process-wide; a per-instance timestamp let concurrent jobs exceed the
# limit and silently drop competitions after repeated HTTP 429 responses.
MIN_REQUEST_INTERVAL = 8.0
_THROTTLE_LOCK = threading.Lock()
_LAST_REQUEST = [0.0]


def _throttle():
    with _THROTTLE_LOCK:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST[0] = time.monotonic()

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
        for attempt in range(3):
            _throttle()
            response = self.client.get(path, params=params)
            if response.status_code == 429 and attempt < 2:
                try:
                    delay = max(60.0, float(response.headers.get("Retry-After", "60")))
                except ValueError:
                    delay = 60.0
                log.warning("API rate limit path=%s retry_in=%.0fs", path, delay)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        raise FootballDataError("API retries exhausted")

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
        catalog = self.get("/competitions").get("competitions", [])
        codes = list(dict.fromkeys(str(x["code"]) for x in catalog if x.get("code")))
        log.info("fixture scan competitions=%d codes=%s", len(codes), ",".join(codes))
        results, seen = [], set()
        succeeded = failed = skipped = missing_total = 0
        retry_codes = []
        # Second pass retries competitions that failed transiently (e.g. 429)
        # so one rate-limited request no longer leaves a league un-updated.
        for attempt, pending in enumerate((codes, retry_codes)):
            for code in list(pending):
                try:
                    data = self.get(f"/competitions/{code}/matches",
                                    {"dateFrom": start_day, "dateTo": end_day})
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (403, 404):
                        skipped += 1
                        log.warning("competition %s inaccessible HTTP %s", code, exc.response.status_code)
                        continue
                    if attempt == 0:
                        retry_codes.append(code)
                        log.warning("competition %s HTTP %s; will retry", code, exc.response.status_code)
                        continue
                    failed += 1
                    log.exception("competition %s HTTP failure", code)
                    continue
                except (httpx.RequestError, FootballDataError) as exc:
                    if attempt == 0:
                        retry_codes.append(code)
                        log.warning("competition %s request failed: %s; will retry", code, exc)
                        continue
                    failed += 1
                    log.warning("competition %s request failed: %s", code, exc)
                    continue
                succeeded += 1
                matches = data.get("matches") or []
                missing = 0
                for item in matches:
                    if item.get("id") is None or item["id"] in seen:
                        continue
                    if (item.get("homeTeam") or {}).get("id") is None or (item.get("awayTeam") or {}).get("id") is None:
                        missing += 1
                        continue
                    seen.add(item["id"])
                    results.append(self._normalize(item))
                missing_total += missing
                log.info("competition %s raw=%d missing_team_ids=%d", code, len(matches), missing)
        log.info("fixture scan complete range=%s..%s competitions=%d succeeded=%d skipped=%d failed=%d missing_team_ids=%d normalized=%d",
                 start_day, end_day, len(codes), succeeded, skipped, failed, missing_total, len(results))
        self.last_scan = {"competitions": len(codes), "succeeded": succeeded,
                          "skipped": skipped, "failed": failed}
        if not succeeded and codes:
            raise FootballDataError("No competition fixture requests succeeded")
        return results

    def competition_fixtures_range(self, competition_code, start_day, end_day):
        """Fetch one authorized competition, avoiding full-catalog scans."""
        if not competition_code or not competition_code.replace("-", "").isalnum():
            raise ValueError("Invalid competition code")
        data = self.get(f"/competitions/{competition_code}/matches",
                        {"dateFrom": start_day, "dateTo": end_day})
        return [self._normalize(item) for item in data.get("matches", [])
                if item.get("id") is not None
                and (item.get("homeTeam") or {}).get("id") is not None
                and (item.get("awayTeam") or {}).get("id") is not None]

    def head_to_head(self, match_id, limit=10):
        """Previous meetings for a match's two teams (football-data /head2head)."""
        raw = str(match_id).removeprefix("fd:")
        if not raw.isdigit():
            raise ValueError("Invalid match id")
        if not isinstance(limit, int) or not 1 <= limit <= 30:
            raise ValueError("limit must be 1..30")
        data = self.get(f"/matches/{raw}/head2head", {"limit": limit})
        return [self._normalize(item) for item in data.get("matches", [])
                if item.get("id") is not None
                and (item.get("homeTeam") or {}).get("id") is not None
                and (item.get("awayTeam") or {}).get("id") is not None]

    def fixtures(self, day):
        return self.fixtures_range(day, day)

    def live(self):
        data = self.get("/matches", {"status": "LIVE"})
        return [self._normalize(x) for x in data.get("matches", [])
                if x.get("id") is not None]

    def close(self):
        self.client.close()
