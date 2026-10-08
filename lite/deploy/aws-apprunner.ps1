<#
.SYNOPSIS
    Build the Lite API image, push it to Amazon ECR, and deploy it to AWS App Runner.

.DESCRIPTION
    Two things to know before you run this.

    1. App Runner closed to new customers on 2026-03-31. If your account has
       never used it, service creation will fail and nothing here can work
       around that. Existing App Runner customers are unaffected. Alternatives
       that run this same image unchanged: ECS Fargate, Lightsail Containers,
       or Google Cloud Run (see gcloud-run.ps1).

    2. App Runner enforces a hard 120-second request timeout, covering the time
       to read the request and write the entire response. It is not
       configurable. Consequences for this service:

         * SOFT_DEADLINE_SEC is set to 110 so the app returns a clean 504
           instead of having the connection cut mid-body.
         * Proxied downloads of slow or large media can hit that ceiling. Use
           POST /api/v1/url and let your client fetch the CDN directly to avoid
           the limit entirely.

    This script deliberately does NOT create IAM roles. Granting App Runner
    access to ECR is a privileged change, so if the role is missing the script
    prints the exact commands and stops for you to review and run.

.PARAMETER Region
    AWS region. Defaults to your configured region.

.PARAMETER Repository
    ECR repository name. Created if absent.

.PARAMETER Service
    App Runner service name. Updated if it already exists.

.PARAMETER ApiKey
    API key clients must present. Generated if omitted.

.PARAMETER Tag
    Image tag. Defaults to a UTC timestamp, which keeps deploys traceable and
    avoids the "did it actually roll out?" ambiguity of reusing :latest.

.PARAMETER SkipServiceDeploy
    Build and push only. Useful for wiring the image into ECS or Lightsail.

.EXAMPLE
    .\aws-apprunner.ps1 -Region ap-south-1

.EXAMPLE
    .\aws-apprunner.ps1 -Region us-east-1 -SkipServiceDeploy
#>

