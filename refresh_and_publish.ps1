<#
  Garmin dashboard — weekly/biweekly refresh + publish.

  Pulls the latest Garmin data, rebuilds the site, and publishes it to the
  CloudFront-backed S3 bucket in the personal account.

  Steps:
    1. pull activity details (routes/splits) + daily wellness   [pipeline/, resumable]
    2. transform wellness -> curated CSV; routes -> site/routes.json
    3. render the static site (charts + chat wired to the deployed API)
    4. sync site/ to S3 and invalidate CloudFront

  Reads bucket / distribution / chat-API from the CloudFormation stack outputs,
  so nothing is hardcoded.

  PREREQS:
    - AWS profile 'personal' configured (aws sts get-caller-identity --profile personal)
    - A valid Garmin session for garminconnect. If Garmin has logged you out,
      the pull step will fail/prompt -- run the pull once interactively to re-auth,
      then scheduled runs work again.

  NOTE: activities / race predictions / tri splits are read by render.py from the
  OLDER ingest at ../garmin-project/out. This script refreshes WELLNESS + ROUTES and
  republishes. To also refresh activities weekly, add the garmin-project ingest step.

  Usage:   powershell -ExecutionPolicy Bypass -File refresh_and_publish.ps1
#>
$ErrorActionPreference = "Stop"
$PROFILE_NAME = "personal"
$STACK = "GarminDashboard"
$REGION = "us-east-1"

$repo = $PSScriptRoot
Set-Location $repo
Write-Host "== Garmin refresh @ $(Get-Date -Format s) ==" -ForegroundColor Cyan

# --- 0. resolve stack outputs (bucket / distribution / chat api) ---
function Get-Output($key) {
  aws cloudformation describe-stacks --stack-name $STACK --profile $PROFILE_NAME --region $REGION `
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text
}
$bucket = Get-Output "BucketName"
$dist   = Get-Output "DistributionId"
$api    = Get-Output "ChatApiUrl"
if (-not $bucket -or $bucket -eq "None") { throw "Could not read BucketName from stack $STACK" }
Write-Host "bucket=$bucket  dist=$dist"

# --- 1. pull + 2. transform (run from pipeline/, which owns out/) ---
Push-Location (Join-Path $repo "pipeline")
try {
  Write-Host "-- pull activity details --" -ForegroundColor Yellow
  python pull_activity_details.py
  Write-Host "-- pull wellness --" -ForegroundColor Yellow
  python pull_wellness.py
  Write-Host "-- transform wellness --" -ForegroundColor Yellow
  python transform_wellness.py
  Write-Host "-- transform routes -> ../site/routes.json --" -ForegroundColor Yellow
  python transform_routes.py --data-dir out/raw --out ../site/routes.json
}
finally { Pop-Location }

# --- 3. render (from repo root; activities from ../garmin-project/out, health from pipeline curated) ---
Write-Host "-- render site --" -ForegroundColor Yellow
python pipeline/render.py --chat-api-url $api

# --- 4. publish: sync + invalidate ---
Write-Host "-- sync site/ -> s3://$bucket --" -ForegroundColor Yellow
aws s3 sync site/ "s3://$bucket/" --delete --profile $PROFILE_NAME
Write-Host "-- invalidate CloudFront $dist --" -ForegroundColor Yellow
aws cloudfront create-invalidation --distribution-id $dist --paths "/*" --profile $PROFILE_NAME --query "Invalidation.Status" --output text

Write-Host "== done: https://$(aws cloudformation describe-stacks --stack-name $STACK --profile $PROFILE_NAME --region $REGION --query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue" --output text) ==" -ForegroundColor Green
