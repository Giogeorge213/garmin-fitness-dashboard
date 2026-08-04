"""Garmin transform -> CSV + Parquet. Coordinates rounded to ~city level for privacy."""
import csv, json, os
from datetime import datetime

RAW = os.path.join("out", "garmin_activities_raw.json")
OUT_CSV = os.path.join("out", "garmin_activities.csv")
OUT_PARQUET = os.path.join("out", "garmin_activities.parquet")
METERS_PER_MILE = 1609.344

COLUMNS = ["activity_id","name","sport","start_datetime_local","date","year","month","week",
           "day_of_week","distance_mi","moving_time_min","total_time_min","pace_min_per_mi",
           "avg_speed_mph","elevation_gain_ft","average_hr","max_hr","avg_cadence","calories",
           "latitude","longitude","days_since_last_activity"]

def f(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def transform(raw):
    rows=[]
    for a in raw:
        dist_m=f(a.get("distance")); mov_s=f(a.get("movingDuration")); dur_s=f(a.get("duration")); spd=f(a.get("averageSpeed"))
        dist_mi=dist_m/METERS_PER_MILE if dist_m else None
        mov_min=mov_s/60.0 if mov_s else None
        tot_min=dur_s/60.0 if dur_s else None
        pace=(mov_min/dist_mi) if (mov_min and dist_mi and dist_mi>0) else None
        mph=spd*2.2369362920544 if spd else None
        elev=f(a.get("elevationGain")); elev_ft=elev*3.280839895 if elev is not None else None
        lat=f(a.get("startLatitude")); lon=f(a.get("startLongitude"))
        sd=a.get("startTimeLocal"); dt=None
        if sd:
            try: dt=datetime.strptime(sd,"%Y-%m-%d %H:%M:%S")
            except ValueError: dt=None
        rows.append({
            "activity_id":a.get("activityId"),"name":a.get("activityName"),
            "sport":a.get("activityType",{}).get("typeKey"),"start_datetime_local":sd,
            "date":dt.date().isoformat() if dt else None,"year":dt.year if dt else None,
            "month":dt.strftime("%Y-%m") if dt else None,"week":dt.strftime("%Y-W%W") if dt else None,
            "day_of_week":dt.strftime("%A") if dt else None,
            "distance_mi":round(dist_mi,3) if dist_mi else None,
            "moving_time_min":round(mov_min,2) if mov_min else None,
            "total_time_min":round(tot_min,2) if tot_min else None,
            "pace_min_per_mi":round(pace,3) if pace else None,
            "avg_speed_mph":round(mph,2) if mph else None,
            "elevation_gain_ft":round(elev_ft,1) if elev_ft is not None else None,
            "average_hr":f(a.get("averageHR")),"max_hr":f(a.get("maxHR")),
            "avg_cadence":f(a.get("averageRunningCadenceInStepsPerMinute")),
            "calories":f(a.get("calories")),
            "latitude":round(lat,1) if lat is not None else None,
            "longitude":round(lon,1) if lon is not None else None,
            "_dt":dt,
        })
    rows.sort(key=lambda r: r["_dt"] or datetime.min)
    prev=None
    for r in rows:
        r["days_since_last_activity"]=(r["_dt"].date()-prev.date()).days if (r["_dt"] and prev) else None
        if r["_dt"]: prev=r["_dt"]
    for r in rows: r.pop("_dt",None)
    return rows

def main():
    raw=json.load(open(RAW)); print(f"loaded {len(raw)}")
    rows=transform(raw)
    with open(OUT_CSV,"w",newline="",encoding="utf-8") as fh:
        w=csv.DictWriter(fh,fieldnames=COLUMNS); w.writeheader()
        for r in rows: w.writerow({c:r.get(c) for c in COLUMNS})
    print(f"wrote {len(rows)} -> {OUT_CSV}")
    try:  # parquet feeds the Athena table; render only needs the CSV, so never fail on it
        import pandas as pd
        df = pd.DataFrame(rows, columns=COLUMNS)
        # Deterministic types so the Glue/Athena schema never drifts on nulls.
        int_cols = ["activity_id", "year", "days_since_last_activity"]
        float_cols = ["distance_mi", "moving_time_min", "total_time_min", "pace_min_per_mi",
                      "avg_speed_mph", "elevation_gain_ft", "average_hr", "max_hr",
                      "avg_cadence", "calories", "latitude", "longitude"]
        str_cols = ["name", "sport", "start_datetime_local", "date", "month", "week", "day_of_week"]
        for c in int_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        for c in float_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
        for c in str_cols:
            df[c] = df[c].astype("object")
        df.to_parquet(OUT_PARQUET, index=False)
        print(f"wrote {len(rows)} -> {OUT_PARQUET}")
    except Exception as e:
        print(f"(skipped parquet: {e})")
    print("coords rounded to 1 decimal (~11km) — home address not resolvable")

if __name__=="__main__": main()
