#!/usr/bin/env python3
"""
Render the Garmin fitness dashboard: read curated CSV/JSON, compute the
aggregations the page needs, and emit a self-contained static site.

Outputs (into --out-dir, default ./site):
  - data.json   compact, plot-ready data (also fed to the chat Lambda)
  - index.html  the dashboard: KPI tiles + charts (Chart.js) + Leaflet map + chat box
  - app.js      front-end logic

Standard library only, so it runs anywhere (locally, Glue/Lambda, CI).

Usage:
  python render.py --chat-api-url https://xxxx.execute-api.us-east-1.amazonaws.com/ask
"""
import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Sports counted as "distance" sports for the monthly-miles chart.
DISTANCE_SPORTS = {
    "running", "treadmill_running", "indoor_running", "trail_running",
    "cycling", "indoor_cycling", "road_biking", "gravel_cycling", "mountain_biking", "virtual_ride",
    "lap_swimming", "open_water_swimming", "hiking", "walking",
}

# Roll raw Garmin sport keys up into a few readable groups (fewer pie slices).
SPORT_GROUP = {
    "running": "Running", "treadmill_running": "Running", "indoor_running": "Running",
    "trail_running": "Running", "track_running": "Running", "virtual_run": "Running",
    "cycling": "Cycling", "indoor_cycling": "Cycling", "road_biking": "Cycling",
    "gravel_cycling": "Cycling", "mountain_biking": "Cycling", "virtual_ride": "Cycling",
    "cyclocross": "Cycling", "bmx": "Cycling",
    "lap_swimming": "Swimming", "open_water_swimming": "Swimming",
    "hiking": "Hiking / Walking", "walking": "Hiking / Walking",
    "strength_training": "Gym / Indoor", "indoor_cardio": "Gym / Indoor", "elliptical": "Gym / Indoor",
    "stair_climbing": "Gym / Indoor", "indoor_rowing": "Gym / Indoor", "yoga": "Gym / Indoor",
    "pilates": "Gym / Indoor", "mobility": "Gym / Indoor", "meditation": "Gym / Indoor",
    "breathwork": "Gym / Indoor", "hiit": "Gym / Indoor",
}


