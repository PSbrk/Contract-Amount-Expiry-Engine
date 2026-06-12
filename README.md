# Contract-Amount-Expiry-Engine

Tracks how much money has been spent against each contract in the Asana Contractor
Database, paces it against the contract term, writes summary numbers back to four
Asana custom fields, and emails alerts when contracts cross budget or pace thresholds.

## Architecture

| Piece | Job |
|---|---|
| **Engine** (this repo) | Python. Runs on GitHub Actions on a cron. Reads the Tableau transaction export from the Drive inbox, reads Asana, computes per-contract spend/pace/alarms, writes the Google Sheet dashboard, writes 4 Asana custom fields, POSTs new alerts to n8n. |
| **n8n Cloud** | 2-node workflow: Webhook trigger → Gmail Send (via the connected `ls.tipsandtricks@gmail.com` OAuth Gmail node). Just sends what the engine hands it. |
| **Google Sheet** | "Contract Amount Expiry Engine" dashboard + working/state tabs. Sheet ID lives in `config/settings.py`. |
| **Google Drive inbox** | Folder where the Tableau export is dropped manually. Folder ID in `config/settings.py`. |
| **Asana** | Contractor Database project. Source of contracts; destination of 4 custom-field values only. |

## Hard guardrails

- Asana is **read-only** except 4 custom-field values: `Spent so far`, `% Spent`,
  `Spending Rate`, `Spending Rate Alarm`.
- **Never** create, rename, or delete any project, section, field, option, task, or
  other field value in the Contractor Database. No structural changes ever.
- Until explicit approval, runs default to `DRY_RUN_ASANA=true` and never write.
- Writes are idempotent — a value is only written when it actually changed.

## Repo layout

```
config/        Non-secret configuration (GIDs, sheet IDs, filters, bands, recipients, campus map)
engine/        All compute and I/O modules
tests/         Pytest suite + sample export fixture
.github/       GitHub Actions workflow (daily cron + manual dispatch)
n8n/           Exported n8n workflow JSON
SETUP.md       One-time setup for the Google service account, sharing, and GitHub secrets
```

## Getting started

1. Read [SETUP.md](SETUP.md) and complete the one-time setup.
2. Copy `.env.example` to `.env` and fill in the three secrets for local runs.
3. `pip install -r requirements.txt`
4. Run a dry-run: `python -m engine.main --dry-run`.

## Build order

See [the original build prompt](#) (Section 15). Current step is tracked in the
task list. Each step lands in a focused diff; no Asana writes happen until you
approve them per-batch.
