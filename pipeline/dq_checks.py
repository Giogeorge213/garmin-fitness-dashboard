#!/usr/bin/env python3
"""
Data-quality publish gate for the Garmin dashboard (fail-closed).

Runs AFTER render, BEFORE the `aws s3 sync --delete` publish. If any check
fails it exits non-zero so run_refresh.sh (set -e) aborts before publishing --
the last-good site stays live, degrading to "stale but correct" instead of
serving a blank or truncated dashboard.

Mirrors the Amazon DD producer patterns: a row-count > 0 gate and a
regression guard against the last good run (agg_refresh refuses to swap on
0 rows / schema drift; here we refuse to publish on 0 activities / a sharp
drop in cumulative counts).

Stdlib only, so it runs anywhere the render step runs.

Usage:
    python pipeline/dq_checks.py --site site --prev prev-metrics.json --out new-metrics.json

  --site   directory holding the freshly rendered site (index.html, app.js,
           data.json, routes.json)
  --prev   optional JSON from the last GOOD run ({} or missing on first run);
           enables the regression guard
  --out    where to write this run's metrics on success (the shell then
           persists it to S3 as the new last-good baseline)
"""
import argparse
import json
import os
import sys

# Thresholds -- deliberately loose; this is a solo project, not a data warehouse.
MIN_HTML_BYTES = 2000     # a real dashboard page is well above this; empty renders are tiny
MIN_ACTIVITIES = 1        # never publish a zero-activity dashboard
MIN_ROUTES = 1            # never publish a blank map
DROP_TOLERANCE = 2        # cumulative counts may reconcile down a little (dedup); a bigger
                          # drop means a partial pull -> fail closed


def fail(msg, problems):
    problems.append(msg)


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--prev", default=None, help="last-good metrics JSON (optional)")
    ap.add_argument("--out", default=None, help="where to write this run's metrics on success")
    args = ap.parse_args()

    problems = []
    site = args.site

    # 1. index.html present and non-trivial ---------------------------------
    idx = os.path.join(site, "index.html")
    if not os.path.exists(idx):
        fail(f"missing {idx}", problems)
    elif os.path.getsize(idx) < MIN_HTML_BYTES:
        fail(f"{idx} is only {os.path.getsize(idx)} bytes (< {MIN_HTML_BYTES}); likely an empty render",
             problems)

    # 2. app.js present and non-empty ---------------------------------------
    appjs = os.path.join(site, "app.js")
    if not os.path.exists(appjs) or os.path.getsize(appjs) == 0:
        fail(f"missing or empty {appjs}", problems)

    # 3. data.json parses and has a sane activity count ---------------------
    activities = None
    data_path = os.path.join(site, "data.json")
    if not os.path.exists(data_path):
        fail(f"missing {data_path}", problems)
    else:
        try:
            data = load_json(data_path)
            activities = int(data.get("kpis", {}).get("total_activities", 0))
            if activities < MIN_ACTIVITIES:
                fail(f"data.json total_activities={activities} (< {MIN_ACTIVITIES})", problems)
        except (ValueError, json.JSONDecodeError) as e:
            fail(f"data.json unreadable: {e}", problems)

    # 4. routes.json parses and has at least one route ----------------------
    routes = None
    routes_path = os.path.join(site, "routes.json")
    if not os.path.exists(routes_path):
        fail(f"missing {routes_path}", problems)
    else:
        try:
            rj = load_json(routes_path)
            routes = len(rj.get("routes", []))
            if routes < MIN_ROUTES:
                fail(f"routes.json has {routes} routes (< {MIN_ROUTES}); the map would be blank",
                     problems)
        except json.JSONDecodeError as e:
            fail(f"routes.json unreadable: {e}", problems)

    # 5. Regression guard vs the last GOOD run ------------------------------
    # total_activities and route count are cumulative, so a real drop = data
    # loss (usually an expired token / partial pull). Fail closed rather than
    # overwrite the good live site with less data.
    prev = {}
    if args.prev and os.path.exists(args.prev):
        try:
            prev = load_json(args.prev) or {}
        except json.JSONDecodeError:
            prev = {}
    if prev.get("activities") is not None and activities is not None:
        if activities < prev["activities"] - DROP_TOLERANCE:
            fail(f"activities dropped {prev['activities']} -> {activities} "
                 f"(> {DROP_TOLERANCE}); looks like a partial pull", problems)
    if prev.get("routes") is not None and routes is not None:
        if routes < prev["routes"] - DROP_TOLERANCE:
            fail(f"routes dropped {prev['routes']} -> {routes} "
                 f"(> {DROP_TOLERANCE}); looks like a partial pull", problems)

    # Verdict ---------------------------------------------------------------
    if problems:
        print("== DQ GATE FAILED -- refusing to publish (last-good site stays live) ==")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    print(f"== DQ gate passed: {activities} activities, {routes} routes ==")
    if prev:
        print(f"   (previous good run: {prev.get('activities')} activities, {prev.get('routes')} routes)")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"activities": activities, "routes": routes}, fh)
        print(f"   wrote new baseline -> {args.out}")


if __name__ == "__main__":
    main()
