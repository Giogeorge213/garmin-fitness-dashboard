"""
Garmin dashboard chat handler with abuse guards.

POST /ask  {"question": "..."} -> {"answer": "..."}

Rate limits (per UTC day, atomic counters in DynamoDB):
  - per client IP:  IP_DAILY_MAX questions/day  -> 429 when exceeded
  - global (all users): GLOBAL_DAILY_MAX/day     -> 429 when exceeded (hard spend brake)
When the GLOBAL cap is first hit, publishes one SNS alert ("maxed out today").

Answers come from Bedrock using the dashboard's data.json as the only context.
"""
import base64
import datetime
import json
import os

import boto3

BUCKET = os.environ["BUCKET_NAME"]
DATA_KEY = os.environ.get("DATA_KEY", "data.json")
MODEL_ID = os.environ["MODEL_ID"]
TABLE = os.environ["COUNTER_TABLE"]
IP_DAILY_MAX = int(os.environ.get("IP_DAILY_MAX", "10"))
GLOBAL_DAILY_MAX = int(os.environ.get("GLOBAL_DAILY_MAX", "500"))
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")
dynamodb = boto3.client("dynamodb")
sns = boto3.client("sns")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type": "application/json",
}

SYSTEM = (
    "You are a concise assistant for a personal Garmin fitness dashboard. "
    "Answer ONLY from the JSON data provided. If the answer isn't in the data, "
    "say you don't have that data. Reply in one or two plain English sentences. "
    "Do NOT output JSON, code blocks, or key-value dumps. Numbers are miles, "
    "minutes, bpm, hours, and min/mi paces as labeled in the JSON."
)


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
    """Atomically +1 the counter at pk; returns the new value."""
    r = dynamodb.update_item(
        TableName=TABLE,
        Key={"pk": {"S": pk}},
        UpdateExpression="ADD #c :one SET #t = if_not_exists(#t, :ttl)",
        ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
        ExpressionAttributeValues={
            ":one": {"N": "1"},
            ":ttl": {"N": str(ttl_epoch)},
        },
        ReturnValues="UPDATED_NEW",
    )
    return int(r["Attributes"]["count"]["N"])


def handler(event, context):
    http = (event.get("requestContext", {}) or {}).get("http", {}) or {}
    if http.get("method") == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    question = (_body(event).get("question") or "").strip()
    if not question:
        return _resp(400, {"message": "Missing 'question'."})

    ip = http.get("sourceIp", "unknown")
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    ttl_epoch = int(datetime.datetime.utcnow().timestamp()) + 2 * 86400  # rows self-clean

    # --- per-IP cap (don't touch the global budget if this IP is already over) ---
    try:
        ip_count = _bump(f"ip#{ip}#{today}", ttl_epoch)
    except Exception as e:
        return _resp(500, {"message": f"rate-limit check failed: {e}"})
    if ip_count > IP_DAILY_MAX:
        return _resp(429, {"message": f"Daily limit reached ({IP_DAILY_MAX} questions per day). Try again tomorrow."})

    # --- global cap (hard spend brake) + one-shot alert when first hit ---
    try:
        g_count = _bump(f"global#{today}", ttl_epoch)
    except Exception as e:
        return _resp(500, {"message": f"rate-limit check failed: {e}"})
    if g_count == GLOBAL_DAILY_MAX + 1 and ALERT_TOPIC_ARN:
        try:
            sns.publish(
                TopicArn=ALERT_TOPIC_ARN,
                Subject="Garmin dashboard: daily question cap reached",
                Message=(f"The dashboard hit its global daily cap of {GLOBAL_DAILY_MAX} "
                         f"questions on {today} (UTC). Further questions are blocked until "
                         f"tomorrow. If this is unexpected, someone may be hammering /ask."),
            )
        except Exception:
            pass  # never fail a request because the alert didn't send
    if g_count > GLOBAL_DAILY_MAX:
        return _resp(429, {"message": "The dashboard has reached its daily question limit. Try again tomorrow."})

    # --- answer from Bedrock ---
    try:
        data_json = s3.get_object(Bucket=BUCKET, Key=DATA_KEY)["Body"].read().decode("utf-8")
    except Exception as e:
        return _resp(500, {"message": f"Could not load dashboard data: {e}"})

    prompt = f"Here is my fitness dashboard data as JSON:\n\n{data_json}\n\nQuestion: {question}"
    try:
        # Converse API: model-agnostic (works with Nova, Anthropic, etc.).
        resp = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 500, "temperature": 0.2},
        )
        blocks = resp.get("output", {}).get("message", {}).get("content", [])
        answer = "".join(b.get("text", "") for b in blocks).strip() or "No answer returned."
        return _resp(200, {"answer": answer})
    except Exception as e:
        return _resp(502, {"message": f"Model call failed: {e}"})
