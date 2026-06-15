# Migration Plan: Portable Local-First Edition

**Status:** Approved, not started. Pick up here next session.
**Created:** 2026-06-15
**Why:** Airtable Free 1,000-record limit hit; Google Workspace + Microsoft 365
blocked by IT at life.church; no budget for Airtable Team plan.

---

## The decision

Migrate from the current Airtable + GitHub Actions architecture to a **fully
portable Windows app**: drop a folder onto any Windows machine, configure one
secret, register a scheduled task, done. No third-party SaaS, no recurring
cost, no IT approval needed beyond the Asana PAT.

The user explicitly chose this over:
- Waiting for IT approval (indefinite blocker)
- Reducing Airtable footprint via $-threshold (rejected: don't want to lose
  visibility into low-$ unmatched groups)
- Moving to git-committed CSVs (acceptable but Needs Tagging UX too clunky)

## Confirmed decisions

| Question | Answer |
|---|---|
| **UI scope** | Full web UI (Flask on `localhost:8080`). Tabs per table. Editable Needs Tagging is the headline workflow. |
| **Backup model** | Engine auto-copies `data/engine.db` to a OneDrive-synced folder after every successful `--ingest`. OneDrive handles the cloud sync; no Microsoft Graph API needed. |
| **Engine entry points** | `EngineApp.exe --ingest` (cron path) and `EngineApp.exe --ui` (opens Flask app). Same exe, different subcommands. |
| **Tableau ingestion** | Drop files into `data/inbox/`; engine moves processed files to `data/processed/`. The existing `LocalFileSource` already does ~80% of this. |
| **Scheduling** | Windows Task Scheduler runs `scripts\run-ingest.bat` daily. NOT GitHub Actions. |
| **Secrets** | `config\secrets.env` (file in folder, gitignored). Contains `ASANA_PAT` only. |

## Target folder layout

```
ContractEngine\                          ← portable; copy to any Windows machine
├── EngineApp.exe                        ← PyInstaller-bundled launcher
├── _internal\                           ← Python runtime + libs (PyInstaller-managed)
├── data\
│   ├── engine.db                        ← SQLite. ALL persistent state.
│   ├── inbox\                           ← drop Tableau exports here
│   └── processed\                       ← engine moves files here after ingest
├── config\
│   ├── secrets.env                      ← ASANA_PAT, ONEDRIVE_BACKUP_PATH (user creates per machine)
│   └── settings.py                      ← non-secret config (committed to source; bundled)
├── logs\
│   └── ingest-YYYY-MM-DD.log
├── scripts\
│   ├── run-ingest.bat                   ← what Task Scheduler calls
│   ├── run-ui.bat                       ← double-click to open localhost:8080 in default browser
│   ├── install-scheduler.ps1            ← one-time: register the daily task
│   └── uninstall-scheduler.ps1          ← clean removal
└── README.txt                           ← plain-text install steps
```

