"""
Garmin activities ingest (2021 -> now).
Reuses cached token (no login). Writes raw JSON to out/garmin_activities_raw.json.
"""
import json
import os
from garminconnect import Garmin

TOKEN_DIR = os.path.expanduser("~/.garmin_tokens")
START_DATE = "2020-01-01"
END_DATE = "2026-12-31"   # safely covers "now"

def get_client():
    garmin = Garmin()
    garmin.login(tokenstore=TOKEN_DIR)   # loads cached token, no creds/MFA
    return garmin

def main():
    g = get_client()
    print("Auth OK —", g.get_full_name())
    print(f"Pulling activities {START_DATE} -> {END_DATE} ...")

    activities = g.get_activities_by_date(START_DATE, END_DATE)
    print(f"Got {len(activities)} activities")

    os.makedirs("out", exist_ok=True)
    path = os.path.join("out", "garmin_activities_raw.json")
    with open(path, "w") as f:
        json.dump(activities, f, indent=2)
    print(f"Wrote {len(activities)} activities -> {path}")

    # quick sanity: sport breakdown + date range
    types = {}
    for a in activities:
        t = a.get("activityType", {}).get("typeKey", "unknown")
        types[t] = types.get(t, 0) + 1
    print("By type:", types)
    if activities:
        dates = sorted(a.get("startTimeLocal", "") for a in activities)
        print("Earliest:", dates[0], "| Latest:", dates[-1])

if __name__ == "__main__":
    main()
