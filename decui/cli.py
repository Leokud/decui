from __future__ import annotations

import argparse
import importlib.util
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
    master_data,
    transaction,
    line_items,
    String,
    Decimal as D,
    Integer,
    Ref,
    action,
    report,
    QueryReport,
    Column,
    DateParam,
)

# ── 1. Master Data (Catalogs) ──────────────────────────────────────────

@master_data
class Customer(MasterData):
    __verbose_name__ = "Клиенты"
    __section__ = "Продажи"

    name: str = String("Наименование", required=True)
    phone: str = String("Телефон")
    email: str = String("Email")


@master_data
class Product(MasterData):
    __verbose_name__ = "Товары"
    __section__ = "Склад"

    name: str = String("Наименование", required=True)
    sku: str = String("Артикул")
    price: Decimal = D("Цена", scale=2, currency="RUB", default=Decimal("0.00"))


# ── 2. Transactions (Documents) ────────────────────────────────────────

@transaction
class SalesOrder(Transaction):
    __verbose_name__ = "Заказы клиентов"
    __section__ = "Продажи"
    __numbering__ = {{"prefix": "SO", "field": "number", "pattern": "{{prefix}}-{{year}}-{{seq:05d}}"}}

    customer = Ref(Customer, "Клиент", required=True)
    comment: str = String("Комментарий", multiline=True)

    @line_items
    class Items:
        product = Ref(Product, "Товар", required=True)
        quantity: int = Integer("Количество", default=1, min_value=1)
        price: Decimal = D("Цена", scale=2, default=Decimal("0.00"))
        amount: Decimal = D("Сумма", scale=2, read_only=True, default=Decimal("0.00"))

    @action("Провести заказ")
    def post_order(self):
        for row in self.Items:
            qty = row.get("quantity") or 0
            prc = row.get("price") or Decimal("0")
            row["amount"] = Decimal(str(qty)) * Decimal(str(prc))
        self.post()


# ── 3. Reports ──────────────────────────────────────────────────────────

