"""Read-side staleness flag for stored match statuses.

The stored status is only ever what the provider last reported. When a match
is long past kickoff but still not final (provider lag, a missed refresh, or
legacy rows from a provider that is no longer configured), the API adds
``status_stale: true`` instead of guessing a result, and the live list leaves
such rows out.
"""
from datetime import datetime, timedelta, timezone

FINAL_OR_CLOSED = {"FT", "AET", "PEN", "AWD", "CANC", "PST", "ABD", "SUSP"}
STALE_AFTER = timedelta(hours=4)


def _to_utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_status_stale(start_time, status, now=None):
    start = _to_utc(start_time)
    if start is None or (status or "").upper() in FINAL_OR_CLOSED:
        return False
    now = now or datetime.now(timezone.utc)
    return now - start > STALE_AFTER


def live_cutoff(now=None):
    """Kickoffs before this instant can no longer be genuinely live."""
    return (now or datetime.now(timezone.utc)) - STALE_AFTER


def with_stale_flag(row, now=None):
    item = dict(row)
    item["status_stale"] = is_status_stale(item.get("start_time"), item.get("status"), now)
    return item


# Provider of a stored match, from its provider id prefix (exposed as "source").
SOURCE_SQL = (
    "CASE WHEN m.provider_match_id LIKE 'espn:%' THEN 'ESPN' "
    "WHEN m.provider_match_id LIKE 'as:%' THEN 'API-SPORTS' "
    "WHEN m.provider_match_id LIKE 'kr:%' THEN 'billnojay' "
    "WHEN m.provider_match_id LIKE 'fd:%' THEN 'football-data.org' "
    "ELSE 'API-Football (legacy)' END"
)
