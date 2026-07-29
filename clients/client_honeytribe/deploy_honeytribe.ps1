# =============================================================================
# deploy_honeytribe.ps1 -- ONE-SHOT, IDEMPOTENT standup of the `honeytribe` client.
#
# Honey Tribe is a Windsor/Shopify-LIVE client (NOT BigQuery-fed): the export job pulls
# the Meta connector from Windsor.ai AND the Shopify Admin API directly each run, and
# writes the private honeytribe.json the gated dash service serves. So this standup has
# NO dataset and NO SQL views -- instead it stores the two API keys as secrets the job reads.
#
# Creates / converges:
#   APIs -> AR repo + private bucket -> job/web service accounts + IAM
#   -> password/session/windsor/shopify secrets -> export job (build/deploy/run)
#   -> run.developer on the export job (the ONLY thing that makes the Sync button work)
#   -> dash web service (private + app-level auth).
#
# NO Cloud Scheduler by default: refresh is Sync-button driven, because each run costs paid
# Windsor/Meta and Shopify calls. Running without -WithScheduler REMOVES any scheduler it finds.
#
# RUN AS YOURSELF (gcloud auth login info@agoradatadriven.com) -- never Cloud Build
# from a laptop. `gcloud builds submit --tag` builds the image (no actAs); the deploy
# runs AS YOU with the runtime SAs.
#
# USAGE
#   .\deploy_honeytribe.ps1 -Password "clientpw" -WindsorKey "xxx" -ShopifyToken "shpat_xxx"
#   (omit any to be prompted; they also read $env:DASH_PASSWORD / WINDSOR_API_KEY /
#    SHOPIFY_ACCESS_TOKEN)
#   -SkipJobRun      deploy without executing the export job (it takes ~90s)
#   -WithScheduler   also run a 6-hourly refresh tick (off by default; see above)
# =============================================================================

param(
    [string]$Password = "",
    [string]$WindsorKey = "",
    [string]$ShopifyToken = "",
    [switch]$SkipJobRun,
    # Refresh is Sync-button driven by the client's decision. Pass this to also run a 6-hourly
    # Cloud Scheduler tick; without it any existing scheduler is REMOVED.
    [switch]$WithScheduler
)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$REPO    = "agora"
$CLIENT  = "honeytribe"

# Derived names (DERIVE from the client key `<c>`; never re-type) --------------
$BUCKET      = "agora-data-driven-$CLIENT-dash"
$EXPORT_JOB  = "$CLIENT-export"
$SCHED       = "$CLIENT-export-6h"
$WEB_SERVICE = "$CLIENT-dash"
$JOB_SA      = "$CLIENT-dash-job@agora-data-driven.iam.gserviceaccount.com"
$WEB_SA      = "$CLIENT-dash-web@agora-data-driven.iam.gserviceaccount.com"
$PW_SECRET   = "$CLIENT-dash-password"
$KEY_SECRET  = "$CLIENT-dash-session-key"
$WIN_SECRET  = "$CLIENT-windsor-key"
$SHOP_SECRET = "$CLIENT-shopify-token"
$AR_HOST     = "$REGION-docker.pkg.dev"

# Client-specific connector config (NOT secrets -- account ids and hostnames).
$WINDSOR_ACCOUNT = "facebook__380023369290925"
$SHOPIFY_DOMAIN  = "midget-giraffe.myshopify.com"

$ROOT     = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$JOB_DIR  = Join-Path $PSScriptRoot "job"
$DASH_DIR = Join-Path $PSScriptRoot "dash"
$VALIDATOR = Join-Path $ROOT "tools\_validate_dash_js.py"
$VENV_PY = Join-Path $ROOT ".venv\Scripts\python.exe"
if (-not (Test-Path $VENV_PY)) { $VENV_PY = Join-Path $ROOT ".venv-portal\Scripts\python.exe" }

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

# --- Step 1: APIs (no bigquery -- Honey Tribe is Windsor/Shopify-live) --------
Write-Host "[..] Enabling required APIs" -ForegroundColor Cyan
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
    storage.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com --project $PROJECT
Must "enable required APIs"
Write-Host "[OK] APIs enabled"

# --- Step 2: AR repo + private bucket (no dataset) ---------------------------
Write-Host "[..] Ensuring AR repo / private bucket" -ForegroundColor Cyan
if (Exists { gcloud artifacts repositories describe $REPO --location $REGION --project $PROJECT }) { Write-Host "    AR repo $REPO already exists" }
else { gcloud artifacts repositories create $REPO --repository-format docker --location $REGION --project $PROJECT --description "Agora Data Driven shared docker images"; Must "create AR repo" }
if (Exists { gcloud storage buckets describe "gs://$BUCKET" --project $PROJECT }) { Write-Host "    bucket $BUCKET already exists" }
else { gcloud storage buckets create "gs://$BUCKET" --project $PROJECT --location $REGION --uniform-bucket-level-access --public-access-prevention; Must "create bucket $BUCKET" }
Write-Host "[OK] AR repo / bucket in place"

