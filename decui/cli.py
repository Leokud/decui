from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

VERSION = "0.2.0"

STARTER_APP_TEMPLATE = '''"""
{project_name} — Business application powered by decui.
"""
from decimal import Decimal
from decui import (
    ERPApplication,
    Database,
    MasterData,
    Transaction,
    line_items,
    String,
    Decimal as D,
    Integer,
    Ref,
    action,
    Report,
    Column,
    DateParam,
)

# ── 1. Master Data (Catalogs) ──────────────────────────────────────────

class Customer(MasterData):
    __verbose_name__ = "Клиенты"
    __section__ = "Продажи"

    name = String(required=True)
    phone = String()
    email = String()


class Product(MasterData):
    __verbose_name__ = "Товары"
    __section__ = "Склад"

    name = String(required=True)
    sku = String()
    price = D(scale=2, currency="RUB")


# ── 2. Transactions (Documents) ────────────────────────────────────────

class SalesOrder(Transaction):
    __verbose_name__ = "Заказы клиентов"
    __section__ = "Продажи"
    __numbering__ = {{"prefix": "SO", "field": "number", "pattern": "{{prefix}}-{{year}}-{{seq:05d}}"}}

    customer = Ref(Customer, required=True)
    comment = String()

    @line_items
    class Items:
        product = Ref(Product, required=True)
        quantity = Integer(default=1)
        price = D(scale=2)
        amount = D(scale=2, read_only=True)

    @action("Провести заказ")
    def post_order(self):
        # Auto-calculate amounts
        for row in self.Items:
            qty = row.get("quantity") or 0
            prc = row.get("price") or Decimal("0")
            row["amount"] = Decimal(str(qty)) * Decimal(str(prc))
        self.post()


# ── 3. Reports ──────────────────────────────────────────────────────────

class SalesReport(Report):
    __verbose_name__ = "Отчет по продажам"
    __section__ = "Отчеты"

    date_from = DateParam("Дата с")
    date_to = DateParam("Дата по")

    columns = [
        Column("Заказ №", "number", width=120),
        Column("Клиент", "customer", width=180),
        Column("Сумма", "total", width=120, align="right", aggregate="sum", suffix=" ₽"),
    ]

    def execute(self, db: Database, params: dict):
        orders = db.list(SalesOrder)
        customers = {{c._id: c.name for c in db.list(Customer)}}
        rows = []
        for o in orders:
            full = db.get(SalesOrder, o._id)
            total = sum(r.get("amount", Decimal("0")) for r in (full.Items if full else []))
            rows.append({{
                "number": o.number,
                "customer": customers.get(o.customer, "—"),
                "total": total,
            }})
        return rows


# ── 4. Main Application ────────────────────────────────────────────────

def main():
    app = ERPApplication(
        title="{project_name}",
        db_path="{project_slug}.db",
        window_size=(1024, 680),
    )
    app.run()


if __name__ == "__main__":
    main()
'''

STARTER_README_TEMPLATE = """# {project_name}

Desktop business management application created with [decui](https://github.com/leokud/decui).

## Quick Start

Run the application:
```bash
decui run
# or
python app.py
```

Run tests:
```bash
decui test
```

Build standalone executable:
```bash
decui build
```
"""


def cmd_new(args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not name:
        print("Error: Project name cannot be empty.")
        return 1

    project_dir = Path(name)
    if project_dir.exists() and any(project_dir.iterdir()):
        print(f"Error: Directory '{name}' already exists and is not empty.")
        return 1

    project_dir.mkdir(parents=True, exist_ok=True)
    slug = name.lower().replace("-", "_").replace(" ", "_")

    app_py = project_dir / "app.py"
    app_py.write_text(
        STARTER_APP_TEMPLATE.format(project_name=name, project_slug=slug),
        encoding="utf-8",
    )

    readme_md = project_dir / "README.md"
    readme_md.write_text(
        STARTER_README_TEMPLATE.format(project_name=name),
        encoding="utf-8",
    )

    print(f"Successfully created project '{name}'!")
    print(f"  cd {name}")
    print(f"  decui run")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    target = args.file
    if target is None:
        for candidate in ["app.py", "main.py"]:
            if Path(candidate).is_file():
                target = candidate
                break
    if target is None:
        print("Error: No Python application file specified and neither app.py nor main.py was found.")
        return 1

    print(f"Running {target}...")
    return subprocess.call([sys.executable, target])


def cmd_test(args: argparse.Namespace) -> int:
    env = os.environ.copy()
    if sys.platform != "win32" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["QT_QPA_PLATFORM"] = "offscreen"

    test_args = [sys.executable, "-m", "pytest"] + (args.pytest_args or [])
    return subprocess.call(test_args, env=env)


def cmd_build(args: argparse.Namespace) -> int:
    target = args.file or "app.py"
    if not Path(target).is_file():
        print(f"Error: Target file '{target}' does not exist.")
        return 1

    build_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        f"--name={Path(target).stem}",
        target,
    ]
    print(f"Building standalone executable for {target}...")
    return subprocess.call(build_cmd)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="decui",
        description="decui CLI — Developer tooling for decui ERP applications",
    )
    parser.add_argument("--version", action="version", version=f"decui {VERSION}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # decui new <name>
    p_new = subparsers.add_parser("new", help="Scaffold a new decui application")
    p_new.add_argument("name", help="Name of the new project directory")

    # decui run [file]
    p_run = subparsers.add_parser("run", help="Run a decui application")
    p_run.add_argument("file", nargs="?", default=None, help="Python entrypoint script (default: app.py)")

    # decui test [pytest args...]
    p_test = subparsers.add_parser("test", help="Run tests with pytest")
    p_test.add_argument("pytest_args", nargs="*", help="Extra arguments passed to pytest")

    # decui build [file]
    p_build = subparsers.add_parser("build", help="Build standalone desktop executable")
    p_build.add_argument("file", nargs="?", default=None, help="Python entrypoint script (default: app.py)")

    args = parser.parse_args()

    if args.command == "new":
        sys.exit(cmd_new(args))
    elif args.command == "run":
        sys.exit(cmd_run(args))
    elif args.command == "test":
        sys.exit(cmd_test(args))
    elif args.command == "build":
        sys.exit(cmd_build(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
