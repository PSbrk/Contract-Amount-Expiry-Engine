<#
.SYNOPSIS
  Removes the scheduled task registered by install-scheduler.ps1.

.DESCRIPTION
  Idempotent: if the task doesn't exist (already removed or never installed),
  prints a friendly note and exits 0.

.NOTES
  Requires the same elevation level that registered the task. Right-click
  the .ps1 -> "Run with PowerShell".
#>

$ErrorActionPreference = "Stop"

# Both names: the current inbox watcher and the legacy fixed-time task.
$TaskNames = @("ContractEngineInboxWatcher", "ContractEngineDailyIngest")

$removed = $false
foreach ($name in $TaskNames) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed scheduled task '$name'."
        $removed = $true
    }
}
if (-not $removed) {
    Write-Host "No Contract Engine scheduled task is registered. Nothing to do."
}
