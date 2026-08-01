#!/usr/bin/env python3
"""
Breadth pull: every useful daily-wellness endpoint, one row's worth per day.

Pulls 21 daily metrics (steps, floors, stress, body battery, respiration,
SpO2, intensity minutes, all-day heart rate, sleep, HRV, resting HR, training
status/readiness, VO2 max, endurance & hill scores, body composition/weight,
daily summary rollups) for each date in the range.

Resumable + rate-limit-safe: one raw JSON per date under out/raw/wellness/.
A date whose file already exists is skipped, so a 429 or Ctrl-C just resumes.

Note on coverage: many of these metrics only exist on newer devices (body
battery, HRV, training readiness are relatively recent), so older dates will
have {_error}/empty values for those endpoints -- that's expected, not a bug.
The transform later just ignores the empties.

Usage:
  python pull_wellness.py                          # default window (2024-01-01 -> today)
  python pull_wellness.py --start 2020-01-01        # everything (slow: ~hours, resumable)
"""
import argparse
import json
import os
from datetime import date, timedelta

from garmin_client import get_client, safe, polite_sleep

RAW = os.path.join("out", "raw", "wellness")

# name in the output file -> Garmin method name. All are called with a single
# date; the (startdate, enddate=None) ones accept a lone date fine.
ENDPOINTS = {
    "steps": "get_steps_data",
    "floors": "get_floors",
    "stress": "get_all_day_stress",
    "body_battery": "get_body_battery",
    "respiration": "get_respiration_data",
    "spo2": "get_spo2_data",
    "intensity_minutes": "get_intensity_minutes_data",
    "hydration": "get_hydration_data",
    "heart_rates": "get_heart_rates",
    "user_summary": "get_user_summary",
    "stats_and_body": "get_stats_and_body",
    "training_status": "get_training_status",
    "training_readiness": "get_training_readiness",
    "max_metrics": "get_max_metrics",
    "sleep": "get_sleep_data",
    "hrv": "get_hrv_data",
    "rhr": "get_rhr_day",
    "endurance_score": "get_endurance_score",
    "hill_score": "get_hill_score",
    "body_composition": "get_body_composition",
    "weigh_ins": "get_daily_weigh_ins",
}


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--sleep", type=float, default=0.7, help="seconds between calls")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    g = get_client()
    print("Auth OK —", g.get_full_name())
    os.makedirs(RAW, exist_ok=True)

    all_days = list(daterange(start, end))
    todo = [d for d in all_days if not os.path.exists(os.path.join(RAW, f"{d.isoformat()}.json"))]
    print(f"{len(all_days)} days in range, {len(all_days) - len(todo)} already pulled, "
          f"{len(todo)} to go (~{len(todo) * len(ENDPOINTS) * args.sleep / 60:.0f} min)")

    methods = {k: getattr(g, v) for k, v in ENDPOINTS.items()}
    for n, d in enumerate(todo, 1):
        ds = d.isoformat()
        rec = {"date": ds}
        for name, fn in methods.items():
            rec[name] = safe(fn, ds)
            polite_sleep(args.sleep)
        with open(os.path.join(RAW, f"{ds}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)
        if n % 10 == 0 or n == len(todo):
            print(f"  {n}/{len(todo)}  ({ds})")

    print(f"done. raw wellness in {RAW}")


if __name__ == "__main__":
    main()
