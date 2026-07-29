# deploy_tcs_ingest.ps1 -- build / deploy / schedule / run the TCS direct-API ingest Cloud Run jobs.
#
# Standalone deployer for the SEVEN TCS loaders. It mirrors the per-job logic of
# tools/deploy_ingest_jobs.ps1 but for these direct-API (non-Windsor) jobs, so they can be deployed
# the proper way (real Cloud Run jobs + daily scheduler) without depending on the shared $JOBS array.
# Each loader reads its OWN key from Secret Manager by id (tcs-shopify-token / tcs-klaviyo-key /
# tcs-tapfiliate-key) via ADC, so no --set-secrets mounting is needed here -- the runtime SA just
# needs secretAccessor on those secrets (granted by tcs_provision_secrets.ps1). The quiz loader reads
# the Google Sheet via ADC (the sheet must be shared with the runtime SA).
#
#   tcs-shopify            orders + marketing attribution   -> raw_windsor.tcs_shopify_orders
#   tcs-klaviyo            per-recipient email events       -> raw_windsor.tcs_klaviyo_events
#   tcs-quiz               Business-Quiz submissions        -> raw_windsor.tcs_quiz
#   tcs-sessions           storefront traffic (ShopifyQL)   -> raw_windsor.tcs_shopify_sessions
#   tcs-klaviyo-profiles   people + CLV / churn scores      -> raw_windsor.tcs_klaviyo_profiles
#   tcs-klaviyo-campaigns  campaign stats + attributed rev  -> raw_windsor.tcs_klaviyo_campaigns
#   tcs-affiliates         Tapfiliate roster + conversions  -> raw_windsor.tcs_affiliate*
#
# BULK BACKFILL: the events loader shards across Cloud Run tasks, so a first-time or catch-up load
# is much faster launched directly with several tasks (each takes a stride of the MISSING months):
#   gcloud run jobs execute tcs-klaviyo-ingest --region asia-southeast1 --tasks 12 --async
#
# RUN AS YOURSELF (build via `gcloud builds submit --tag`, then deploy the job).
# USAGE:  .\deploy_tcs_ingest.ps1 [-Only tcs-klaviyo] [-SkipBuild] [-Run]

param([string]$Only="", [switch]$SkipBuild, [switch]$Run)

$ErrorActionPreference = "Continue"
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$REPO    = "agora"
$SA      = "ingest-runner@agora-data-driven.iam.gserviceaccount.com"
$RAW_DATASET = "raw_windsor"
$AR_HOST = "$REGION-docker.pkg.dev"

function Die([string]$m){ Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }
function Must([string]$w){ if ($LASTEXITCODE -ne 0){ Die "$w (exit $LASTEXITCODE)" } }

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # services/ingest -> repo root

# Per-job env beyond the shared GCP_PROJECT/RAW_DATASET (the loaders read secrets from SM directly).
#
# BACKFILL_START is 'YYYY-MM' (or 'YYYY-MM-DD' for sessions) and is each loader's FLOOR, not a
# window length -- every one of these loaders checkpoints on per-period COVERAGE, so it simply
# keeps filling whatever is still missing between the floor and now, run after run. The floors
# are the real start of each source's history:
#     orders    2017-10  (first Shopify order, #1001)
#     sessions  2022-09  (Shopify sessions analytics goes no further back on this store)
#     events    2023-06  (first Klaviyo "Received Email")
#
# The cron times are staggered so the three Klaviyo jobs never collide on the API rate limit.
$JOBS = @(
  @{ key="tcs-shopify";           dir="services/ingest/tcs_shopify";           job="tcs-shopify-ingest";           mem="1Gi";   cpu="1"; cron="45 1 * * *"; env="SHOPIFY_STORE_DOMAIN=contractshop.myshopify.com,BACKFILL_START=2017-10" }
  @{ key="tcs-klaviyo";           dir="services/ingest/tcs_klaviyo";           job="tcs-klaviyo-ingest";           mem="1Gi";   cpu="1"; cron="50 1 * * *"; env="BACKFILL_START=2023-06" }
  @{ key="tcs-quiz";              dir="services/ingest/tcs_quiz";              job="tcs-quiz-ingest";              mem="512Mi"; cpu="1"; cron="55 1 * * *"; env="" }
  @{ key="tcs-sessions";          dir="services/ingest/tcs_sessions";          job="tcs-sessions-ingest";          mem="1Gi";   cpu="1"; cron="10 2 * * *"; env="SHOPIFY_STORE_DOMAIN=contractshop.myshopify.com,BACKFILL_START=2022-09-01" }
  @{ key="tcs-klaviyo-profiles";  dir="services/ingest/tcs_klaviyo_profiles";  job="tcs-klaviyo-profiles-ingest";  mem="1Gi";   cpu="1"; cron="20 2 * * *"; env="" }
  @{ key="tcs-klaviyo-campaigns"; dir="services/ingest/tcs_klaviyo_campaigns"; job="tcs-klaviyo-campaigns-ingest"; mem="1Gi";   cpu="1"; cron="35 2 * * *"; env="" }
  @{ key="tcs-affiliates";        dir="services/ingest/tcs_affiliates";        job="tcs-affiliates-ingest";        mem="512Mi"; cpu="1"; cron="45 2 * * *"; env="" }
)

