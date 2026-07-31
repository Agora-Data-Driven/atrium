# =============================================================================
# deploy_assistant_reindex.ps1 -- build/deploy/schedule the Assistant index rebuild
#   Cloud Run JOB `assistant-reindex` (the FULL rebuild, off the request path).
#
# Mirrors deploy_mail_refresh.ps1 / deploy_intel_refresh.ps1: ADDITIVE and infra-light.
#   * REUSES the platform-dash image -- it just runs `python assistant_reindex.py`.
#   * RUNS AS the existing platform-dash-web SA (objectAdmin on the registry bucket,
#     where the workspaces, watcher archives and assistant indexes all live).
#   * The ONE new piece is the Cloud Scheduler job + its IAM (same shape as mail).
#
# WHY (2026-07-31): a full rebuild re-chunks every source and re-embeds the whole
# corpus -- ~344s and a multi-hundred-MB peak on a large workspace. Inside a request
# that blew both the 512 MiB memory limit and the 300s request timeout, and because
# the index is written LAST it persisted nothing, so every later ask retried the same
# doomed work and the Assistant stayed permanently dead. This job is where that work
# belongs. main._assistant_index now serves the existing index and queues the client.
#
# 🔴 MEMORY/TIMEOUT ARE THE POINT -- 2Gi + a 3600s task timeout. Do not trim them to
# match the other jobs: this is the one that actually holds the whole corpus.
#
# The Rebuild button triggers this job per-client via sync_dash.trigger_job with an
# env override, which needs run.jobs.runWithOverrides -- roles/run.invoker does NOT
# carry it, so the web SA is granted roles/run.developer ON THIS JOB below.
#
# GATED: the job is a logged no-op unless ASSISTANT_REINDEX_ENABLED=1, which this
# script sets. Turn it OFF with -Disable.
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop (build-only submit is fine).
#
# USAGE
#   .\deploy_assistant_reindex.ps1             # build, deploy, schedule (every 15 min)
#   .\deploy_assistant_reindex.ps1 -SkipBuild  # reuse current image, redeploy
#   .\deploy_assistant_reindex.ps1 -Run        # also execute the job once now
#   .\deploy_assistant_reindex.ps1 -Sweep      # execute a full --sweep now (post version bump)
#   .\deploy_assistant_reindex.ps1 -Disable    # deploy with the feature OFF
# =============================================================================

param([switch]$SkipBuild, [switch]$Run, [switch]$Sweep, [switch]$Disable)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT  = "agora-data-driven"
$REGION   = "asia-southeast1"   # Singapore. One region, never another.
$REPO     = "agora"             # shared Artifact Registry docker repo
$PLATFORM = "platform-dash"     # we reuse this service's image for the job
$JOB      = "assistant-reindex"
$WEB_SA   = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
$BUCKET   = "agora-data-driven-platform-dash"   # PRIVATE registry bucket
$CRON     = "*/15 * * * *"      # every 15 min -- picks up clients the ask path queued.
                                # Cheap: a tick with nothing queued only reads the small
                                # workspace JSONs (the flag), never the indexes themselves.

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
# Step 2.5 -- Env assembly. Embeddings MUST match the service's settings or the job
#             would write an index the app immediately treats as stale.
#             VERTEX_EMBED_LOCATION is region-pinned so private chunk text stays in-region.
# =============================================================================
$ENV_VARS = "REGISTRY_BUCKET=$BUCKET,REGISTRY_OBJECT=platform.json,WORKSPACE_BUCKET=$BUCKET,ASSISTANT_REINDEX_ENABLED=$ENABLED,ASSISTANT_EMBED_ENABLED=1,VERTEX_EMBED_LOCATION=$REGION,VERTEX_GEMINI_ENABLED=1,VERTEX_PROJECT=$PROJECT,VERTEX_LOCATION=global"

