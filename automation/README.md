# Automated weekly refresh (containerized)

Runs the same pull → transform → render → publish flow as `refresh_and_publish.ps1`,
but as a **scheduled AWS Fargate task** so it needs no local machine.

## How it works

```
EventBridge (weekly, Sun) ──▶ ECS RunTask (Fargate) ──▶ this container
                                                              │
   restore Garmin token + raw cache from S3  ◀───────────────┤
   pull (incremental) → transform → render                   │
   sync site/ → CloudFront bucket + invalidate               │
   push refreshed token + raw cache back to S3  ─────────────┘
```

- **Incremental cache**: `pipeline/out/raw` (the per-activity / per-day pull cache)
  lives in S3 at `automation/raw-cache/`. Restored at start, pushed back at end, so
  each weekly run only pulls the new week — not a full backfill.
- **Auth**: the Garmin token dir (`~/.garmin_tokens`) lives in S3 at
  `automation/garmin-token/`. Restored into the container each run. AWS auth is the
  Fargate task role (no credentials in the image).

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | image (python:3.12-slim + garminconnect/pandas/pyarrow/awscli). Build context = repo root. |
| `requirements.txt` | container Python deps |
| `run_refresh.sh` | entrypoint — the full flow above |

## One-time setup

1. **Container runtime** (local, to build/push the image at `cdk deploy`):
   Docker Engine in WSL (`sudo apt-get install -y docker.io`) or Finch. Docker
   Desktop is NOT required.
2. **Seed the Garmin token to S3** (unattended runs reuse it). Log in once locally
   so `~/.garmin_tokens` is fresh, then:
   ```
   aws s3 sync ~/.garmin_tokens s3://strava-analytics-989567198465/automation/garmin-token --profile personal
   ```
3. **Seed the raw cache** (optional but recommended, so the first cloud run isn't a
   multi-hour backfill): after a local run,
   ```
   aws s3 sync pipeline/out/raw s3://strava-analytics-989567198465/automation/raw-cache --profile personal
   ```
4. **Deploy** the stack (adds the ECR image, Fargate task, and weekly schedule):
   ```
   cd cdk && npx cdk deploy --profile personal
   ```

## Re-auth (rare)

When Garmin invalidates the session (~annually or after a password change), the
scheduled run fails at the pull step. Fix: run the local login once, then re-run the
`aws s3 sync ~/.garmin_tokens ...` command from step 2. Nothing else changes.

## Test it without waiting for Sunday

After deploy, run the task on demand from the ECS console (or `aws ecs run-task ...`)
and watch the CloudWatch logs. A successful run republishes the CloudFront site.
