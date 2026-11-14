"""Opt-in historical fixture backfill using SportIQ's existing football-data.org adapter.

Preview by default. Use --write to persist; never scheduled automatically.
Run: python -m app.backfill --days 30 --window-days 7 --competition PL
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from app.collector import store_fixtures
from app.providers.football_data import FootballDataClient

log = logging.getLogger("sportiq-backfill")


def positive_int(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Historical football fixture backfill (opt-in)")
    parser.add_argument("--days", type=positive_int, default=30)
    parser.add_argument("--window-days", type=positive_int, default=7)
    parser.add_argument("--competition", default="PL", help="football-data.org competition code, e.g. PL or BSA")
    parser.add_argument("--write", action="store_true", help="Persist to PostgreSQL (otherwise preview only)")
    parser.add_argument("--sleep", type=float, default=8.0, help="Seconds between date windows")
    args = parser.parse_args(argv)
    if args.days > 365 or args.window_days > 30:
        parser.error("--days must be <=365 and --window-days <=30")
    if args.sleep < 0 or args.sleep > 300:
        parser.error("--sleep must be between 0 and 300")
    if not args.competition.replace("-", "").isalnum():
        parser.error("invalid competition code")
    return args


def run_backfill(days, window_days, competition, write=False, sleep_seconds=8.0):
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    total_fetched = total_saved = failed = 0
    client = FootballDataClient()
    try:
        current = start
        while current <= today:
            end = min(current + timedelta(days=window_days - 1), today)
            log.info("window=%s..%s competition=%s write=%s", current, end, competition, write)
            try:
                records = client.competition_fixtures_range(
                    competition, current.isoformat(), end.isoformat()
                )
                total_fetched += len(records)
                if write and records:
                    # Reuse existing transaction-safe league/team/match upsert,
                    # including provider-prefixed IDs and normalized match statuses.
                    saved = store_fixtures(records)
                    total_saved += saved
                log.info("window complete fetched=%d saved=%d", len(records), len(records) if write else 0)
            except Exception:
                failed += 1
                log.exception("window failed %s..%s; continue to next window", current, end)
            current = end + timedelta(days=1)
            if current <= today and sleep_seconds:
                time.sleep(sleep_seconds)
    finally:
        client.close()
    log.info("backfill finished fetched=%d saved=%d failed_windows=%d", total_fetched, total_saved, failed)
    return {"fetched": total_fetched, "saved": total_saved, "failed_windows": failed}


def main(argv=None):
    args = parse_args(argv)
    result = run_backfill(args.days, args.window_days, args.competition, args.write, args.sleep)
    return 1 if result["failed_windows"] else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(main())