[CmdletBinding()]
param(
    [string]$Region,
    [string]$Repository = "clip-api-lite",
    [string]$Service = "clip-api-lite",
    [string]$ApiKey,
    [string]$Tag = (Get-Date -Format "yyyyMMdd-HHmmss"),
    [ValidateSet("0.25 vCPU", "0.5 vCPU", "1 vCPU", "2 vCPU", "4 vCPU")]
    [string]$Cpu = "1 vCPU",
    [ValidateSet("0.5 GB", "1 GB", "2 GB", "3 GB", "4 GB", "6 GB", "8 GB", "10 GB", "12 GB")]
    [string]$Memory = "2 GB",
    [switch]$SkipServiceDeploy
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step { param([string]$Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Note { param([string]$Message) Write-Host "    $Message" -ForegroundColor DarkGray }

foreach ($tool in @("aws", "docker")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool CLI not found on PATH. Install it and re-run."
    }
}

if (-not $Region) {
    $Region = (aws configure get region 2>$null)
    if (-not $Region) { throw "No region configured. Pass -Region <name> or run: aws configure" }
}

$accountId = (aws sts get-caller-identity --query Account --output text)
if ($LASTEXITCODE -ne 0 -or -not $accountId) {
    throw "Could not resolve your AWS account. Check your credentials with: aws sts get-caller-identity"
}

$registry = "$accountId.dkr.ecr.$Region.amazonaws.com"
$imageUri = "$registry/${Repository}:$Tag"

Write-Note "Account  : $accountId"
Write-Note "Region   : $Region"
Write-Note "Image    : $imageUri"

Write-Host "`n  Reminder: App Runner has been closed to new customers since 2026-03-31." -ForegroundColor Yellow
Write-Host "  If this account has never created an App Runner service, use gcloud-run.ps1" -ForegroundColor Yellow
Write-Host "  or deploy this image to ECS Fargate instead." -ForegroundColor Yellow

# --- ECR repository -------------------------------------------------------

Write-Step "Ensuring the ECR repository exists"
aws ecr describe-repositories --repository-names $Repository --region $Region 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    aws ecr create-repository `
        --repository-name $Repository `
        --region $Region `
        --image-scanning-configuration scanOnPush=true `
        --image-tag-mutability IMMUTABLE | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create ECR repository $Repository." }
    Write-Note "Created $Repository (immutable tags, scan on push)"
} else {
    Write-Note "$Repository already exists"
}

# --- build and push -------------------------------------------------------

Write-Step "Logging docker in to ECR"
$password = aws ecr get-login-password --region $Region
if ($LASTEXITCODE -ne 0) { throw "aws ecr get-login-password failed." }
$password | docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { throw "docker login to $registry failed." }

Write-Step "Building the image for linux/amd64"
Write-Note "App Runner rejects arm64, so the platform is pinned regardless of your machine."
docker build --platform linux/amd64 -t $imageUri -t "${registry}/${Repository}:latest" $repoRoot
if ($LASTEXITCODE -ne 0) { throw "docker build failed." }

Write-Step "Pushing to ECR"
docker push $imageUri
if ($LASTEXITCODE -ne 0) { throw "docker push failed for $imageUri." }

if ($SkipServiceDeploy) {
    Write-Step "Image pushed; skipping service deployment as requested"
    Write-Host "  $imageUri" -ForegroundColor Green
    return
}

# --- IAM prerequisite (checked, never created silently) -------------------

Write-Step "Checking the App Runner ECR access role"
$roleName = "AppRunnerECRAccessRole"
$roleArn = "arn:aws:iam::${accountId}:role/$roleName"
aws iam get-role --role-name $roleName 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host @"

  Role '$roleName' does not exist. App Runner needs it to pull from a private
  ECR repository. Creating IAM roles changes account-wide permissions, so this
  script will not do it for you. Review and run:

    aws iam create-role --role-name $roleName \
      --assume-role-policy-document '{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"build.apprunner.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]}'

    aws iam attach-role-policy --role-name $roleName \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess

  Then re-run this script. The image is already in ECR, so it will resume from
  the service step:
    $imageUri

"@ -ForegroundColor Yellow
    return
}
Write-Note "Found $roleArn"

# --- API key --------------------------------------------------------------

if (-not $ApiKey) {
    $ApiKey = python -c "import secrets; print(secrets.token_urlsafe(32))"
    if ($LASTEXITCODE -ne 0 -or -not $ApiKey) {
        throw "Could not generate an API key. Pass -ApiKey <value> explicitly."
    }
    Write-Host "`nGenerated API key (store it now, it is not shown again):" -ForegroundColor Yellow
    Write-Host "  $ApiKey" -ForegroundColor Yellow
}

# --- service create or update --------------------------------------------

Write-Step "Deploying the App Runner service"

$envPairs = [ordered]@{
    LOG_FORMAT              = "json"
    ENABLE_UI               = "true"
    REQUIRE_API_KEY         = "true"
    API_KEYS                = $ApiKey
    ENABLE_SERVER_DOWNLOAD  = "true"
    MAX_DURATION_SEC        = "300"
    MAX_FILESIZE_MB         = "250"
    MAX_QUALITY              = "1080"
    SOFT_DEADLINE_SEC       = "110"   # App Runner cuts the request at 120s
    MAX_CONCURRENT          = "4"
    TMP_DIR                 = "/tmp"
    WEB_CONCURRENCY         = "2"
    THREADS                 = "8"
}

$sourceConfig = @{
    ImageRepository = @{
        ImageIdentifier     = $imageUri
        ImageRepositoryType = "ECR"
        ImageConfiguration  = @{
            Port                        = "8080"
            RuntimeEnvironmentVariables = $envPairs
        }
    }
    AutoDeploymentsEnabled = $false
    AuthenticationConfiguration = @{ AccessRoleArn = $roleArn }
}

