<#
.SYNOPSIS
  Registers a Windows Task Scheduler job that runs the engine's ingest
  whenever a new Tableau export lands in data\inbox -- event-driven, not on a
  clock.

.DESCRIPTION
  Creates a task named "ContractEngineInboxWatcher" that launches
  scripts\watch-inbox.ps1 at logon. The watcher stays resident and fires
  scripts\run-ingest.bat within seconds of an export arriving (see that
  script for how it watches the folder).

  Runs as the current user at logon with no stored password, so NO elevation
  is required -- the operator drops exports while logged in, which is exactly
  when the watcher is alive. If the watcher process ever dies, Task Scheduler
  restarts it; on reboot it comes back at the next logon.

  Re-running this script overwrites any prior registration with the current
  bundle location, and removes the old fixed-time "ContractEngineDailyIngest"
  task if it's still present -- safe to use after moving the bundle or when
  migrating off the 08:30 schedule.
#>

$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundleRoot = (Get-Item $ScriptDir).Parent.FullName
$WatcherPs1 = Join-Path $ScriptDir "watch-inbox.ps1"

if (-not (Test-Path $WatcherPs1)) {
    Write-Error "watch-inbox.ps1 not found at $WatcherPs1. Run this from inside the ContractEngine\scripts folder."
    exit 1
}

# Migrate off the old fixed-time task if it exists.
$OldTaskName = "ContractEngineDailyIngest"
if (Get-ScheduledTask -TaskName $OldTaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $OldTaskName -Confirm:$false
    Write-Host "Removed old fixed-time task '$OldTaskName'."
}

$TaskName    = "ContractEngineInboxWatcher"
$Description = "Contract Amount Expiry Engine: watches data\inbox and ingests new Tableau exports on arrival. Bundle at $BundleRoot."

# Hidden, no-profile PowerShell so nothing flashes on screen at logon.
$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WatcherPs1`"" `
    -WorkingDirectory $BundleRoot

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Interactive logon: no stored password, no admin needed.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# ExecutionTimeLimit 0 = no limit (the watcher is meant to run indefinitely;
# the default would kill it after 3 days). RestartCount/Interval relaunch it
# if the process dies. IgnoreNew so a second logon doesn't spawn a duplicate.
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description $Description `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

# Start it now so the operator doesn't have to log out/in to activate it.
# This also runs the catch-up pass over anything already in the inbox.
Start-ScheduledTask -TaskName $TaskName

Write-Host "Registered '$TaskName' to watch data\inbox and ingest on new exports."
Write-Host "Bundle root:   $BundleRoot"
Write-Host "Watcher log:   $BundleRoot\logs\watcher-YYYY-MM-DD.log"
Write-Host "Ingest log:    $BundleRoot\logs\ingest-YYYY-MM-DD.log"
Write-Host ""
Write-Host "It's running now. Drop a .csv/.xlsx export into data\inbox to test."
Write-Host "To remove it:"
Write-Host "  .\uninstall-scheduler.ps1"