Write-Host "[..] Resolving image tag + scheduler agent" -ForegroundColor Cyan
$SHA = (git -C $RepoRoot rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) { $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss") }
$SHA = $SHA.Trim()
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
$SCHED_AGENT = "service-$PNUM@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
Write-Host "[OK] tag=$SHA scheduler-agent=$SCHED_AGENT"

foreach ($J in $JOBS) {
    if ($Only -ne "" -and $Only -ne $J.key) { continue }
    $dir = Join-Path $RepoRoot $J.dir
    $job = $J.job
    $img = "$AR_HOST/$PROJECT/$REPO/${job}:$SHA"
    Write-Host ""
    Write-Host "=== $($J.key) -> $job ===" -ForegroundColor Cyan

    if (-not $SkipBuild) {
        if (-not (Test-Path $dir)) { Die "build dir not found: $dir" }
        Write-Host "[..] Building $img" -ForegroundColor Cyan
        gcloud builds submit $dir --tag $img --project $PROJECT
        Must "build image for $($J.key)"
    }

    $envVars = "GCP_PROJECT=$PROJECT,RAW_DATASET=$RAW_DATASET"
    if ($J.env -ne "") { $envVars = "$envVars,$($J.env)" }

    Write-Host "[..] Deploying job $job" -ForegroundColor Cyan
    gcloud run jobs deploy $job `
        --image $img --region $REGION --project $PROJECT `
        --service-account $SA `
        --memory $J.mem --cpu $J.cpu --max-retries 1 --task-timeout 3600 `
        --set-env-vars $envVars
    Must "deploy job $job"

    gcloud run jobs add-iam-policy-binding $job --region $REGION --project $PROJECT `
        --member "serviceAccount:$SA" --role "roles/run.invoker" *> $null
    Must "grant run.invoker on $job"

    $sched = "$job-daily"
    $run_uri = "https://$REGION-run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/${job}:run"
    gcloud scheduler jobs describe $sched --location $REGION --project $PROJECT *> $null
    if ($LASTEXITCODE -eq 0) {
        gcloud scheduler jobs update http $sched --location $REGION --project $PROJECT `
            --schedule "$($J.cron)" --time-zone "Asia/Singapore" --uri $run_uri `
            --http-method POST --oauth-service-account-email $SA *> $null
        Must "update scheduler $sched"
    } else {
        gcloud scheduler jobs create http $sched --location $REGION --project $PROJECT `
            --schedule "$($J.cron)" --time-zone "Asia/Singapore" --uri $run_uri `
            --http-method POST --oauth-service-account-email $SA *> $null
        Must "create scheduler $sched"
    }
    Write-Host "[OK] $job deployed + scheduled ($($J.cron))"

    if ($Run) {
        Write-Host "[..] Executing $job once" -ForegroundColor Cyan
        gcloud run jobs execute $job --region $REGION --project $PROJECT
        Must "execute $job"
        Write-Host "[OK] $job execution started"
    }
}
Write-Host ""
Write-Host "[done] TCS ingest jobs deployed." -ForegroundColor Green
