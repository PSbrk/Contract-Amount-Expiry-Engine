# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Contract-Amount-Expiry-Engine portable bundle.

Build:
    pyinstaller engine.spec --clean --noconfirm

Output:
    dist/ContractEngine/                  ← shippable folder
        EngineApp.exe                     ← entry point (subcommands: --ingest, --ui, …)
        _internal/                        ← bundled Python runtime + deps + templates

Mode: --onedir (folder of files).
Rationale (from MIGRATION-PLAN.md):
  - Friendlier to corporate antivirus than --onefile (no self-extract).
  - Faster cold-start (no unpack on each launch).
  - Easier to debug missing-module issues — you can `ls` the bundle.

Distribution: zip dist/ContractEngine/, copy to the target machine, unzip,
fill config/secrets.env (ASANA_PAT, optionally ONEDRIVE_BACKUP_PATH),
then `EngineApp.exe --ingest` from the folder root.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


# ---------------------------------------------------------------------------
# Data files — Jinja templates Flask loads at runtime.
# PyInstaller does NOT detect render_template() string references; without
# this entry, every --ui request would TemplateNotFound.
# ---------------------------------------------------------------------------

datas = [
    ("engine/ui/templates", "engine/ui/templates"),
]

# pandas hides some test/IO modules behind dynamic imports — let the hook
# collect what it knows about and trust it. Same for asana (v5 uses
# generated submodules) and openpyxl (workbook engines).
hiddenimports: list[str] = []
hiddenimports += collect_submodules("asana")
hiddenimports += collect_submodules("openpyxl")
# Flask + Jinja are auto-detected via the entry-point graph, but the
# explicit names make build failures surface earlier if something gets
# excluded by a future PyInstaller release.
hiddenimports += [
    "flask",
    "jinja2",
    "werkzeug",
    "click",
    "rapidfuzz",
    "rapidfuzz.fuzz",
    "rapidfuzz.process",
    "pandas",
    "pyairtable",  # legacy airtable_inbox transition path; removed in Phase 6
    "dotenv",
    "sqlite3",
]


block_cipher = None


a = Analysis(
    ["engine/main.py"],
    pathex=[str(Path(".").resolve())],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavyweight test / notebook deps we never call at runtime; trimming
        # these knocks ~40 MB off the bundle without breaking anything the
        # engine actually uses.
        "pytest",
        "_pytest",
        "IPython",
        "jupyter",
        "matplotlib",
        "scipy",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EngineApp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX compression triggers more AV false positives
    console=True,        # --ingest prints to stdout; operator tails the log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ContractEngine",
)
