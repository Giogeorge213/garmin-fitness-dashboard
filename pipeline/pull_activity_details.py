#!/usr/bin/env python3
"""
Depth pull: per-activity detail streams (the big dataset + the map routes).

For every activity, get_activity_details returns the per-sample streams inside
the activity -- heart rate, altitude, speed, cadence, power, and GPS lat/lon,
sampled every second or two. Across all activities that's the millions-of-rows
dataset that makes a real Parquet + Athena/Spark lake worthwhile, and the
lat/lon is what draws the route map.

Resumable + rate-limit-safe: one raw JSON per activity under
out/raw/activity_details/. Already-downloaded activities are skipped, so a 429
or a Ctrl-C just means "run it again to resume." Nothing is ever re-pulled.

Steps:
  1. Build/refresh the activity index (all activities in the date range).
  2. For each activity id not yet on disk, pull details (+ optional splits /
     weather) and write the raw file.

Pulls details + lap splits + weather for each activity by default.

Usage:
  python pull_activity_details.py                    # details + splits + weather (all activities)
  python pull_activity_details.py --no-weather        # skip weather
  python pull_activity_details.py --start 2024-01-01  # limit the index window
"""
import argparse
import json
import os

from garmin_client import get_client, safe, polite_sleep

RAW = os.path.join("out", "raw")
DETAILS_DIR = os.path.join(RAW, "activity_details")
INDEX = os.path.join(RAW, "activities_index.json")
# Ask for far more points than any activity has; the API returns what exists.
MAX_POINTS = 1_000_000


def build_index(g, start, end):
    """Fetch the full activity list once and cache it (ids + summary fields)."""
    acts = g.get_activities_by_date(start, end)
    os.makedirs(RAW, exist_ok=True)
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(acts, f)
    print(f"index: {len(acts)} activities {start} -> {end} -> {INDEX}")
    return acts


def load_or_build_index(g, start, end, refresh):
    if os.path.exists(INDEX) and not refresh:
        with open(INDEX, encoding="utf-8") as f:
            acts = json.load(f)
        print(f"index: reused {len(acts)} activities from {INDEX} (use --refresh to rebuild)")
        return acts
    return build_index(g, start, end)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--refresh", action="store_true", help="rebuild the activity index")
    ap.add_argument("--splits", action=argparse.BooleanOptionalAction, default=True,
                    help="pull lap splits (default on; --no-splits to skip)")
    ap.add_argument("--weather", action=argparse.BooleanOptionalAction, default=True,
                    help="pull activity weather (default on; --no-weather to skip)")
    ap.add_argument("--sleep", type=float, default=0.7, help="seconds between calls")
    args = ap.parse_args()

    g = get_client()
    print("Auth OK —", g.get_full_name())
    os.makedirs(DETAILS_DIR, exist_ok=True)

    acts = load_or_build_index(g, args.start, args.end, args.refresh)
    ids = [a.get("activityId") for a in acts if a.get("activityId")]

    def load_existing(path):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def missing(rec):
        """Which requested pieces this file still lacks (enables cheap top-up)."""
        m = []
        if "details" not in rec:
            m.append("details")
        if args.splits and "splits" not in rec:
            m.append("splits")
        if args.weather and "weather" not in rec:
            m.append("weather")
        return m

    todo = [i for i in ids if missing(load_existing(os.path.join(DETAILS_DIR, f"{i}.json")))]
    print(f"{len(ids)} activities, {len(ids) - len(todo)} complete, {len(todo)} to pull/top-up")

    for n, aid in enumerate(todo, 1):
        path = os.path.join(DETAILS_DIR, f"{aid}.json")
        rec = load_existing(path)
        rec.setdefault("activityId", aid)
        m = missing(rec)
        if "details" in m:
            rec["details"] = safe(g.get_activity_details, aid, MAX_POINTS, MAX_POINTS)
            polite_sleep(args.sleep)
        if "splits" in m:
            rec["splits"] = safe(g.get_activity_splits, aid)
            polite_sleep(args.sleep)
        if "weather" in m:
            rec["weather"] = safe(g.get_activity_weather, aid)
            polite_sleep(args.sleep)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        if n % 25 == 0 or n == len(todo):
            print(f"  {n}/{len(todo)}  (last: {aid}, pulled: {m})")

    print(f"done. raw details in {DETAILS_DIR}")


if __name__ == "__main__":
    main()
