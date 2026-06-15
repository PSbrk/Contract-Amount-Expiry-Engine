# legacy/

Code retained for archaeological reference only. Nothing here is on the
import path of the engine or its tests; `pytest.ini` confines pytest to
`tests/` so files here will not be discovered.

## airtable_client.py

The pre-migration Airtable client. Frozen at the commit it was moved here
and **will not run as-is** — it imports `pyairtable` (no longer in
`requirements.txt`) and `config.airtable_schema` (the schema declarations
moved into `config/schema.py`). To revive it, restore both dependencies
and any callers from git history.

The Airtable era's `_RecordsBase` / `_FakeBase` test fakes lived in
`tests/test_airtable_client.py`, which was deleted as part of the migration.
Recover via `git log --follow --all -- tests/test_airtable_client.py` if
needed.

## What stays in the engine

The 8-table schema (`config/schema.py`) and most domain modules
(attribution, compute, state, asana_client, asana_writer, etc.) are
storage-agnostic and survived the migration unchanged.
