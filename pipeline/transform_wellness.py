#!/usr/bin/env python3
"""
Flatten the raw daily-wellness files into one clean row per day.

Reads out/raw/wellness/<date>.json (each holding ~21 endpoint responses) and
writes out/curated/wellness_daily.csv -- one row per date, one column per
metric. Field paths were verified against the real pulled files:

  user_summary  -> steps, distance, calories, floors, intensity mins,
                   min/max/resting HR, avg stress  (richest single source)
  stress        -> max stress
  body_battery  -> charged / drained
  sleep         -> total + stage hours
  hrv           -> last-night avg + status
  max_metrics   -> VO2 max            (list; empty on days without a reading)
  training_readiness -> score + level (list; empty some days)
  weigh_ins     -> latest weight (kg) when logged
  hydration     -> intake (mL) when logged

Endpoints/days with no data (older device, no wear, {_error}) just yield NULLs
for those columns -- expected, not an error.
"""
import csv
import glob
import json
import os

RAW = os.path.join("out", "raw", "wellness")
OUT = os.path.join("out", "curated", "wellness_daily.csv")


def ok(x):
    """Real data present (not None, not an {_error} marker, not empty)."""
    if x is None:
        return False
    if isinstance(x, dict) and "_error" in x:
        return False
    return True


def g(obj, *path, default=None):
    """Safe nested get through dicts/lists; returns default on any miss."""
    cur = obj
    for p in path:
        if isinstance(p, int):
            if isinstance(cur, list) and -len(cur) <= p < len(cur):
                cur = cur[p]
            else:
                return default
        elif isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur if cur is not None else default


def hrs(seconds):
    return round(seconds / 3600.0, 2) if isinstance(seconds, (int, float)) else None


def row_for(day):
    us = day.get("user_summary") if ok(day.get("user_summary")) else {}
    stress = day.get("stress") if ok(day.get("stress")) else {}
    bb = g(day, "body_battery", 0, default={}) if ok(day.get("body_battery")) else {}
    sl = g(day, "sleep", "dailySleepDTO", default={})
    hv = g(day, "hrv", "hrvSummary", default={})
    dist_m = us.get("totalDistanceMeters")

    return {
        "date": day.get("date"),
        # activity / movement
        "steps": us.get("totalSteps"),
        "distance_km": round(dist_m / 1000.0, 2) if isinstance(dist_m, (int, float)) else None,
        "floors": round(us["floorsAscended"], 1) if isinstance(us.get("floorsAscended"), (int, float)) else None,
        "moderate_min": us.get("moderateIntensityMinutes"),
        "vigorous_min": us.get("vigorousIntensityMinutes"),
        "total_kcal": us.get("totalKilocalories"),
        "active_kcal": us.get("activeKilocalories"),
        # heart / stress
        "resting_hr": us.get("restingHeartRate"),
        "min_hr": us.get("minHeartRate"),
        "max_hr": us.get("maxHeartRate"),
        "avg_stress": us.get("averageStressLevel"),
        "max_stress": stress.get("maxStressLevel"),
        "body_battery_charged": bb.get("charged"),
        "body_battery_drained": bb.get("drained"),
        # sleep
        "sleep_hours": hrs(sl.get("sleepTimeSeconds")),
        "deep_sleep_hours": hrs(sl.get("deepSleepSeconds")),
        "light_sleep_hours": hrs(sl.get("lightSleepSeconds")),
        "rem_sleep_hours": hrs(sl.get("remSleepSeconds")),
        "awake_hours": hrs(sl.get("awakeSleepSeconds")),
        # recovery / fitness
        "hrv": hv.get("lastNightAvg"),
        "hrv_status": hv.get("status"),
        "vo2max": g(day, "max_metrics", 0, "generic", "vo2MaxValue"),
        "readiness_score": g(day, "training_readiness", 0, "score"),
        "readiness_level": g(day, "training_readiness", 0, "level"),
        # body
        "weight_kg": _latest_weight(day),
        "hydration_ml": g(day, "hydration", "valueInML"),
    }


def _latest_weight(day):
    """Most recent weigh-in for the day, grams -> kg, if any."""
    lst = g(day, "weigh_ins", "dateWeightList", default=[]) or []
    if lst and isinstance(lst, list):
        w = lst[-1].get("weight")
        if isinstance(w, (int, float)):
            return round(w / 1000.0, 1)  # Garmin stores grams
    return None


def main():
    files = sorted(glob.glob(os.path.join(RAW, "*.json")))
    rows = []
    for f in files:
        try:
            day = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        rows.append(row_for(day))

    if not rows:
        print("no wellness files yet")
        return

    cols = list(rows[0].keys())
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"wrote {len(rows)} days -> {OUT}")

    # Typed parquet for the Athena table garmin.wellness (deterministic schema).
    try:
        import pandas as pd
        df = pd.DataFrame(rows, columns=cols)
        int_cols = ["steps", "moderate_min", "vigorous_min", "resting_hr", "min_hr", "max_hr",
                    "avg_stress", "max_stress", "body_battery_charged", "body_battery_drained",
                    "hrv", "readiness_score", "hydration_ml"]
        float_cols = ["distance_km", "floors", "total_kcal", "active_kcal", "sleep_hours",
                      "deep_sleep_hours", "light_sleep_hours", "rem_sleep_hours", "awake_hours",
                      "vo2max", "weight_kg"]
        for c in int_cols:
            if c in df:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        for c in float_cols:
            if c in df:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        for c in ["date", "hrv_status", "readiness_level"]:
            if c in df:
                df[c] = df[c].astype("object")
        pq = OUT.replace(".csv", ".parquet")
        df.to_parquet(pq, index=False)
        print(f"wrote {len(rows)} -> {pq}")
    except Exception as e:  # parquet is a bonus; the CSV is what render.py needs
        print(f"(skipped parquet: {e})")

    for c in cols[1:]:
        n = sum(1 for r in rows if r.get(c) not in (None, ""))
        print(f"  {c}: {n}/{len(rows)}")


if __name__ == "__main__":
    main()
