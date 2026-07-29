# deploy_RHE.ps1 — one-shot, idempotent standup for the Rooming House Expert dashboard.
#
# RHE is an API-LIVE client (the client_honeytribe / client_riverdance pattern):
#   NO BigQuery dataset, NO SQL views. Two live sources, one private JSON:
#     Meta ads (3 ad accounts) via Windsor.ai  +  ActiveCampaign REST v3
#
# What this creates / updates (all derived from the client key `RHE`, never re-typed):
#   APIs -> Artifact Registry repo + PRIVATE bucket -> service accounts + least-privilege IAM
#   -> secrets -> export job (build, deploy, first run) -> run.developer grants
#   -> dash web service
#
# NO Cloud Scheduler by default: refresh is driven by the dashboard's Sync button, because each
# run costs paid Windsor/Meta calls. `-WithScheduler` adds the 6-hourly tick back.
#
# Build as YOURSELF, never Cloud Build from a laptop: the Cloud Build SA cannot actAs the runtime
# SA. `gcloud builds submit --tag` builds the image (no actAs); the deploy step runs as you.
# Never --allow-unauthenticated (org policy forbids it) — the service does its own auth behind
# --no-invoker-iam-check.
#
# Usage:
#   .\deploy_RHE.ps1                        # prompts for the three secrets
#   .\deploy_RHE.ps1 -SkipJobRun            # stand up without the (slow) first export
#   .\deploy_RHE.ps1 -WithScheduler         # also add the 6-hourly refresh tick
#   .\deploy_RHE.ps1 -Password ... -WindsorKey ... -ActiveCampaignKey ...
#
# ROTATE both supplied credentials before/after standup: the Windsor key and the ActiveCampaign
# Api-Token both arrived inside a shared document (playbook 10).

param(
    [string]$Password = "",
    [string]$WindsorKey = "",
    [string]$ActiveCampaignKey = "",
    [switch]$SkipJobRun,
    # Refresh is Sync-button driven by the client's decision. Pass this to also run a 6-hourly
    # Cloud Scheduler tick; without it any existing scheduler is REMOVED.
    [switch]$WithScheduler
)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$REPO    = "agora"
$CLIENT  = "RHE"                 # display / folder / data-object key
# GCS bucket names, Cloud Run service+job names and service-account ids are all
# LOWERCASE-ONLY. This client's key is upper-case, so every CLOUD RESOURCE derives from a
# lower-cased key while `$CLIENT` keeps its casing for the data object and log lines.
# (First standup failed here: "Invalid bucket name: 'agora-data-driven-RHE-dash'".)
$KEY     = $CLIENT.ToLowerInvariant()

# Derived names (DERIVE from the client key; never re-type) --------------------
$BUCKET      = "agora-data-driven-$KEY-dash"
$EXPORT_JOB  = "$KEY-export"
$SCHED       = "$KEY-export-6h"
$WEB_SERVICE = "$KEY-dash"
$JOB_SA      = "$KEY-dash-job@agora-data-driven.iam.gserviceaccount.com"
$WEB_SA      = "$KEY-dash-web@agora-data-driven.iam.gserviceaccount.com"
$PW_SECRET   = "$KEY-dash-password"
$KEY_SECRET  = "$KEY-dash-session-key"
$WIN_SECRET  = "$KEY-windsor-key"
$AC_SECRET   = "$KEY-activecampaign-key"
$DATA_OBJECT = "$KEY.json"
$AR_HOST     = "$REGION-docker.pkg.dev"

# Client-specific connector config (NOT secrets -- account ids and hostnames).
# Three SEPARATE brands, all live: Stuart Baker / RHE / Super Cashflow Development.
$WINDSOR_ACCOUNTS   = "facebook__291824415053555,facebook__744718258097253,facebook__819110256113106"
$ACTIVECAMPAIGN_URL = "https://roominghouse.api-us1.com"

$ROOT     = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$JOB_DIR  = Join-Path $PSScriptRoot "job"
$DASH_DIR = Join-Path $PSScriptRoot "dash"
$VALIDATOR = Join-Path $ROOT "tools\_validate_dash_js.py"
$VENV_PY = Join-Path $ROOT ".venv\Scripts\python.exe"
if (-not (Test-Path $VENV_PY)) { $VENV_PY = Join-Path $ROOT ".venv-portal\Scripts\python.exe" }
if (-not (Test-Path $VENV_PY)) { $VENV_PY = "py" }

