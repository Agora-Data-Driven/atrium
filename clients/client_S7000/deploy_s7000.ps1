# =============================================================================
# deploy_s7000.ps1 -- ONE-SHOT, IDEMPOTENT standup of the Campaign Uptime Monitor.
#
# ONE pipeline, THREE scoped web services. INTO Schueleraustausch and Service 7000 AG are
# unrelated companies and both clients can reach this dashboard, so the brief's number-one
# technical requirement is that neither can ever see the other's data. This script enforces that
# in FOUR independent layers, any one of which would hold on its own:
#
#   1. DATA          the export job writes three separate objects -- internal.json, into.json,
#                    service7000.json -- each containing only that scope's rows. No combined
#                    payload is ever published.
#   2. RUNTIME       three Cloud Run services, each with DATA_OBJECT pinned by an env var to one
#                    object. No request parameter can change it.
#   3. IAM           one service account per scope, granted objectViewer with an IAM CONDITION
#                    restricting resource.name to its own object. Even a code bug in the app
#                    cannot read the other client's file -- GCS refuses.
#   4. AUTH          a separate password + session secret per scope; DASH_OPEN=0 everywhere.
#
# RUN AS YOURSELF (gcloud auth login info@agoradatadriven.com) -- never Cloud Build from a
# laptop. `gcloud builds submit --tag` only BUILDS the image (no actAs needed); the deploy runs
# AS YOU with the runtime SAs.
#
# USAGE
#   .\deploy_s7000.ps1                              # prompts for the three passwords
#   .\deploy_s7000.ps1 -IntoPassword a -S7000Password b -InternalPassword c
#   .\deploy_s7000.ps1 -SeedDemoData                # also upload the local demo payloads
#   .\deploy_s7000.ps1 -DashOnly                    # rebuild + redeploy the three web services
#
# The export job is NOT created here yet: the live Windsor pull is blocked on the two open
# questions in the brief (the conversion action is unresolved, and the shared API key must be
# rotated into Secret Manager first). Everything else stands up, and `-SeedDemoData` publishes
# the synthetic payloads so the routes are live and reviewable end to end. See README.md.
# =============================================================================

param(
    [string]$IntoPassword = "",
    [string]$S7000Password = "",
    [string]$InternalPassword = "",
    [switch]$SeedDemoData,
    [switch]$DashOnly
)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$REPO    = "agora"
$CLIENT  = "s7000"

# Derived names (DERIVE from the client key; never re-type) -------------------
$BUCKET  = "agora-data-driven-$CLIENT-dash"
$AR_HOST = "$REGION-docker.pkg.dev"
$IMAGE   = "$AR_HOST/$PROJECT/$REPO/$CLIENT-dash"

# One row per scoped route. `Object` IS the isolation boundary at runtime.
$SCOPES = @(
    @{ Key = "internal";    Service = "$CLIENT-internal-dash"; Object = "internal.json";
       Sa = "$CLIENT-internal-web";    Secret = "$CLIENT-internal-dash-password";
       Session = "$CLIENT-internal-dash-session-key";
       Name = "INTO / Service 7000 -- internal" }
    @{ Key = "into";        Service = "$CLIENT-into-dash";     Object = "into.json";
       Sa = "$CLIENT-into-web";        Secret = "$CLIENT-into-dash-password";
       Session = "$CLIENT-into-dash-session-key";
       Name = "INTO Schueleraustausch" }
    @{ Key = "service7000"; Service = "$CLIENT-service-dash";  Object = "service7000.json";
       Sa = "$CLIENT-service-web";     Secret = "$CLIENT-service-dash-password";
       Session = "$CLIENT-service-dash-session-key";
       Name = "Service 7000 AG" }
)

