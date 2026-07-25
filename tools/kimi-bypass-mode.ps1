<#
  kimi-bypass-mode.ps1  -  launch Claude Code on Moonshot Kimi (shared org key)
  -----------------------------------------------------------------------
  Claude Code normally talks to the Anthropic API. This launcher re-points it at
  Moonshot's Anthropic-compatible endpoint and the Kimi model family, using ONE
  org key from Secret Manager (kimi-api-key) so every dev runs the SAME setup
  without a per-machine key.

  This script is REPO-AGNOSTIC: it does NOT change directory, so it launches
  Claude in whatever folder you are standing in (any of the Agora repos, or the
  agora-devtools repo). That's why an identical copy lives in every repo's
  scripts/ (atrium: tools/) AND in agora-devtools/ -- run whichever is handy.

  "Bypass mode" means TWO bypasses:
    1. bypass Anthropic  -> route to Kimi (the env vars below)
    2. bypass permission prompts -> launches Claude with
       --dangerously-skip-permissions (ON by default; opt out with
       -NoSkipPermissions). Any --dangerously-skip-permissions you pass yourself
       is not duplicated.

  It sets these ONLY for the Claude process and restores them afterwards, so the
  token never lingers in your shell after Claude exits:
    ANTHROPIC_BASE_URL                  https://api.kimi.com/coding
    ANTHROPIC_AUTH_TOKEN                (from Secret Manager; never printed)
    ANTHROPIC_MODEL                     k3
    ANTHROPIC_DEFAULT_OPUS_MODEL        k3
    ANTHROPIC_DEFAULT_SONNET_MODEL      k3
    ANTHROPIC_DEFAULT_HAIKU_MODEL       k3
    ANTHROPIC_DEFAULT_FABLE_MODEL       k3
    CLAUDE_CODE_SUBAGENT_MODEL          k3
    ENABLE_TOOL_SEARCH                  false  (Kimi endpoint doesn't support it)
    CLAUDE_CODE_AUTO_COMPACT_WINDOW     1048576 (matches k3 1M context)
    CLAUDE_CODE_EFFORT_LEVEL            max

  This targets the Kimi Coding Plan endpoint (sk-kimi-... keys), whose model ids
  differ from the open platform: `k3` (1M ctx) is the default here; the plan also
  offers `kimi-for-coding` / `kimi-for-coding-highspeed` (K2.7, 256K ctx) and
  `k3-256k`. Switch with --model <id> (a 256K model wants AUTO_COMPACT_WINDOW
  lowered too). Guide: https://platform.kimi.ai/docs/guide/claude-code-kimi

  Run:
    .\scripts\kimi-bypass-mode.ps1              # launch in THIS terminal (current folder)
    .\scripts\kimi-bypass-mode.ps1 -NewWindow   # launch in a fresh window
    .\scripts\kimi-bypass-mode.cmd              # double-click = fresh window
    .\scripts\kimi-bypass-mode.ps1 --resume <id>  # any extra args pass straight to claude
    .\scripts\kimi-bypass-mode.ps1 -Dir ..\website  # launch Claude in another repo

  Prereqs: `claude` on PATH + gcloud logged in with access to the kimi-api-key
  secret. Each repo's setup.ps1 installs claude guidance; start_day.ps1 checks
  the secret. If the secret does not exist yet in agora-data-driven, either
  create it there or point this at another project:
    .\scripts\kimi-bypass-mode.ps1 -Project bidbrain-analytics
#>

# PositionalBinding=$false: bare trailing args (e.g. "/go", a --resume id) must
# fall through to $ClaudeArgs for claude, not silently bind to -Project/-Secret.
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Project = "agora-data-driven",
    [string]$Secret  = "kimi-api-key",
    [string]$Dir      = "",     # optional: launch Claude in this folder instead of the current one
    [switch]$NewWindow,
    [switch]$NoSkipPermissions, # opt out of the permission-bypass flag (kept ON by default)
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ClaudeArgs
)

$ErrorActionPreference = "Stop"

# Optional: hop into another folder before launching (e.g. -Dir ..\website).
if ($Dir) {
    if (-not (Test-Path $Dir)) { Write-Host "[X] -Dir '$Dir' does not exist." -ForegroundColor Red; exit 1 }
    Set-Location $Dir
}

# --- 1. resolve a WORKING claude executable -----------------------------------
# We resolve by full path rather than trusting whatever 'claude' is on PATH.
# Reason: the npm-installed claude.exe self-migrates to native on launch, renaming
# itself to claude.exe.old.<ts> and (if the native step never lands) leaving the
# npm shim pointing at a missing file. The stable native binary lives in
# ~/.local/bin. We prefer that; only if it is absent do we fall back to PATH.
$claudeExe = $null
$nativeClaude = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
if (Test-Path $nativeClaude) {
    $claudeExe = $nativeClaude
} else {
    $cmd = Get-Command claude -ErrorAction SilentlyContinue
    if ($cmd) { $claudeExe = $cmd.Source }
}
if (-not $claudeExe) {
    Write-Host "[X] No working 'claude' found (checked $nativeClaude and PATH)." -ForegroundColor Red
    Write-Host "    Install Claude Code:  npm install -g @anthropic-ai/claude-code" -ForegroundColor Yellow
    Write-Host "    (or the native installer from https://claude.ai/code)" -ForegroundColor Yellow
    exit 1
}

