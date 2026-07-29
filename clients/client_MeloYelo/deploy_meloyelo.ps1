# =============================================================================
# deploy_meloyelo.ps1 -- ONE-SHOT, IDEMPOTENT standup of the `meloyelo` client.
#
# MeloYelo is an API-LIVE client (NOT BigQuery-fed): the export job pulls Unleashed
# (invoices/credits/COGS + stock) and Campaign Monitor (per-campaign engagement) directly
# each run, reads the CRM master Google Sheet when it has been shared with the job SA
# (falls back to the previous publication), and writes the private meloyelo.json the
# gated dash service serves. NO dataset, NO SQL views.
#
# Creates / converges:
#   APIs (incl. sheets + iamcredentials) -> AR repo + private bucket
#   -> job/web service accounts + IAM (incl. tokenCreator-on-self for the Sheets pull)
#   -> password/session/unleashed/campaign-monitor secrets -> export job (build/deploy/run)
#   -> run.developer for portal + web SAs -> 6-hourly scheduler
#   -> dash web service (private + app-level password auth, DASH_OPEN=0).
#
# NON-INTERACTIVE BY DESIGN: connector keys fall back to the notebooks in context/
# (git-ignored) and the dashboard password is auto-generated when not supplied --
# both are printed as a summary at the end, never echoed midway.
#
# RUN AS YOURSELF (gcloud auth login) -- never Cloud Build from a laptop for the DEPLOY
# step. `gcloud builds submit --tag` (image build only) is fine.
#
# USAGE
#   .\deploy_meloyelo.ps1                       # everything from context/ + generated password
#   .\deploy_meloyelo.ps1 -Password "..."       # fix the dashboard password
#   -SkipJobRun          deploy without executing the export job (~4 min run)
#   -LarkSeedFile <path> also store a Lark refresh-token seed (see job/lark_auth.py)
# =============================================================================

param(
    [string]$Password = "",
    [string]$UnleashedId = "",
    [string]$UnleashedKey = "",
    [string]$CampaignMonitorKey = "",
    [string]$CampaignMonitorClientId = "",
    [string]$LarkSeedFile = "",
    [switch]$SkipJobRun
)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$REPO    = "agora"
$CLIENT  = "meloyelo"

# Derived names (DERIVE from the client key `<c>`; never re-type) --------------
$BUCKET      = "agora-data-driven-$CLIENT-dash"
$EXPORT_JOB  = "$CLIENT-export"
$SCHED       = "$CLIENT-export-6h"
$WEB_SERVICE = "$CLIENT-dash"
$JOB_SA      = "$CLIENT-dash-job@agora-data-driven.iam.gserviceaccount.com"
$WEB_SA      = "$CLIENT-dash-web@agora-data-driven.iam.gserviceaccount.com"
$PW_SECRET   = "$CLIENT-dash-password"
$KEY_SECRET  = "$CLIENT-dash-session-key"
$UID_SECRET  = "$CLIENT-unleashed-id"
$UKEY_SECRET = "$CLIENT-unleashed-key"
$CM_SECRET   = "$CLIENT-cm-key"
$LARK_SECRET = "$CLIENT-lark-refresh-seed"
$AR_HOST     = "$REGION-docker.pkg.dev"

# Client-specific connector config (ids, not credentials).
$CRM_SHEET_ID = "1iNkF_WDa_5yY7MLPzynEP2uQWmlUnJKlq052eUTl5ZE"   # Customer Data - master

$ROOT     = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$JOB_DIR  = Join-Path $PSScriptRoot "job"
$DASH_DIR = Join-Path $PSScriptRoot "dash"
$VALIDATOR = Join-Path $ROOT "tools\_validate_dash_js.py"
# PS 5.1 has no ternary operator — this estate runs Windows PowerShell 5.1, and the
# `?:` form made this whole script fail to parse (found by the 2026-07-29 audit).
$PY = "python"; if (Get-Command py -ErrorAction SilentlyContinue) { $PY = "py" }

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
function From-Notebook([string]$nbPath, [string]$pattern) {
    if (-not (Test-Path $nbPath)) { return "" }
    $code = @"
import json, re, sys
nb = json.load(open(sys.argv[1], encoding='utf-8'))
src = ''.join(''.join(c.get('source', [])) for c in nb['cells'] if c['cell_type'] == 'code')
m = re.search(sys.argv[2], src)
sys.stdout.write(m.group(1) if m else '')
"@
    return (& $PY -X utf8 -c $code $nbPath $pattern)
}

