#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Weekly Garmin refresh — container entrypoint.
#
# Mirrors refresh_and_publish.ps1, but sources the Garmin token and the
# incremental raw-pull cache from S3 so it runs UNATTENDED as a Fargate
# scheduled task. All AWS auth comes from the task role (no --profile).
#
# The raw cache is what keeps each run cheap: pull_activity_details.py and
# pull_wellness.py skip anything already on disk, so we restore the cache from
# S3 first (else every run would be a full multi-hour backfill) and push it
# back at the end.
#
# Required env (set by the CDK task definition from the stack outputs):
#   SITE_BUCKET      CloudFront origin bucket (stack output BucketName)
#   DISTRIBUTION_ID  CloudFront distribution id (stack output DistributionId)
#   CHAT_API_URL     chat API url baked into the page (stack output ChatApiUrl)
# Optional env (sensible defaults for the personal account):
#   ANALYTICS_BUCKET default strava-analytics-989567198465
#   STATE_PREFIX     default s3://$ANALYTICS_BUCKET/automation
#   TOKEN_S3         default $STATE_PREFIX/garmin-token   (the ~/.garmin_tokens dir)
#   RAW_CACHE_S3     default $STATE_PREFIX/raw-cache       (the pipeline/out/raw tree)
# ---------------------------------------------------------------------------
set -euo pipefail

: "${SITE_BUCKET:?set SITE_BUCKET (CloudFront origin bucket)}"
: "${DISTRIBUTION_ID:?set DISTRIBUTION_ID}"
: "${CHAT_API_URL:?set CHAT_API_URL}"

ANALYTICS_BUCKET="${ANALYTICS_BUCKET:-strava-analytics-989567198465}"
STATE_PREFIX="${STATE_PREFIX:-s3://${ANALYTICS_BUCKET}/automation}"
TOKEN_S3="${TOKEN_S3:-${STATE_PREFIX}/garmin-token}"
RAW_CACHE_S3="${RAW_CACHE_S3:-${STATE_PREFIX}/raw-cache}"
ACT_PARQUET_S3="s3://${ANALYTICS_BUCKET}/garmin/curated/garmin_activities.parquet"
WELL_PARQUET_S3="s3://${ANALYTICS_BUCKET}/garmin/wellness/wellness_daily.parquet"
METRICS_S3="${STATE_PREFIX}/last-good-metrics.json"

echo "== Garmin refresh @ $(date -u +%FT%TZ) =="

# 0. Restore Garmin token + incremental raw cache from S3 -------------------
echo "-- restore token + raw cache from S3 --"
mkdir -p "$HOME/.garmin_tokens" pipeline/out/raw pipeline/activities/out site
aws s3 sync "$TOKEN_S3" "$HOME/.garmin_tokens" --quiet
aws s3 sync "$RAW_CACHE_S3" pipeline/out/raw --quiet

# 1. Pull (incremental) + transform wellness/routes -------------------------
pushd pipeline >/dev/null
echo "-- pull activity details --"; python pull_activity_details.py
echo "-- pull wellness --";         python pull_wellness.py
echo "-- transform wellness --";    python transform_wellness.py
echo "-- transform routes -> ../site/routes.json --"
python transform_routes.py --data-dir out/raw --out ../site/routes.json
popd >/dev/null

if [ -f pipeline/out/curated/wellness_daily.parquet ]; then
  echo "-- upload wellness parquet -> Athena (garmin.wellness) --"
  aws s3 cp pipeline/out/curated/wellness_daily.parquet "$WELL_PARQUET_S3"
fi

# 2. Activities (KPIs / PRs / charts) --------------------------------------
pushd pipeline/activities >/dev/null
echo "-- ingest activities --";   python garmin_ingest.py
echo "-- transform activities --"; python garmin_transform.py
popd >/dev/null

if [ -f pipeline/activities/out/garmin_activities.parquet ]; then
  echo "-- upload activities parquet -> Athena (garmin.activities) --"
  aws s3 cp pipeline/activities/out/garmin_activities.parquet "$ACT_PARQUET_S3"
fi

# 3. Render the static site (data.json + index.html + app.js) --------------
echo "-- render site --"
python pipeline/render.py \
  --chat-api-url "$CHAT_API_URL" \
  --data-dir pipeline/activities/out \
  --health-file pipeline/out/curated/wellness_daily.csv

# 3b. Data-quality publish gate (FAIL-CLOSED) ------------------------------
# Validate the freshly rendered site BEFORE the destructive `--delete` sync.
# If it fails, set -e aborts here and the last-good site stays live (stale but
# correct) instead of publishing a blank/truncated dashboard.
echo "-- restore last-good metrics baseline --"
aws s3 cp "$METRICS_S3" prev-metrics.json --quiet 2>/dev/null || echo '{}' > prev-metrics.json
echo "-- data-quality gate --"
python pipeline/dq_checks.py --site site --prev prev-metrics.json --out new-metrics.json

# 4. Publish: sync site -> CloudFront bucket, invalidate --------------------
echo "-- sync site/ -> s3://$SITE_BUCKET --"
aws s3 sync site/ "s3://$SITE_BUCKET/" --delete
echo "-- invalidate CloudFront $DISTRIBUTION_ID --"
aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" \
  --paths '/*' --query 'Invalidation.Status' --output text

# 5. Persist refreshed token + raw cache back to S3 -------------------------
# (garth may rotate the OAuth2 token; pushing it back keeps the next run warm.)
echo "-- push token + raw cache back to S3 --"
aws s3 sync "$HOME/.garmin_tokens" "$TOKEN_S3" --quiet
aws s3 sync pipeline/out/raw "$RAW_CACHE_S3" --quiet

# Only reached if the gate passed and the publish succeeded: record this run's
# counts as the new last-good baseline for the next run's regression guard.
echo "-- persist new last-good metrics baseline --"
aws s3 cp new-metrics.json "$METRICS_S3" --quiet

echo "== done =="
