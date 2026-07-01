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
$Stalled    = Join-Path $BundleRoot "data\stalled"
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

function Get-OldestPending {
    # Ingest processes the OLDEST allowed file per run, so this is the file a
    # stalled run was working on.
    Get-ChildItem $Inbox -File -ErrorAction SilentlyContinue |
        Where-Object { $Allowed -contains $_.Extension.ToLower() } |
        Sort-Object LastWriteTime | Select-Object -First 1
}

# Watchdog ceiling for a single ingest. A stalled Asana pull can otherwise block
# this synchronous call forever, and a hang (unlike a crash) never trips Task
# Scheduler's restart -- so one stall would silently disable all auto-ingest. On
# timeout we kill the run; the next event or the 60s re-check retries.
# ponytail: bump this if a legitimate ingest ever genuinely needs longer.
$IngestTimeoutMs = 600000   # 10 min

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
        # Run under a watchdog so a stalled run can't block the watcher forever.
        # Start-Process + bounded WaitForExit; run-ingest.bat writes its own log.
        $poison = Get-OldestPending   # the file this run is working on
        $p = Start-Process -FilePath $Bat -PassThru -WindowStyle Hidden
        if (-not $p.WaitForExit($IngestTimeoutMs)) {
            $secs = [int]($IngestTimeoutMs / 1000)
            Write-WatcherLog "ingest EXCEEDED ${secs}s (stalled) -> killing pid $($p.Id) + children"
            $out = & taskkill /PID $p.Id /T /F 2>&1
            # Confirm the tree is actually dead. If the kill failed, DO NOT loop
            # or return-then-retry -- a surviving EngineApp is still writing the
            # SQLite DB, and starting a second run would put two writers on it.
            if (-not (Get-Process -Id $p.Id -ErrorAction SilentlyContinue)) {
                if ($poison) {
                    # Quarantine the stalling file so good files behind it can
                    # still drain, instead of livelocking on it every re-check.
                    if (-not (Test-Path $Stalled)) { New-Item -ItemType Directory -Path $Stalled -Force | Out-Null }
                    Move-Item -LiteralPath $poison.FullName -Destination (Join-Path $Stalled $poison.Name) -Force -ErrorAction SilentlyContinue
                    Write-WatcherLog "quarantined stalled file -> data\stalled\$($poison.Name); continuing drain"
                    continue
                }
                Write-WatcherLog "killed; no pending file to quarantine -> will retry on next event/re-check"
                return
            }
            Write-WatcherLog "ERROR taskkill did NOT reap pid $($p.Id) ($out) -> leaving watcher idle to avoid a second concurrent ingest; needs manual attention"
            return
        }
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
