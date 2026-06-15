# Contract-Amount-Expiry-Engine

Tracks how much money has been spent against each contract in the Asana
Contractor Database, paces it against the contract term, writes five summary
values back to Asana, and surfaces alerts via a binary `Alarms` field — an
Asana automation rule (built by the operator) is what sends the email when
that field flips to `ALARM`.

## Architecture

| Piece | Job |
|---|---|
| **Engine** (this repo) | Python. Runs on GitHub Actions cron + manual dispatch. Pulls the latest Tableau export from the Airtable Inbox, reads Asana, computes per-contract spend / pace / alarms, populates the Airtable Dashboard, writes five Asana custom-field values. |
| **Airtable** | Single base — Inbox (attachment dropzone), Dashboard (one row per live contract), Needs Tagging (ambiguous attribution groupings), Vendor Aliases, Campus Map, Learned Mappings, State, Run Log. Hosts an Interface bar chart for `% Spent` per contract. |
| **Asana** | Contractor Database project — source of contracts; destination of five custom-field values. The operator builds an automation rule on `Alarms` that emails when it flips to `ALARM`. |

## Hard guardrails

- Asana is **read-only** except five custom-field values on contracts passing the live gate: `Spent so far`, `% Spent`, `Spending Rate`, `Spending Rate Alarm`, and `Alarms`.
- Never create, rename, delete, or modify any project, section, custom field, option, task, or non-listed field value in the Contractor Database. No structural changes ever.
- Until explicit approval, runs default to `DRY_RUN_ASANA=true` and write nothing to Asana.
- Writes are idempotent — a value is only written when it actually changed, so the Asana automation rule fires once per trip and doesn't re-fire on no-op runs.
- Raw transactions (~50k rows) are processed in memory and never stored in Airtable. Airtable holds aggregates, state, and working tables only.

## Repo layout

```
config/        Non-secret configuration (Asana + Airtable + filter + threshold constants)
engine/        Compute and I/O modules
tests/         Pytest suite
.github/       GitHub Actions workflows — daily `ingest` cron + manual dispatch
SETUP.md       One-time setup walkthrough (Airtable base + PAT, Asana PAT, GitHub secrets)
```

## Getting started

1. Read [SETUP.md](SETUP.md) and complete the one-time setup.
2. For local runs, `cp .env.example .env` and paste the three secrets.
3. `pip install -r requirements.txt`
4. Verify the Asana schema matches the engine's expectations:
   ```
   python -m engine.audit
   ```
   Exits `0` on pass. Any `[FAIL]` line names the offending field, option, or section.
5. Provision the eight Airtable tables (idempotent — safe to re-run):
   ```
   python -m engine.main --provision --dry-run    # preview the plan
   python -m engine.main --provision              # apply
   ```
6. Drop a Tableau export into the Airtable **Inbox** table as an attachment, then:
   ```
   python -m engine.main --ingest
   ```
   The engine: pulls the newest unprocessed attachment, hashes it for dedup,
   parses, applies the account/dept filter, prints signed sums, **runs
   attribution against your Asana contracts**, writes any unmatched / ambiguous
   groupings to the Airtable `Needs Tagging` table for one-time assignment,
   **computes per-contract Spent so far / % Spent / Spending Rate / Alarms
   and populates the Airtable Dashboard**, **writes the five Asana custom-field
   values** (idempotent — only fields that actually changed), writes a Run Log
   row, and marks the Inbox record `Processed`.
   Each subsequent run also **promotes** any Needs Tagging rows you filled in
   to the `Learned Mappings` table, so the same grouping never needs to be
   tagged twice.

   By default, Asana writes are **dry-run** (logged only) until you opt in
   per the rollout sequence in `SETUP.md` §6.
7. To sanity-check the parser against a file on disk without round-tripping
   through Airtable:
   ```
   python -m engine.main --ingest-file C:\path\to\Transactions.csv
   ```

## Build order

Nine-step build per the prompt. Step 1 (config + read-only Asana audit) is
landed; subsequent steps add Airtable wiring, ingestion, attribution,
computation, Asana writes (gated on per-batch approval), change detection, a
Tableau REST source stub, and the GitHub Actions schedule. Each step lands in
a focused diff; no Asana writes happen until the operator approves them
per-batch.