$ROOT      = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$DASH_DIR  = Join-Path $PSScriptRoot "dash"
$DATA_DIR  = Join-Path $PSScriptRoot "data"
$BUILDER   = Join-Path $PSScriptRoot "job\build_local.py"
$VALIDATOR = Join-Path $ROOT "tools\_validate_dash_js.py"
$VENV_PY = Join-Path $ROOT ".venv\Scripts\python.exe"
if (-not (Test-Path $VENV_PY)) { $VENV_PY = Join-Path $ROOT ".venv-portal\Scripts\python.exe" }
if (-not (Test-Path $VENV_PY)) { $VENV_PY = "python" }

function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) { if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" } }
function Exists([scriptblock]$Probe) { & $Probe *> $null; return ($LASTEXITCODE -eq 0) }
function Step([string]$n) { Write-Host ""; Write-Host "=== $n" -ForegroundColor Cyan }

function Write-SecretFile([string]$path, [string]$value) {
    # UTF-8, no BOM, no trailing newline -- see the root CLAUDE.md "Never" list.
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $value, $enc)
}
function Ensure-Secret([string]$name, [string]$value) {
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        Write-SecretFile $tmp $value
        if (Exists { gcloud secrets describe $name --project $PROJECT }) {
            gcloud secrets versions add $name --project $PROJECT --data-file=$tmp | Out-Null
            Must "add version to secret $name"
            Write-Host "    rotated $name"
        } else {
            gcloud secrets create $name --project $PROJECT --replication-policy=automatic `
                --data-file=$tmp | Out-Null
            Must "create secret $name"
            Write-Host "    created $name" -ForegroundColor Yellow
        }
    } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}
function Ensure-Sa([string]$accountId, [string]$displayName) {
    $email = "$accountId@$PROJECT.iam.gserviceaccount.com"
    if (Exists { gcloud iam service-accounts describe $email --project $PROJECT }) {
        Write-Host "    $email already exists"
    } else {
        Write-Host "    creating $email" -ForegroundColor Yellow
        gcloud iam service-accounts create $accountId --project $PROJECT --display-name $displayName
        Must "create service account $email"
    }
    return $email
}
function New-Password() {
    # 20 chars from an unambiguous alphabet (no O/0/I/l) -- these get read over the phone.
    $abc = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789".ToCharArray()
    $b = New-Object byte[] 20
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    -join ($b | ForEach-Object { $abc[$_ % $abc.Length] })
}
function Resolve-Password([string]$given, [string]$label) {
    if ($given) { return $given }
    $p = New-Password
    Write-Host "    generated a password for $label" -ForegroundColor Yellow
    return $p
}

Write-Host "Campaign Uptime Monitor -- standup" -ForegroundColor Green
Write-Host "  project $PROJECT / region $REGION / bucket $BUCKET"

# --- 0. Preflight -----------------------------------------------------------
Step "0. Preflight"
$acct = (gcloud config get-value account 2>$null)
if (-not $acct) { Die "not signed in. Run: gcloud auth login info@agoradatadriven.com" }
Write-Host "    running as $acct"
$PROJECT_NUMBER = (gcloud projects describe $PROJECT --format='value(projectNumber)')
Must "resolve project number (is the auth token still valid? run: gcloud auth login)"
Write-Host "    project number $PROJECT_NUMBER"

# The esprima gate. A JS syntax error in dashboard.html does not surface as an error -- the page
# just hangs on "Loading", because the script that fetches data.json never runs.
if (Test-Path $VALIDATOR) {
    & $VENV_PY $VALIDATOR (Join-Path $DASH_DIR "dashboard.html")
    Must "dashboard.html JS gate"
} else { Write-Host "    [warn] validator not found, skipping the JS gate" -ForegroundColor Yellow }

if (-not $DashOnly) {
    # --- 1. APIs ------------------------------------------------------------
    Step "1. APIs"
    gcloud services enable run.googleapis.com artifactregistry.googleapis.com `
        cloudbuild.googleapis.com secretmanager.googleapis.com storage.googleapis.com `
        cloudscheduler.googleapis.com --project $PROJECT
    Must "enable APIs"

    # --- 2. Artifact Registry + the private bucket --------------------------
    Step "2. Artifact Registry repo + private bucket"
    if (Exists { gcloud artifacts repositories describe $REPO --location $REGION --project $PROJECT }) {
        Write-Host "    repo $REPO already exists"
    } else {
        gcloud artifacts repositories create $REPO --repository-format=docker `
            --location $REGION --project $PROJECT --description "Agora shared images"
        Must "create Artifact Registry repo"
    }
    if (Exists { gcloud storage buckets describe "gs://$BUCKET" --project $PROJECT }) {
        Write-Host "    bucket gs://$BUCKET already exists"
    } else {
        # Uniform bucket-level access: no per-object ACLs, IAM only. That is what makes the
        # conditional grants in step 4 the single source of truth for who can read what.
        gcloud storage buckets create "gs://$BUCKET" --project $PROJECT --location $REGION `
            --uniform-bucket-level-access --public-access-prevention
        Must "create bucket"
    }
    # NEVER make this bucket or its objects public: the payloads are client data and are only
    # ever served through each service's authenticated /data.json proxy.
    gcloud storage buckets update "gs://$BUCKET" --project $PROJECT --public-access-prevention | Out-Null
}

# --- 3. Per-scope service accounts, secrets and conditional IAM -------------
Step "3. Per-scope service accounts, secrets and object-scoped IAM"
$pwGiven = @{ internal = $InternalPassword; into = $IntoPassword; service7000 = $S7000Password }
$passwords = @{}
foreach ($s in $SCOPES) {
    Write-Host "  -- $($s.Key)"
    $email = Ensure-Sa $s.Sa "Uptime monitor web ($($s.Key))"
    $s.SaEmail = $email

    if (-not $DashOnly) {
        $pw = Resolve-Password $pwGiven[$s.Key] $s.Key
        $passwords[$s.Key] = $pw
        Ensure-Secret $s.Secret $pw
        # A per-scope session key, so a cookie minted for one client's service is worthless on
        # another's even if someone replays it.
        $sess = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
        Ensure-Secret $s.Session $sess

        foreach ($sec in @($s.Secret, $s.Session)) {
            gcloud secrets add-iam-policy-binding $sec --project $PROJECT `
                --member "serviceAccount:$email" --role roles/secretmanager.secretAccessor `
                --condition=None | Out-Null
            Must "grant secretAccessor on $sec"
        }

        # THE IMPORTANT ONE. objectViewer on the bucket, CONDITIONED to this scope's single
        # object. Without the condition, every scope's SA could read every payload and the
        # isolation would rest entirely on the app never having a bug.
        # gcloud parses --condition as a comma-separated dict, so the description must contain
        # no commas; apostrophes are avoided too, since the expression is already single-quoted.
        $cond = "expression=resource.name.endsWith('/objects/$($s.Object)'),title=only-$($s.Key)-payload,description=Restricts this service account to its own scoped payload so no client can ever read data belonging to the other"
        gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --project $PROJECT `
            --member "serviceAccount:$email" --role roles/storage.objectViewer `
            --condition=$cond | Out-Null
        Must "grant scoped objectViewer for $($s.Key)"
        Write-Host "    objectViewer LIMITED to $($s.Object)" -ForegroundColor Green
    }
}

# --- 4. Build ONE image, shared by all three services ----------------------
Step "4. Build the web image"
$TAG = (Get-Date -Format "yyyyMMdd-HHmmss")
Push-Location $DASH_DIR
try {
    gcloud builds submit --tag "${IMAGE}:$TAG" --project $PROJECT --region $REGION .
    Must "build web image"
} finally { Pop-Location }
Write-Host "    built ${IMAGE}:$TAG"

# --- 5. Deploy the three services -----------------------------------------
Step "5. Deploy the three scoped services"
# Org policy (Domain Restricted Sharing) rejects --allow-unauthenticated; every web service
# deploys with --no-invoker-iam-check and does its own password/SSO auth in-process.
$urls = @{}
foreach ($s in $SCOPES) {
    Write-Host "  -- $($s.Service)  ->  $($s.Object)"
    gcloud run deploy $s.Service --project $PROJECT --region $REGION `
        --image "${IMAGE}:$TAG" `
        --service-account $s.SaEmail `
        --no-invoker-iam-check `
        --port 8080 --cpu 1 --memory 512Mi --min-instances 0 --max-instances 4 `
        --set-env-vars "GCS_BUCKET=$BUCKET,DATA_OBJECT=$($s.Object),SCOPE_NAME=$($s.Name),DASH_OPEN=0,GCP_PROJECT=$PROJECT,REGION=$REGION" `
        --set-secrets "DASH_PASSWORD=$($s.Secret):latest,SESSION_SECRET=$($s.Session):latest"
    Must "deploy $($s.Service)"
    $u = (gcloud run services describe $s.Service --project $PROJECT --region $REGION `
          --format='value(status.url)')
    $urls[$s.Key] = $u
}