**Distribution:** zip the entire `ContractEngine\` folder, share via OneDrive
or USB. Recipient unzips, fills `config\secrets.env`, runs the install
script. ~10 minutes per new machine.

## Phase plan

Estimates assume one focused session each. Total: ~24-34 hours of work.

### Phase 1 — SQLite storage layer (8-10 hrs)
- New module: `engine/sqlite_client.py`
- Schema: reuse the existing declarative `config/airtable_schema.py` —
  translate fieldType → SQLite column types in a small mapper.
- All public functions of `airtable_client.py` get SQLite equivalents:
  `ensure_schema`, `get_unprocessed_inbox` (now reads `data/inbox/`),
  `upsert_dashboard_row`, `upsert_needs_tagging_group`,
  `promote_filled_needs_tagging`, `cleanup_stale_needs_tagging`,
  `upsert_dashboard_row`, `load_state_priors`, `upsert_state_for_contract`,
  `cleanup_stale_state`, `append_run_log`, `prune_run_log_older_than`.
- Test infrastructure: replace `_RecordsBase` / `_FakeBase` fakes with an
  in-memory SQLite database (`sqlite3.connect(':memory:')`). Existing tests
  should mostly carry over.
- `engine/main.py` flips from `airtable_client` imports to `sqlite_client`.

**Definition of done:** `python -m pytest -x -q` green. `python -m engine.main
--ingest-file <path>` populates `engine.db` end to end.

### Phase 2 — Inbox folder convention (1-2 hrs)
- Replace `AirtableInboxSource` with `LocalInboxSource`:
  - Scans `data/inbox/*.csv` and `data/inbox/*.xlsx`.
  - Picks oldest by mtime.
  - SHA-256 hashes for dedup against the existing `file_hash_already_processed`
    function (now backed by SQLite).
  - On success, moves the file to `data/processed/<hash>-<filename>` and
    inserts an Inbox table row marking it processed.
- `engine/main.py`'s source selector replaces `airtable_inbox` with
  `local_inbox` as the default.

**Definition of done:** Drop a file in `data/inbox/`, run `--ingest`, file
moves to `data/processed/` and is recorded in the Inbox table.

### Phase 3 — Flask web UI (10-14 hrs)
- New package: `engine/ui/` with Flask app
- Routes:
  - `/` — Dashboard overview (live contracts with Spent / % / pace / Alarms)
  - `/needs-tagging` — list of unmatched + ambiguous groups; click row → inline edit
    "Assign Contract"; save POST writes via `sqlite_client`.
  - `/dashboard-detail/<contract_gid>` — drill-in: contract row + recent
    transactions attributed to it.
  - `/run-log` — Run Log rows, newest first, paginated
  - `/vendor-aliases`, `/campus-map`, `/learned-mappings` — small admin tables
    with full CRUD
  - `/state` — read-only view of the State table
  - `/settings` — view-only display of `config/settings.py` values + env state
- Styling: minimal CSS — table-heavy, fast. Probably Pico.css or hand-rolled.
- Auth: none (localhost only — single-user single-machine context)
- Launch: `EngineApp.exe --ui` starts Flask on port 8080 and opens the
  default browser to `http://localhost:8080`. Ctrl-C in the spawned console
  to stop.

**Definition of done:** Operator can fill `Assign Contract` on a Needs Tagging
row entirely in the browser; refresh shows the change; next `--ingest` promotes
it correctly.

### Phase 4 — OneDrive backup (1-2 hrs)
- `config/secrets.env` gains `ONEDRIVE_BACKUP_PATH=C:\Users\<user>\OneDrive\Backups\engine.db`
- After each successful `--ingest`, `engine/main.py` does
  `shutil.copy2(data/engine.db, ONEDRIVE_BACKUP_PATH)`.
- Wrapped in try/except so a backup failure logs a warning but doesn't fail
  the ingest run.
- Restore: copy the backup back to `data/engine.db` and run `--ingest` to
  pick up where it left off.

**Definition of done:** After a manual `--ingest`, the .db is also at the
OneDrive path with the same mtime.

### Phase 5 — PyInstaller packaging (3-5 hrs)
- New file: `engine.spec` (PyInstaller spec)
  - Entry point: `engine/main.py` with the existing `main()` function
  - `--onedir` mode (better antivirus profile than `--onefile` and easier
    to debug)
  - Hidden imports for `pyairtable` removal: ensure `sqlite3`, `flask`,
    `requests`, `asana`, `rapidfuzz`, `openpyxl`, `pandas` all bundle
- Iterate: build → copy `dist\ContractEngine\` to a VM or clean Windows
  partition → run → fix the missing-import or DLL issues that surface.
- Antivirus check: SmartScreen and Windows Defender don't flag the binary.
- Output: `dist\ContractEngine\` is the shippable folder.

**Definition of done:** Zip `dist\ContractEngine\`, send to a fresh Windows
machine (or test laptop), unzip, configure `secrets.env`, run `--ingest`
against a known fixture, get expected output.

### Phase 6 — .bat scripts + Task Scheduler + README (2-3 hrs)
- `scripts\run-ingest.bat`:
  ```batch
  @echo off
  cd /d "%~dp0\.."
  EngineApp.exe --ingest >> logs\ingest-%date:~10,4%-%date:~4,2%-%date:~7,2%.log 2>&1
  ```
- `scripts\run-ui.bat`:
  ```batch
  @echo off
  cd /d "%~dp0\.."
  start "" "http://localhost:8080"
  EngineApp.exe --ui
  ```
- `scripts\install-scheduler.ps1` registers a Task Scheduler entry:
  trigger daily at 02:00 local time, action = run `run-ingest.bat`,
  user = current user, run whether logged in or not.
- `README.txt` (plain ASCII, no markdown — operator can open in Notepad):
  install steps, troubleshooting, where logs live, how to back up.

**Definition of done:** Cold-install on a fresh machine completes in <10
minutes following only README.txt. The next 02:00 fires automatically.

## What gets deleted or archived

The Airtable / GitHub Actions architecture is no longer the supported path.
Migration deletes:

- `engine/airtable_client.py` → archive to `legacy/airtable_client.py`
  (in case we ever go back). Same for the `_RecordsBase` / `_FakeBase`
  test fakes.
- `.github/workflows/ingest.yml` and `.github/workflows/provision.yml` →
  delete. The cron is now local.
- `pyairtable` line from `requirements.txt`
- `AIRTABLE_PAT` and `AIRTABLE_BASE_ID` from `.env.example`
- `engine/audit.py` is KEPT — Asana schema audit is still useful even
  locally
- Provision workflow concept survives as `EngineApp.exe --provision`
  which creates the SQLite schema in `data/engine.db`

## What stays unchanged

These modules are storage-layer-agnostic and translate 1:1:
- `engine/asana_client.py` (read-only Asana wrapper)
- `engine/asana_contracts.py` (Contract loader)
- `engine/asana_writer.py` (gated Asana writes)
- `engine/attribution.py` (the attribution algorithm)
- `engine/campus_map.py` (runtime crosswalk)
- `engine/compute.py` (per-contract compute)
- `engine/filters.py` (in-scope filter)
- `engine/ingest.py` `parse_tableau_export` (the parser itself)
- `engine/state.py` (change detection)
- `config/settings.py` (most of it)
- `config/airtable_schema.py` → rename to `config/schema.py` (declarative
  schema is reused; just translated to SQLite types instead of Airtable
  fieldType strings)

## Test strategy

- All non-Airtable tests stay (compute, attribution, state, etc.) — 283 - ~50
  Airtable-specific = ~233 tests still pass unchanged.
- The ~50 Airtable-specific tests get rewritten against an in-memory SQLite
  database. Faster than the current fakes; closer to the real I/O surface.
- New tests for:
  - `LocalInboxSource` (file picking, mtime ordering, dedup, move-to-processed)
  - Flask routes (use `client = app.test_client()`)
  - OneDrive backup (mock `shutil.copy2`)
  - PyInstaller spec sanity (separate, manual)

## How to resume next session

1. Read this file first — it captures every decision.
2. Verify `git status` is clean and `git pull` to be on latest main.
3. Read the **current** state of:
   - `engine/airtable_client.py` — the API surface that needs porting
   - `config/airtable_schema.py` — the declarative schema to reuse
   - `tests/test_airtable_client.py` — the test patterns to port
4. **Start with Phase 1** — SQLite storage layer. The other phases depend
   on it but it's also the cleanest to do in isolation.
5. End each phase with a commit. Phases are roughly session-sized.

## Open questions (resolve next session)

- **Pandas in the bundle:** pandas pulls in NumPy + SciPy and the bundle
  bloats fast (~150 MB). Options:
  - Accept the size (~200 MB folder)
  - Replace pandas with `csv` + `openpyxl` + hand-rolled grouping (more
    work, smaller bundle)
  - Acceptable to keep pandas; we'll iterate if size is a problem.
- **Asana PAT rotation:** if the operator's PAT expires, they need a path
  to update it without re-installing. `secrets.env` is in the folder — they
  edit it in Notepad. Document this.
- **OneDrive auth:** if `ONEDRIVE_BACKUP_PATH` points at a synced folder,
  no auth is needed (OneDrive's sync client handles it). If the user wants
  the engine to push directly via Microsoft Graph, that's the same IT
  blocker we already have. Stick with the synced-folder pattern.
- **Inbox table is still useful as a log even in local mode** — keep it
  as a SQLite table that gets one row per processed file. The Inbox folder
  is the "queue"; the table is the "history".
