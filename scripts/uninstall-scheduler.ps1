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

$TaskName = "ContractEngineDailyIngest"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Scheduled task '$TaskName' is not registered. Nothing to do."
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'."
