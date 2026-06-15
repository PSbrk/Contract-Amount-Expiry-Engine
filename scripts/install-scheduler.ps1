<#
.SYNOPSIS
  Registers a Windows Task Scheduler job that runs the engine's daily
  ingest at 02:00 local time.

.DESCRIPTION
  Creates a task named "ContractEngineDailyIngest" that fires scripts\run-ingest.bat
  in the bundle this script lives inside. The task runs as the current user
  (S4U logon: no stored password, runs whether logged in or not). Missed
  runs (laptop asleep, machine off) catch up on the next available window.

  Re-running this script overwrites any prior registration with the
  current bundle location -- safe to use after moving the bundle.

.NOTES
  Requires elevation. Right-click the .ps1 -> "Run with PowerShell" will
  prompt for admin. If you'd rather not elevate, register the task by
  hand in Task Scheduler and point it at scripts\run-ingest.bat.
#>

$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundleRoot = (Get-Item $ScriptDir).Parent.FullName
$BatPath    = Join-Path $ScriptDir "run-ingest.bat"

if (-not (Test-Path $BatPath)) {
    Write-Error "run-ingest.bat not found at $BatPath. Run this from inside the ContractEngine\scripts folder."
    exit 1
}

$TaskName    = "ContractEngineDailyIngest"
$Description = "Contract Amount Expiry Engine: nightly Tableau ingest. Bundle at $BundleRoot."

$Action    = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $BundleRoot
$Trigger   = New-ScheduledTaskTrigger -Daily -At 2am
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$Settings  = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description $Description `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force

Write-Host "Registered scheduled task '$TaskName' to run daily at 02:00."
Write-Host "Bundle root:  $BundleRoot"
Write-Host "Logs land in: $BundleRoot\logs\ingest-YYYY-MM-DD.log"
Write-Host ""
Write-Host "To run it immediately as a smoke test:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove it:"
Write-Host "  .\uninstall-scheduler.ps1"
