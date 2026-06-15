# tools/

One-shot scripts that aren't part of the running engine -- migration helpers,
data dumps, etc. Pytest ignores this directory; `requirements.txt` does NOT
list anything these tools need beyond the runtime engine deps.

## Migrating operator-curated data from the old Airtable base

The Inbox / Dashboard / Needs Tagging / State / Run Log tables are all
re-derived on the next `--ingest`, so they don't need to come across. Three
tables hold accumulated operator hand-work and **do** need to:

- **Vendor Aliases** -- Asana contract name -> Tableau Vendor formatting variants
- **Campus Map** -- Tableau campus code overrides + drop flags
- **Learned Mappings** -- every Needs Tagging answer the operator has ever filled

Two-step migration:

### 1. Dump Airtable to JSON (run on the dev machine)

The old `pyairtable` dep is no longer in `requirements.txt`. Install it
temporarily into your venv just for this export:

```powershell
pip install pyairtable
$env:AIRTABLE_PAT = "patABC..."        # PAT scoped to the source base
$env:AIRTABLE_BASE_ID = "appXYZ..."    # appId of the source base
python -m tools.export_from_airtable --out airtable_export.json
```

The script prints row counts per table when it finishes.

`airtable_export.json` is operator data -- think of it like a database
backup. Do not commit it.

### 2. Import the JSON into SQLite

```powershell
python -m engine.main --provision               # create data/engine.db
python -m tools.import_to_sqlite --in airtable_export.json
```

By default the importer is idempotent: it catches UNIQUE-constraint
collisions and reports them as `skipped 3 duplicate`, so re-running is
safe. Pass `--truncate` to wipe the three operator tables first if you
want a clean reset.

### 3. (Optional) Ship the pre-seeded database in the bundle

Copy the seeded `data/engine.db` into the production bundle before zipping:

```powershell
Copy-Item data/engine.db dist/ContractEngine/data/engine.db
```

Operators on the target machine then start with all of your accumulated
attribution knowledge already in place -- no cold-start period of re-tagging
the same vendors.

## tools/import_to_sqlite.py only touches three tables

It never writes to Inbox, Dashboard, Needs Tagging, State, or Run Log. Those
get populated by the engine's normal `--ingest` cycle; importing stale rows
would just be confusing.
