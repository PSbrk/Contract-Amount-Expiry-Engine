# SETUP

This file used to walk through the pre-migration Airtable + GitHub Actions
setup. As of the **2026-06-15 portable migration**, none of that is the
install path anymore.

## Where to look now

**If you are the operator installing the engine on a Windows machine:**
read `README.txt` inside the `ContractEngine/` bundle folder. It covers
unzipping, creating `config/secrets.env`, registering the daily Task
Scheduler job, and rotating the Asana PAT.

**If you are a developer working on the engine codebase:** read
[README.md](README.md) at the repo root. It covers the dev workflow,
test suite, and how to rebuild the bundle.

**If you are migrating accumulated operator data out of an old Airtable
base** (Vendor Aliases, Campus Map, Learned Mappings -- the three tables
that hold hand-curated work that doesn't re-derive): read
[tools/README.md](tools/README.md).

**If you want the historical migration spec** (decisions, phase plan,
trade-offs): read [MIGRATION-PLAN.md](MIGRATION-PLAN.md).

The Airtable era's `airtable_client.py` is archived in
[legacy/](legacy/README.md) and intentionally does NOT run as-is.
