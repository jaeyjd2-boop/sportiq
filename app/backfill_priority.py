"""Explicit priority-league backfill (no scheduled execution).

Usage: python -m app.backfill_priority --codes BSA --history-days 90 --future-days 14
"""
import argparse
from datetime import datetime, timedelta, timezone
from app.collector import store_fixtures
from app.providers.football_data import FootballDataClient

ALLOWED_CODES = {"BSA", "BL1", "FL1"}
FINISHED = {"FT", "AET", "PEN"}

def backfill_priority(codes=("BSA",), history_days=90, future_days=14, window_days=7):
    codes = tuple(codes)
    if not codes or any(code not in ALLOWED_CODES for code in codes):
        raise ValueError("Allowed competition codes: BSA, BL1, FL1")
    if not 1 <= history_days <= 90 or not 0 <= future_days <= 14 or not 1 <= window_days <= 14:
        raise ValueError("history_days 1..90, future_days 0..14, window_days 1..14")
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=history_days)
    end = today + timedelta(days=future_days)
    client = FootballDataClient()
    results = {}
    try:
        for code in codes:
            saved_history = saved_future = 0
            cursor = start
            while cursor <= end:
                last = min(cursor + timedelta(days=window_days - 1), end)
                items = client.competition_fixtures_range(code, cursor.isoformat(), last.isoformat())
                if last < today:
                    selected = [x for x in items if (x.get("fixture") or {}).get("status", {}).get("short") in FINISHED
                                and (x.get("goals") or {}).get("home") is not None
                                and (x.get("goals") or {}).get("away") is not None]
                    saved_history += store_fixtures(selected)
                else:
                    # Current/future window: retain scheduled and finished fixtures.
                    selected = [x for x in items if (x.get("fixture") or {}).get("date")]
                    saved_future += store_fixtures(selected)
                cursor = last + timedelta(days=1)
            results[code] = {"historical_processed": saved_history, "current_and_future_processed": saved_future}
    finally:
        client.close()
    return results

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", nargs="+", default=["BSA"], choices=sorted(ALLOWED_CODES))
    parser.add_argument("--history-days", type=int, default=90)
    parser.add_argument("--future-days", type=int, default=14)
    parser.add_argument("--window-days", type=int, default=7)
    args = parser.parse_args()
    print(backfill_priority(args.codes, args.history_days, args.future_days, args.window_days))

if __name__ == "__main__":
    main()
