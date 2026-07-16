# =============================================================================
# deploy_mail_refresh.ps1 -- build/deploy/schedule the HOURLY client-mail sync
#   Cloud Run JOB `mail-refresh` (the Atrium Mail tab's automatic pull).
#
# Mirrors deploy_intel_refresh.ps1: ADDITIVE and infra-light.
#   * REUSES the platform-dash image -- it just runs `python mail_refresh.py`.
#   * RUNS AS the existing platform-dash-web SA (objectAdmin on the registry bucket,
#     where the mailbox registry + workspaces + thread archives all live).
#   * The ONE new piece is the Cloud Scheduler job + its IAM (same shape as intel).
#
# Connectors (see mailroom.py):
#   * imap mailboxes work with NOTHING but this deploy.
#   * dwd (our Workspace domain) also needs the one-time enable_atrium_mail.ps1 --
#     this script auto-detects the mail-sync SA and sets MAIL_DWD_SA when it exists.
#
# GATED: the job is a logged no-op unless MAIL_SYNC_ENABLED=1, which this script
# sets. Turn the feature OFF with -Disable (or delete the scheduler job).
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop (build-only submit is fine).
#
# USAGE
#   .\deploy_mail_refresh.ps1            # build, deploy, schedule (hourly, on the hour)
#   .\deploy_mail_refresh.ps1 -SkipBuild # reuse current image, redeploy + reschedule
#   .\deploy_mail_refresh.ps1 -Run       # also execute the job once now
#   .\deploy_mail_refresh.ps1 -Disable   # deploy with the feature OFF (MAIL_SYNC_ENABLED=0)
# =============================================================================

param([switch]$SkipBuild, [switch]$Run, [switch]$Disable)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"   # Singapore. One region, never another.
$REPO    = "agora"             # shared Artifact Registry docker repo
$PLATFORM = "platform-dash"    # we reuse this service's image for the job
$JOB     = "mail-refresh"
$WEB_SA  = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
$MAIL_SA = "mail-sync@agora-data-driven.iam.gserviceaccount.com"
$BUCKET  = "agora-data-driven-platform-dash"   # PRIVATE registry bucket
$CRON    = "0 * * * *"         # hourly, on the hour (SGT) -- fresh mail all day

$ENABLED = if ($Disable) { "0" } else { "1" }

# Default $ErrorActionPreference stays "Continue" (gcloud logs progress to stderr); gate via Must.
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) { if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" } }

$DASH_DIR = $PSScriptRoot

# =============================================================================
# Step 1 -- Image tag + build (build ONLY; we deploy ourselves below).
# =============================================================================
Write-Host "[..] Resolving image tag" -ForegroundColor Cyan
$SHA = (git -C $DASH_DIR rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) {
    $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss")
    Write-Host "    not a git repo; using fallback tag $SHA" -ForegroundColor Yellow
}
$SHA = $SHA.Trim()
$AR_HOST = "$REGION-docker.pkg.dev"
$IMG = "$AR_HOST/$PROJECT/$REPO/${PLATFORM}:$SHA"
Write-Host "[OK] image = $IMG"

if (-not $SkipBuild) {
    Write-Host "[..] Building image $IMG" -ForegroundColor Cyan
    gcloud builds submit $DASH_DIR --tag $IMG --project=$PROJECT
    Must "build image for $JOB"
    Write-Host "[OK] built $IMG"
} else {
    Write-Host "[..] -SkipBuild: deploying existing image $IMG" -ForegroundColor Yellow
}

# =============================================================================
# Step 2 -- Project number (NEVER hardcode) + the scheduler agent SA.
# =============================================================================
Write-Host "[..] Resolving project number" -ForegroundColor Cyan
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($PNUM)) { Die "project number came back empty" }
$SCHED_AGENT = "service-$PNUM@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
Write-Host "[OK] project number = $PNUM ; scheduler agent = $SCHED_AGENT"

# =============================================================================
# Step 2.5 -- Env assembly. AI summaries reuse the intel brain (Vertex Gemini via the
#             SA token + the optional DeepSeek secret); MAIL_DWD_SA rides along only
#             when enable_atrium_mail.ps1 has created the delegation SA.
# =============================================================================
$ENV_VARS = "REGISTRY_BUCKET=$BUCKET,REGISTRY_OBJECT=platform.json,WORKSPACE_BUCKET=$BUCKET,MAIL_SYNC_ENABLED=$ENABLED,VERTEX_GEMINI_ENABLED=1,VERTEX_PROJECT=$PROJECT,VERTEX_LOCATION=global"

gcloud iam service-accounts describe $MAIL_SA --project=$PROJECT *> $null
if ($LASTEXITCODE -eq 0) {
    $ENV_VARS += ",MAIL_DWD_SA=$MAIL_SA"
    Write-Host "[OK] mail-sync SA found -- Workspace (dwd) mailboxes enabled" -ForegroundColor Green
} else {
    Write-Host "[..] mail-sync SA absent -- imap mailboxes only (run enable_atrium_mail.ps1 for dwd)" -ForegroundColor Yellow
}