@report
class SalesReport(QueryReport):
    title = "Отчет по продажам"
    __section__ = "Отчеты"

    date_from = DateParam("Дата с")
    date_to = DateParam("Дата по")

    columns = [
        Column("Заказ №", "number", width=120),
        Column("Клиент", "customer", width=180),
        Column("Сумма", "total", width=120, align="right", aggregate="sum", format="{{:,.2f}}", suffix=" ₽"),
    ]

    def execute(self, db: Database, date_from=None, date_to=None, **kw):
        orders = db.list(SalesOrder)
        customers = {{c._id: c.name for c in db.list(Customer)}}
        rows = []
        for o in orders:
            if o.date:
                od = o.date.date() if hasattr(o.date, "date") else o.date
                if date_from and od < date_from:
                    continue
                if date_to and od > date_to:
                    continue
            full = db.get(SalesOrder, o._id) if o._id else o
            items = full.Items if full else []
            total = sum(Decimal(str(r.get("amount") or 0)) for r in items)
            rows.append({{
                "number": o.number or "",
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

Validate schemas before running:
```bash
decui check
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

_HIDDEN_IMPORTS = [
    "sqlalchemy.sql.default_comparator",
    "sqlalchemy.ext.declarative",
    "sqlalchemy.orm",
    "decui",
    "decui.fields",
    "decui.entity",
    "decui.registry",
    "decui.commands",
    "decui.orm",
    "decui.numbering",
    "decui.movement",
    "decui.sequence",
    "decui.reports",
    "decui.reminders",
    "decui.app",
    "decui.cli",
    "decui.ui",
    "decui.ui.editors",
    "decui.ui.form",
    "decui.ui.listview",
    "decui.ui.navigation",
    "decui.ui.report_view",
    "decui.ui.chart",
]


def _resolve_entrypoint(file_arg: str | None) -> str | None:
    if file_arg is not None:
        return file_arg
    for candidate in ("app.py", "main.py"):
        if Path(candidate).is_file():
            return candidate
    return None


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
    slug = project_dir.name.lower().replace("-", "_").replace(" ", "_")

    app_py = project_dir / "app.py"
    app_py.write_text(
        STARTER_APP_TEMPLATE.format(project_name=project_dir.name, project_slug=slug),
        encoding="utf-8",
    )

    readme_md = project_dir / "README.md"
    readme_md.write_text(
        STARTER_README_TEMPLATE.format(project_name=project_dir.name),
        encoding="utf-8",
    )

    print(f"Successfully created project '{name}'!")
    print(f"  cd {name}")
    print("  decui check && decui run")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    target = _resolve_entrypoint(args.file)
    if target is None:
        print("Error: No Python application file specified and neither app.py nor main.py was found.")
        return 1

    print(f"Running {target}...")
    return subprocess.call([sys.executable, target])


def cmd_check(args: argparse.Namespace) -> int:
    """Validate entrypoint models, Ref targets, registers, and SQLite initialization without GUI."""
    target = _resolve_entrypoint(args.file)
    if target is None:
        print("Error: No Python application file specified and neither app.py nor main.py was found.")
        return 1
    target_path = Path(target).resolve()
    if not target_path.is_file():
        print(f"Error: Target file '{target}' does not exist.")
        return 1

    spec = importlib.util.spec_from_file_location("_decui_check_target", target_path)
    if spec is None or spec.loader is None:
        print(f"Error: Cannot load module from {target_path}")
        return 1

    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(target_path.parent))
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        print(f"Error importing {target}: {exc}")
        return 1

    from decui.fields import RefField
    from decui.orm import Database
    from decui.registry import get_all_entities, get_all_reports

    entities = get_all_entities()
    reports = get_all_reports()
    errors: list[str] = []

    for cls in entities:
        for fname, field in cls._meta.fields.items():
            if isinstance(field, RefField):
                try:
                    field.resolve_target()
                except Exception as exc:
                    errors.append(f"{cls.__name__}.{fname}: {exc}")

    db = Database.memory()
    if not db._ready:
        errors.append(f"Database init failed: {db._last_error}")

    if errors:
        print("Validation FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"Validation OK ({target_path.name}): {len(entities)} entities, {len(reports)} reports.")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    env = os.environ.copy()
    if sys.platform != "win32" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["QT_QPA_PLATFORM"] = "offscreen"

    test_args = [sys.executable, "-m", "pytest"] + (args.pytest_args or [])
    return subprocess.call(test_args, env=env)


def cmd_build(args: argparse.Namespace) -> int:
    target = _resolve_entrypoint(args.file)
    if target is None or not Path(target).is_file():
        print(f"Error: Target file '{target or 'app.py'}' does not exist.")
        return 1

    build_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        f"--name={Path(target).stem}",
        "--collect-all",
        "sqlalchemy",
    ]
    for hi in _HIDDEN_IMPORTS:
        build_cmd.extend(["--hidden-import", hi])
    build_cmd.append(target)

    print(f"Building standalone executable for {target}...")
    return subprocess.call(build_cmd)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="decui",
        description="decui CLI — Developer tooling for decui ERP applications",
    )
    parser.add_argument("--version", action="version", version=f"decui {VERSION}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    p_new = subparsers.add_parser("new", help="Scaffold a new decui application")
    p_new.add_argument("name", help="Name of the new project directory")

    p_run = subparsers.add_parser("run", help="Run a decui application")
    p_run.add_argument("file", nargs="?", default=None, help="Python entrypoint script (default: app.py / main.py)")

    p_check = subparsers.add_parser("check", help="Validate entity models, Ref targets, and DB schema")
    p_check.add_argument("file", nargs="?", default=None, help="Python entrypoint script (default: app.py / main.py)")

    p_test = subparsers.add_parser("test", help="Run tests with pytest")
    p_test.add_argument("pytest_args", nargs=argparse.REMAINDER, help="Extra arguments passed to pytest")

    p_build = subparsers.add_parser("build", help="Build standalone desktop executable")
    p_build.add_argument("file", nargs="?", default=None, help="Python entrypoint script (default: app.py / main.py)")

    args = parser.parse_args()

    if args.command == "new":
        sys.exit(cmd_new(args))
    elif args.command == "run":
        sys.exit(cmd_run(args))
    elif args.command == "check":
        sys.exit(cmd_check(args))
    elif args.command == "test":
        sys.exit(cmd_test(args))
    elif args.command == "build":
        sys.exit(cmd_build(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
