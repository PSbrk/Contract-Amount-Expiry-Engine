// Builds Contract Engine Bookmarks.docx -- a short reference card of the
// most-used local web UI URLs plus a "last refreshed" line summarizing
// the most recent engine change. Re-run after each feature lands so the
// doc reflects current routes.
//
//   node tools/build_bookmarks_doc.js
//
// Writes to C:\Users\philip.seabrook\Downloads\Contract Engine Bookmarks.docx.
// If the file is open in Word, the write fails with EBUSY and prints a
// reminder to close it.

const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, ExternalHyperlink,
  HeadingLevel, AlignmentType, BorderStyle, PageOrientation,
  PageBreak,
} = require("docx");

const OUT = path.join(
  process.env.USERPROFILE || "C:\\Users\\philip.seabrook",
  "Downloads", "Contract Engine Bookmarks.docx",
);
const BASE = "http://127.0.0.1:8080";
// Click-to-launch the bundled engine UI. Word renders file:/// URLs as
// real hyperlinks; the operator clicks once and run-ui.bat boots
// EngineApp.exe in UI mode in a new console window.
const LAUNCH_UI =
  "file:///C:/Users/philip.seabrook/Contract-Amount-Expiry-Engine/" +
  "dist/ContractEngine/scripts/run-ui.bat";
// Use the *interactive* variant for the Word-doc link: it tees output
// to the console so the operator can watch ingest progress live, and
// pauses at the end so the window doesn't slam shut. The silent
// run-ingest.bat is reserved for Task Scheduler (no console attached).
const LAUNCH_INGEST =
  "file:///C:/Users/philip.seabrook/Contract-Amount-Expiry-Engine/" +
  "dist/ContractEngine/scripts/run-ingest-interactive.bat";
// File-Explorer hyperlink to the inbox folder. Word renders file:/// dir
// URLs as clickable links that open the folder in File Explorer.
const OPEN_INBOX =
  "file:///C:/Users/philip.seabrook/Contract-Amount-Expiry-Engine/" +
  "dist/ContractEngine/data/inbox";

const LAST_REFRESHED =
  "MAJOR REBUILD (2026-06-24): the engine now tracks Capital Projects " +
  "(account 63015) as a dedicated CapEx tier. CapEx transactions match an " +
  "Asana contract by an EXACT CapEx ID = Tableau Project ID join (no fuzzy " +
  "guessing); each project's spend is summed across ALL its transactions and " +
  "compared to a total budget you enter once on the new CapEx Budgets page, " +
  "and the resulting % / band / alarm is broadcast to EVERY contract sharing " +
  "that CapEx ID. Scope changed: 63015 is tracked again, 63020 is no longer " +
  "tracked, and department 110 was added. Opex (everything else) now narrows " +
  "candidates by the Campus + Dept + Acc coding Asana mirrors from Tableau " +
  "before fuzzy vendor matching, and campus is matched EXACTLY — an " +
  "unrecognized campus (e.g. YVN, ZNR with no Asana option yet) goes to Needs " +
  "Tagging for you rather than being auto-guessed. Learned mappings were reset " +
  "to a clean slate; rebuild them by answering Needs Tagging exactly as " +
  "before. Asana write-back of the five spend custom fields (Spent so far, % " +
  "Spent, Spending Rate, Spending Rate Alarm, Alarms) is PAUSED " +
  "(DRY_RUN_ASANA=true): validate the dry-run Dashboard — especially the CapEx " +
  "broadcast and your re-taught mappings — then set DRY_RUN_ASANA=false in " +
  "config\\secrets.env to go live. CapEx contracts deliberately leave Spending " +
  "Rate untouched (a multi-year project has no annual pace). The zero-" +
  "overwrite guard still prevents an un-attributed contract being written back " +
  "to $0 over real spend. Ingests still run automatically within ~10-15s of " +
  "dropping an export into data\\inbox. Page 2 documents the OneDrive " +
  "operator-handoff process.";

function p(text, opts = {}) {
  return new Paragraph({
    children: [new TextRun({ text, ...(opts.run || {}) })],
    ...(opts.paragraph || {}),
  });
}

function h(text, level) {
  return new Paragraph({
    heading: level,
    children: [new TextRun({ text, bold: true })],
  });
}