# --- Step 0: credentials (env/params -> notebooks), password (param -> generated) -----
$CTX = Join-Path $PSScriptRoot "context"
if (-not $UnleashedId)  { $UnleashedId  = $env:UNLEASHED_API_ID }
if (-not $UnleashedKey) { $UnleashedKey = $env:UNLEASHED_API_KEY }
if (-not $CampaignMonitorKey)      { $CampaignMonitorKey      = $env:CAMPAIGN_MONITOR_API_KEY }
if (-not $CampaignMonitorClientId) { $CampaignMonitorClientId = $env:CAMPAIGN_MONITOR_CLIENT_ID }
$larkNb = Join-Path $CTX "Lark_Unleashed_Dash.ipynb"
$cmNb   = Join-Path $CTX "Campaign_Monitor_Full_Email_Extract (3).ipynb"
if (-not $UnleashedId)  { $UnleashedId  = From-Notebook $larkNb 'UNLEASHED_API_ID\s*=\s*"([^"]+)"' }
if (-not $UnleashedKey) { $UnleashedKey = From-Notebook $larkNb 'UNLEASHED_API_KEY\s*=\s*"([^"]+)"' }
if (-not $CampaignMonitorKey)      { $CampaignMonitorKey      = From-Notebook $cmNb 'API_KEY\s*=\s*"([^"]+)"' }
if (-not $CampaignMonitorClientId) { $CampaignMonitorClientId = From-Notebook $cmNb 'CLIENT_ID\s*=\s*"([^"]+)"' }
$LARK_APP_ID    = From-Notebook $larkNb 'LARK_APP_ID\s*=\s*"([^"]+)"'
$LARK_APP_TOKEN = From-Notebook $larkNb 'LARK_APP_TOKEN\s*=\s*"([^"]+)"'
$LARK_TABLE_ID  = From-Notebook $larkNb 'LARK_TABLE_ID\s*=\s*"([^"]+)"'
$LARK_APP_SECRET = From-Notebook $larkNb 'LARK_APP_SECRET\s*=\s*"([^"]+)"'
if (-not $UnleashedId -or -not $UnleashedKey) { Die "no Unleashed credentials (params, env, or context notebook)" }
if (-not $CampaignMonitorKey -or -not $CampaignMonitorClientId) { Die "no Campaign Monitor credentials (params, env, or context notebook)" }

# A password is stored even though the service deploys OPEN (DASH_OPEN=1): re-gating later is
# one env-var flip, so the secret must hold something strong from day one.
if (-not $Password) { $Password = $env:DASH_PASSWORD }
if (-not $Password) {
    $rngP = [Security.Cryptography.RandomNumberGenerator]::Create(); $pb = New-Object byte[] 15; $rngP.GetBytes($pb)
    $Password = ([Convert]::ToBase64String($pb) -replace '[+/=]', '').Substring(0, 16)
}