# --- Step 3: service accounts + least-privilege IAM --------------------------
Write-Host "[..] Ensuring service accounts + IAM" -ForegroundColor Cyan
Ensure-Sa $JOB_SA "$CLIENT-dash-job" "honeytribe export job (Windsor+Shopify -> bucket)"
Ensure-Sa $WEB_SA "$CLIENT-dash-web" "honeytribe dash web (bucket reader + auth)"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$JOB_SA" --role "roles/storage.objectAdmin"; Must "grant objectAdmin to job SA"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$WEB_SA" --role "roles/storage.objectViewer"; Must "grant objectViewer to web SA"
Write-Host "[OK] service accounts + IAM in place"

# --- Step 4: secrets ---------------------------------------------------------
Write-Host "[..] Ensuring secrets" -ForegroundColor Cyan
$Password     = Read-Secret $Password     "DASH_PASSWORD"        "Dashboard password for '$CLIENT'"
$WindsorKey   = Read-Secret $WindsorKey   "WINDSOR_API_KEY"      "Windsor.ai API key for '$CLIENT'"
$ShopifyToken = Read-Secret $ShopifyToken "SHOPIFY_ACCESS_TOKEN" "Shopify Admin token for '$CLIENT'"
if ([string]::IsNullOrEmpty($Password))     { Die "no dashboard password supplied" }
if ([string]::IsNullOrEmpty($WindsorKey))   { Die "no Windsor API key supplied" }
if ([string]::IsNullOrEmpty($ShopifyToken)) { Die "no Shopify token supplied" }

$rng = [Security.Cryptography.RandomNumberGenerator]::Create(); $bytes = New-Object byte[] 32; $rng.GetBytes($bytes)
$SessionKey = [Convert]::ToBase64String($bytes)

$tmpPw   = Join-Path ([IO.Path]::GetTempPath()) ("agora-pw-"   + [Guid]::NewGuid().ToString("N") + ".txt")
$tmpKey  = Join-Path ([IO.Path]::GetTempPath()) ("agora-key-"  + [Guid]::NewGuid().ToString("N") + ".txt")
$tmpWin  = Join-Path ([IO.Path]::GetTempPath()) ("agora-win-"  + [Guid]::NewGuid().ToString("N") + ".txt")
$tmpShop = Join-Path ([IO.Path]::GetTempPath()) ("agora-shop-" + [Guid]::NewGuid().ToString("N") + ".txt")
Write-SecretFile $tmpPw $Password; Write-SecretFile $tmpKey $SessionKey
Write-SecretFile $tmpWin $WindsorKey; Write-SecretFile $tmpShop $ShopifyToken
try {
    # secret -> which SA may read it
    $secrets = @(
        @{ name=$PW_SECRET;   file=$tmpPw;   reader=$WEB_SA },
        @{ name=$KEY_SECRET;  file=$tmpKey;  reader=$WEB_SA },
        @{ name=$WIN_SECRET;  file=$tmpWin;  reader=$JOB_SA },
        @{ name=$SHOP_SECRET; file=$tmpShop; reader=$JOB_SA }
    )
    foreach ($s in $secrets) {
        if (Exists { gcloud secrets describe $s.name --project $PROJECT }) {
            gcloud secrets versions add $s.name --project $PROJECT --data-file="$($s.file)"; Must "add version to $($s.name)"
        } else {
            gcloud secrets create $s.name --project $PROJECT --replication-policy=automatic --data-file="$($s.file)"; Must "create secret $($s.name)"
        }
        gcloud secrets add-iam-policy-binding $s.name --project $PROJECT --member "serviceAccount:$($s.reader)" --role "roles/secretmanager.secretAccessor"; Must "grant accessor on $($s.name)"
    }
} finally { Remove-Item $tmpPw, $tmpKey, $tmpWin, $tmpShop -ErrorAction SilentlyContinue }
Write-Host "[OK] secrets created + readers granted"

