# SportIQ Historical Fixture Backfill

Optional, **not scheduled**, command-line historical collection using the existing `football-data.org` integration, not API-Football. No new dependency, ORM model, or schema migration is required.

## Preview (does not write)
```sh
python -m app.backfill --days 30 --window-days 7 --competition PL
```

## Persist to PostgreSQL (explicit opt-in)
```sh
python -m app.backfill --days 30 --window-days 7 --competition PL --write
```

Requires `FOOTBALL_DATA_TOKEN` and, for `--write`, `DATABASE_URL`. `PL` is the football-data.org Premier League competition code, **not** API-Football league ID 39. Other codes include `BSA` if the connected account has access. Provider access/plan may restrict historical matches.

The command uses UTC calendar dates, inclusive, at most 365 days per run and 30 days per window. It reuses `FootballDataClient.competition_fixtures_range` and `store_fixtures`, preserving SportIQ's existing league/team upsert, provider-prefixed IDs, match status mapping and conflict keys. A failed window is logged and the process exits nonzero at the end; earlier successful windows may have committed. Rerunning is safe with the existing upsert but may update already stored match records. Existing periodic collector and prediction evaluation schedules remain unchanged.

**Caution:** preview still calls the provider API and consumes quota. The existing provider adapter handles 429 retries. No automatic background backfill is enabled. Source was committed without executing a test or live backfill.