# --- Step 0b: image tag + project number --------------------------------------
Write-Host "[..] Resolving image tag + project number" -ForegroundColor Cyan
$SHA = (git -C $ROOT rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) { $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss") }
$SHA = $SHA.Trim()
$PNUM = (gcloud projects describe $PROJECT --format='value(projectNumber)'); Must "resolve project number"
$PNUM = ($PNUM | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($PNUM)) { Die "project number came back empty" }
Write-Host "[OK] image tag = $SHA ; project number = $PNUM"

# --- Step 1: APIs (sheets + iamcredentials for the keyless CRM pull) ----------
Write-Host "[..] Enabling required APIs" -ForegroundColor Cyan
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
    storage.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com `
    sheets.googleapis.com iamcredentials.googleapis.com --project $PROJECT
Must "enable required APIs"
Write-Host "[OK] APIs enabled"

# --- Step 2: AR repo + private bucket -----------------------------------------
Write-Host "[..] Ensuring AR repo / private bucket" -ForegroundColor Cyan
if (Exists { gcloud artifacts repositories describe $REPO --location $REGION --project $PROJECT }) { Write-Host "    AR repo $REPO already exists" }
else { gcloud artifacts repositories create $REPO --repository-format docker --location $REGION --project $PROJECT --description "Agora Data Driven shared docker images"; Must "create AR repo" }
if (Exists { gcloud storage buckets describe "gs://$BUCKET" --project $PROJECT }) { Write-Host "    bucket $BUCKET already exists" }
else { gcloud storage buckets create "gs://$BUCKET" --project $PROJECT --location $REGION --uniform-bucket-level-access --public-access-prevention; Must "create bucket $BUCKET" }
Write-Host "[OK] AR repo / bucket in place"

# --- Step 3: service accounts + least-privilege IAM ---------------------------
Write-Host "[..] Ensuring service accounts + IAM" -ForegroundColor Cyan
Ensure-Sa $JOB_SA "$CLIENT-dash-job" "meloyelo export job (Unleashed+CM+Sheets -> bucket)"
Ensure-Sa $WEB_SA "$CLIENT-dash-web" "meloyelo dash web (bucket reader + auth)"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$JOB_SA" --role "roles/storage.objectAdmin"; Must "grant objectAdmin to job SA"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$WEB_SA" --role "roles/storage.objectViewer"; Must "grant objectViewer to web SA"
# The Sheets pull mints a spreadsheets.readonly token via iamcredentials SELF-impersonation
# (metadata tokens are cloud-platform-only, which Sheets rejects) -- same keyless pattern as
# the Atrium signed uploads.
gcloud iam service-accounts add-iam-policy-binding $JOB_SA --project $PROJECT `
    --member "serviceAccount:$JOB_SA" --role "roles/iam.serviceAccountTokenCreator"; Must "grant tokenCreator-on-self to job SA"
Write-Host "[OK] service accounts + IAM in place"

# --- Step 4: secrets -----------------------------------------------------------
Write-Host "[..] Ensuring secrets" -ForegroundColor Cyan
$rng = [Security.Cryptography.RandomNumberGenerator]::Create(); $bytes = New-Object byte[] 32; $rng.GetBytes($bytes)
$SessionKey = [Convert]::ToBase64String($bytes)

$tmp = @{}
foreach ($n in @("pw","key","uid","ukey","cm")) { $tmp[$n] = Join-Path ([IO.Path]::GetTempPath()) ("agora-$n-" + [Guid]::NewGuid().ToString("N") + ".txt") }
Write-SecretFile $tmp.pw   $Password
Write-SecretFile $tmp.key  $SessionKey
Write-SecretFile $tmp.uid  $UnleashedId
Write-SecretFile $tmp.ukey $UnleashedKey
Write-SecretFile $tmp.cm   $CampaignMonitorKey
try {
    $secrets = @(
        @{ name=$PW_SECRET;   file=$tmp.pw;   reader=$WEB_SA },
        @{ name=$KEY_SECRET;  file=$tmp.key;  reader=$WEB_SA },
        @{ name=$UID_SECRET;  file=$tmp.uid;  reader=$JOB_SA },
        @{ name=$UKEY_SECRET; file=$tmp.ukey; reader=$JOB_SA },
        @{ name=$CM_SECRET;   file=$tmp.cm;   reader=$JOB_SA }
    )
    if ($LarkSeedFile -and (Test-Path $LarkSeedFile)) {
        $secrets += @{ name=$LARK_SECRET; file=$LarkSeedFile; reader=$JOB_SA }
    }
    foreach ($s in $secrets) {
        if (Exists { gcloud secrets describe $s.name --project $PROJECT }) {
            gcloud secrets versions add $s.name --project $PROJECT --data-file="$($s.file)"; Must "add version to $($s.name)"
        } else {
            gcloud secrets create $s.name --project $PROJECT --replication-policy=automatic --data-file="$($s.file)"; Must "create secret $($s.name)"
        }
        gcloud secrets add-iam-policy-binding $s.name --project $PROJECT --member "serviceAccount:$($s.reader)" --role "roles/secretmanager.secretAccessor"; Must "grant accessor on $($s.name)"
    }
} finally { Remove-Item $tmp.pw, $tmp.key, $tmp.uid, $tmp.ukey, $tmp.cm -ErrorAction SilentlyContinue }
Write-Host "[OK] secrets created + readers granted"

# --- Step 5: build + deploy + run the export job -------------------------------
Write-Host "[..] Building + deploying export job $EXPORT_JOB" -ForegroundColor Cyan
$jobImg = "$AR_HOST/$PROJECT/$REPO/${EXPORT_JOB}:$SHA"
gcloud builds submit $JOB_DIR --tag $jobImg --project $PROJECT; Must "build export job image"

$jobEnv = "GCS_BUCKET=$BUCKET,DATA_OBJECT=$CLIENT.json,GCP_PROJECT=$PROJECT,CRM_SHEET_ID=$CRM_SHEET_ID,PYTHONUNBUFFERED=1"
if ($LARK_APP_ID)    { $jobEnv += ",LARK_APP_ID=$LARK_APP_ID,LARK_APP_TOKEN=$LARK_APP_TOKEN,LARK_TABLE_ID=$LARK_TABLE_ID" }
$jobSecrets = "UNLEASHED_API_ID=${UID_SECRET}:latest,UNLEASHED_API_KEY=${UKEY_SECRET}:latest,CAMPAIGN_MONITOR_API_KEY=${CM_SECRET}:latest"
# CM client id + Lark app secret ride as env only when present (both are low-churn config).
$jobEnv += ",CAMPAIGN_MONITOR_CLIENT_ID=$CampaignMonitorClientId"
if ($LARK_APP_SECRET) { $jobEnv += ",LARK_APP_SECRET=$LARK_APP_SECRET" }
if (Exists { gcloud secrets describe $LARK_SECRET --project $PROJECT }) {
    $jobSecrets += ",LARK_REFRESH_SEED=${LARK_SECRET}:latest"
    gcloud secrets add-iam-policy-binding $LARK_SECRET --project $PROJECT --member "serviceAccount:$JOB_SA" --role "roles/secretmanager.secretAccessor" *> $null
}

gcloud run jobs deploy $EXPORT_JOB --image $jobImg --region $REGION --project $PROJECT `
    --service-account $JOB_SA --max-retries 1 --task-timeout 1800 --memory 1Gi `
    --set-env-vars $jobEnv --set-secrets $jobSecrets
Must "deploy export job $EXPORT_JOB"

# Seed the bucket with the locally built JSON when the object doesn't exist yet, so the very
# first cloud run can carry the CRM section forward (the master sheet may not be shared with
# the job SA yet). The job overwrites this immediately with the live pull.
$localJson = Join-Path $PSScriptRoot "data\meloyelo.json"
if ((Test-Path $localJson) -and -not (Exists { gcloud storage objects describe "gs://$BUCKET/$CLIENT.json" })) {
    Write-Host "[..] Seeding gs://$BUCKET/$CLIENT.json from the local build (first-run CRM carry-forward)" -ForegroundColor Cyan
    gcloud storage cp $localJson "gs://$BUCKET/$CLIENT.json"; Must "seed data object"
}
if (-not $SkipJobRun) {
    gcloud run jobs execute $EXPORT_JOB --region $REGION --project $PROJECT --wait; Must "execute export job (initial)"
    Write-Host "[OK] initial live data export complete"
}

# --- Step 6: let the platform sync + Sync button trigger this job --------------
# POST :run WITH env overrides needs run.jobs.runWithOverrides, which roles/run.invoker does
# NOT carry (the riverdance 13-days-stale trap). Grant run.developer.
$PORTAL_SA = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
Write-Host "[..] Granting run.developer on $EXPORT_JOB (portal sync + Sync button)" -ForegroundColor Cyan
gcloud run jobs add-iam-policy-binding $EXPORT_JOB --region $REGION --project $PROJECT --member "serviceAccount:$PORTAL_SA" --role "roles/run.developer"; Must "grant run.developer to portal SA"
gcloud run jobs add-iam-policy-binding $EXPORT_JOB --region $REGION --project $PROJECT --member "serviceAccount:$WEB_SA" --role "roles/run.developer"; Must "grant run.developer to web SA"
Write-Host "[OK] $EXPORT_JOB is triggerable"

# --- Step 6b: NO scheduler, BY DECISION (operator, 2026-07-28) -------------------
# Refresh is driven ONLY by the dashboard's Sync button (POST /refresh -> the export job,
# with the service's cooldown). Every export run costs Unleashed + Campaign Monitor API
# calls, so nothing fires unattended. If the client later wants hands-off freshness, the
# honeytribe-style 6-hourly scheduler block lives in git history (and the delete was:
#   gcloud scheduler jobs delete $SCHED --location $REGION --project $PROJECT).
if (Exists { gcloud scheduler jobs describe $SCHED --location $REGION --project $PROJECT }) {
    Write-Host "[..] Removing scheduler $SCHED (Sync-button-only refresh policy)" -ForegroundColor Cyan
    gcloud scheduler jobs delete $SCHED --location $REGION --project $PROJECT --quiet; Must "delete scheduler $SCHED"
}
Write-Host "[OK] refresh policy: Sync button only (no scheduler)"

# --- Step 7: JS gate + build + deploy the dash service --------------------------
Write-Host "[..] Validating dashboard.html inline JS" -ForegroundColor Cyan
if (Test-Path $VALIDATOR) { & $PY -X utf8 $VALIDATOR (Join-Path $DASH_DIR "dashboard.html"); Must "dashboard.html failed JS gate" }
else { Write-Host "    (skipping JS gate: validator not found)" -ForegroundColor Yellow }

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
Write-Host "[OK] meloyelo standup complete (tag $SHA)" -ForegroundColor Green
Write-Host "     dash service : $WEB_SERVICE"
Write-Host "     live URL     : $SVC_URL   (works immediately; no DNS needed)"
Write-Host "     access       : OPEN (DASH_OPEN=1) - no login, operator's request 2026-07-28."
Write-Host "                    The URL is unguessable, not secret. Set DASH_OPEN=0 to re-gate;"
Write-Host "                    the password secret + /login stay wired (stored in $PW_SECRET)."
Write-Host "     export job   : $EXPORT_JOB   (live Unleashed + Campaign Monitor; CRM goes live"
Write-Host "                    the moment the master sheet is shared with $JOB_SA)"
Write-Host "     next         : share BOTH Google Sheets (Viewer) with $JOB_SA"
Write-Host "                    Lark one-time auth: see job\lark_auth.py"
