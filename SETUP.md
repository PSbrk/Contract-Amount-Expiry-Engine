# SETUP — one-time, do this before the engine can run

You only do this once. After it's done, the engine runs headless on GitHub
Actions and you only return here if you rotate a token.

## 1. Create the Airtable base + PAT

1. Sign in at https://airtable.com. The free plan is fine — the engine never
   stores raw transactions in Airtable, only aggregates and working state.
2. **Create the base**: Home → **+ Create a base** → **Start from scratch** →
   name it `Contract Amount Expiry Engine` → Create. Leave it empty — the
   engine provisions the eight tables itself on first run.
3. **Grab the base ID**: open the base, look at the URL — the `app...` segment
   right after `airtable.com/`. That's `AIRTABLE_BASE_ID`.
4. **Create a Personal Access Token**: https://airtable.com/create/tokens →
   **Create new token**.
   - Name: `Contract Amount Expiry Engine`
   - **Scopes** (all four are required):
     - `data.records:read`
     - `data.records:write`
     - `schema.bases:read`
     - `schema.bases:write` — lets the engine create the eight tables
   - **Access** → **Add a base** → pick the base from step 1.2. Do **not**
     grant access to all bases; keep the token scoped to one.
   - **Create token** and copy it immediately — Airtable only shows it once.
     That's `AIRTABLE_PAT`.

## 2. Create / rotate the Asana PAT

If a token exists from a previous build that ever touched chat or files,
rotate it.

1. Open https://app.asana.com/0/my-apps (Profile → **My Settings → Apps →
   Manage Developer Apps**).
2. Under **Personal access tokens**, **Deauthorize** any stale tokens.
3. **+ Create new token** → description `Contract Amount Expiry Engine` →
   Create. Copy it immediately. That's `ASANA_PAT`.

## 3. Put the three secrets into GitHub Actions

Repo: https://github.com/PSbrk/Contract-Amount-Expiry-Engine
→ **Settings → Secrets and variables → Actions → New repository secret**.

| Name | Value |
|---|---|
| `AIRTABLE_PAT` | from step 1.4 |
| `AIRTABLE_BASE_ID` | the `app...` from step 1.3 |
| `ASANA_PAT` | from step 2.3 |

## 4. (Optional) Local development

For running the engine on your laptop without GitHub Actions:

```powershell
cd C:\Users\philip.seabrook\Contract-Amount-Expiry-Engine
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env: paste ASANA_PAT, AIRTABLE_PAT, AIRTABLE_BASE_ID
python -m engine.audit
```

`.env` is gitignored. CI does not use it — secrets come straight from GitHub
Actions environment variables.

## 4b. How attribution works in --ingest

Each `--ingest` run does these stages in order:

1. **Promote** — any rows in the `Needs Tagging` table where you've filled
   `Assign Contract` are moved into `Learned Mappings` (so the same grouping
   is auto-attributed forever after), then deleted from Needs Tagging.
2. **Parse + filter** — the Inbox attachment is parsed and filtered to the
   in-scope accounts/depts (per `config/settings.py`).
3. **Attribute** — for each `(Campus, Dept, Account No, Vendor)` grouping
   in the in-scope rows, the engine tries to identify the right Asana
   contract by:
   - first consulting `Learned Mappings` (your prior answer wins);
   - else fuzzy-matching the Tableau `Vendor` against Asana contract names
     and the `Vendor Aliases` table (rapidfuzz WRatio, threshold 90);
   - then narrowing to contracts whose Asana `Campus` set (after crosswalk)
     covers the Tableau campus — `All Campuses` matches any, `INT` is dropped.
4. **Needs Tagging upsert** — groups that are ambiguous (multiple contracts
   match) or unmatched (no contract matches) become rows in the `Needs Tagging`
   Airtable table. The engine fills in: Group Key, Campus, Dept, Account No,
   Vendor, Sample Record Description, $ in group, engine's candidate matches
   (in Notes). The `Assign Contract` field is left for you.

To tag an ambiguous / unmatched row:
1. Open the `Needs Tagging` view in your Airtable base.
2. Read `Sample Record Description` + the candidate suggestions in
   `Engine Candidates`. (The `Notes` column is yours — the engine never
   writes there. Use it for your own annotations.)
3. Type the right Asana contract task name into `Assign Contract`. (Match the
   Asana name exactly — copy/paste from Asana is safest. The engine
   validates against the open contracts list at promotion time, so a typo
   is logged as a warning and the row stays put for you to correct.)
4. Run `--ingest` again. The next run promotes your answer into
   `Learned Mappings` automatically, **deletes the Needs Tagging row** (its
   historical record now lives in Learned Mappings), and that grouping is
   auto-attributed from then on. The engine also drains the promotion queue
   when `--ingest` exits with no new data — you don't have to wait for a
   fresh export.

If the engine often picks the wrong vendor for a contract, add the Tableau
vendor variant to the `Vendor Aliases` table (one row per contract; comma- or
newline-separated `Aliases` field). The next `--ingest` picks it up.

## 5. Build the Airtable Interface (bar chart) — UI-only

Spec §3 calls for a bar chart of `% Spent` per contract on the Dashboard table.
Airtable Interfaces can only be built in the UI (not via API). Do this once
the engine starts populating Dashboard (build Step 4); rebuilding from an
empty table is harmless if you want it ready earlier.

