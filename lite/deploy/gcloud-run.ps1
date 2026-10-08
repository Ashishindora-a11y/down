<#
.SYNOPSIS
    Build and deploy the Lite API to Google Cloud Run.

.DESCRIPTION
    Cloud Run is the better fit of the two managed hosts for this workload:

      * Request timeout up to 3600s, versus App Runner's hard 120s. Long
        proxied downloads simply work.
      * Scales to zero, so an idle service costs nothing.
      * Still open to new customers (App Runner closed to new sign-ups on
        2026-03-31).

    One Cloud Run quirk to respect: the filesystem is RAM-backed, so anything
    written to /tmp counts against the memory you are billed for. The default
    download path (via=proxy) streams and never writes to disk. Keep
    ENABLE_SERVER_DOWNLOAD on for ffmpeg work, but size memory accordingly.

.PARAMETER Project
    GCP project id. Defaults to the active gcloud config value.

.PARAMETER Region
    Cloud Run region. Pick one near the clients that will call this.

.PARAMETER Service
    Cloud Run service name.

.PARAMETER ApiKey
    API key clients must present. One is generated if you omit it.

.PARAMETER AllowUnauthenticated
    Expose the service publicly. The app's own API key still gates every
    /api/v1 route. Without this flag the service requires a Google IAM token,
    which is the stronger option for service-to-service calls.

.EXAMPLE
    .\gcloud-run.ps1 -Project my-project -Region asia-south1

.EXAMPLE
    # Private: only callers with roles/run.invoker can reach it.
    .\gcloud-run.ps1 -Project my-project -Region us-central1 -Service clip-api
#>

[CmdletBinding()]
param(
    [string]$Project,
    [string]$Region = "us-central1",
    [string]$Service = "clip-api-lite",
    [string]$ApiKey,
    [switch]$AllowUnauthenticated,
    [int]$MemoryMb = 2048,
    [double]$Cpu = 1,
    [int]$TimeoutSec = 600,
    [int]$MaxInstances = 5,
    [int]$Concurrency = 8
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step { param([string]$Message) Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Note { param([string]$Message) Write-Host "    $Message" -ForegroundColor DarkGray }

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud CLI not found. Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install"
}

if (-not $Project) {
    $Project = (gcloud config get-value project 2>$null)
    if (-not $Project -or $Project -eq "(unset)") {
        throw "No project set. Pass -Project <id> or run: gcloud config set project <id>"
    }
}
Write-Note "Project : $Project"
Write-Note "Region  : $Region"
Write-Note "Service : $Service"

if (-not $ApiKey) {
    $ApiKey = python -c "import secrets; print(secrets.token_urlsafe(32))"
    if ($LASTEXITCODE -ne 0 -or -not $ApiKey) {
        throw "Could not generate an API key. Pass -ApiKey <value> explicitly."
    }
    Write-Host "`nGenerated API key (store it now, it is not shown again):" -ForegroundColor Yellow
    Write-Host "  $ApiKey" -ForegroundColor Yellow
}

Write-Step "Enabling required services"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
    artifactregistry.googleapis.com --project $Project
if ($LASTEXITCODE -ne 0) { throw "Failed to enable required APIs." }

# The API key goes into Secret Manager rather than a plaintext env var, so it
# stays out of the service description and out of deploy logs.
Write-Step "Storing the API key in Secret Manager"
$secretName = "$Service-api-keys"
$exists = gcloud secrets describe $secretName --project $Project 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud services enable secretmanager.googleapis.com --project $Project | Out-Null
    $ApiKey | gcloud secrets create $secretName --data-file=- --project $Project `
        --replication-policy=automatic
    if ($LASTEXITCODE -ne 0) { throw "Could not create secret $secretName." }
    Write-Note "Created secret $secretName"
} else {
    $ApiKey | gcloud secrets versions add $secretName --data-file=- --project $Project
    if ($LASTEXITCODE -ne 0) { throw "Could not add a version to secret $secretName." }
    Write-Note "Added a new version to secret $secretName"
}

$projectNumber = (gcloud projects describe $Project --format="value(projectNumber)")
$runtimeSa = "$projectNumber-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding $secretName `
    --member="serviceAccount:$runtimeSa" `
    --role="roles/secretmanager.secretAccessor" `
    --project $Project --quiet | Out-Null
Write-Note "Granted secret access to $runtimeSa"

Write-Step "Building and deploying from source"
Write-Note "Cloud Build reads lite/Dockerfile, so ffmpeg is included."

$envVars = @(
    "LOG_FORMAT=json"
    "ENABLE_UI=true"
    "REQUIRE_API_KEY=true"
    "ENABLE_SERVER_DOWNLOAD=true"
    "MAX_DURATION_SEC=300"
    "MAX_FILESIZE_MB=250"
    "MAX_QUALITY=1080"
    # Stay just under the Cloud Run request timeout so the app returns a clean
    # 504 instead of the platform severing the connection.
    "SOFT_DEADLINE_SEC=$([Math]::Max(30, $TimeoutSec - 20))"
    "MAX_CONCURRENT=$Concurrency"
    "TMP_DIR=/tmp"
    "WEB_CONCURRENCY=1"
    "THREADS=$($Concurrency * 2)"
) -join ","

$deployArgs = @(
    "run", "deploy", $Service,
    "--source", $repoRoot,
    "--project", $Project,
    "--region", $Region,
    "--platform", "managed",
    "--port", "8080",
    "--memory", "${MemoryMb}Mi",
    "--cpu", "$Cpu",
    "--timeout", "${TimeoutSec}s",
    "--max-instances", "$MaxInstances",
    "--concurrency", "$Concurrency",
    "--set-env-vars", $envVars,
    "--set-secrets", "API_KEYS=${secretName}:latest",
    "--quiet"
)

if ($AllowUnauthenticated) {
    Write-Host "`n  Deploying with --allow-unauthenticated." -ForegroundColor Yellow
    Write-Host "  The URL will be reachable by anyone; the app's API key is the only gate." -ForegroundColor Yellow
    $deployArgs += "--allow-unauthenticated"
} else {
    $deployArgs += "--no-allow-unauthenticated"
    Write-Note "Private service: callers need roles/run.invoker and an identity token."
}

& gcloud @deployArgs
if ($LASTEXITCODE -ne 0) { throw "Cloud Run deployment failed." }

$url = (gcloud run services describe $Service --project $Project --region $Region `
    --format="value(status.url)")

Write-Step "Deployed"
Write-Host "  URL: $url" -ForegroundColor Green

Write-Host "`nVerify:" -ForegroundColor Cyan
if ($AllowUnauthenticated) {
    Write-Host "  curl $url/healthz"
    Write-Host "  curl -X POST $url/api/v1/info -H `"X-API-Key: <key>`" -H `"Content-Type: application/json`" -d '{\`"url\`":\`"https://youtube.com/shorts/VIDEO_ID\`"}'"
} else {
    Write-Host "  `$t = gcloud auth print-identity-token"
    Write-Host "  curl -H `"Authorization: Bearer `$t`" $url/healthz"
}

Write-Host "`nNotes:" -ForegroundColor Cyan
Write-Host "  * Health check path: /healthz   Readiness: /readyz"
Write-Host "  * Raise --timeout (max 3600s) if you proxy larger media."
Write-Host "  * Rate limits are per instance, so the effective ceiling is limit x instances."
Write-Host "  * Rebuild monthly: yt-dlp goes stale as platforms change their players."