$healthCheck = @{
    Protocol            = "HTTP"
    Path                = "/healthz"
    Interval            = 10
    Timeout             = 5
    HealthyThreshold    = 1
    UnhealthyThreshold  = 5
}

$instanceConfig = @{ Cpu = $Cpu; Memory = $Memory }

$tempDir = Join-Path $env:TEMP "clip-api-deploy"
New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
$sourcePath = Join-Path $tempDir "source-config.json"
$healthPath = Join-Path $tempDir "health-config.json"
$instancePath = Join-Path $tempDir "instance-config.json"

try {
    $sourceConfig   | ConvertTo-Json -Depth 10 | Set-Content $sourcePath   -Encoding utf8
    $healthCheck    | ConvertTo-Json -Depth 10 | Set-Content $healthPath   -Encoding utf8
    $instanceConfig | ConvertTo-Json -Depth 10 | Set-Content $instancePath -Encoding utf8

    $existingArn = aws apprunner list-services --region $Region `
        --query "ServiceSummaryList[?ServiceName=='$Service'].ServiceArn | [0]" --output text 2>$null

    if ($existingArn -and $existingArn -ne "None") {
        Write-Note "Updating existing service $existingArn"
        aws apprunner update-service `
            --region $Region `
            --service-arn $existingArn `
            --source-configuration "file://$sourcePath" `
            --instance-configuration "file://$instancePath" `
            --health-check-configuration "file://$healthPath" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "update-service failed." }
        $serviceArn = $existingArn
    } else {
        Write-Note "Creating service $Service"
        $created = aws apprunner create-service `
            --region $Region `
            --service-name $Service `
            --source-configuration "file://$sourcePath" `
            --instance-configuration "file://$instancePath" `
            --health-check-configuration "file://$healthPath" | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw "create-service failed. If this account is new to App Runner, that is expected: the service closed to new customers on 2026-03-31." }
        $serviceArn = $created.Service.ServiceArn
    }
} finally {
    # These files hold the API key in plaintext.
    Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Step "Waiting for the service to become available"
Write-Note "First deploys usually take 3-6 minutes."

$deadline = (Get-Date).AddMinutes(15)
do {
    Start-Sleep -Seconds 20
    $status = aws apprunner describe-service --region $Region --service-arn $serviceArn `
        --query "Service.Status" --output text
    Write-Note "status: $status"
    if ($status -in @("CREATE_FAILED", "DELETE_FAILED")) {
        throw "Service entered $status. Inspect the deployment logs in CloudWatch."
    }
} while ($status -ne "RUNNING" -and (Get-Date) -lt $deadline)

if ($status -ne "RUNNING") {
    Write-Warning "Service is still '$status' after 15 minutes. Check the App Runner console."
    return
}

$serviceUrl = aws apprunner describe-service --region $Region --service-arn $serviceArn `
    --query "Service.ServiceUrl" --output text

Write-Step "Deployed"
Write-Host "  https://$serviceUrl" -ForegroundColor Green

Write-Host "`nVerify:" -ForegroundColor Cyan
Write-Host "  curl https://$serviceUrl/healthz"
Write-Host "  curl -X POST https://$serviceUrl/api/v1/info ``"
Write-Host "    -H `"X-API-Key: $ApiKey`" -H `"Content-Type: application/json`" ``"
Write-Host "    -d '{\`"url\`":\`"https://youtube.com/shorts/VIDEO_ID\`"}'"

Write-Host "`nNotes:" -ForegroundColor Cyan
Write-Host "  * The API key was passed as an env var and is visible to anyone with"
Write-Host "    apprunner:DescribeService. Move it to Secrets Manager for production."
Write-Host "  * 120s request cap is a platform limit. For long downloads, call"
Write-Host "    /api/v1/url and fetch the CDN URL from your client."
Write-Host "  * Rebuild monthly so yt-dlp stays current."
