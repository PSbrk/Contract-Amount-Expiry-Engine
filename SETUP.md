# SETUP — one-time, do these before the engine can run

You only do this once. After it's done, the engine runs headless on GitHub Actions.

## 1. Create the Google service account

The engine logs into Google as a robot user called a **service account**. You create
it under a Google Cloud project owned by your personal `ls.tipsandtricks@gmail.com`
account — no life.church admin involvement needed.

1. Go to https://console.cloud.google.com/ and sign in as `ls.tipsandtricks@gmail.com`.
2. Top bar → project dropdown → **New Project**. Name it `contract-expiry-engine`
   (or anything). Create it.
3. With that project selected, open the left nav → **APIs & Services → Library**.
   Search for and **Enable** each of these:
   - **Google Sheets API**
   - **Google Drive API**
4. Left nav → **IAM & Admin → Service Accounts** → **+ Create service account**.
   - Name: `contract-expiry-engine`
   - Click **Create and Continue**.
   - Skip the optional role grants (we use per-resource sharing instead). **Done**.
5. Click into the new service account → **Keys** tab → **Add Key → Create new key →
   JSON → Create**. A `.json` file downloads. Keep it; this is the engine's
   identity. Treat it like a password.
6. From the same page, copy the **service-account email** (looks like
   `contract-expiry-engine@<project-id>.iam.gserviceaccount.com`). You'll paste it
   into share dialogs in the next step.

## 2. Share the Google resources with the service account

Service accounts don't get implicit access to your files — even files that are
"anyone with the link". You have to share each resource explicitly with the
service-account email from step 1.6.

| Resource | Permission | Link |
|---|---|---|
| Drive **inbox folder** (where Tableau exports drop) | **Viewer** | https://drive.google.com/drive/folders/1q_SdjiC-0VKhYdbsz2TX5WP5VulU-h3j |
| **Dashboard Sheet** | **Editor** | https://docs.google.com/spreadsheets/d/16JEcoVozcOuV_6kxMxqcHMSfRiuWOivmqwKWyT_DA3I/edit |
| **Capital Project Breakdown** (redundancy lookup only) | **Viewer** | https://docs.google.com/spreadsheets/d/1HTX7NVQYso56CL25g4TE1yxY7Nl5luSkMosyhfK7iRo/edit |

For each: click **Share**, paste the service-account email, set the permission,
**uncheck "Notify people"** (it would bounce — it's not a real mailbox), Send.

## 3. Put secrets into GitHub Actions

Repo: https://github.com/PSbrk/Contract-Amount-Expiry-Engine
→ **Settings → Secrets and variables → Actions → New repository secret**.
Create exactly these three:

| Name | Value |
|---|---|
| `ASANA_PAT` | Your fresh Asana Personal Access Token (the one you generated). |
| `GOOGLE_CREDENTIALS` | The **entire contents** of the JSON file from step 1.5 (open it in a text editor, copy-all, paste). |
| `N8N_WEBHOOK_URL` | Filled in during build Step 5 once the n8n workflow exists. Leave blank for now. |

## 4. (Optional) Set up local development

For running the engine on your laptop without GitHub Actions:

```powershell
cd C:\Users\philip.seabrook\Contract-Amount-Expiry-Engine
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env: paste ASANA_PAT, point GOOGLE_CREDENTIALS_FILE at the JSON path
```

Then dry-run: `python -m engine.main --dry-run`.

## 5. n8n workflow

Built in build Step 5. It's a 2-node flow: **Webhook** trigger → **Gmail Send**
node using the already-connected `ls.tipsandtricks@gmail.com` OAuth Gmail account.
Once published, copy the production webhook URL into the `N8N_WEBHOOK_URL` GitHub
secret.

---

### Checklist

- [ ] Service account created, JSON key downloaded
- [ ] Sheets API + Drive API enabled in the same Cloud project
- [ ] Drive inbox folder shared (Viewer) with the service-account email
- [ ] Dashboard Sheet shared (Editor) with the service-account email
- [ ] Capital Project Breakdown sheet shared (Viewer) with the service-account email
- [ ] `ASANA_PAT` secret set in GitHub
- [ ] `GOOGLE_CREDENTIALS` secret set in GitHub (full JSON)
- [ ] `N8N_WEBHOOK_URL` secret set in GitHub *(deferred to Step 5)*
