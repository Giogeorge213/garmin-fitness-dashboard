# Garmin Fitness Dashboard

A static dashboard for personal Garmin Connect data: lifetime KPIs, monthly training
volume, sport distribution, sleep and VO2 max trends, a route map, and a chat box that
answers questions about the training data.

Live site is served from S3 + CloudFront. Infrastructure is defined with AWS CDK.

## What's in the repo

```
pipeline/            data pull + transform + render (Python, stdlib only)
  garmin_client.py         Garmin Connect auth (token cached to ~/.garmin_tokens)
  pull_activity_details.py pull activities
  pull_wellness.py         pull daily wellness
  transform_wellness.py    raw wellness JSON -> curated daily CSV
  transform_routes.py      GPS traces -> routes.json (home clipped)
  render.py                build data.json + index.html + app.js
cdk/                 AWS CDK app (TypeScript): S3, CloudFront, chat Lambda,
                     HTTP API, DynamoDB rate limits, budget + SNS alerts
refresh_and_publish.ps1   weekly refresh: pull -> transform -> render -> deploy
```

## Not in the repo (by design)

Personal data never gets committed. `.gitignore` excludes `pipeline/out/`, the generated
`site/` (render inlines data into `index.html`), GPS `routes.json`, and Garmin tokens.
The live dashboard reads its data from S3, not from git.

## Build locally

```bash
python pipeline/render.py --chat-api-url https://<api-id>.execute-api.us-east-1.amazonaws.com/ask
# open site/index.html
```

## Deploy

```bash
cd cdk
npm install
npx cdk deploy
```

Built with [Kiro](https://kiro.dev). Data via Garmin Connect.