function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) { if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" } }
function Exists([scriptblock]$Probe) { & $Probe *> $null; return ($LASTEXITCODE -eq 0) }
function Ensure-Sa([string]$email, [string]$accountId, [string]$displayName) {
    if (Exists { gcloud iam service-accounts describe $email --project $PROJECT }) {
        Write-Host "    $email already exists"
    } else {
        Write-Host "    creating $email" -ForegroundColor Yellow
        gcloud iam service-accounts create $accountId --project $PROJECT --display-name $displayName
        Must "create service account $email"
    }
}
function Write-SecretFile([string]$path, [string]$value) {
    # UTF-8, no BOM, no trailing newline -- see the root CLAUDE.md "Never" list.
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $value, $enc)
}
function Read-Secret([string]$current, [string]$envName, [string]$prompt) {
    if (-not [string]::IsNullOrEmpty($current)) { return $current }
    $v = [Environment]::GetEnvironmentVariable($envName)
    if (-not [string]::IsNullOrEmpty($v)) { return $v }
    $sec = Read-Host $prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

# --- Step 0: image tag + project number --------------------------------------
Write-Host "[..] Resolving image tag + project number" -ForegroundColor Cyan
$SHA = (git -C $ROOT rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) { $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss") }
$SHA = $SHA.Trim()
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($PNUM)) { Die "project number came back empty" }
Write-Host "[OK] image tag = $SHA ; project number = $PNUM"

# --- Step 1: APIs (no bigquery -- RHE is API-live) ---------------------------
Write-Host "[..] Enabling required APIs" -ForegroundColor Cyan
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
    storage.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com --project $PROJECT
Must "enable required APIs"
Write-Host "[OK] APIs enabled"

# --- Step 2: AR repo + private bucket (no dataset) ---------------------------
Write-Host "[..] Ensuring AR repo / private bucket" -ForegroundColor Cyan
if (Exists { gcloud artifacts repositories describe $REPO --location $REGION --project $PROJECT }) { Write-Host "    AR repo $REPO already exists" }
else { gcloud artifacts repositories create $REPO --repository-format docker --location $REGION --project $PROJECT --description "Agora Data Driven shared docker images"; Must "create AR repo" }
# The bucket is ALWAYS private. DASH_OPEN only controls whether the SERVICE asks for a password.
if (Exists { gcloud storage buckets describe "gs://$BUCKET" --project $PROJECT }) { Write-Host "    bucket $BUCKET already exists" }
else { gcloud storage buckets create "gs://$BUCKET" --project $PROJECT --location $REGION --uniform-bucket-level-access --public-access-prevention; Must "create bucket $BUCKET" }
Write-Host "[OK] AR repo / bucket in place"

# --- Step 3: service accounts + least-privilege IAM --------------------------
Write-Host "[..] Ensuring service accounts + IAM" -ForegroundColor Cyan
Ensure-Sa $JOB_SA "$KEY-dash-job" "RHE export job (Windsor + ActiveCampaign -> bucket)"
Ensure-Sa $WEB_SA "$KEY-dash-web" "RHE dash web (bucket reader + auth)"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$JOB_SA" --role "roles/storage.objectAdmin"; Must "grant objectAdmin to job SA"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$WEB_SA" --role "roles/storage.objectViewer"; Must "grant objectViewer to web SA"
Write-Host "[OK] service accounts + IAM in place"

# --- Step 4: secrets ---------------------------------------------------------
Write-Host "[..] Ensuring secrets" -ForegroundColor Cyan
$Password          = Read-Secret $Password          "DASH_PASSWORD"          "Dashboard password for '$CLIENT'"
$WindsorKey        = Read-Secret $WindsorKey        "WINDSOR_API_KEY"        "Windsor.ai API key for '$CLIENT'"
$ActiveCampaignKey = Read-Secret $ActiveCampaignKey "ACTIVECAMPAIGN_API_KEY" "ActiveCampaign Api-Token for '$CLIENT'"
if ([string]::IsNullOrEmpty($Password))          { Die "no dashboard password supplied" }
if ([string]::IsNullOrEmpty($WindsorKey))        { Die "no Windsor API key supplied" }
if ([string]::IsNullOrEmpty($ActiveCampaignKey)) { Die "no ActiveCampaign Api-Token supplied" }

$rng = [Security.Cryptography.RandomNumberGenerator]::Create(); $bytes = New-Object byte[] 32; $rng.GetBytes($bytes)
$SessionKey = [Convert]::ToBase64String($bytes)

$tmpPw  = Join-Path ([IO.Path]::GetTempPath()) ("agora-pw-"  + [Guid]::NewGuid().ToString("N") + ".txt")
$tmpKey = Join-Path ([IO.Path]::GetTempPath()) ("agora-key-" + [Guid]::NewGuid().ToString("N") + ".txt")
$tmpWin = Join-Path ([IO.Path]::GetTempPath()) ("agora-win-" + [Guid]::NewGuid().ToString("N") + ".txt")
$tmpAc  = Join-Path ([IO.Path]::GetTempPath()) ("agora-ac-"  + [Guid]::NewGuid().ToString("N") + ".txt")
Write-SecretFile $tmpPw $Password; Write-SecretFile $tmpKey $SessionKey
Write-SecretFile $tmpWin $WindsorKey; Write-SecretFile $tmpAc $ActiveCampaignKey
try {
    # secret -> which SA may read it
    $secrets = @(
        @{ name=$PW_SECRET;  file=$tmpPw;  reader=$WEB_SA },
        @{ name=$KEY_SECRET; file=$tmpKey; reader=$WEB_SA },
        @{ name=$WIN_SECRET; file=$tmpWin; reader=$JOB_SA },
        @{ name=$AC_SECRET;  file=$tmpAc;  reader=$JOB_SA }
    )
    foreach ($s in $secrets) {
        if (Exists { gcloud secrets describe $s.name --project $PROJECT }) {
            gcloud secrets versions add $s.name --project $PROJECT --data-file="$($s.file)"; Must "add version to $($s.name)"
        } else {
            gcloud secrets create $s.name --project $PROJECT --replication-policy=automatic --data-file="$($s.file)"; Must "create secret $($s.name)"
        }
        gcloud secrets add-iam-policy-binding $s.name --project $PROJECT --member "serviceAccount:$($s.reader)" --role "roles/secretmanager.secretAccessor"; Must "grant accessor on $($s.name)"
    }
} finally { Remove-Item $tmpPw, $tmpKey, $tmpWin, $tmpAc -ErrorAction SilentlyContinue }
Write-Host "[OK] secrets created + readers granted"

# --- Step 5: build + deploy + run the export job -----------------------------
Write-Host "[..] Building + deploying export job $EXPORT_JOB" -ForegroundColor Cyan
$jobImg = "$AR_HOST/$PROJECT/$REPO/${EXPORT_JOB}:$SHA"
gcloud builds submit $JOB_DIR --tag $jobImg --project $PROJECT; Must "build export job image"
# task-timeout 3600: ActiveCampaign caps `limit` at 100, so the first run pages ~540 send pages
# plus the contact and event crawls. Later runs are incremental off the stored watermark and
# take a couple of minutes. memory 2Gi: the payload holds ~7k Meta rows + ~4.9k contacts.
# `WINDSOR_ACCOUNTS` is itself a COMMA-SEPARATED list, and --set-env-vars splits on commas — so
# the default delimiter makes gcloud read `facebook__744718258097253` as a second key and fail
# ("Bad syntax for dict arg"). The leading `^@^` switches the delimiter to `@`. Honeytribe never
# hit this because it has a single ad account.
gcloud run jobs deploy $EXPORT_JOB --image $jobImg --region $REGION --project $PROJECT `
    --service-account $JOB_SA --max-retries 1 --task-timeout 3600 --memory 2Gi `
    --set-env-vars "^@^GCS_BUCKET=$BUCKET@DATA_OBJECT=$DATA_OBJECT@WINDSOR_ACCOUNTS=$WINDSOR_ACCOUNTS@ACTIVECAMPAIGN_URL=$ACTIVECAMPAIGN_URL@PYTHONUNBUFFERED=1@FORCE_IPV4=1" `
    --set-secrets "WINDSOR_API_KEY=${WIN_SECRET}:latest,ACTIVECAMPAIGN_API_KEY=${AC_SECRET}:latest"
Must "deploy export job $EXPORT_JOB"
if (-not $SkipJobRun) {
    gcloud run jobs execute $EXPORT_JOB --region $REGION --project $PROJECT --wait; Must "execute export job (initial)"
    Write-Host "[OK] initial live data export complete"
}

# --- Step 6: make the job triggerable ----------------------------------------
# This is what powers the dashboard's Sync button, which POSTs :run WITH env overrides. That
# needs run.jobs.runWithOverrides, which roles/run.invoker does NOT carry -- an invoker-only
# grant 403s while the IAM policy looks correct (it left riverdance stale for 13 days). So grant
# run.developer. Load-bearing: without it the Sync button silently does nothing.
$PORTAL_SA = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
Write-Host "[..] Granting run.developer on $EXPORT_JOB (Sync button + portal sync)" -ForegroundColor Cyan
gcloud run jobs add-iam-policy-binding $EXPORT_JOB --region $REGION --project $PROJECT --member "serviceAccount:$PORTAL_SA" --role "roles/run.developer"; Must "grant run.developer to portal SA"
gcloud run jobs add-iam-policy-binding $EXPORT_JOB --region $REGION --project $PROJECT --member "serviceAccount:$WEB_SA" --role "roles/run.developer"; Must "grant run.developer to web SA"
Write-Host "[OK] $EXPORT_JOB is triggerable"

# --- Step 6b: scheduled refresh is OFF by default ----------------------------
# The client's decision (2026-07-28): refresh happens through the dashboard's **Sync button**,
# not on a timer. Each run costs paid Windsor/Meta calls, so an unattended tick spends money on
# data nobody asked for. Pass -WithScheduler to turn the 6-hourly tick back on; without it this
# step actively REMOVES any scheduler left over from a previous standup, so re-running the
# script converges on the intended state rather than quietly leaving one behind.
$RUN_URI = "https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/${EXPORT_JOB}:run"
if ($WithScheduler) {
    Write-Host "[..] Ensuring 6-hourly scheduler $SCHED" -ForegroundColor Cyan
    if (Exists { gcloud scheduler jobs describe $SCHED --location $REGION --project $PROJECT }) {
        gcloud scheduler jobs update http $SCHED --location $REGION --project $PROJECT `
            --schedule "35 */6 * * *" --time-zone "Asia/Singapore" --uri $RUN_URI --http-method POST `
            --oauth-service-account-email $WEB_SA; Must "update scheduler $SCHED"
    } else {
        gcloud scheduler jobs create http $SCHED --location $REGION --project $PROJECT `
            --schedule "35 */6 * * *" --time-zone "Asia/Singapore" --uri $RUN_URI --http-method POST `
            --oauth-service-account-email $WEB_SA; Must "create scheduler $SCHED"
    }
    Write-Host "[OK] $SCHED refreshes $EXPORT_JOB every 6h"
} elseif (Exists { gcloud scheduler jobs describe $SCHED --location $REGION --project $PROJECT }) {
    Write-Host "[..] Removing scheduler $SCHED (refresh is Sync-button only)" -ForegroundColor Yellow
    gcloud scheduler jobs delete $SCHED --location $REGION --project $PROJECT --quiet; Must "delete scheduler $SCHED"
    Write-Host "[OK] $SCHED removed"
} else {
    Write-Host "[OK] no scheduler (refresh is Sync-button only; -WithScheduler re-enables it)"
}

# --- Step 7: JS gate + build + deploy the dash service -----------------------
Write-Host "[..] Validating dashboard.html inline JS" -ForegroundColor Cyan
if (Test-Path $VALIDATOR) { & $VENV_PY $VALIDATOR (Join-Path $DASH_DIR "dashboard.html"); Must "dashboard.html failed JS gate" }
else { Write-Host "    (skipping JS gate: validator not found)" -ForegroundColor Yellow }

Write-Host "[..] Building + deploying dash service $WEB_SERVICE" -ForegroundColor Cyan
$webImg = "$AR_HOST/$PROJECT/$REPO/${WEB_SERVICE}:$SHA"
gcloud builds submit $DASH_DIR --tag $webImg --project $PROJECT; Must "build dash service image"
gcloud run deploy $WEB_SERVICE --image $webImg --region $REGION --project $PROJECT `
    --service-account $WEB_SA --no-invoker-iam-check --memory 512Mi `
    --set-env-vars "GCS_BUCKET=$BUCKET,DATA_OBJECT=$DATA_OBJECT,CLIENT_KEY=$KEY,REFRESH_JOB=$EXPORT_JOB,REGION=$REGION,GCP_PROJECT=$PROJECT,DASH_OPEN=1" `
    --set-secrets "SESSION_SECRET=${KEY_SECRET}:latest,DASH_PASSWORD=${PW_SECRET}:latest"
Must "deploy dash service $WEB_SERVICE"

$SVC_URL = (gcloud run services describe $WEB_SERVICE --region $REGION --project $PROJECT --format='value(status.url)')
Write-Host ""
Write-Host "[OK] RHE standup complete (tag $SHA)" -ForegroundColor Green
Write-Host "     dash service : $WEB_SERVICE"
Write-Host "     live URL     : $SVC_URL   (works immediately; no DNS needed)"
Write-Host "     access       : OPEN (DASH_OPEN=1) - no login. Set DASH_OPEN=0 to re-gate."
Write-Host "     export job   : $EXPORT_JOB   (live Windsor 3-account + ActiveCampaign pull)"
Write-Host "     refresh      : Sync button only - no scheduler ( -WithScheduler adds a 6h tick )"
Write-Host ""
Write-Host "     ROTATE the Windsor key and the ActiveCampaign token now - both were shared in a" -ForegroundColor Yellow
Write-Host "     document. Re-run this script with the new values to roll the secrets." -ForegroundColor Yellow
Write-Host "     next (optional): map $CLIENT.agoradatadriven.com -> $WEB_SERVICE, then tools\enable_platform_sso.ps1 -Keys $CLIENT"