# --- Step 5: build + deploy + run the export job -----------------------------
Write-Host "[..] Building + deploying export job $EXPORT_JOB" -ForegroundColor Cyan
$jobImg = "$AR_HOST/$PROJECT/$REPO/${EXPORT_JOB}:$SHA"
gcloud builds submit $JOB_DIR --tag $jobImg --project $PROJECT; Must "build export job image"
# task-timeout 1800: the Shopify pull pages through ~9,300 orders (~38 pages, politely paced).
gcloud run jobs deploy $EXPORT_JOB --image $jobImg --region $REGION --project $PROJECT `
    --service-account $JOB_SA --max-retries 1 --task-timeout 1800 --memory 1Gi `
    --set-env-vars "GCS_BUCKET=$BUCKET,DATA_OBJECT=$CLIENT.json,WINDSOR_ACCOUNT=$WINDSOR_ACCOUNT,SHOPIFY_STORE_DOMAIN=$SHOPIFY_DOMAIN,PYTHONUNBUFFERED=1" `
    --set-secrets "WINDSOR_API_KEY=${WIN_SECRET}:latest,SHOPIFY_ACCESS_TOKEN=${SHOP_SECRET}:latest"
Must "deploy export job $EXPORT_JOB"
if (-not $SkipJobRun) {
    gcloud run jobs execute $EXPORT_JOB --region $REGION --project $PROJECT --wait; Must "execute export job (initial)"
    Write-Host "[OK] initial live data export complete"
}

# --- Step 6: let the Sync button trigger this job ----------------------------
# With no scheduler, the dashboard's Sync button is the ONLY refresh path -- so this grant is
# load-bearing, not a nicety. It POSTs :run WITH env overrides, which needs
# run.jobs.runWithOverrides; roles/run.invoker does NOT carry it, and an invoker-only grant makes
# every trigger 403 while the IAM policy looks correct (it left riverdance stale for 13 days).
# So grant run.developer. The portal SA gets it too, in case this client is ever registered.
$PORTAL_SA = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
Write-Host "[..] Granting run.developer on $EXPORT_JOB (portal sync + Sync button)" -ForegroundColor Cyan
gcloud run jobs add-iam-policy-binding $EXPORT_JOB --region $REGION --project $PROJECT --member "serviceAccount:$PORTAL_SA" --role "roles/run.developer"; Must "grant run.developer to portal SA"
gcloud run jobs add-iam-policy-binding $EXPORT_JOB --region $REGION --project $PROJECT --member "serviceAccount:$WEB_SA" --role "roles/run.developer"; Must "grant run.developer to web SA"
Write-Host "[OK] $EXPORT_JOB is triggerable"

# --- Step 6b: refresh policy --------------------------------------------------
# NO scheduler by the client's decision (2026-07-29): refresh runs off the dashboard's Sync
# button, because every export costs paid Windsor/Meta and Shopify calls and a timer would burn
# them on data nobody asked for. Honey Tribe is not in the portal registry either, so the
# platform-wide `sync-refresh` never reaches it -- the Sync button really is the only trigger,
# which is what the run.developer grant above exists for.
#
# Pass -WithScheduler to turn a 6-hourly tick back on (it impersonates the WEB SA, not the
# cloudscheduler service agent, which owners cannot actAs). Without it this step actively REMOVES
# any scheduler left over from a previous standup, so re-running converges on the intended state
# instead of quietly leaving one behind.
$RUN_URI = "https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/${EXPORT_JOB}:run"
if ($WithScheduler) {
    Write-Host "[..] Ensuring 6-hourly scheduler $SCHED" -ForegroundColor Cyan
    if (Exists { gcloud scheduler jobs describe $SCHED --location $REGION --project $PROJECT }) {
        gcloud scheduler jobs update http $SCHED --location $REGION --project $PROJECT `
            --schedule "20 */6 * * *" --time-zone "Asia/Singapore" --uri $RUN_URI --http-method POST `
            --oauth-service-account-email $WEB_SA; Must "update scheduler $SCHED"
    } else {
        gcloud scheduler jobs create http $SCHED --location $REGION --project $PROJECT `
            --schedule "20 */6 * * *" --time-zone "Asia/Singapore" --uri $RUN_URI --http-method POST `
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
if ((Test-Path $VENV_PY) -and (Test-Path $VALIDATOR)) { & $VENV_PY $VALIDATOR (Join-Path $DASH_DIR "dashboard.html"); Must "dashboard.html failed JS gate" }
else { Write-Host "    (skipping JS gate: validator/python not found)" -ForegroundColor Yellow }

Write-Host "[..] Building + deploying dash service $WEB_SERVICE" -ForegroundColor Cyan
$webImg = "$AR_HOST/$PROJECT/$REPO/${WEB_SERVICE}:$SHA"
gcloud builds submit $DASH_DIR --tag $webImg --project $PROJECT; Must "build dash service image"
gcloud run deploy $WEB_SERVICE --image $webImg --region $REGION --project $PROJECT `
    --service-account $WEB_SA --no-invoker-iam-check --memory 512Mi `
    --set-env-vars "GCS_BUCKET=$BUCKET,DATA_OBJECT=$CLIENT.json,CLIENT_KEY=$CLIENT,REFRESH_JOB=$EXPORT_JOB,REGION=$REGION,GCP_PROJECT=$PROJECT,DASH_OPEN=1" `
    --set-secrets "SESSION_SECRET=${KEY_SECRET}:latest,DASH_PASSWORD=${PW_SECRET}:latest"
Must "deploy dash service $WEB_SERVICE"

$SVC_URL = (gcloud run services describe $WEB_SERVICE --region $REGION --project $PROJECT --format='value(status.url)')
Write-Host ""
Write-Host "[OK] honeytribe standup complete (tag $SHA)" -ForegroundColor Green
Write-Host "     dash service : $WEB_SERVICE"
Write-Host "     live URL     : $SVC_URL   (works immediately; no DNS needed)"
Write-Host "     access       : OPEN (DASH_OPEN=1) - no login. Set DASH_OPEN=0 to re-gate."
Write-Host "     export job   : $EXPORT_JOB   (live Windsor + Shopify pull)"
Write-Host "     refresh      : Sync button only - no scheduler ( -WithScheduler adds a 6h tick )"
Write-Host "     next (optional): map $CLIENT.agoradatadriven.com -> $WEB_SERVICE, then tools\enable_platform_sso.ps1 -Keys $CLIENT"
