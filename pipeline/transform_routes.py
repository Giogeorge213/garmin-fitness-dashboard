#!/usr/bin/env python3
"""
Build map-ready route polylines from the activity detail files.

For every activity that has GPS (geoPolylineDTO.polyline), this:
  1. Extracts the [lat, lon] track.
  2. Clips a privacy zone off BOTH ends -- drops points within CLIP_M meters of
     the start and of the end -- so the published map never shows your home.
  3. Downsamples to at most MAX_PTS points so the map file stays small.
  4. Tags each route with sport + date from the activity index.

Writes site/routes.json:  { "center": [lat, lon], "routes": [ {id, sport, date,
coords:[[lat,lon],...]}, ... ] }  -- consumed by the Leaflet map on the dashboard.

Indoor activities (no polyline) are skipped. Nothing here contacts Garmin; it
only reads the already-downloaded raw files.
"""
import argparse
import glob
import json
import math
import os

CLIP_M = 300      # privacy zone radius (meters) clipped off each end
MAX_PTS = 80      # max points per route after downsampling (keeps the map light)


def haversine_m(a, b):
    """Great-circle distance in meters between (lat,lon) a and b."""
    R = 6371000.0
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def clip_privacy(coords):
    """Drop leading points near the start and trailing points near the end."""
    if len(coords) < 4:
        return []
    start, end = coords[0], coords[-1]
    lo = 0
    while lo < len(coords) and haversine_m(coords[lo], start) < CLIP_M:
        lo += 1
    hi = len(coords) - 1
    while hi > lo and haversine_m(coords[hi], end) < CLIP_M:
        hi -= 1
    return coords[lo:hi + 1]


def downsample(coords, max_pts=MAX_PTS):
    if len(coords) <= max_pts:
        return coords
    step = len(coords) / max_pts
    out = [coords[int(i * step)] for i in range(max_pts)]
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def load_index(data_dir):
    idx = {}
    p = os.path.join(data_dir, "activities_index.json")
    if os.path.exists(p):
        for a in json.load(open(p, encoding="utf-8")):
            aid = a.get("activityId")
            if aid:
                idx[aid] = {
                    "sport": a.get("activityType", {}).get("typeKey"),
                    "date": (a.get("startTimeLocal") or "")[:10],
                    "loc": a.get("locationName"),
                }
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="out/raw")
    ap.add_argument("--out", default="site/routes.json")
    ap.add_argument("--max-routes", type=int, default=0, help="0 = all outdoor activities")
    args = ap.parse_args()

    idx = load_index(args.data_dir)
    files = glob.glob(os.path.join(args.data_dir, "activity_details", "*.json"))
    routes = []
    all_lat, all_lon = [], []

    for f in files:
        try:
            det = (json.load(open(f, encoding="utf-8")).get("details") or {})
        except Exception:
            continue
        poly = (det.get("geoPolylineDTO") or {}).get("polyline") or []
        coords = [[p["lat"], p["lon"]] for p in poly
                  if p.get("lat") is not None and p.get("lon") is not None]
        coords = clip_privacy(coords)
        if len(coords) < 10:            # nothing meaningful left after clipping
            continue
        coords = downsample(coords)
        aid = det.get("activityId")
        meta = idx.get(aid, {})
        routes.append({"id": aid, "sport": meta.get("sport"),
                       "date": meta.get("date"), "loc": meta.get("loc"),
                       "coords": [[round(c[0], 5), round(c[1], 5)]
                                  for c in coords]})
        all_lat += [c[0] for c in coords]
        all_lon += [c[1] for c in coords]

    routes.sort(key=lambda r: r.get("date") or "", reverse=True)
    if args.max_routes and len(routes) > args.max_routes:
        routes = routes[:args.max_routes]

    center = [sum(all_lat) / len(all_lat), sum(all_lon) / len(all_lon)] if all_lat else [39.5, -98.35]
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"center": [round(center[0], 5), round(center[1], 5)], "routes": routes}, fh,
                  separators=(",", ":"))

    sz = os.path.getsize(args.out) / 1024
    print(f"wrote {len(routes)} routes -> {args.out} ({sz:.0f} KB), privacy-clip {CLIP_M}m, <= {MAX_PTS} pts each")


if __name__ == "__main__":
    main()
