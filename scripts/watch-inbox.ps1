<#
.SYNOPSIS
  Resident inbox watcher: runs an ingest whenever a new Tableau export lands
  in data\inbox. Replaces the old fixed-time daily task.

.DESCRIPTION
  Event-driven via .NET FileSystemWatcher, so an export is picked up within
  seconds of arriving instead of waiting for a clock. A 60s safety re-check
  means a missed/buffered-over event still gets caught, and a catch-up pass on
  start handles anything dropped while the watcher was down.

  Launched by install-scheduler.ps1 as an At-Logon task (the operator drops
  exports while logged in, so no "run whether logged off" / admin is needed).
  Loops forever; if it ever crashes, Task Scheduler restarts it.

  run-ingest.bat does the real work and writes its own logs\ingest-DATE.log;
  this script only logs start/trigger lines to logs\watcher-DATE.log.
#>

$ErrorActionPreference = "Continue"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$BundleRoot = (Get-Item $ScriptDir).Parent.FullName
$Bat        = Join-Path $ScriptDir "run-ingest.bat"
$Inbox      = Join-Path $BundleRoot "data\inbox"
$LogDir     = Join-Path $BundleRoot "logs"
# ponytail: mirror engine.ingest.LocalInboxSource._ALLOWED_SUFFIXES. If the
# engine ever accepts another extension, add it here too.
$Allowed    = @(".csv", ".xlsx")

if (-not (Test-Path $Inbox))  { New-Item -ItemType Directory -Path $Inbox  -Force | Out-Null }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-WatcherLog($msg) {
    $day = Get-Date -Format "yyyy-MM-dd"
    $ts  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path (Join-Path $LogDir "watcher-$day.log") -Value "$ts  $msg"
}

function Get-PendingCount {
    @(Get-ChildItem $Inbox -File -ErrorAction SilentlyContinue |
        Where-Object { $Allowed -contains $_.Extension.ToLower() }).Count
}

function Invoke-Drain {
    # Ingest processes the OLDEST file per run and moves it out of inbox on
    # success. Loop until the folder stops shrinking: empty (done) or a run
    # that processed nothing -- a no-op or a file the parser rejected and left
    # behind. Stopping on "no shrink" avoids spinning forever on a bad file;
    # the next event or the 60s safety re-check will retry it.
    while ($true) {
        $before = Get-PendingCount
        if ($before -eq 0) { return }
        Write-WatcherLog "ingest: $before file(s) pending -> running"
        & $Bat | Out-Null
        if ((Get-PendingCount) -ge $before) {
            Write-WatcherLog "ingest: nothing processed (no-op or parse error) -> waiting for next drop"
            return
        }
    }
}

Write-WatcherLog "watcher started; bundle=$BundleRoot"
Invoke-Drain   # catch up on anything dropped while the watcher was down

$fsw = New-Object System.IO.FileSystemWatcher $Inbox
$fsw.IncludeSubdirectories = $false
$fsw.EnableRaisingEvents   = $true
$changes = [System.IO.WatcherChangeTypes]::Created -bor `
           [System.IO.WatcherChangeTypes]::Changed -bor `
           [System.IO.WatcherChangeTypes]::Renamed

while ($true) {
    # Blocks until a change OR the timeout. ponytail: the 60s timeout doubles
    # as a safety re-check so a missed/over-buffered event can't strand a file.
    $r = $fsw.WaitForChanged($changes, 60000)
    # ponytail: a copy / OneDrive sync may still be writing when the first
    # event fires; pause so the parser sees a complete file. Bump to 30s if
    # large exports are still landing half-written.
    if (-not $r.TimedOut) { Start-Sleep -Seconds 10 }
    Invoke-Drain
}
