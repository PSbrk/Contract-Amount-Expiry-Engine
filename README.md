# Contract-Amount-Expiry-Engine

Tracks how much money has been spent against each contract in the Asana
Contractor Database, paces it against the contract term, writes five summary
values back to Asana, and surfaces alerts via a binary `Alarms` field -- an
Asana automation rule (built by the operator) is what sends the email when
that field flips to `ALARM`.

This README is the **developer's** entry point. The **operator's** entry point
is the plain-ASCII `README.txt` shipped inside the `dist/ContractEngine/`
bundle.

## Architecture (post-migration, 2026-06-15)

Portable Windows folder. No third-party SaaS subscription, no cloud account,
no IT approval beyond the one Asana PAT.

| Piece | Job |
|---|---|
| **EngineApp.exe** | PyInstaller `--onedir` bundle of the Python engine. Subcommands: `--audit`, `--provision`, `--ingest`, `--ingest-file PATH`, `--ui`. |
| **data/engine.db** | SQLite. Single source of truth for Inbox / Dashboard / Needs Tagging / Vendor Aliases / Campus Map / Learned Mappings / State / Run Log. |
| **data/inbox/** | Drop Tableau exports here. The engine moves processed files to `data/processed/<hash>-<name>` on success. |
| **engine/ui/ (Flask)** | Localhost-only web UI on `:8080`. Operator edits Needs Tagging answers and browses everything else. Pico.css, no JS framework. |
| **Windows Task Scheduler** | Daily 08:30 cron registered by `scripts/install-scheduler.ps1`. Runs `scripts/run-ingest.bat`. |
| **OneDrive backup** | `shutil.copy2(data/engine.db, ONEDRIVE_BACKUP_PATH)` after every successful ingest. OneDrive sync handles the cloud upload; no Graph API auth needed. |
| **Asana** | Source of contracts; destination of FIVE custom-field values. The operator builds an automation rule on `Alarms == ALARM` that emails. |

## Hard guardrails

- Asana is **read-only** except five custom-field values on contracts passing
  the live gate: `Spent so far`, `% Spent`, `Spending Rate`,
  `Spending Rate Alarm`, and `Alarms`.
- Never create, rename, delete, or modify any project, section, custom field,
  option, task, or non-listed field value in the Contractor Database. No
  structural changes ever.
- Until explicit operator approval, runs default to `DRY_RUN_ASANA=true` and
  write nothing to Asana.
- Writes are idempotent -- a value is only written when it actually changed,
  so the Asana automation fires once per trip and doesn't re-fire on no-op runs.

## Repo layout

```
config/        Non-secret configuration + the 8-table declarative schema.
engine/        Engine modules: sqlite_client, ingest, attribution, compute,
               state, asana_*, ui/ (Flask app).
tests/         Pytest suite. 325 tests, all green.
scripts/       .bat + .ps1 the bundle ships with for install + daily run.
tools/         One-shot migration helpers: export_from_airtable +
               import_to_sqlite. See tools/README.md.
legacy/        Frozen pre-migration code (airtable_client.py). Will not run
               as-is; see legacy/README.md.
engine.spec    PyInstaller spec. `pyinstaller engine.spec --clean --noconfirm`
               produces dist/ContractEngine/.
README.txt     OPERATOR-facing install guide. Shipped inside the bundle.
MIGRATION-PLAN.md  Historical migration spec (all six phases complete).
```

## Dev workflow

```powershell
# One-time:
pip install -r requirements.txt
pip install -r requirements-build.txt   # for PyInstaller

# Run tests:
python -m pytest

# Smoke a parser run against a file (no Asana required):
python -m engine.main --ingest-file path\to\Transactions.csv

# Provision the SQLite schema in data\engine.db (idempotent):
python -m engine.main --provision

# Boot the web UI on http://localhost:8080:
python -m engine.main --ui

# Verify Asana schema matches the engine's expectations (needs ASANA_PAT):
python -m engine.main --audit

# Rebuild the shippable bundle:
pyinstaller engine.spec --clean --noconfirm
# -> dist/ContractEngine/ (~98 MB), ready to zip + ship.
```

For end-to-end installation on a target Windows machine, see the bundled
`README.txt`.

For migrating accumulated operator data out of an old Airtable base, see
[tools/README.md](tools/README.md).
