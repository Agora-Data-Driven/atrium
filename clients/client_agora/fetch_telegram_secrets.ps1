# Bootstrap the Telegram pull on a NEW machine: fetch the API credentials and the
# signed-in Telethon session from Secret Manager into raw_files\ (both gitignored).
# After this, refresh_upwork.ps1 / install_refresh_task.ps1 work with no login step.
#
# Secrets (project agora-data-driven, written 2026-07-14):
#   agora-telegram-api       telegram_api.json content (api_id/api_hash/chat)
#   agora-telegram-session   base64 of telegram.session (a FULL Telegram account
#                            login for the scraper account — handle accordingly)
#
# If Telegram ever invalidates the session (password change, "terminate sessions"),
# re-run telegram_pull.py --login on one machine and re-upload:
#   python -c "import base64;open('s.tmp','w').write(base64.b64encode(open(r'raw_files\telegram.session','rb').read()).decode())"
#   gcloud secrets versions add agora-telegram-session --project agora-data-driven --data-file=s.tmp

$ErrorActionPreference = "Continue"
$RAW = Join-Path $PSScriptRoot "raw_files"
if (-not (Test-Path $RAW)) { New-Item -ItemType Directory -Force $RAW | Out-Null }
$env:CLOUDSDK_CORE_ACCOUNT = "info@agoradatadriven.com"
$PROJECT = "agora-data-driven"

$api = (gcloud secrets versions access latest --secret agora-telegram-api --project $PROJECT) -join ""
if ($LASTEXITCODE -ne 0 -or -not $api) { throw "could not read secret agora-telegram-api" }
[IO.File]::WriteAllText((Join-Path $RAW "telegram_api.json"), $api,
    (New-Object System.Text.UTF8Encoding($false)))

$b64 = (gcloud secrets versions access latest --secret agora-telegram-session --project $PROJECT) -join ""
if ($LASTEXITCODE -ne 0 -or -not $b64) { throw "could not read secret agora-telegram-session" }
[IO.File]::WriteAllBytes((Join-Path $RAW "telegram.session"), [Convert]::FromBase64String($b64))

Write-Host "[OK] telegram_api.json + telegram.session restored into raw_files\" -ForegroundColor Green
Write-Host "     Next: .\install_refresh_task.ps1  (daily 07:15 refresh on this machine)"
