# Stand up / update the FULLY-IN-GCP daily Upwork refresh:
#   Cloud Run job `agora-upwork-refresh` (pull -> process -> upload) on a daily
#   Cloud Scheduler tick, 07:15 Asia/Manila. The dash service hot-reloads the
#   uploaded data within ~2 min (see dash/main.py _data_refresher) — the job
#   needs NO permissions on the service.
#
# One-time raw migration (idempotent): uploads raw_files/result.json (~944 MB),
# pull_state.json and pulled/*.jsonl into gs://<bucket>/raw/ so the job owns the
# whole pipeline state in the cloud. After this the laptop task is redundant.
#
# Usage:  .\deploy_job_agora.ps1          # migrate raw (if missing) + build + deploy + schedule
#         .\deploy_job_agora.ps1 -Run     # ...then execute the job once now and wait
#         .\deploy_job_agora.ps1 -SkipRaw # skip the raw-migration checks

param(
    [switch]$Run,
    [switch]$SkipRaw
)

$ErrorActionPreference = "Continue"   # gcloud writes progress to stderr; we check $LASTEXITCODE

$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$BUCKET  = "agora-data-driven-agora-dash"
$JOB     = "agora-upwork-refresh"
$SA      = "agora-dash-web@$PROJECT.iam.gserviceaccount.com"
$IMAGE   = "$REGION-docker.pkg.dev/$PROJECT/agora/$JOB"
$HERE    = $PSScriptRoot
$RAWDIR  = Join-Path $HERE "..\raw_files"

$env:CLOUDSDK_CORE_ACCOUNT = "info@agoradatadriven.com"

Write-Host "== $JOB deploy (project=$PROJECT region=$REGION) ==" -ForegroundColor Cyan

# -- 1) one-time raw migration to gs://$BUCKET/raw/ ---------------------------
if (-not $SkipRaw) {
    $null = gcloud storage objects describe "gs://$BUCKET/raw/result.json" --project $PROJECT 2>$null
    if ($LASTEXITCODE -ne 0) {
        $base = Join-Path $RAWDIR "result.json"
        if (-not (Test-Path $base)) { throw "raw/result.json not in bucket and no local base export to upload" }
        Write-Host "-- uploading base export (~944 MB, one-time) ..."
        gcloud storage cp $base "gs://$BUCKET/raw/result.json" --project $PROJECT
        if ($LASTEXITCODE -ne 0) { throw "base export upload failed" }
    } else { Write-Host "-- base export already in bucket" }

    $state = Join-Path $RAWDIR "pull_state.json"
    $null = gcloud storage objects describe "gs://$BUCKET/raw/pull_state.json" --project $PROJECT 2>$null
    if ($LASTEXITCODE -ne 0 -and (Test-Path $state)) {
        Write-Host "-- migrating pull_state.json + pulled/*.jsonl"
        gcloud storage cp $state "gs://$BUCKET/raw/pull_state.json" --project $PROJECT
        if ($LASTEXITCODE -ne 0) { throw "state upload failed" }
        $pulled = Join-Path $RAWDIR "pulled"
        if (Test-Path $pulled) {
            gcloud storage cp "$pulled\*.jsonl" "gs://$BUCKET/raw/pulled/" --project $PROJECT
            if ($LASTEXITCODE -ne 0) { throw "pulled/*.jsonl upload failed" }
        }
    } else { Write-Host "-- pull state already in bucket (or none locally)" }
}

# -- 2) IAM: job SA writes the bucket + reads the two telegram secrets --------
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --project $PROJECT `
    --member "serviceAccount:$SA" --role "roles/storage.objectAdmin" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "bucket objectAdmin binding failed" }
foreach ($secret in @("agora-telegram-api", "agora-telegram-session")) {
    gcloud secrets add-iam-policy-binding $secret --project $PROJECT `
        --member "serviceAccount:$SA" --role "roles/secretmanager.secretAccessor" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "secretAccessor binding failed for $secret" }
}

# -- 3) build (stage: job files + the two pipeline scripts from processing/) --
$stage = Join-Path $env:TEMP "agora_upwork_job_build"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item (Join-Path $HERE "main.py") $stage
Copy-Item (Join-Path $HERE "Dockerfile") $stage
Copy-Item (Join-Path $HERE "..\processing\telegram_pull.py") $stage
Copy-Item (Join-Path $HERE "..\processing\process_upwork.py") $stage
Write-Host "-- building $IMAGE"
gcloud builds submit $stage --tag $IMAGE --project $PROJECT
if ($LASTEXITCODE -ne 0) { throw "build failed" }
Remove-Item -Recurse -Force $stage

# -- 4) deploy the job (gcsfuse volume + secret files; 4Gi: sqlite in /tmp) ---
gcloud run jobs deploy $JOB --image $IMAGE --project $PROJECT --region $REGION `
    --service-account $SA --memory 4Gi --cpu 2 --task-timeout 3600 --max-retries 1 `
    --set-env-vars "DATA_BUCKET=$BUCKET" `
    --add-volume "name=data,type=cloud-storage,bucket=$BUCKET" `
    --add-volume-mount "volume=data,mount-path=/data" `
    --set-secrets "/secrets/api/telegram_api.json=agora-telegram-api:latest,/secrets/session/b64=agora-telegram-session:latest"
if ($LASTEXITCODE -ne 0) { throw "job deploy failed" }

# -- 5) scheduler: daily 07:15 Manila, impersonating the web SA ---------------
gcloud run jobs add-iam-policy-binding $JOB --project $PROJECT --region $REGION `
    --member "serviceAccount:$SA" --role "roles/run.invoker" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "job run.invoker binding failed" }
$uri = "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/${JOB}:run"
$null = gcloud scheduler jobs describe "$JOB-daily" --location $REGION --project $PROJECT 2>$null
$verb = "create"
if ($LASTEXITCODE -eq 0) { $verb = "update" }
gcloud scheduler jobs $verb http "$JOB-daily" --location $REGION --project $PROJECT `
    --schedule "15 7 * * *" --time-zone "Asia/Manila" `
    --uri $uri --http-method POST --oauth-service-account-email $SA
if ($LASTEXITCODE -ne 0) { throw "scheduler $verb failed" }

Write-Host "== deployed: job $JOB + scheduler $JOB-daily (07:15 Asia/Manila) ==" -ForegroundColor Green

if ($Run) {
    Write-Host "-- executing once now (waits for completion) ..."
    gcloud run jobs execute $JOB --project $PROJECT --region $REGION --wait
    if ($LASTEXITCODE -ne 0) { throw "job execution FAILED - check logs" }
    Write-Host "[OK] execution finished." -ForegroundColor Green
}
