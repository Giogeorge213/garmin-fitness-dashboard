# Garmin Dashboard — CDK (personal account)

Infrastructure-as-code for hosting the Garmin fitness dashboard + chat box.
Personal AWS account `989567198465`, region `us-east-1`, profile `personal`.

## What this stack creates
- Private **S3 bucket** for the site (no public access).
- **CloudFront** distribution reading the bucket via Origin Access Control (OAC).
- **Chat Lambda** (Python) that reads `data.json` from the bucket and calls **Bedrock**.
- **HTTP API** `POST /ask` — throttled (burst 5, rate 2/s), CORS locked to the CloudFront domain.
- **Monthly cost budget** with an email alert at 80%.

Content is **not** deployed by this stack — the site is synced separately by the
local refresh job (the Garmin pull needs your Garmin login, so it stays off-cloud).

## One-time setup
```powershell
cd cdk
npm install
# bootstrap the account for CDK (creates the CDKToolkit stack, once per acct/region)
npx cdk bootstrap aws://989567198465/us-east-1 --profile personal
```

## Deploy
```powershell
npx cdk deploy --profile personal `
  -c budgetEmail=YOUR_EMAIL@example.com `
  -c budgetUsd=20
```
Note the outputs: **DashboardUrl**, **ChatApiUrl**, **BucketName**, **DistributionId**.

## Publish the site (first time + every refresh)
```powershell
# from repo root, after render.py has written ./site
aws s3 sync site/ s3://<BucketName>/ --delete --profile personal
aws cloudfront create-invalidation --distribution-id <DistributionId> --paths "/*" --profile personal
```

## Wire up chat
1. Bedrock console (personal acct) > **Model access** > enable the model in `bin/garmin.ts`
   (default `anthropic.claude-3-haiku-20240307-v1:0`).
2. Re-render with the API url so the chat box points at it, then re-sync:
   ```powershell
   python pipeline/render.py --chat-api-url <ChatApiUrl>
   aws s3 sync site/ s3://<BucketName>/ --delete --profile personal
   aws cloudfront create-invalidation --distribution-id <DistributionId> --paths "/*" --profile personal
   ```

## Teardown
```powershell
npx cdk destroy --profile personal
```