def group_sport(s):
    return SPORT_GROUP.get(s, "Other")


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rows(path):
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(r)
    return rows


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def fmt_hms(seconds):
    if seconds is None:
        return None
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def build_data(activities, health, race_pred):
    """Turn raw rows into the compact, plot-ready structure the page consumes."""

    # ---- KPI tiles (all-time activities) --------------------------------
    total_activities = len(activities)
    total_distance = sum(fnum(a.get("distance_mi")) or 0 for a in activities)
    total_move_min = sum(fnum(a.get("moving_time_min")) or 0 for a in activities)
    dates = sorted(a.get("date") for a in activities if a.get("date"))
    date_min = dates[0] if dates else None
    date_max = dates[-1] if dates else None

    # ---- Sport distribution (rolled up into groups) ---------------------
    raw_sports = set()
    group_counts = defaultdict(int)
    group_dist = defaultdict(float)
    for a in activities:
        sp = a.get("sport") or "unknown"
        raw_sports.add(sp)
        g = group_sport(sp)
        group_counts[g] += 1
        d = fnum(a.get("distance_mi"))
        if d:
            group_dist[g] += d
    sport_dist = sorted(group_counts.items(), key=lambda kv: kv[1], reverse=True)

    # ---- Monthly training hours (all sports) + miles run / biked --------
    hrs = defaultdict(float)
    run_mi = defaultdict(float)
    bike_mi = defaultdict(float)
    for a in activities:
        m = a.get("month")
        if not m:
            continue
        t = fnum(a.get("moving_time_min"))
        if t:
            hrs[m] += t / 60.0
        g = group_sport(a.get("sport") or "")
        d = fnum(a.get("distance_mi"))
        if d:
            if g == "Running":
                run_mi[m] += d
            elif g == "Cycling":
                bike_mi[m] += d

    def _series(dd, rnd=1):
        ks = sorted(dd.keys())
        return {"labels": ks, "values": [round(dd[k], rnd) for k in ks]}

    monthly_hours = _series(hrs, 1)
    monthly_run = _series(run_mi, 1)
    monthly_bike = _series(bike_mi, 1)

    # ---- Monthly avg daily steps (from daily wellness) ------------------
    def monthly_avg(col, rnd=0):
        tot, n = defaultdict(float), defaultdict(int)
        for r in health:
            v = fnum(r.get(col))
            m = (r.get("date") or "")[:7]
            if v is not None and m:
                tot[m] += v
                n[m] += 1
        ms = sorted(n.keys())
        return {"labels": ms, "values": [round(tot[m] / n[m], rnd) for m in ms]}

    monthly_steps = monthly_avg("steps", 0)

    # ---- VO2 max time series --------------------------------------------
    vo2 = {"labels": [], "values": []}
    for r in health:
        v = fnum(r.get("vo2max"))
        if v is not None:
            vo2["labels"].append(r["date"])
            vo2["values"].append(round(v, 1))

    # ---- KPI extras -----------------------------------------------------
    step_vals = [fnum(r.get("steps")) for r in health]
    step_vals = [v for v in step_vals if v]
    avg_steps = round(sum(step_vals) / len(step_vals)) if step_vals else None
    total_steps = int(sum(step_vals))
    floor_vals = [fnum(r.get("floors")) for r in health]
    floor_vals = [v for v in floor_vals if v]
    total_floors = int(sum(floor_vals))
    sleep_vals = [fnum(r.get("sleep_hours")) for r in health]
    sleep_vals = [v for v in sleep_vals if v]
    avg_sleep = round(sum(sleep_vals) / len(sleep_vals), 2) if sleep_vals else None
    latest_vo2 = {"date": vo2["labels"][-1], "value": vo2["values"][-1]} if vo2["labels"] else None

    # ---- Personal records (best single efforts) -------------------------
    def _pace_str(p):
        m = int(p)
        s = int(round((p - m) * 60))
        if s == 60:
            m, s = m + 1, 0
        return f"{m}:{s:02d}"

    def _best(group, key, want_min=False, min_dist=0.0):
        br, bv = None, None
        for a in activities:
            if group_sport(a.get("sport") or "") != group:
                continue
            v = fnum(a.get(key))
            if v is None or v <= 0:
                continue
            if min_dist and (fnum(a.get("distance_mi")) or 0) < min_dist:
                continue
            if bv is None or (v < bv if want_min else v > bv):
                bv, br = v, a
        return br, bv

    def _rec_dist(group):
        row, val = _best(group, "distance_mi")
        return {"mi": round(val, 1), "date": row.get("date")} if row else None

    records = {
        "longest_run": _rec_dist("Running"),
        "longest_ride": _rec_dist("Cycling"),
    }
    # Fastest time PRs for standard race distances (best run within each band,
    # by moving time). Approximate: uses full-activity moving time for runs near
    # the target distance, and picks the fastest one.
    def _time_str(mins):
        total = int(round(mins * 60))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _fastest_time(lo, hi):
        br, bt = None, None
        for a in activities:
            if group_sport(a.get("sport") or "") != "Running":
                continue
            dist = fnum(a.get("distance_mi"))
            t = fnum(a.get("moving_time_min"))
            if dist is None or t is None or t <= 0 or not (lo <= dist <= hi):
                continue
            if bt is None or t < bt:
                bt, br = t, a
        return br, bt

    for label, lo, hi in (("5K", 3.05, 3.35), ("10K", 6.0, 6.5),
                          ("Half", 13.0, 13.4), ("Marathon", 26.0, 26.6)):
        rrow, rt = _fastest_time(lo, hi)
        if rrow:
            records[f"pr_{label}"] = {"time": _time_str(rt), "date": rrow.get("date")}

    # Most steps in a single day (from daily wellness)
    srow, sbest = None, None
    for r in health:
        v = fnum(r.get("steps"))
        if v is None:
            continue
        if sbest is None or v > sbest:
            sbest, srow = v, r
    if srow:
        records["most_steps"] = {"steps": int(sbest), "date": srow.get("date")}

    # ---- Commentary + structured weekly rollups (last FULL week, Mon-Sun) ----
    weekly_commentary = ""
    last_week = {}
    recent_weeks = []
    if date_max:
        dmax = datetime.strptime(date_max, "%Y-%m-%d").date()
        # Monday of the week containing the latest activity. If that week isn't
        # complete yet (latest day isn't Sunday), step back to the prior week.
        this_monday = dmax - timedelta(days=dmax.weekday())
        week_start = this_monday if dmax.weekday() == 6 else this_monday - timedelta(days=7)
        week_end = week_start + timedelta(days=6)
        prev_start = week_start - timedelta(days=7)
        prev_end = week_start - timedelta(days=1)

        def _pd(s):
            try:
                return datetime.strptime(s, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                return None

        def _btw(s, lo, hi):
            dd = _pd(s)
            return dd is not None and lo <= dd <= hi

        wk = [a for a in activities if _btw(a.get("date") or "", week_start, week_end)]
        pv = [a for a in activities if _btw(a.get("date") or "", prev_start, prev_end)]
        wk_mi = sum(fnum(a.get("distance_mi")) or 0 for a in wk)
        pv_mi = sum(fnum(a.get("distance_mi")) or 0 for a in pv)
        gwk = defaultdict(float)
        for a in wk:
            gwk[group_sport(a.get("sport") or "")] += fnum(a.get("distance_mi")) or 0
        rng = f"{week_start:%b} {week_start.day}\u2013{week_end:%b} {week_end.day}"
        bits = [f"Week of {rng}: {len(wk)} activities and {round(wk_mi)} mi."]
        tops = [f"{k} {round(v)} mi" for k, v in sorted(gwk.items(), key=lambda kv: kv[1], reverse=True) if v > 0]
        if tops:
            bits.append("Breakdown: " + ", ".join(tops) + ".")
        if pv:
            diff = wk_mi - pv_mi
            word = "more" if diff >= 0 else "less"
            bits.append(f"That is {round(abs(diff))} mi {word} than the week before.")
        weekly_commentary = " ".join(bits)

        # Structured version so the chat can answer weekly hours / breakdown / trend.
        gwk_hr = defaultdict(float)
        for a in wk:
            gwk_hr[group_sport(a.get("sport") or "")] += (fnum(a.get("moving_time_min")) or 0) / 60.0
        last_week = {
            "range": rng,
            "start": week_start.isoformat(),
            "end": week_end.isoformat(),
            "activities": len(wk),
            "hours": round(sum((fnum(a.get("moving_time_min")) or 0) for a in wk) / 60.0, 1),
            "miles": round(wk_mi, 1),
            "miles_by_sport": {k: round(v, 1) for k, v in sorted(gwk.items(), key=lambda kv: kv[1], reverse=True) if v > 0},
            "hours_by_sport": {k: round(v, 1) for k, v in sorted(gwk_hr.items(), key=lambda kv: kv[1], reverse=True) if v > 0},
            "vs_prior_week_miles": round(wk_mi - pv_mi, 1) if pv else None,
        }

        # Last 12 complete weeks (hours / miles / count) for trend questions.
        wkh, wkm, wkc = defaultdict(float), defaultdict(float), defaultdict(int)
        for a in activities:
            dd = _pd(a.get("date") or "")
            if not dd:
                continue
            ws = (dd - timedelta(days=dd.weekday())).isoformat()
            wkh[ws] += (fnum(a.get("moving_time_min")) or 0) / 60.0
            wkm[ws] += fnum(a.get("distance_mi")) or 0
            wkc[ws] += 1
        keys = [k for k in sorted(wkh.keys()) if k <= week_start.isoformat()]
        recent_weeks = [{"week_start": k, "hours": round(wkh[k], 1),
                         "miles": round(wkm[k], 1), "activities": wkc[k]} for k in keys[-12:]]

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kpis": {
            "total_activities": total_activities,
            "total_distance_mi": round(total_distance, 1),
            "total_move_hours": round(total_move_min / 60.0, 1),
            "n_sports": len(raw_sports),
            "date_min": date_min,
            "date_max": date_max,
            "avg_daily_steps": avg_steps,
            "avg_sleep_hours": avg_sleep,
            "latest_vo2max": latest_vo2,
            "bike_distance_mi": round(group_dist.get("Cycling", 0), 1),
            "run_distance_mi": round(group_dist.get("Running", 0), 1),
            "total_steps": total_steps,
            "total_floors": total_floors,
        },
        "monthly_hours": monthly_hours,
        "monthly_steps": monthly_steps,
        "monthly_run": monthly_run,
        "monthly_bike": monthly_bike,
        "records": records,
        "weekly_commentary": weekly_commentary,
        "last_week": last_week,
        "recent_weeks": recent_weeks,
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="__ROBOTS__">
<title>Garmin Fitness Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root{--bg:#f4f6f9;--card:#fff;--line:#e2e6ec;--txt:#0d1b2a;--muted:#5c6b80;--accent:#2f81f7;--accent2:#3fb950;--navy:#16305c;--amz:#ff9900;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 'Amazon Ember','Segoe UI',Roboto,Helvetica,Arial,sans-serif}
  .wrap{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
  header{margin-bottom:8px}
  header h1{margin:0;font-size:42px;font-weight:800;line-height:1.05;letter-spacing:-1px;
            background:linear-gradient(90deg,#16305c 0%,#2f81f7 60%,#6b3fa0 100%);
            -webkit-background-clip:text;background-clip:text;color:transparent}
  header h1::after{content:"";display:block;width:72px;height:5px;margin-top:12px;border-radius:3px;
                   background:linear-gradient(90deg,#ff9900,#6b3fa0);-webkit-background-clip:border-box;background-clip:border-box}
  header p{margin:14px 0 0;color:var(--muted);font-size:15px;font-weight:500}
  .grid{display:grid;gap:20px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin:24px 0}
  .cards{grid-template-columns:repeat(auto-fit,minmax(440px,1fr))}
  .card{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--accent);border-radius:12px;padding:18px;box-shadow:0 1px 3px rgba(13,27,42,.08)}
  .card.c-blue{border-top-color:#2f81f7}
  .card.c-orange{border-top-color:#ff9900}
  .card.c-purple{border-top-color:#6b3fa0}
  .card.c-red{border-top-color:#c0392b}
  .card.c-teal{border-top-color:#0aa3b0}
  .card.c-green{border-top-color:#0a7d33}
  .card.c-pink{border-top-color:#c2478a}
  .kpi{text-align:center;border:1.5px solid var(--navy);border-top:1.5px solid var(--navy)}
  .kpi .n{font-size:26px;font-weight:800;color:var(--navy)}
  .kpi .l{color:var(--navy);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
  .kpi .s{color:var(--muted);font-size:12px;margin-top:2px}
  h2{font-size:15px;margin:0 0 12px;color:var(--navy);font-weight:700;text-transform:uppercase;letter-spacing:.04em}
  canvas{max-height:300px}
  .race{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
  .race .n{font-size:20px;font-weight:800;color:#0a7d33}
  .race .l{color:var(--muted);font-size:12px}
  .chat{margin-top:24px}
  .chatlog{min-height:60px;max-height:320px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;margin-bottom:12px}
  .msg{padding:10px 12px;border-radius:10px;max-width:85%;white-space:pre-wrap}
  .msg.you{align-self:flex-end;background:var(--accent);color:#fff}
  .msg.bot{align-self:flex-start;background:#eef1f5;border:1px solid var(--line)}
  .msg.sys{align-self:center;color:var(--muted);font-size:13px;background:none}
  .chatform{display:flex;gap:8px}
  .chatform input{flex:1;background:#fff;border:1px solid var(--line);border-radius:8px;color:var(--txt);padding:10px 12px;font-size:14px}
  .chatform button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:10px 16px;font-weight:600;cursor:pointer}
  .chatform button:disabled{opacity:.5;cursor:default}
  footer{color:var(--muted);font-size:12px;margin-top:32px;text-align:center}
  a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Garmin Fitness Dashboard</h1>
    <p id="subtitle"></p>
  </header>

  <section class="grid kpis" id="kpis"></section>

  <section class="card c-orange" id="commentaryCard" style="margin-bottom:20px">
    <h2>Last Full Week</h2>
    <p id="commentary" style="margin:0;color:var(--txt);font-size:15px"></p>
  </section>

  <section class="grid cards">
    <div class="card c-blue"><h2>Monthly Training Hours</h2><canvas id="hoursChart"></canvas></div>
    <div class="card c-orange"><h2>Avg Daily Steps by Month</h2><canvas id="stepsChart"></canvas></div>
    <div class="card c-green"><h2>Miles Run by Month</h2><canvas id="runChart"></canvas></div>
    <div class="card c-teal"><h2>Miles Biked by Month</h2><canvas id="bikeChart"></canvas></div>
  </section>

  <section class="card c-pink" style="margin-top:20px" id="mapCard">
    <h2>Where I've Trained <span id="mapCount" style="text-transform:none;font-weight:400"></span></h2>
    <div id="map" style="height:440px;border-radius:8px"></div>
    <div id="mapNote" style="color:var(--muted);font-size:12px;margin-top:8px"></div>
  </section>

  <section class="card c-green" style="margin-top:20px">
    <h2>Personal Records</h2>
    <div class="race" id="records"></div>
  </section>

  <section class="card c-purple chat">
    <h2>Ask about my training</h2>
    <div class="chatlog" id="chatlog">
      <div class="msg sys">Ask a question about the training data — e.g. "what's my biggest training month?" or "how have my steps trended?"</div>
    </div>
    <form class="chatform" id="chatform">
      <input id="q" placeholder="Ask about the data..." autocomplete="off">
      <button type="submit" id="send">Ask</button>
    </form>
  </section>

  <footer>
    Built with Kiro • data via Garmin Connect • generated __GENERATED__ •
    <a href="https://github.com/__GH_REPO__">source</a>
  </footer>
</div>

<script>
const DATA = __DATA_JSON__;
const CHAT_API_URL = "__CHAT_API_URL__";
</script>
<script src="app.js"></script>
</body>
</html>
"""


APP_JS = r"""
Chart.defaults.color = "#5c6b80";
Chart.defaults.borderColor = "#e2e6ec";
Chart.defaults.font.family = "'Amazon Ember','Segoe UI',Roboto,sans-serif";
const $ = id => document.getElementById(id);
const fmt = n => (n == null ? "\u2013" : Number(n).toLocaleString());
const BLUE = "#2f81f7", GREEN = "#0a7d33", ORANGE = "#ff9900", TEAL = "#0aa3b0", PURPLE = "#6b3fa0";
const PIE = ["#2f81f7", "#0a7d33", "#ff9900", "#6b3fa0", "#0aa3b0", "#c2478a", "#c0392b", "#b8860b"];
const noLegend = { maintainAspectRatio: false, plugins: { legend: { display: false } } };

function subtitle() {
  const k = DATA.kpis;
  $("subtitle").textContent = `${fmt(k.total_activities)} activities \u00b7 ${k.date_min} \u2192 ${k.date_max}`;
}

function kpis() {
  const k = DATA.kpis;
  const tiles = [
    ["Biking distance", fmt(k.bike_distance_mi) + " mi", "lifetime"],
    ["Running distance", fmt(k.run_distance_mi) + " mi", "lifetime"],
    ["Moving time", fmt(k.total_move_hours) + " h", "all sports"],
    ["Total steps", fmt(k.total_steps), "lifetime"],
    ["Total floors", fmt(k.total_floors), "lifetime"],
    ["Avg sleep", (k.avg_sleep_hours ?? "\u2013") + " h", "lifetime"],
  ];
  $("kpis").innerHTML = tiles.map(([l, n, s]) =>
    `<div class="card kpi"><div class="l">${l}</div><div class="n">${n}</div><div class="s">${s}</div></div>`
  ).join("");
}

function charts() {
  // x labels are "YYYY-MM"; show compact MM/YY ticks, auto-skipping to fit.
  const monthX = () => ({
    grid: { display: true },
    ticks: {
      maxRotation: 0, autoSkip: true, maxTicksLimit: 16, font: { size: 10 },
      callback: function (v) { const l = String(this.getLabelForValue(v)); return l.slice(5, 7) + "/" + l.slice(2, 4); },
    },
  });
  const bar = (id, series, color) => new Chart($(id), {
    type: "bar",
    data: { labels: series.labels, datasets: [{ data: series.values, backgroundColor: color }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: monthX() } },
  });
  bar("hoursChart", DATA.monthly_hours, BLUE);
  bar("stepsChart", DATA.monthly_steps, ORANGE);
  bar("runChart", DATA.monthly_run, GREEN);
  bar("bikeChart", DATA.monthly_bike, TEAL);
}

function commentary() {
  const c = DATA.weekly_commentary;
  if (c) { $("commentary").textContent = c; }
  else { const el = $("commentaryCard"); if (el) el.style.display = "none"; }
}

function records() {
  const r = DATA.records || {};
  const items = [];
  (DATA.manual_records || []).forEach(m => items.push([m.label, m.value, m.date, m.detail]));
  ["5K", "10K", "Half", "Marathon"].forEach(k => {
    const pr = r["pr_" + k];
    if (pr) items.push(["Fastest " + k, pr.time, pr.date]);
  });
  if (r.longest_run)   items.push(["Longest run",   fmt(r.longest_run.mi) + " mi",   r.longest_run.date]);
  if (r.longest_ride)  items.push(["Longest ride",  fmt(r.longest_ride.mi) + " mi",  r.longest_ride.date]);
  if (r.most_steps)    items.push(["Most steps (1 day)", fmt(r.most_steps.steps), r.most_steps.date]);
  $("records").innerHTML = items.map(([l, n, s, d]) =>
    `<div><div class="n">${n}</div><div class="l">${l}</div>` +
    `<div class="l" style="opacity:.65">${s || ""}</div>` +
    (d ? `<div class="l" style="opacity:.65;margin-top:3px">${d}</div>` : "") +
    `</div>`
  ).join("") || "<div class='l'>no records</div>";
}

// ---- Chat -----------------------------------------------------------------
function addMsg(text, cls) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.textContent = text;
  $("chatlog").appendChild(d);
  $("chatlog").scrollTop = $("chatlog").scrollHeight;
  return d;
}

async function ask(q) {
  if (!CHAT_API_URL) { addMsg("Chat isn't wired up yet.", "sys"); return; }
  const send = $("send"); send.disabled = true;
  const thinking = addMsg("\u2026", "bot");
  try {
    const res = await fetch(CHAT_API_URL, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    const data = await res.json();
    thinking.textContent = data.answer || data.message || "No answer returned.";
  } catch (e) {
    thinking.textContent = "Error contacting the chat service.";
  } finally { send.disabled = false; }
}

document.getElementById("chatform").addEventListener("submit", ev => {
  ev.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  addMsg(q, "you");
  $("q").value = "";
  ask(q);
});

// ---- Route map (Leaflet + CARTO light tiles, Canvas renderer) -------------
const SPORT_COLORS = {
  running: "#0a7d33", treadmill_running: "#0a7d33", trail_running: "#0a7d33",
  cycling: "#ff9900", road_biking: "#ff9900", gravel_cycling: "#ff9900", mountain_biking: "#ff9900",
  lap_swimming: "#2f81f7", open_water_swimming: "#2f81f7",
  hiking: "#c2478a", walking: "#6b3fa0",
};

async function drawMap() {
  let data;
  try {
    const res = await fetch("routes.json", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    data = await res.json();
  } catch (e) {
    $("mapNote").textContent = "Could not load routes.json: " + e.message;
    return;
  }
  if (!data.routes || !data.routes.length) { $("mapNote").textContent = "No route data available."; return; }

  const el = document.getElementById("map");
  // Wait until the card has laid out with real dimensions. Creating a Leaflet
  // map on a 0-size element is what made canvas paint nothing.
  await new Promise(resolve => {
    const ok = () => el.clientWidth > 0 && el.clientHeight > 0;
    if (ok()) return resolve();
    const ro = new ResizeObserver(() => { if (ok()) { ro.disconnect(); resolve(); } });
    ro.observe(el);
    setTimeout(() => { ro.disconnect(); resolve(); }, 3000);
  });

  // Panning blanks the SVG features on this globe-spanning dataset, so dragging
  // is locked. Zoom stays on (zoom triggers a full redraw, which paints fine).
  const map = L.map(el, {
    renderer: L.svg({ padding: 3 }),
    dragging: false, keyboard: false, boxZoom: false,
    scrollWheelZoom: true, doubleClickZoom: true, touchZoom: true,
    minZoom: 2, worldCopyJump: false,
    maxBounds: [[-72, -180], [82, 180]], maxBoundsViscosity: 1.0,
  });
  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    { maxZoom: 19, minZoom: 2, subdomains: "abcd", noWrap: true,
      attribution: "\u00a9 OpenStreetMap \u00a9 CARTO" }).addTo(map);

  // Routes as lines (visible when you zoom into a region) + a fixed-size dot at
  // each start, so every location shows even in the zoomed-out overview.
  const pts = [];
  data.routes.forEach(r => {
    if (!r.coords || r.coords.length < 2) return;
    const color = SPORT_COLORS[r.sport] || "#5c6b80";
    L.polyline(r.coords, { color, weight: 3, opacity: 0.85 }).addTo(map);
    L.circleMarker(r.coords[0], { radius: 3, weight: 0, fillColor: color, fillOpacity: 0.8 }).addTo(map);
    pts.push(r.coords[0]);
  });

  // Open on the full overview of everywhere trained. invalidateSize first so
  // fitBounds measures the real container; SVG repaints on each call.
  map.invalidateSize();
  if (pts.length) map.fitBounds(pts, { padding: [20, 20], maxZoom: 11 });
  else map.setView(data.center || [20, 0], 2);
  requestAnimationFrame(() => requestAnimationFrame(() => map.invalidateSize()));
  setTimeout(() => map.invalidateSize(), 400);
  $("mapCount").textContent = "(" + data.routes.length + " GPS activities)";
  $("mapNote").textContent = "Each dot marks where an activity started. Scroll or use +/- to zoom.";
}

// Run each section independently so a failure in one never blanks the others.
[subtitle, kpis, commentary, charts, records, drawMap].forEach(fn => {
  try { fn(); } catch (e) { console.error(fn.name, e); }
});
"""


def build_html(data, chat_api_url, gh_repo, robots):
    return (HTML_TEMPLATE
            .replace("__DATA_JSON__", json.dumps(data, separators=(",", ":")))
            .replace("__CHAT_API_URL__", chat_api_url or "")
            .replace("__GENERATED__", data["generated_at"])
            .replace("__GH_REPO__", gh_repo)
            .replace("__ROBOTS__", robots))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="../garmin-project/out")
    ap.add_argument("--health-file", default="pipeline/out/curated/wellness_daily.csv",
                    help="curated full-history daily wellness CSV (from transform_wellness.py)")
    ap.add_argument("--manual-records", default="pipeline/manual_records.json",
                    help="optional JSON of manually-entered records (e.g. Ironman finish time)")
    ap.add_argument("--out-dir", default="./site")
    ap.add_argument("--chat-api-url", default="")
    ap.add_argument("--gh-repo", default="giorgram/garmin-fitness-dashboard")
    ap.add_argument("--robots", default="noindex", help="'noindex' (default) or 'index'")
    args = ap.parse_args()

    d = args.data_dir
    activities = load_rows(os.path.join(d, "garmin_activities.csv"))
    health = load_rows(args.health_file)
    race_pred = load_json(os.path.join(d, "garmin_race_predictions.json"), default={})

    data = build_data(activities, health, race_pred)

    # Merge in any manually-entered records (survives every re-render).
    # An entry with "replaces": "<records key>" overwrites a computed PR in
    # place (keeps ordering, no duplicate). Otherwise it renders as its own
    # tile at the top of the records card.
    manual = load_json(args.manual_records, default={}) or {}
    manual_list = []
    for k, v in manual.items():
        if not (isinstance(v, dict) and v.get("value") and v.get("value") != "REPLACE_ME"):
            continue
        repl = v.get("replaces")
        if repl:
            data["records"][repl] = {"time": v["value"], "date": v.get("date", "")}
        else:
            manual_list.append({"label": v.get("label", k), "value": v["value"],
                                "date": v.get("date", ""), "detail": v.get("detail", "")})
    data["manual_records"] = manual_list

    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    with open(os.path.join(args.out_dir, "app.js"), "w", encoding="utf-8") as f:
        f.write(APP_JS)
    with open(os.path.join(args.out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_html(data, args.chat_api_url, args.gh_repo, args.robots))

    print(f"built {args.out_dir}/index.html  ({data['kpis']['total_activities']} activities, "
          f"{len(health)} health days)")
    print(f"  monthly hours points: {len(data['monthly_hours']['labels'])}")
    print(f"  records: {list(data['records'].keys())} + manual {[m['label'] for m in data.get('manual_records', [])]}")


if __name__ == "__main__":
    main()
