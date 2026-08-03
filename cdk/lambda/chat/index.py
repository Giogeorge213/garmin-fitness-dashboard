"""
Garmin dashboard chat handler.

POST /ask  {"question": "..."} -> {"answer": "..."}

The model answers quick facts from the dashboard summary (data.json) and, for
anything the summary doesn't cover, calls a read-only SQL tool over the Athena
table garmin.activities (via Bedrock Converse tool use).

Rate limits (per UTC day, atomic counters in DynamoDB):
  - per client IP:  IP_DAILY_MAX questions/day  -> 429 when exceeded
  - global (all users): GLOBAL_DAILY_MAX/day     -> 429 when exceeded (hard spend brake)
When the GLOBAL cap is first hit, publishes one SNS alert.
"""
import base64
import datetime
import json
import os
import re
import time

import boto3

BUCKET = os.environ["BUCKET_NAME"]
DATA_KEY = os.environ.get("DATA_KEY", "data.json")
MODEL_ID = os.environ["MODEL_ID"]
TABLE = os.environ["COUNTER_TABLE"]
IP_DAILY_MAX = int(os.environ.get("IP_DAILY_MAX", "10"))
GLOBAL_DAILY_MAX = int(os.environ.get("GLOBAL_DAILY_MAX", "500"))
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")
ATHENA_DB = os.environ.get("ATHENA_DB", "garmin")
ATHENA_OUTPUT = os.environ.get("ATHENA_OUTPUT", "")

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")
dynamodb = boto3.client("dynamodb")
sns = boto3.client("sns")
athena = boto3.client("athena")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type": "application/json",
}

SYSTEM = (
    "You are a concise assistant for a personal Garmin fitness dashboard. You have a "
    "SUMMARY JSON and a SQL tool (query_garmin) over two Athena tables in the "
    f"'{ATHENA_DB}' database: activities (one row per workout) and wellness (one row per day).\n"
    "The SUMMARY contains ONLY these fields: kpis (lifetime totals - bike/run distance, "
    "moving hours, total steps, total floors, avg daily steps, avg sleep, latest VO2, "
    "date range); monthly_hours/monthly_steps/monthly_run/monthly_bike (one value per "
    "month); records (all-time PRs: fastest 5K/10K/half/marathon, longest run, longest "
    "ride, most steps, and a manual Ironman); last_week and recent_weeks (weekly hours/"
    "miles/activity counts).\n"
    "Answer directly from the SUMMARY ONLY when the question maps to one of those exact "
    "fields. For EVERYTHING ELSE - any specific workout or day, any specific date, month, "
    "or year, any filter or condition, any count, or any per-row value - you MUST call "
    "query_garmin. Do NOT answer those from the summary, and NEVER guess, estimate, or "
    "invent a number. If a query returns no rows, say you don't have that data.\n"
    f"{ATHENA_DB}.activities columns: activity_id (bigint), name (string), sport (string), "
    "start_datetime_local (string), date (string 'YYYY-MM-DD'), year (bigint), month "
    "(string 'YYYY-MM'), week (string), day_of_week (string), distance_mi, moving_time_min, "
    "total_time_min, pace_min_per_mi, avg_speed_mph, elevation_gain_ft, average_hr, max_hr, "
    "avg_cadence, calories, latitude, longitude (all double), days_since_last_activity (bigint).\n"
    f"{ATHENA_DB}.wellness columns (one row per day): date (string 'YYYY-MM-DD'), steps (bigint), "
    "distance_km, floors, moderate_min, vigorous_min, total_kcal, active_kcal, resting_hr, min_hr, "
    "max_hr, avg_stress, max_stress, body_battery_charged, body_battery_drained, sleep_hours, "
    "deep_sleep_hours, light_sleep_hours, rem_sleep_hours, awake_hours, hrv, hrv_status (string), "
    "vo2max, readiness_score, readiness_level (string), weight_kg, hydration_ml. "
    "Use wellness for sleep, steps, heart rate, stress, HRV, body battery, VO2, weight, and "
    "hydration questions; use activities for workouts.\n"
    "activities.sport values include running, cycling, lap_swimming, treadmill_running, "
    "indoor_cycling, hiking, walking, stair_climbing, indoor_rowing, road_biking, "
    "open_water_swimming, yoga, elliptical. Use the year column for a given year; "
    "for running totals include running, treadmill_running, trail_running, indoor_running.\n"
    "Reply in one or two plain English sentences. Never show SQL, raw JSON, or any "
    "<thinking> notes to the user. Distances are miles, times minutes/hours, paces min/mi."
)

TOOL_CONFIG = {
    "tools": [{
        "toolSpec": {
            "name": "query_garmin",
            "description": (
                "Run a read-only Athena SQL SELECT over the garmin database "
                "(tables: activities, wellness) to answer questions the summary can't. "
                "Returns result rows as text."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": ("A single Athena/Presto SELECT over "
                                        f"{ATHENA_DB}.activities or {ATHENA_DB}.wellness. "
                                        "No DDL/DML, no semicolons. Keep results small "
                                        "(aggregate or LIMIT <= 50)."),
                    }
                },
                "required": ["sql"],
            }},
        }
    }]
}

