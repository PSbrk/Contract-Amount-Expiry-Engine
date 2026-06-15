================================================================================
  Contract Amount Expiry Engine -- Portable Local-First Edition
================================================================================

A Windows app that watches Tableau exports of spend, attributes each
transaction to a contract in Asana, tracks budget usage, and flags
contracts that have run hot or expired.

No third-party SaaS subscription. No cloud account. No IT approval beyond
the one Asana Personal Access Token (PAT) you already have.


--------------------------------------------------------------------------------
  Folder layout (after you unzip)
--------------------------------------------------------------------------------

    ContractEngine\                 <-- this folder; portable; move it anywhere
        EngineApp.exe               <-- entry point
        _internal\                  <-- Python runtime + libraries (do not edit)
        config\
            secrets.env             <-- you create this; see step 2 below
        data\                       <-- created on first run
            engine.db               <-- SQLite database; ALL persistent state
            inbox\                  <-- drop Tableau exports here
            processed\              <-- engine moves files here after ingest
        logs\                       <-- one log file per day
        scripts\
            run-ingest.bat          <-- what Task Scheduler calls
            run-ui.bat              <-- double-click to open the web UI
            install-scheduler.ps1   <-- one-time: register the daily task
            uninstall-scheduler.ps1 <-- clean removal
        README.txt                  <-- this file


--------------------------------------------------------------------------------
  One-time setup (about 10 minutes)
--------------------------------------------------------------------------------

1.  Unzip ContractEngine\ wherever you want it to live. Suggested:
        C:\ContractEngine\
    The folder is fully portable; you can move it later without breaking
    anything as long as you re-run install-scheduler.ps1 after the move.

2.  Create config\secrets.env in Notepad. Put your Asana PAT in it. The
    file must look exactly like this (no spaces around the equals sign):

        ASANA_PAT=YOUR_ASANA_PAT_HERE
        ONEDRIVE_BACKUP_PATH=C:\Users\<you>\OneDrive\Backups\engine.db

    ONEDRIVE_BACKUP_PATH is optional but recommended -- after every
    successful ingest, the engine copies data\engine.db there so
    OneDrive syncs it to the cloud. If the machine dies, restore by
    copying the backup file back to data\engine.db.

    Where do I get an Asana PAT?
        https://app.asana.com/0/my-apps  (Create new token)

3.  Register the daily scheduled task. Right-click
    scripts\install-scheduler.ps1 and choose "Run with PowerShell". If
    Windows prompts about execution policy, answer A (Run once).

    This creates a task named "ContractEngineDailyIngest" that fires at
    08:30 local time every day. Missed runs (laptop asleep, machine off)
    catch up at the next available window.

4.  Smoke test: drop a small Tableau export into data\inbox\ and run

        scripts\run-ingest.bat

    Check logs\ingest-YYYY-MM-DD.log for the run summary. The file
    should have moved from data\inbox\ to data\processed\.

5.  Open the web UI to inspect results:

        scripts\run-ui.bat

    Your default browser opens to http://localhost:8080. Use the Needs
    Tagging tab to assign contracts to unmatched / ambiguous groups;
    your answers get baked into Learned Mappings on the next ingest.


--------------------------------------------------------------------------------
  Daily use
--------------------------------------------------------------------------------

You don't have to do anything daily. The scheduled task picks up any new
Tableau export from data\inbox\ at 08:30 and writes results to
data\engine.db. The web UI shows the latest state whenever you open it.

To do an ad-hoc run (e.g. after dropping a new file at noon):
    scripts\run-ingest.bat

To inspect / edit / fix attributions:
    scripts\run-ui.bat


--------------------------------------------------------------------------------
  Rotating the Asana PAT
--------------------------------------------------------------------------------

When your PAT expires (Asana ages them out periodically):

1.  Generate a new one at https://app.asana.com/0/my-apps
2.  Open config\secrets.env in Notepad
3.  Replace the ASANA_PAT value with the new token; save
4.  The next ingest run picks it up automatically -- no restart needed


--------------------------------------------------------------------------------
  Backup and restore
--------------------------------------------------------------------------------

Backup:
    If you set ONEDRIVE_BACKUP_PATH in secrets.env, every successful
    --ingest run copies data\engine.db there automatically. You don't
    have to do anything else.

    If you didn't set it, just copy data\engine.db anywhere safe
    whenever you want a snapshot.

Restore:
    1. Stop the scheduled task (or just don't run --ingest):
           Disable-ScheduledTask -TaskName ContractEngineDailyIngest
    2. Copy your backup file over data\engine.db
    3. Re-enable the task:
           Enable-ScheduledTask -TaskName ContractEngineDailyIngest

    The engine will pick up where the backup left off on the next run.


--------------------------------------------------------------------------------
  Logs
--------------------------------------------------------------------------------

One file per day at logs\ingest-YYYY-MM-DD.log. Contains the full stdout
+ stderr of the day's run. Safe to delete old logs at any time.

For finer-grained history, the engine also writes a Run Log table inside
data\engine.db visible in the web UI at http://localhost:8080/run-log.
By default the engine prunes Run Log rows older than 365 days at the
end of each run.


--------------------------------------------------------------------------------
  Troubleshooting
--------------------------------------------------------------------------------

"FATAL: ASANA_PAT not set" in the log
    config\secrets.env is missing, mis-named, or the ASANA_PAT line is
    empty. See setup step 2.

"FileNotFoundError: data\inbox" or similar
    The data\ folder hasn't been created yet. Run scripts\run-ingest.bat
    once manually -- it creates the folders on first run.

The scheduled task says "ran" but no log was written
    Open Task Scheduler, find ContractEngineDailyIngest, check the
    History tab for the actual error. Most common: the bundle was moved
    and the task is pointing at the old path -- re-run
    install-scheduler.ps1 to update it.

Web UI shows old data
    Refresh the page (Ctrl-F5). The UI reads SQLite live -- if it's
    stale, the latest run probably hasn't happened yet.

Browser opens to "site can't be reached"
    The dev server is still booting. Wait two seconds and refresh.

OneDrive backup not appearing in the cloud
    OneDrive sync runs in your user session. If the scheduled task ran
    while you were logged out, the file is on disk but won't sync until
    your next login. This is fine -- the local DB stays authoritative.

"SSLCertVerificationError: self-signed certificate in certificate chain"
    Your corporate network has SSL inspection and the appliance's CA is
    NOT in the Windows machine cert store. The engine reads from the
    Windows store automatically (via the truststore library); when the
    cert is missing there it falls back to Python's bundled certifi,
    which won't know about the corp CA.

    Fix: ask IT to add the corporate inspection CA to the Windows
    machine cert store (Local Computer > Trusted Root Certification
    Authorities). Once it's there, no engine changes are needed --
    the next run will pick it up automatically.


--------------------------------------------------------------------------------
  Uninstalling
--------------------------------------------------------------------------------

1.  Right-click scripts\uninstall-scheduler.ps1 -> Run with PowerShell.
    Removes the daily task.

2.  If you want to keep the data: copy data\engine.db somewhere safe.

3.  Delete the ContractEngine\ folder. There is nothing else to clean up
    -- the engine doesn't touch the registry or system files.


--------------------------------------------------------------------------------
  Where the source lives
--------------------------------------------------------------------------------

https://github.com/PSbrk/Contract-Amount-Expiry-Engine

To rebuild the bundle from source (PowerShell, in a checkout):
    pip install -r requirements.txt -r requirements-build.txt
    pyinstaller engine.spec --clean --noconfirm
The result lands in dist\ContractEngine\.
