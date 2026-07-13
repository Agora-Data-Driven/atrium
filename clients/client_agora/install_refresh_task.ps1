# Registers the "Agora Upwork Refresh" scheduled task on THIS machine: every day at
# 07:15 it runs refresh_upwork.ps1 hidden (pull new Zenfl bot messages -> reprocess ->
# upload -> restart agora-dash). Idempotent: re-running replaces the task.
#
# Requirements on the machine (one-time):
#   - raw_files\telegram_api.json + `python processing\telegram_pull.py --login`
#   - gcloud authed as info@agoradatadriven.com (same as deploys)
#
# Remove with: Unregister-ScheduledTask -TaskName "Agora Upwork Refresh" -Confirm:$false

$script = Join-Path $PSScriptRoot "refresh_upwork.ps1"
if (-not (Test-Path $script)) { throw "refresh_upwork.ps1 not found next to this script." }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At "07:15"
# Catch up after sleep/reboot; never overlap; a wedged run gets killed after 4 h.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 4)

Register-ScheduledTask -TaskName "Agora Upwork Refresh" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null

Write-Host "[OK] 'Agora Upwork Refresh' registered: daily 07:15, hidden, single-instance."
Write-Host "     Last-run log: clients\client_agora\raw_files\refresh_last.log"