BANNED = ["insert", "update", "delete", "drop", "create", "alter", "grant",
          "revoke", "truncate", "merge", "call", "msck", "load", "describe", "show"]


def _resp(status, obj):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(obj)}


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _bump(pk, ttl_epoch):
    r = dynamodb.update_item(
        TableName=TABLE,
        Key={"pk": {"S": pk}},
        UpdateExpression="ADD #c :one SET #t = if_not_exists(#t, :ttl)",
        ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
        ExpressionAttributeValues={":one": {"N": "1"}, ":ttl": {"N": str(ttl_epoch)}},
        ReturnValues="UPDATED_NEW",
    )
    return int(r["Attributes"]["count"]["N"])


def _sql_is_safe(sql):
    s = (sql or "").strip().rstrip(";")
    if not s or ";" in s:
        return False
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return False
    return not any(re.search(r"\b" + b + r"\b", low) for b in BANNED)


def _clean(text):
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.S | re.I)
    text = re.sub(r"</?thinking>", "", text, flags=re.I)
    return text.strip()


def _run_athena(sql):
    qid = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": ATHENA_DB},
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
    )["QueryExecutionId"]
    state = "RUNNING"
    for _ in range(22):  # ~15s cap; queries on this tiny table finish in ~1-3s
        info = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        state = info["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.7)
    if state != "SUCCEEDED":
        reason = info.get("StateChangeReason", "")
        return f"ERROR: query {state}. {reason}"[:800]
    res = athena.get_query_results(QueryExecutionId=qid, MaxResults=51)
    out = []
    for row in res["ResultSet"]["Rows"]:
        out.append(" | ".join(c.get("VarCharValue", "") for c in row["Data"]))
    return "\n".join(out) if out else "(no rows)"


def _answer(question, data_json):
    messages = [{"role": "user", "content": [
        {"text": f"Summary data:\n{data_json}\n\nQuestion: {question}"}]}]
    for _ in range(3):  # at most one tool round in practice
        resp = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM}],
            messages=messages,
            toolConfig=TOOL_CONFIG,
            inferenceConfig={"maxTokens": 600, "temperature": 0},
        )
        msg = resp["output"]["message"]
        messages.append(msg)
        if resp.get("stopReason") == "tool_use":
            tool_results = []
            for block in msg["content"]:
                tu = block.get("toolUse")
                if not tu:
                    continue
                sql = (tu.get("input") or {}).get("sql", "")
                if not _sql_is_safe(sql):
                    text = "ERROR: only a single read-only SELECT is allowed."
                else:
                    try:
                        text = _run_athena(sql)
                    except Exception as e:  # noqa: BLE001
                        text = f"ERROR: {e}"
                tool_results.append({"toolResult": {
                    "toolUseId": tu["toolUseId"],
                    "content": [{"text": text[:6000]}],
                }})
            messages.append({"role": "user", "content": tool_results})
            continue
        return _clean("".join(b.get("text", "") for b in msg["content"])) or "No answer returned."
    return "I couldn't work that out from the data."


def handler(event, context):
    http = (event.get("requestContext", {}) or {}).get("http", {}) or {}
    if http.get("method") == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    question = (_body(event).get("question") or "").strip()
    if not question:
        return _resp(400, {"message": "Missing 'question'."})

    ip = http.get("sourceIp", "unknown")
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    ttl_epoch = int(datetime.datetime.utcnow().timestamp()) + 2 * 86400

    try:
        ip_count = _bump(f"ip#{ip}#{today}", ttl_epoch)
    except Exception as e:  # noqa: BLE001
        return _resp(500, {"message": f"rate-limit check failed: {e}"})
    if ip_count > IP_DAILY_MAX:
        return _resp(429, {"message": f"Daily limit reached ({IP_DAILY_MAX} questions per day). Try again tomorrow."})

    try:
        g_count = _bump(f"global#{today}", ttl_epoch)
    except Exception as e:  # noqa: BLE001
        return _resp(500, {"message": f"rate-limit check failed: {e}"})
    if g_count == GLOBAL_DAILY_MAX + 1 and ALERT_TOPIC_ARN:
        try:
            sns.publish(
                TopicArn=ALERT_TOPIC_ARN,
                Subject="Garmin dashboard: daily question cap reached",
                Message=(f"The dashboard hit its global daily cap of {GLOBAL_DAILY_MAX} "
                         f"questions on {today} (UTC)."),
            )
        except Exception:  # noqa: BLE001
            pass
    if g_count > GLOBAL_DAILY_MAX:
        return _resp(429, {"message": "The dashboard has reached its daily question limit. Try again tomorrow."})

    try:
        data_json = s3.get_object(Bucket=BUCKET, Key=DATA_KEY)["Body"].read().decode("utf-8")
    except Exception as e:  # noqa: BLE001
        return _resp(500, {"message": f"Could not load dashboard data: {e}"})

    try:
        return _resp(200, {"answer": _answer(question, data_json)})
    except Exception as e:  # noqa: BLE001
        return _resp(502, {"message": f"Model call failed: {e}"})
