# =============================================================================
# enable_atrium_mail.ps1 -- ONE-TIME infra for the Atrium Mail tab's Workspace
#   (domain-wide-delegation) connector. Idempotent; safe to re-run.
#
# The Mail tab has two connector kinds (see mailroom.py):
#   * imap -- app-password mailboxes. NEED NOTHING from this script; they work on a
#     default deploy the moment one is added in the console.
#   * dwd  -- our own @agoradatadriven.com Workspace mailboxes, read via the Gmail
#     API with domain-wide delegation and KEYLESS signing (no SA key file ever).
#     That path needs the small, one-time setup THIS script does:
#       1. Enable the Gmail + IAM Credentials APIs on the project.
#       2. Create a dedicated `mail-sync` service account (the delegation identity;
#          dedicated so the Gmail grant is scoped to exactly one SA, not the web SA).
#       3. Grant the platform web SA roles/iam.serviceAccountTokenCreator ON mail-sync,
#          so the running portal/job can signJwt as it (same keyless posture as the
#          large-creative signed uploads).
#       4. Print the ONE manual step only a Workspace ADMIN can do: register the SA's
#          client id + the gmail.readonly scope under admin.google.com -> Security ->
#          Access and data control -> API controls -> Domain-wide delegation.
#
# After this: redeploy the portal (deploy_dash_platform.ps1) and the job
# (deploy_mail_refresh.ps1) -- both auto-detect the mail-sync SA and set MAIL_DWD_SA.
# =============================================================================

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT = "agora-data-driven"
$MAIL_SA_NAME = "mail-sync"
$MAIL_SA = "$MAIL_SA_NAME@$PROJECT.iam.gserviceaccount.com"
$WEB_SA  = "platform-dash-web@$PROJECT.iam.gserviceaccount.com"
$SCOPE   = "https://www.googleapis.com/auth/gmail.readonly"

function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) { if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" } }

# --- 1. APIs ------------------------------------------------------------------
Write-Host "[..] Enabling the Gmail + IAM Credentials APIs" -ForegroundColor Cyan
gcloud services enable gmail.googleapis.com iamcredentials.googleapis.com --project=$PROJECT
Must "enable APIs"
Write-Host "[OK] APIs enabled"

# --- 2. The dedicated delegation SA (create-if-absent) -------------------------
gcloud iam service-accounts describe $MAIL_SA --project=$PROJECT *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[..] Creating service account $MAIL_SA" -ForegroundColor Cyan
    gcloud iam service-accounts create $MAIL_SA_NAME `
        --project=$PROJECT `
        --display-name="Atrium Mail sync (domain-wide delegation identity)"
    Must "create $MAIL_SA"
} else {
    Write-Host "[OK] service account $MAIL_SA already exists"
}

# --- 3. Keyless signing: the web SA may signJwt AS mail-sync --------------------
Write-Host "[..] Granting $WEB_SA Token Creator on $MAIL_SA" -ForegroundColor Cyan
gcloud iam service-accounts add-iam-policy-binding $MAIL_SA `
    --project=$PROJECT `
    --member "serviceAccount:$WEB_SA" `
    --role "roles/iam.serviceAccountTokenCreator"
Must "grant tokenCreator on $MAIL_SA"
Write-Host "[OK] keyless signing wired (no key file anywhere)"

# --- 4. The ONE manual Workspace-admin step -------------------------------------
$CLIENT_ID = (gcloud iam service-accounts describe $MAIL_SA --project=$PROJECT --format="value(uniqueId)")
Must "read the SA's client id"
$CLIENT_ID = ($CLIENT_ID | Out-String).Trim()

Write-Host ""
Write-Host "[OK] GCP side done. ONE manual step remains (Workspace admin, ~2 minutes):" -ForegroundColor Green
Write-Host ""
Write-Host "  1. Open admin.google.com (as the Workspace super admin) ->" -ForegroundColor Yellow
Write-Host "     Security -> Access and data control -> API controls -> Domain-wide delegation" -ForegroundColor Yellow
Write-Host "  2. Add new ->" -ForegroundColor Yellow
Write-Host "       Client ID : $CLIENT_ID" -ForegroundColor Yellow
Write-Host "       Scopes    : $SCOPE" -ForegroundColor Yellow
Write-Host "  3. Authorize." -ForegroundColor Yellow
Write-Host ""
Write-Host "  Then redeploy so MAIL_DWD_SA lands on the service + job:" -ForegroundColor Yellow
Write-Host "       .\deploy_dash_platform.ps1" -ForegroundColor Yellow
Write-Host "       .\deploy_mail_refresh.ps1 -Run" -ForegroundColor Yellow
Write-Host ""
Write-Host "  After that, ANY @agoradatadriven.com mailbox connects from the console with" -ForegroundColor Yellow
Write-Host "  just its address -- no password, nothing stored, tokens minted per run." -ForegroundColor Yellow