# --- 6. Optionally publish the demo payloads ------------------------------
if ($SeedDemoData) {
    Step "6. Publish the demo payloads"
    Write-Host "    rebuilding locally..."
    $env:PYTHONUTF8 = "1"
    & $VENV_PY $BUILDER
    Must "build demo payloads"
    foreach ($s in $SCOPES) {
        $f = Join-Path $DATA_DIR $s.Object
        if (-not (Test-Path $f)) { Die "missing $f" }
        # Content-Type set explicitly; the proxy reads bytes but a wrong type confuses gsutil cp -Z
        gcloud storage cp $f "gs://$BUCKET/$($s.Object)" --project $PROJECT `
            --content-type=application/json --cache-control="no-store"
        Must "upload $($s.Object)"
    }
    Write-Host "    NOTE: these payloads are SYNTHETIC and flagged demo=true. The dashboard shows" -ForegroundColor Yellow
    Write-Host "    a standing 'Demo data' ribbon so nobody mistakes them for real numbers." -ForegroundColor Yellow
}

# --- 7. Verify the isolation actually holds -------------------------------
Step "7. Verify object-scoped IAM"
# A positive test would need to impersonate each SA; what we can assert cheaply is that every
# binding carries its condition. A grant that silently lost its condition is the failure mode
# that would matter, and it is invisible otherwise -- the policy still "looks correct".
$policy = (gcloud storage buckets get-iam-policy "gs://$BUCKET" --project $PROJECT --format=json) | ConvertFrom-Json
foreach ($s in $SCOPES) {
    $member = "serviceAccount:$($s.SaEmail)"
    $bound = @($policy.bindings | Where-Object {
        $_.role -eq "roles/storage.objectViewer" -and $_.members -contains $member })
    $conditioned = @($bound | Where-Object { $_.condition -and
        $_.condition.expression -like "*$($s.Object)*" })
    if ($conditioned.Count -ge 1 -and $bound.Count -eq $conditioned.Count) {
        Write-Host "    OK   $($s.Key): objectViewer only on $($s.Object)" -ForegroundColor Green
    } else {
        Write-Host "    FAIL $($s.Key): has an UNCONDITIONED objectViewer binding -- it can read" -ForegroundColor Red
        Write-Host "         every payload in the bucket. Remove it before handing out any URL." -ForegroundColor Red
    }
}

# --- Summary --------------------------------------------------------------
Write-Host ""
Write-Host "=============================================================" -ForegroundColor Green
Write-Host " Campaign Uptime Monitor is live" -ForegroundColor Green
Write-Host "=============================================================" -ForegroundColor Green
foreach ($s in $SCOPES) {
    Write-Host ""
    Write-Host " $($s.Name)"
    Write-Host "   $($urls[$s.Key])"
    Write-Host "   serves $($s.Object)"
    if ($passwords.ContainsKey($s.Key)) {
        Write-Host "   password: $($passwords[$s.Key])" -ForegroundColor Yellow
    }
}
Write-Host ""
Write-Host " Next:"
Write-Host "   * map the subdomains (into / service7000 / s7000 .agoradatadriven.com)"
Write-Host "   * give each client ONLY their own URL and password"
Write-Host "   * wire the live Windsor pull -- see README.md, 'What is left'"
Write-Host ""
