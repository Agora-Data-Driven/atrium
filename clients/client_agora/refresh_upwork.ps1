# One-command refresh for the Agora Upwork-demand dashboard — the automated
# replacement for the manual Telegram Desktop export:
#
#   1. processing\telegram_pull.py        pull new bot messages (Telethon, incremental)
#   2. processing\process_upwork.py       rebuild jobs.sqlite + aggregates.json
#   3. dash\deploy_dash_agora.ps1 -DataOnly   upload to gs://…-agora-dash/upwork/
#   4. bounce the agora-dash service (DATA_STAMP env bump -> new revision re-downloads)
#
# Skips steps 2-4 when the pull finds nothing new (override with -Force).
# One-time setup first: see processing\telegram_pull.py docstring (--login).
#
# Usage:  .\refresh_upwork.ps1            # normal (what the scheduled task runs)
#         .\refresh_upwork.ps1 -Force     # rebuild + redeploy even with 0 new messages
#         .\refresh_upwork.ps1 -SkipPull  # process + deploy only (e.g. after a manual export)
#         .\refresh_upwork.ps1 -Score     # also run score_jobs.py --all (resumable; only
#                                         #   unscored jobs — cheap once the backlog is done)

param(
    [switch]$Force,
    [switch]$SkipPull,
    [switch]$Score
)

# Not "Stop": gcloud/python write progress to stderr; every step checks $LASTEXITCODE.
$ErrorActionPreference = "Continue"

$HERE = $PSScriptRoot
$ROOT = (Resolve-Path (Join-Path $HERE "..\..")).Path           # atrium repo root
$PY   = Join-Path $ROOT ".venv\Scripts\python.exe"
if (-not (Test-Path $PY)) { $PY = "python" }
# hidden scheduled runs pipe stdout (locale cp1252 by default) — job text is unicode
$env:PYTHONIOENCODING = "utf-8"
$LOG  = Join-Path $HERE "raw_files\refresh_last.log"

$PROJECT = "agora-data-driven"
$REGION  = "asia-southeast1"
$SERVICE = "agora-dash"

try { Start-Transcript -Path $LOG -Force | Out-Null } catch {}
Write-Host "== Upwork dashboard refresh $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ==" -ForegroundColor Cyan

try {
    # -- 1) pull ------------------------------------------------------------
    $newMsgs = -1
    if (-not $SkipPull) {
        $pullOut = & $PY (Join-Path $HERE "processing\telegram_pull.py") 2>&1 |
            ForEach-Object { Write-Host $_; "$_" }
        if ($LASTEXITCODE -eq 2) {
            throw "Telegram pull needs one-time setup/login - see processing\telegram_pull.py"
        }
        if ($LASTEXITCODE -ne 0) { throw "telegram_pull.py failed (exit $LASTEXITCODE)" }
        $m = ($pullOut | Select-String "NEW_MESSAGES=(\d+)" | Select-Object -Last 1)
        if ($m) { $newMsgs = [int]$m.Matches[0].Groups[1].Value }
        if ($newMsgs -eq 0 -and -not $Force) {
            Write-Host "[OK] no new messages - nothing to do." -ForegroundColor Green
            exit 0
        }
        Write-Host "-- $newMsgs new messages pulled"
    }

    # -- 2) process (base export + all pulled increments; ~11 min) ----------
    & $PY (Join-Path $HERE "processing\process_upwork.py")
    if ($LASTEXITCODE -ne 0) { throw "process_upwork.py failed (exit $LASTEXITCODE)" }

    # -- 2b) optional AI fit scoring (resumable, unscored jobs only) ---------
    if ($Score) {
        & $PY (Join-Path $HERE "processing\score_jobs.py") --all
        if ($LASTEXITCODE -ne 0) { Write-Warning "score_jobs.py failed - continuing without fresh scores" }
    }

    # -- 3) upload data (deploy script owns bucket + account details) --------
    & (Join-Path $HERE "dash\deploy_dash_agora.ps1") -DataOnly
    if ($LASTEXITCODE -ne 0) { throw "data upload failed (exit $LASTEXITCODE)" }

    # -- 4) restart the service so it re-downloads the data ------------------
    $env:CLOUDSDK_CORE_ACCOUNT = "info@agoradatadriven.com"
    $stamp = Get-Date -Format yyyyMMddHHmmss
    Write-Host "-- restarting $SERVICE (DATA_STAMP=$stamp)"
    gcloud run services update $SERVICE --region $REGION --project $PROJECT `
        --update-env-vars "DATA_STAMP=$stamp" --quiet
    if ($LASTEXITCODE -ne 0) { throw "service restart failed" }

    Write-Host "[OK] dashboard refreshed and live." -ForegroundColor Green
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
}