1. Open the base in Airtable → **Interfaces** (left sidebar) → **+ Start
   building**.
2. Pick **Dashboard** as the layout starting point → choose the **Record
   review** or **Dashboard** template (either works).
3. Delete any pre-filled elements you don't want. **+ Add element → Chart**.
4. Configure the chart:
   - **Source table**: `Dashboard`
   - **Chart type**: `Bar`
   - **X-axis (category)**: `Contract`
   - **Y-axis (value)**: `% Spent`
   - **Sort**: `% Spent` descending (so over-budget contracts top the chart)
   - **Color**: optionally segment by `Alarms` (Clear vs ALARM) for a quick
     red/green read of the portfolio
5. Add a filter `Last Updated is within → today` if you only want the
   freshly-computed contracts (otherwise stale rows appear).
6. **Publish** the interface. The shareable URL is the dashboard the team
   bookmarks. Permissions inherit from the base.

You can iterate later — add a stacked bar for `Spent so far` vs
`Contract Amount`, a count of contracts in each `Spending Rate Alarm` band,
etc. The engine's contract is just to keep `Dashboard` current.

## 6. Turn on Asana writes (gated rollout)

The engine ships with `DRY_RUN_ASANA=true` as the default — every
`--ingest` logs what it would write to Asana but does not call the API.
This protects against any first-run surprises. The recommended rollout:

### 6a. Inspect the dry-run output

Run `python -m engine.main --ingest` against your real Airtable + Asana
setup and look at the `Asana writes [DRY RUN]` section:

```
Asana writes [DRY RUN]
  contracts evaluated:    NN
  contracts with changes: NN
  contracts no-change:    NN
  fields that WOULD write: NN
  [DRY] <Contract Name> (<gid>)
        Spent so far: 0->1234.56, % Spent: 0->12.35, ...
```

For each contract you'll see exactly which fields would be touched and the
old → new transitions. **Eyeball this carefully before continuing.**
Especially look for:
- contracts you don't recognize getting writes (suggests attribution drift)
- huge transitions (e.g. 0 → very large numbers) that look implausible
- alarm transitions you didn't expect (Clear → ALARM on a contract you
  thought was fine; ALARM → Clear that should still be tripping)

### 6b. Write to ONE test contract first

Pick a single contract you want to verify. Get its Asana task GID from the
URL when you open the task in Asana (`https://app.asana.com/0/<project_gid>/<task_gid>`).
Add to your local `.env`:

```
WRITE_TEST_CONTRACT=<that_task_gid>
DRY_RUN_ASANA=false
```

Run `python -m engine.main --ingest`. The output will say
`[LIVE (test contract <gid> only)]` and write to only that one task.
Open the task in Asana, verify the five fields look right.

### 6c. Broaden to all live contracts

Once the test contract looks correct in Asana, edit `.env`:

```
WRITE_TEST_CONTRACT=
DRY_RUN_ASANA=false
```

(blank `WRITE_TEST_CONTRACT` removes the filter). Next `--ingest` writes to
every live contract. The write is **idempotent** — only fields that actually
changed get touched, so re-running on the same data is a no-op.

### 6d. Hand off to the scheduled cron (when Step 8 lands)

Once you trust the live writes, set `DRY_RUN_ASANA=false` in the GitHub
Actions repository secrets and let the daily cron drive it.

## 7. Your Asana automation rule (final step)

The engine sets `Alarms` to `ALARM` on a contract when any budget band
(75% / 90% / 100% / Over) is reached **or** runaway pace trips (subject to the
30-day pace guard and minimum-spend floor). It writes the field idempotently —
only when it actually changes — so a rule on this field fires once per trip
and does not re-fire on subsequent runs while the value stays `ALARM`.

Build the rule in Asana once the engine is writing the field (after build
Step 5 approval):

1. Open the Contractor Database project → **Customize → Rules → + Add rule**.
2. **Trigger**: `Alarms` field changes to `ALARM`.
3. **Action**: Send email to `philip.seabrook@life.church`. Add per-PM
   recipients later as desired — the `PM Email` text field is read by the
   engine and exposed on the task for use here.

Escalation note: `Alarms` is binary. Once a contract trips `75%` and goes to
`ALARM`, climbing to `90%` / `100%` / `Over` does **not** re-fire this rule
(the field stays `ALARM`). If you want email at every band, build separate
rules on the **`Spending Rate Alarm`** field instead, which the engine keeps
current with the granular band detail.

---

### Checklist

- [ ] Airtable base created and left empty
- [ ] `AIRTABLE_PAT` generated (4 scopes, scoped to one base) and copied
- [ ] `AIRTABLE_BASE_ID` (the `app...`) captured
- [ ] `ASANA_PAT` rotated to a fresh token
- [ ] All three secrets pasted into GitHub Actions
- [ ] `python -m engine.audit` returns 0 once Step 1 is landed
- [ ] `python -m engine.main --provision` creates the 8 tables (Step 2)
- [ ] `python -m engine.main --ingest` processes an Inbox attachment (Step 2)
- [ ] Airtable Interface bar chart built (after Dashboard starts populating in Step 4)
- [ ] Dry-run `--ingest` output reviewed; one test contract written and verified (§6a + 6b)
- [ ] `DRY_RUN_ASANA=false` + `WRITE_TEST_CONTRACT=` (empty) in `.env`; all live contracts writing (§6c)
- [ ] Asana `Alarms → ALARM` email rule built