$secretPairs = @()
foreach ($sec in @("DEEPSEEK_API_KEY", "KIMI_API_KEY")) {
    # Upper-case KIMI_API_KEY is the SERVICES key; the lower-case `kimi-api-key` secret is the
    # VS Code / Claude Code launcher key (a different value) -- never mount that one here.
    gcloud secrets describe $sec --project $PROJECT *> $null
    if ($LASTEXITCODE -eq 0) {
        gcloud secrets add-iam-policy-binding $sec `
            --project $PROJECT `
            --member "serviceAccount:$WEB_SA" `
            --role "roles/secretmanager.secretAccessor" *> $null
        Must "grant secretAccessor on $sec"
        $secretPairs += "${sec}=${sec}:latest"
        Write-Host "[OK] will mount $sec"
    } else {
        Write-Host "[..] $sec not found -- that provider's video summaries unavailable (Gemini still works)" -ForegroundColor Yellow
    }
}

# =============================================================================
# Step 3 -- Deploy the Cloud Run JOB AS YOURSELF, overriding the entrypoint.
# =============================================================================
Write-Host "[..] Deploying Cloud Run job $JOB (ASSISTANT_REINDEX_ENABLED=$ENABLED)" -ForegroundColor Cyan
$deployArgs = @(
    "run", "jobs", "deploy", $JOB,
    "--image", $IMG,
    "--region", $REGION,
    "--project", $PROJECT,
    "--service-account", $WEB_SA,
    "--command", "python",
    "--args", "assistant_reindex.py",
    "--memory", "2Gi",          # 🔴 the rebuild holds the whole corpus -- see the header
    "--cpu", "1",
    "--max-retries", "1",
    "--task-timeout", "3600",   # 🔴 a full re-embed measured ~344s; leave generous headroom
    "--set-env-vars", $ENV_VARS
)
if ($secretPairs.Count -gt 0) {
    $deployArgs += @("--set-secrets", ($secretPairs -join ","))
}
gcloud @deployArgs
Must "deploy Cloud Run job $JOB"
Write-Host "[OK] deployed $JOB"

# =============================================================================
# Step 4 -- IAM. Same shape as mail-refresh, PLUS run.developer so the app's Rebuild
#           button can trigger the job WITH env overrides (run.invoker cannot).
# =============================================================================
$DEPLOYER = (gcloud config get-value account 2>$null); $DEPLOYER = ($DEPLOYER | Out-String).Trim()

Write-Host "[..] Granting scheduler agent tokenCreator on $WEB_SA" -ForegroundColor Cyan
gcloud iam service-accounts add-iam-policy-binding $WEB_SA `
    --project $PROJECT `
    --member "serviceAccount:$SCHED_AGENT" `
    --role "roles/iam.serviceAccountTokenCreator"
Must "grant serviceAccountTokenCreator to scheduler agent on $WEB_SA"

# 🔴 run.developer, NOT run.invoker: the Rebuild button POSTs :run WITH containerOverrides
# (REINDEX_CLIENT=<key>), which requires run.jobs.runWithOverrides. Granting only invoker makes
# every trigger 403 while the IAM policy looks correct.
Write-Host "[..] Granting run.developer to the web SA on $JOB (needed for run-with-overrides)" -ForegroundColor Cyan
gcloud run jobs add-iam-policy-binding $JOB `
    --region $REGION `
    --project $PROJECT `
    --member "serviceAccount:$WEB_SA" `
    --role "roles/run.developer"
Must "grant run.developer on $JOB"

if ($DEPLOYER) {
    Write-Host "[..] Granting $DEPLOYER actAs on $WEB_SA (needed to create the scheduler job)" -ForegroundColor Cyan
    gcloud iam service-accounts add-iam-policy-binding $WEB_SA `
        --project $PROJECT `
        --member "user:$DEPLOYER" `
        --role "roles/iam.serviceAccountUser" *> $null
}

# =============================================================================
# Step 5 -- Create-or-update the Cloud Scheduler HTTP job (the queued-client tick).
# =============================================================================
$sched   = "$JOB-tick"
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
# Step 6 -- Optional immediate execution.
# =============================================================================
if ($Sweep) {
    Write-Host "[..] Executing $JOB with --sweep (inspects EVERY stored index)" -ForegroundColor Cyan
    gcloud run jobs execute $JOB --region $REGION --project $PROJECT --wait `
        --args "assistant_reindex.py,--sweep"
    Must "execute job $JOB --sweep"
    Write-Host "[OK] swept $JOB"
} elseif ($Run) {
    Write-Host "[..] Executing $JOB once" -ForegroundColor Cyan
    gcloud run jobs execute $JOB --region $REGION --project $PROJECT
    Must "execute job $JOB"
    Write-Host "[OK] executed $JOB"
}

Write-Host ""
Write-Host "[OK] assistant-reindex deploy complete (tag $SHA, enabled=$ENABLED)" -ForegroundColor Green
