# Weekly Refresh Automation — Process & Reference

How the Garmin dashboard's weekly data refresh was moved off a local machine into a
containerized, self-running cloud job — the architecture, key concepts, the gotchas that
cost time, and how to operate it.

## What changed

The dashboard (static site on S3 + CloudFront, a Bedrock chat Lambda, Athena tables, a
Python data pipeline) already existed. The weak spot: the weekly refresh ran from a
**local PowerShell script on Windows Task Scheduler**, so it only worked when the machine
was on. We moved that refresh into the cloud as a **containerized ECS Fargate scheduled
task**. The site and chat were unchanged — only *how the data gets refreshed*.

## Architecture decision (and why)

Three options for running unattended:

- **Container Lambda** — ruled out by the 15-min limit + heavy deps (pandas/pyarrow) + a
  potentially large pull cache.
- **GitHub Actions** — viable, no Docker needed, but less container/portfolio value.
- **Fargate scheduled task** — chosen. EventBridge fires it weekly, it runs a few minutes,
  then exits.

Key mental model: **a Fargate scheduled task is ephemeral, not 24/7.** You pay only for the
minutes it runs (pennies/month). The always-on cost trap is an ECS *service* — a different
thing. We also used a **no-NAT VPC** (public subnets + public IP) because a NAT gateway
would add ~$32/month and dwarf everything else.

## The two concepts that make it work

1. **State lives in S3, because containers start empty.** The pipeline is *incremental* —
   it skips any activity/day already pulled, so a weekly run is fast, not a 4-hour backfill.
   But a fresh container has an empty disk every run. So the entrypoint **restores the
   raw-pull cache (`pipeline/out/raw`) from S3 at the start and pushes it back at the end.**
   Without that, every run would re-pull 6 years from scratch. "Persist working state to S3"
   is the core trick for any stateful job on ephemeral compute.

2. **Auth is a stored token, not a password.** `garminconnect` caches a login token in
   `~/.garmin_tokens`. For unattended runs that token lives in S3
   (`automation/garmin-token/`), is restored into the container each run, and pushed back if
   refreshed. It's long-lived, so you only re-seed it (~annually or after a Garmin password
   change). In the cloud, AWS auth comes from the **Fargate task role** — no keys in the image.

## Gotchas that cost time (recognize these next time)

1. **The deploy looked "hung" but mostly wasn't.** After the CDK warnings there's a long
   *silent* stretch: synth (~54s), Docker build, then CloudFormation creating the VPC/ECS
   (several minutes). Ctrl+C-ing during that silence was the mistake. Run `cdk deploy -v` for
   visible progress, and when unsure, **check the CloudFormation stack status directly**
   instead of assuming a hang.
2. **CDK's Docker asset walk ignores `.dockerignore`.** The build context was the repo root
   (~1.1 GB with `node_modules`/`.git`). `.dockerignore` trims what Docker *sends*, but CDK
   computes the asset fingerprint by walking the tree itself and does **not** honor
   `.dockerignore` — you must pass `exclude: [...]` on `fromAsset`. That, plus building over
   the slow `/mnt/c` filesystem, was the real slowness.
3. **The real deploy failure was the email.** The stack intentionally defaults the alert
   email to `REPLACE_WITH_YOUR_EMAIL` (a *good* pattern — the repo is public, so the email
   isn't hardcoded). Pass it at deploy time with `-c notifyEmail=...`. Forgetting it → SNS
   rejects the placeholder → deploy fails.
4. **The VPC construct needs credentials at synth time** to look up Availability Zones. Fine
   once the profile creds are valid; it's why a no-creds synth "hangs then gives up."
5. **CRLF would break the entrypoint.** Git on Windows wanted to convert `run_refresh.sh` to
   CRLF, which breaks bash with `\r` errors. `.gitattributes` (`*.sh text eol=lf`) prevents
   that on any checkout.

## How we diagnosed it (the useful method)

When it kept "hanging," the breakthrough was to stop guessing and get ground truth: ran
`cdk synth` to completion (proved synth works, ~54s), ran `docker build` directly (proved the
image builds), then read the **CloudFormation stack status and events directly**. General
move: isolate each layer and read the actual system state rather than inferring from silence.

## Operate it

- **Automatic:** runs every **Sunday 13:00 UTC**, no action needed.
- **Run on demand:** `ecs run-task` on cluster
  `GarminDashboard-RefreshClusterFF90AF15-I2x0e56Xa6e6`, task family
  `GarminDashboardRefreshTask3F332C26` (or ECS console -> Run task). Needs the public
  subnets + `assignPublicIp=ENABLED`.
- **Watch it:** CloudWatch log group
  `GarminDashboard-RefreshTaskrefreshLogGroupC732CFAA-ZwVH7UxenrNo`, or the ECS task's Logs
  tab. Logs end at `== done ==` on success.
- **Re-auth Garmin (rare):** local login, then
  `aws s3 sync ~/.garmin_tokens s3://strava-analytics-989567198465/automation/garmin-token --profile personal`.
- **Change the pipeline:** edit code, then
  `cdk deploy --profile personal --require-approval never -c notifyEmail=<you>`.
  Rebuilds the image and updates the task.

## Key resources

| Thing | Value |
|---|---|
| Account / region | 989567198465 / us-east-1 (profile `personal`) |
| ECS cluster | `GarminDashboard-RefreshClusterFF90AF15-I2x0e56Xa6e6` |
| Task family | `GarminDashboardRefreshTask3F332C26` |
| Log group | `GarminDashboard-RefreshTaskrefreshLogGroupC732CFAA-ZwVH7UxenrNo` |
| Site bucket | `garmindashboard-sitebucket397a1860-z3k6foldzcwx` |
| State bucket | `s3://strava-analytics-989567198465/automation/` (`garmin-token/`, `raw-cache/`) |
| Schedule | EventBridge rule, weekly Sun 13:00 UTC |

## Repo layout of the automation

```
automation/
  Dockerfile          image: python:3.12-slim + garminconnect/pandas/pyarrow/awscli
  requirements.txt    container deps
  run_refresh.sh       entrypoint: restore token+cache from S3 -> pull -> transform ->
                       render -> sync site to CloudFront -> invalidate -> push cache+token back
  README.md           setup + operate
pipeline/activities/  vendored activities scripts (so the image has ALL pipeline code)
.dockerignore         trims what Docker sends
.gitattributes        LF for *.sh
cdk/lib/garmin-dashboard-stack.ts   VPC (no NAT) + ECS + Fargate task + weekly rule
```

## Note

- The local Windows Task Scheduler job (`\GarminDashboardRefresh`) was disabled so it does
  not double-refresh alongside the cloud task.
