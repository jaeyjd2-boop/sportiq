"""Manual historical football backfill: python -m app.backfill_history --days 30"""
import argparse
import logging
from app.collector import collect_historical

def main():
    parser = argparse.ArgumentParser(description="Import completed historical football matches")
    parser.add_argument("--days", type=int, default=30, help="1-90 previous days")
    parser.add_argument("--window-days", type=int, default=7, help="1-14 days per API scan")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    count = collect_historical(args.days, args.window_days)
    print(f"Historical matches processed: {count}")

if __name__ == "__main__":
    main()