function link(label, href) {
  return new Paragraph({
    children: [
      new TextRun({ text: "•  ", bold: true }),
      new ExternalHyperlink({
        children: [new TextRun({ text: label, style: "Hyperlink" })],
        link: href,
      }),
    ],
    spacing: { after: 60 },
  });
}

function linkWithBlurb(label, href, blurb) {
  return new Paragraph({
    children: [
      new TextRun({ text: "•  ", bold: true }),
      new ExternalHyperlink({
        children: [new TextRun({ text: label, style: "Hyperlink" })],
        link: href,
      }),
      new TextRun({ text: "  —  " + blurb, italics: true,
                    color: "5A5A5A" }),
    ],
    spacing: { after: 80 },
  });
}

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal",
        quickFormat: true,
        run: { size: 32, bold: true, font: "Arial" },
        paragraph: { spacing: { before: 240, after: 160 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal",
        quickFormat: true,
        run: { size: 26, bold: true, font: "Arial" },
        paragraph: { spacing: { before: 200, after: 120 }, outlineLevel: 1 } },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 },
      },
    },
    children: [
      h("Contract Engine — Bookmarks", HeadingLevel.HEADING_1),
      new Paragraph({
        children: [new TextRun({ text: LAST_REFRESHED, italics: true,
                                  color: "5A5A5A" })],
        spacing: { after: 240 },
      }),

      h("Launch the engine", HeadingLevel.HEADING_2),
      linkWithBlurb("Start the local web UI", LAUNCH_UI,
        "runs scripts/run-ui.bat; opens the engine UI on http://127.0.0.1:8080"),
      linkWithBlurb("Run an ingest now (one-shot)", LAUNCH_INGEST,
        "runs scripts/run-ingest-interactive.bat; window stays open and shows live progress, full log saved to logs\\ingest-<date>.log"),
      linkWithBlurb("Open the inbox folder", OPEN_INBOX,
        "opens dist\\ContractEngine\\data\\inbox in File Explorer; drop a Tableau export here, then click 'Run an ingest now'"),

      h("Local Web UI", HeadingLevel.HEADING_2),
      linkWithBlurb("Dashboard", BASE + "/",
        "live contracts (opex + CapEx), spend, alarm bands; amendments show cross-reference"),
      linkWithBlurb("CapEx Budgets", BASE + "/capex-budgets",
        "enter total budget per CapEx ID (account 63015 projects); shows the Needs-Budget queue + a bulk paste box for the Google-Doc figures"),
      linkWithBlurb("Vendor Conflicts", BASE + "/vendor-conflicts",
        "pick which Asana task a same-vendor group belongs to; declare amendment links"),
      linkWithBlurb("Miscoded?", BASE + "/miscoded",
        "vendor + campus + term align but the Dept/Acct coding differs; accept as miscoded to attribute anyway, or confirm correctly coded"),
      linkWithBlurb("Needs Tagging — Open", BASE + "/needs-tagging?show=open",
        "actionable queue: ambiguous / unmatched groups awaiting your call"),
      linkWithBlurb("Needs Tagging — Once Off", BASE + "/needs-tagging?show=once_off",
        "valid one-off charges hidden until new activity in the same group"),
      linkWithBlurb("Needs Tagging — Dismissed", BASE + "/needs-tagging?show=dismissed",
        "rows marked irrelevant; never re-surfaced"),
      linkWithBlurb("P-Card Spend", BASE + "/p-card-spend",
        "read-only audit of blank-vendor purchasing-card / journal spend (Amazon, Lowes, fuel, …) — not contracted spend; Ignore once to clear reviewed rows"),
      linkWithBlurb("Run Log", BASE + "/run-log",
        "every engine run, newest first; outcomes, anomalies, review flags"),
      linkWithBlurb("State", BASE + "/state",
        "read-only audit view of prior totals per contract"),
      linkWithBlurb("Settings", BASE + "/settings",
        "active config + env-var presence (values never shown)"),

      h("Admin tables", HeadingLevel.HEADING_2),
      linkWithBlurb("Vendor Aliases", BASE + "/vendor-aliases",
        "Asana contract name → Tableau Vendor spellings"),
      linkWithBlurb("Campus Map", BASE + "/campus-map",
        "Tableau code → Asana Campus option names; or Drop to exclude"),
      linkWithBlurb("Learned Mappings", BASE + "/learned-mappings",
        "(Campus, Dept, Acct, Vendor) → contract attribution"),

      h("How to make changes stick", HeadingLevel.HEADING_2),
      p("Drop a Tableau export into data\\inbox\\ and the next scheduled run " +
        "(or manual run-ingest.bat) will process it, update the Dashboard, " +
        "and prune the Inbox file into data\\processed\\.",
        { paragraph: { spacing: { after: 120 } } }),
      p("Any decisions you make in the web UI (Pin this task, Mark as " +
        "amendment, Save description picks, Dismiss, Once Off, Save Assign " +
        "Contract) are written immediately to data\\engine.db and applied " +
        "on the next ingest.",
        { paragraph: { spacing: { after: 120 } } }),

      // ----- New page: Operator handoff via OneDrive -----
      new Paragraph({ children: [new PageBreak()] }),

      h("Operator handoff via OneDrive", HeadingLevel.HEADING_1),
      new Paragraph({
        children: [new TextRun({
          text:
            "The engine's memory (every Learned Mapping, Dismissal, " +
            "P-Card flag, Once Off, Pin, description pick, and the full " +
            "Run Log) lives in a single SQLite file: data\\engine.db. That " +
            "file is mirrored to OneDrive after every successful ingest and " +
            "pulled back down at every engine startup if the cloud copy is " +
            "newer. The handoff between operators is therefore: outgoing " +
            "operator pushes (by running an ingest), incoming operator " +
            "pulls (by launching the engine). No manual file copying.",
          italics: true, color: "5A5A5A",
        })],
        spacing: { after: 240 },
      }),

      h("Outgoing operator — before walking away", HeadingLevel.HEADING_2),
      p("1. Make sure today's work is captured by running an ingest " +
        "(or letting the scheduled run fire). The auto-backup to " +
        "OneDrive only happens after a successful ingest — without it, " +
        "the cloud copy is stale.",
        { paragraph: { spacing: { after: 120 } } }),
      p("2. Open Settings in the web UI and read the \"OneDrive sync " +
        "state\" panel at the top. You want to see either \"In sync\" " +
        "(blue) or \"Local newer\" (amber — fine, will push on next " +
        "ingest). If it says \"Restore failed\" or \"OneDrive backup " +
        "not configured,\" fix that before handing off.",
        { paragraph: { spacing: { after: 120 } } }),
      p("3. Close the engine console window (the black window titled " +
        "EngineApp). The bundle isn't multi-machine-safe while running, " +
        "so closing prevents the next operator from racing you.",
        { paragraph: { spacing: { after: 200 } } }),

      h("Incoming operator — first time on a new machine",
        HeadingLevel.HEADING_2),
      p("1. Install OneDrive and sign in to the LIFE.CHURCH tenant. " +
        "Confirm the path C:\\Users\\<you>\\OneDrive - LIFE.CHURCH\\" +
        "ContractEngine\\engine.db syncs locally (the file should show " +
        "a green checkmark in File Explorer).",
        { paragraph: { spacing: { after: 120 } } }),
      p("2. Receive the bundle from the outgoing operator (a zip of " +
        "dist\\ContractEngine\\, ~50-80 MB). Unzip somewhere stable " +
        "— Documents, Desktop, or your own folder. The location " +
        "doesn't matter; the engine reads paths relative to its own " +
        "directory.",
        { paragraph: { spacing: { after: 120 } } }),
      p("3. Edit dist\\ContractEngine\\config\\secrets.env in Notepad. " +
        "Set three lines:",
        { paragraph: { spacing: { after: 60 } } }),
      p("        ASANA_PAT=<your own Asana personal access token>",
        { paragraph: { spacing: { after: 40 } },
          run: { font: "Consolas", size: 20 } }),
      p("        DRY_RUN_ASANA=true",
        { paragraph: { spacing: { after: 40 } },
          run: { font: "Consolas", size: 20 } }),
      p("        ONEDRIVE_BACKUP_PATH=C:\\Users\\<you>\\OneDrive - " +
        "LIFE.CHURCH\\ContractEngine\\engine.db",
        { paragraph: { spacing: { after: 120 } },
          run: { font: "Consolas", size: 20 } }),
      p("Keep DRY_RUN_ASANA=true unless you've been told otherwise. The " +
        "Asana PAT is per-operator — do not reuse someone else's. " +
        "ONEDRIVE_BACKUP_PATH must point at your OneDrive path (same " +
        "OneDrive - LIFE.CHURCH\\ContractEngine\\engine.db location, " +
        "just under your own user directory).",
        { paragraph: { spacing: { after: 200 } } }),
      p("4. Click the \"Start the local web UI\" link on page 1 of " +
        "this document. The engine boots, auto-pulls engine.db from " +
        "OneDrive (you'll see \"Pulled engine.db from OneDrive\" or " +
        "\"Restored engine.db from OneDrive\" in the console), and " +
        "opens the UI on http://127.0.0.1:8080.",
        { paragraph: { spacing: { after: 120 } } }),
      p("5. Open Settings. The \"OneDrive sync state\" panel should " +
        "show \"Pulled from OneDrive (first run on this machine)\" or " +
        "\"In sync\" — confirms you're seeing the previous operator's " +
        "decisions. The Dashboard should look the same as the outgoing " +
        "operator's last screenshot.",
        { paragraph: { spacing: { after: 120 } } }),
      p("6. Drop the next Tableau export into data\\inbox\\ (use the " +
        "\"Open the inbox folder\" link on page 1). Click \"Run an " +
        "ingest now\" — the engine processes the new export, applies " +
        "every prior decision, and pushes the updated engine.db back " +
        "to OneDrive.",
        { paragraph: { spacing: { after: 200 } } }),

      h("How the sync actually works", HeadingLevel.HEADING_2),
      p("On every engine startup (UI launch OR one-shot ingest), the " +
        "engine compares mtimes:",
        { paragraph: { spacing: { after: 120 } } }),
      p("•  Cloud copy newer than local (or local missing) → pulls " +
        "down, logs \"Restored from OneDrive.\"",
        { paragraph: { spacing: { after: 60 } } }),
      p("•  Local copy newer than cloud → does NOT overwrite. Local is " +
        "always source of truth in that case; next successful ingest " +
        "pushes up.",
        { paragraph: { spacing: { after: 60 } } }),
      p("•  Mtimes within 2 seconds of each other → treated as in " +
        "sync, no copy.",
        { paragraph: { spacing: { after: 120 } } }),
      p("After every successful ingest, the engine copies its local " +
        "data\\engine.db up to OneDrive (mtime preserved via " +
        "shutil.copy2, so the round trip is idempotent). Backup " +
        "failures are warned but never crash the run — your local DB " +
        "is the source of truth, the cloud is a mirror.",
        { paragraph: { spacing: { after: 200 } } }),

      h("Concurrency caveat", HeadingLevel.HEADING_2),
      p("This is a serial-handoff design, not a multi-user system. " +
        "Only one operator should be actively running ingests at a " +
        "time. If two operators run simultaneously on different " +
        "machines, the later-mtime save wins on the next pull and the " +
        "earlier operator's most recent decisions are lost. The " +
        "Settings sync panel makes drift visible (\"Local newer\" " +
        "tells you you're holding work the cloud doesn't have yet), " +
        "but it doesn't prevent stomping. Coordinate handoffs over " +
        "Slack/Teams; close the engine console when you're done.",
        { paragraph: { spacing: { after: 120 } } }),
      p("If the Settings panel ever shows \"Restore attempt failed,\" " +
        "the engine kept your local DB and logged the OS error " +
        "(usually a OneDrive sync glitch or a permission issue). " +
        "Re-launch the UI; the engine will retry the pull on next " +
        "startup. If it persists, check that the OneDrive path in " +
        "secrets.env exists and that OneDrive is signed in.",
        { paragraph: { spacing: { after: 120 } } }),
    ],
  }],
});

Packer.toBuffer(doc).then((buf) => {
  try {
    fs.writeFileSync(OUT, buf);
    const stat = fs.statSync(OUT);
    console.log("wrote " + OUT + " (" + Math.round(stat.size / 1024) + " KB)");
  } catch (e) {
    if (e.code === "EBUSY") {
      console.error(
        "EBUSY: " + OUT + " is currently open in Word -- close it and re-run."
      );
      process.exit(1);
    }
    throw e;
  }
});