# --- 2. -NewWindow: spawn a fresh terminal that re-runs THIS script inline -----
# Re-fetching the secret inside the child keeps the token out of any command-line
# arg or window title. The child launches WITHOUT -NewWindow, so no recursion, and
# in the SAME working directory so it opens the same repo.
if ($NewWindow) {
    $psExe = (Get-Process -Id $PID).Path      # powershell.exe / pwsh.exe
    $self  = $MyInvocation.MyCommand.Path
    $childArgs = @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $self, "-Project", $Project, "-Secret", $Secret)
    if ($NoSkipPermissions) { $childArgs += "-NoSkipPermissions" }
    Start-Process -FilePath $psExe `
        -WorkingDirectory (Get-Location).Path `
        -ArgumentList $childArgs
    return
}

# --- 3. fetch the key from Secret Manager (never print it) --------------------
Write-Host "[*] Reading $Secret from Secret Manager (project $Project)..." -ForegroundColor Yellow
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Host "[X] gcloud not found on PATH. Run this repo's setup.ps1 first." -ForegroundColor Red
    exit 1
}
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$tok = $null
try {
    $tok = (gcloud secrets versions access latest --secret $Secret --project $Project 2>$null)
} catch {}
$ErrorActionPreference = $prevEAP
if (-not $tok) {
    Write-Host "[X] Could not read '$Secret' from project '$Project'." -ForegroundColor Red
    Write-Host "    Likely causes + fixes:" -ForegroundColor Yellow
    Write-Host "      * Not logged in       ->  gcloud auth login" -ForegroundColor Yellow
    Write-Host "      * Secret not created  ->  create it in $Project, e.g.:" -ForegroundColor Yellow
    Write-Host "          `"<your-kimi-key>`" | gcloud secrets create $Secret --data-file=- --project $Project" -ForegroundColor Yellow
    Write-Host "      * No IAM access       ->  gcloud secrets add-iam-policy-binding $Secret ``" -ForegroundColor Yellow
    Write-Host "          --member=user:<you> --role=roles/secretmanager.secretAccessor --project $Project" -ForegroundColor Yellow
    Write-Host "      * Use another project ->  .\kimi-bypass-mode.ps1 -Project bidbrain-analytics" -ForegroundColor Yellow
    exit 1
}

# --- 4. set env for the Claude process (snapshot prior values to restore) ------
$vars = [ordered]@{
    ANTHROPIC_BASE_URL                = "https://api.kimi.com/coding"
    ANTHROPIC_AUTH_TOKEN              = $tok
    ANTHROPIC_MODEL                   = "k3"
    ANTHROPIC_DEFAULT_OPUS_MODEL      = "k3"
    ANTHROPIC_DEFAULT_SONNET_MODEL    = "k3"
    ANTHROPIC_DEFAULT_HAIKU_MODEL     = "k3"
    ANTHROPIC_DEFAULT_FABLE_MODEL     = "k3"
    CLAUDE_CODE_SUBAGENT_MODEL        = "k3"
    ENABLE_TOOL_SEARCH                = "false"
    CLAUDE_CODE_AUTO_COMPACT_WINDOW   = "1048576"
    CLAUDE_CODE_EFFORT_LEVEL          = "max"
}
$prior = @{}
foreach ($k in $vars.Keys) { $prior[$k] = [Environment]::GetEnvironmentVariable($k, "Process") }
foreach ($k in $vars.Keys) { [Environment]::SetEnvironmentVariable($k, $vars[$k], "Process") }

# "bypass mode" = bypass BOTH: Anthropic (route to Kimi) AND Claude's permission
# prompts. Skip-permissions is ON by default; pass -NoSkipPermissions to opt out.
# Guard against a duplicate flag if the caller already passed it through ClaudeArgs.
$launchArgs = @($ClaudeArgs)
if (-not $NoSkipPermissions -and ($launchArgs -notcontains "--dangerously-skip-permissions")) {
    $launchArgs = @("--dangerously-skip-permissions") + $launchArgs
}

# ~/.claude/settings.json may pin a FULL Anthropic model id (e.g. "claude-fable-5[1m]",
# written by /model in a normal session). Full ids bypass the ANTHROPIC_DEFAULT_*_MODEL
# alias remaps above and the Kimi endpoint rejects them (model not found). The --model
# CLI flag outranks settings.json, so force Kimi unless the caller passed their own --model.
if ($launchArgs -notcontains "--model") {
    $launchArgs = @("--model", "k3") + $launchArgs
}

Write-Host "[OK] Launching Claude Code on Kimi K3 (Coding Plan) in $((Get-Location).Path)..." -ForegroundColor Green
Write-Host "     endpoint https://api.kimi.com/coding  |  all tiers=k3" -ForegroundColor DarkGray
if ($launchArgs -contains "--dangerously-skip-permissions") {
    Write-Host "     permission prompts: BYPASSED (--dangerously-skip-permissions)" -ForegroundColor DarkGray
}
try {
    & $claudeExe @launchArgs
}
finally {
    # Restore prior env so the token does not outlive this script.
    foreach ($k in $vars.Keys) {
        [Environment]::SetEnvironmentVariable($k, $prior[$k], "Process")
    }
}
