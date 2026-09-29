<#
Nightly sourcing + Prompt 1: Task Scheduler entry point.

Runs scripts\run_nightly.py with this repo's .venv and appends all output to
runs\<date>.log (UTF-8). The morning report is runs\<date>.md (+ .json).

Manual use:
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\nightly.ps1 -Cap 3 -DryRun

--- Registering the task (run this once, in an elevated PowerShell) ---
Runs daily at 01:00 as the current user, whether logged on or not (prompts for the Windows
password so the user profile, Credential Manager/keyring and the claude login
are available), wakes the PC, and is killed after 6 hours:

  schtasks /Create /TN "AccountSourcing\Nightly" /SC DAILY /ST 01:00 /RU "$env:USERDOMAIN\$env:USERNAME" /RP * /RL LIMITED /F `
    /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\nightly.ps1"
  $s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6) -MultipleInstances IgnoreNew
  Set-ScheduledTask -TaskPath "\AccountSourcing\" -TaskName "Nightly" -Settings $s

Test it once:   schtasks /Run /TN "AccountSourcing\Nightly"   then read runs\<today>.log / .md
Remove it:      schtasks /Delete /TN "AccountSourcing\Nightly" /F

If the hosted Apollo connector is not reachable from the "whether logged on or
not" context (check runs\<date>.md for a /source failure), re-create the task
with /IT (only when user is logged on) and leave the session locked instead.
#>
param(
    [int]$Cap = 0,           # 0 = use config\settings.json per_run_cap
    [string]$Model = "",     # "" = use config\settings.json model
    [string]$Date = "",      # "" = today
    [switch]$DryRun,
    [switch]$Resume
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# claude_auth in config\settings.json: "subscription" (default) or "api_key".
# The HubSpot token never reaches claude -p. API credentials are removed only in subscription mode.
$AuthMode = "subscription"
try {
    $cfg = Get-Content -Raw -Encoding UTF8 (Join-Path $Root "config\settings.json") | ConvertFrom-Json
    if ($cfg.claude_auth) { $AuthMode = [string]$cfg.claude_auth }
} catch { }
$Remove = @("HUBSPOT_TOKEN")
if ($AuthMode -ne "api_key") { $Remove += @("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") }
foreach ($v in $Remove) {
    Remove-Item "Env:$v" -ErrorAction SilentlyContinue
}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# Task Scheduler does not always carry the user's PATH additions; claude.exe lives here.
$claudeDir = Join-Path $env:USERPROFILE ".local\bin"
if (($env:PATH -split ";") -notcontains $claudeDir) { $env:PATH = "$claudeDir;$env:PATH" }

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not $Date) { $Date = Get-Date -Format "yyyy-MM-dd" }
$Runs = Join-Path $Root "runs"
New-Item -ItemType Directory -Force $Runs | Out-Null
$Log = Join-Path $Runs "$Date.log"

$argList = @("scripts\run_nightly.py", "--date", $Date)
if ($Cap -gt 0) { $argList += @("--cap", "$Cap") }
if ($Model)     { $argList += @("--model", $Model) }
if ($DryRun)    { $argList += "--dry-run" }
if ($Resume)    { $argList += "--resume" }

if (-not (Test-Path $Py)) {
    "[$(Get-Date -Format s)] ERROR: $Py not found (create the venv: py -3.11 -m venv .venv; .venv\Scripts\pip install -r requirements.txt)" |
        Out-File -Append -Encoding utf8 $Log
    exit 2
}

"[$(Get-Date -Format s)] nightly.ps1 start: $($argList -join ' ') (claude_auth: $AuthMode; removed from environment: $($Remove -join ', '))" | Out-File -Append -Encoding utf8 $Log
# cmd.exe redirection keeps Python's UTF-8 bytes as-is (PowerShell 5.1 '>' would write UTF-16).
$quoted = ($argList | ForEach-Object { '"' + $_ + '"' }) -join " "
cmd.exe /d /c "`"$Py`" $quoted >> `"$Log`" 2>&1"
$code = $LASTEXITCODE
"[$(Get-Date -Format s)] nightly.ps1 exit $code" | Out-File -Append -Encoding utf8 $Log
exit $code