$secretPairs = @()
gcloud secrets describe "DEEPSEEK_API_KEY" --project $PROJECT *> $null
if ($LASTEXITCODE -eq 0) {
    gcloud secrets add-iam-policy-binding "DEEPSEEK_API_KEY" `
        --project $PROJECT `
        --member "serviceAccount:$WEB_SA" `
        --role "roles/secretmanager.secretAccessor" *> $null
    Must "grant secretAccessor on DEEPSEEK_API_KEY"
    $secretPairs += "DEEPSEEK_API_KEY=DEEPSEEK_API_KEY:latest"
    Write-Host "[OK] will mount DEEPSEEK_API_KEY"
} else {
    Write-Host "[..] DEEPSEEK_API_KEY not found -- DeepSeek summaries unavailable (Gemini still works)" -ForegroundColor Yellow
}

# =============================================================================
# Step 3 -- Deploy the Cloud Run JOB AS YOURSELF, overriding the entrypoint.
# =============================================================================
Write-Host "[..] Deploying Cloud Run job $JOB (MAIL_SYNC_ENABLED=$ENABLED)" -ForegroundColor Cyan
$deployArgs = @(
    "run", "jobs", "deploy", $JOB,
    "--image", $IMG,
    "--region", $REGION,
    "--project", $PROJECT,
    "--service-account", $WEB_SA,
    "--command", "python",
    "--args", "mail_refresh.py",
    "--memory", "512Mi",
    "--cpu", "1",
    "--max-retries", "1",
    "--task-timeout", "900",
    "--set-env-vars", $ENV_VARS
)
if ($secretPairs.Count -gt 0) {
    $deployArgs += @("--set-secrets", ($secretPairs -join ","))
}
gcloud @deployArgs
Must "deploy Cloud Run job $JOB"
Write-Host "[OK] deployed $JOB"

# =============================================================================
# Step 4 -- Scheduler IAM (identical shape to intel-refresh: the scheduler POSTs the
#           :run URI AS the web SA; see that script's comment for why not the agent).
# =============================================================================
$DEPLOYER = (gcloud config get-value account 2>$null); $DEPLOYER = ($DEPLOYER | Out-String).Trim()

Write-Host "[..] Granting scheduler agent tokenCreator on $WEB_SA" -ForegroundColor Cyan
gcloud iam service-accounts add-iam-policy-binding $WEB_SA `
    --project $PROJECT `
    --member "serviceAccount:$SCHED_AGENT" `
    --role "roles/iam.serviceAccountTokenCreator"
Must "grant serviceAccountTokenCreator to scheduler agent on $WEB_SA"

Write-Host "[..] Granting run.invoker to the web SA on $JOB" -ForegroundColor Cyan
gcloud run jobs add-iam-policy-binding $JOB `
    --region $REGION `
    --project $PROJECT `
    --member "serviceAccount:$WEB_SA" `
    --role "roles/run.invoker"
Must "grant run.invoker on $JOB"

if ($DEPLOYER) {
    Write-Host "[..] Granting $DEPLOYER actAs on $WEB_SA (needed to create the scheduler job)" -ForegroundColor Cyan
    gcloud iam service-accounts add-iam-policy-binding $WEB_SA `
        --project $PROJECT `
        --member "user:$DEPLOYER" `
        --role "roles/iam.serviceAccountUser" *> $null
}

# =============================================================================
# Step 5 -- Create-or-update the hourly Cloud Scheduler HTTP job.
# =============================================================================
$sched   = "$JOB-hourly"
$run_uri = "https://$REGION-run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/${JOB}:run"

gcloud scheduler jobs describe $sched --location $REGION --project $PROJECT *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[..] Updating scheduler job $sched ($CRON SGT)" -ForegroundColor Cyan
    gcloud scheduler jobs update http $sched `
        --location $REGION --project $PROJECT `
        --schedule "$CRON" --time-zone "Asia/Singapore" `
        --uri $run_uri --http-method POST `
        --oauth-service-account-email $WEB_SA
    Must "update scheduler job $sched"
} else {
    Write-Host "[..] Creating scheduler job $sched ($CRON SGT)" -ForegroundColor Cyan
    gcloud scheduler jobs create http $sched `
        --location $REGION --project $PROJECT `
        --schedule "$CRON" --time-zone "Asia/Singapore" `
        --uri $run_uri --http-method POST `
        --oauth-service-account-email $WEB_SA
    Must "create scheduler job $sched"
}
Write-Host "[OK] scheduled $sched"

# =============================================================================
# Step 6 -- -Run: execute the job once now (smoke run / first backfill).
# =============================================================================
if ($Run) {
    Write-Host "[..] Executing $JOB once" -ForegroundColor Cyan
    gcloud run jobs execute $JOB --region $REGION --project $PROJECT
    Must "execute job $JOB"
    Write-Host "[OK] executed $JOB"
}

Write-Host ""
Write-Host "[OK] mail-refresh deploy complete (tag $SHA, enabled=$ENABLED)" -ForegroundColor Green
