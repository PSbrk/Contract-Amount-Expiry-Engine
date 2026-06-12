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

## 5. Your Asana automation rule (final step, deferred to build Step 5)

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
- [ ] Asana `Alarms → ALARM` email rule built (deferred to build Step 5)
