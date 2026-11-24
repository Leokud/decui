"""Build a DECUI application into a single executable with PyInstaller.

Usage:
    python build_exe.py [entrypoint.py] [--clean-db]

The resulting executable is placed in ``dist/ERP`` (Linux) or ``dist/ERP.exe`` (Windows).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Build standalone DECUI executable")
    parser.add_argument(
        "entrypoint",
        nargs="?",
        default=str(ROOT / "examples" / "simple.py"),
        help="Path to the Python entrypoint script (default: examples/simple.py)",
    )
    parser.add_argument("--name", default="ERP", help="Output binary name (default: ERP)")
    parser.add_argument(
        "--clean-db",
        action="store_true",
        help="Delete *.db files in the project root before building",
    )
    opts = parser.parse_args()

    entry_point = Path(opts.entrypoint).resolve()
    if not entry_point.is_file():
        print(f"Error: Entrypoint '{entry_point}' not found.")
        sys.exit(1)

    dist_dir = ROOT / "dist"
    build_dir = ROOT / "build"

    for p in (dist_dir, build_dir):
        if p.exists():
            shutil.rmtree(p)

    if opts.clean_db:
        for db in ROOT.glob("*.db"):
            try:
                db.unlink()
                print(f"  Удалена БД: {db.name}")
            except Exception:
                pass

    args = [
        str(entry_point),
        "--name", opts.name,
        "--onefile",
        "--windowed",
        "--clean",
        "--noconfirm",
        "--distpath", str(dist_dir),
        "--workpath", str(build_dir),
        "--add-data", f"{ROOT / 'decui'}{os.pathsep}decui",
        "--hidden-import", "sqlalchemy.sql.default_comparator",
        "--hidden-import", "sqlalchemy.ext.declarative",
        "--hidden-import", "sqlalchemy.orm",
        "--collect-all", "sqlalchemy",
        "--hidden-import", "decui",
        "--hidden-import", "decui.fields",
        "--hidden-import", "decui.entity",
        "--hidden-import", "decui.registry",
        "--hidden-import", "decui.commands",
        "--hidden-import", "decui.orm",
        "--hidden-import", "decui.numbering",
        "--hidden-import", "decui.movement",
        "--hidden-import", "decui.sequence",
        "--hidden-import", "decui.reports",
        "--hidden-import", "decui.reminders",
        "--hidden-import", "decui.app",
        "--hidden-import", "decui.cli",
        "--hidden-import", "decui.ui",
        "--hidden-import", "decui.ui.editors",
        "--hidden-import", "decui.ui.form",
        "--hidden-import", "decui.ui.listview",
        "--hidden-import", "decui.ui.navigation",
        "--hidden-import", "decui.ui.report_view",
        "--hidden-import", "decui.ui.chart",
    ]

    PyInstaller.__main__.run(args)

    exe_ext = ".exe" if sys.platform == "win32" else ""
    print(f"\nBuild complete: {dist_dir / f'{opts.name}{exe_ext}'}")


if __name__ == "__main__":
    main()
