"""Build the ERP application into a single .exe with PyInstaller.

Usage:
    python build_exe.py

The resulting .exe will be placed in ``dist/erp/erp.exe``.
"""

import shutil
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
ENTRY_POINT = ROOT / "examples" / "simple.py"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
APP_NAME = "ERP"

# ── Clean previous builds + databases ─────────────────────────────
for p in (DIST_DIR, BUILD_DIR):
    if p.exists():
        shutil.rmtree(p)
for db in ROOT.glob("*.db"):
    try:
        db.unlink()
        print(f"  Удалена БД: {db.name}")
    except Exception:
        pass

# ── PyInstaller arguments ──────────────────────────────────────────
args = [
    str(ENTRY_POINT),
    "--name", APP_NAME,
    "--onefile",                   # single .exe
    "--windowed",                  # no console window
    "--clean",
    "--noconfirm",
    "--distpath", str(DIST_DIR),
    "--workpath", str(BUILD_DIR),
    "--add-data", f"{ROOT / 'decui'}{Path(';')}decui",
    # PySide6 hooks are auto-detected by PyInstaller >= 6.x
    # SQLAlchemy
    "--hidden-import", "sqlalchemy.sql.default_comparator",
    "--hidden-import", "sqlalchemy.ext.declarative",
    "--hidden-import", "sqlalchemy.orm",
    "--collect-all", "sqlalchemy",
    # decui
    "--hidden-import", "decui",
    "--hidden-import", "decui.fields",
    "--hidden-import", "decui.entity",
    "--hidden-import", "decui.registry",
    "--hidden-import", "decui.commands",
    "--hidden-import", "decui.orm",
    "--hidden-import", "decui.numbering",
    "--hidden-import", "decui.reports",
    "--hidden-import", "decui.entity",
    "--hidden-import", "decui.ui",
    "--hidden-import", "decui.ui.report_view",
    "--hidden-import", "decui.ui.editors",
    "--hidden-import", "decui.ui.form",
    "--hidden-import", "decui.ui.listview",
    "--hidden-import", "decui.ui.navigation",
    "--hidden-import", "decui.app",
]

PyInstaller.__main__.run(args)

print(f"\nBuild complete: {DIST_DIR / APP_NAME / f'{APP_NAME}.exe'}")
