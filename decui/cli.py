from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .entity import Entity

VERSION = "0.3.0"

BASIC_APP = '''from decui import Decimal, ERPApplication, MasterData, String, master_data


@master_data
class Product(MasterData):
    __verbose_name__ = "Товары"
    __section__ = "Склад"

    name = String("Наименование", required=True)
    price = Decimal("Цена", scale=2, min_value=0)


if __name__ == "__main__":
    app = ERPApplication(title="Учёт товаров", db_path="data.sqlite3", start=Product)
    raise SystemExit(app.run())
'''

BASIC_TEST = '''from decimal import Decimal

from app import Product
from decui import Database


def test_product_round_trip():
    db = Database.memory()
    product = db.save(Product(name="Чай", price=Decimal("125.50")), validate=True)

    saved = db.get(Product, product.id)
    assert saved is not None
    assert saved.name == "Чай"
    assert saved.price == Decimal("125.50")
'''

BASIC_README = """# {project_name}

Запускайте команды из каталога с `app.py` в окружении с установленным DECUI.

```bash
decui run
```

Если используете окружение из репозитория DECUI через uv, запустите
`uv run --locked decui run` из каталога созданного проекта внутри репозитория.

1. В списке «Товары» нажмите «Новый», заполните поля и сохраните карточку.
2. Закройте окно и запустите приложение снова. Запись хранится в `data.sqlite3`.
3. Импортируйте `Integer` из `decui` и добавьте в класс `Product`
   строку `priority = Integer("Приоритет", default=3)`.
   Перезапустите приложение и откройте карточку товара.

`decui check` проверяет схему приложения. `decui test` запускает тест
сохранения товара в базе в памяти.

Файл `.gitignore` исключает локальную базу, кэши и результаты сборки.
"""

STARTER_APP_TEMPLATE = '''from decimal import Decimal
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

    @action("Провести")
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
        orders = db.filter(
            SalesOrder,
            date__date_range=(date_from, date_to),
            include_table_parts=True,
        )
        customers = {{c.id: c.name for c in db.list(Customer)}}
        rows = []
        for order in orders:
            rows.append({{
                "number": order.number or "",
                "customer": customers.get(order.customer, "—"),
                "total": order.Items.total("amount"),
            }})
        return rows


# ── 4. Main Application ────────────────────────────────────────────────

def main():
    app = ERPApplication(
        title={project_name!r},
        db_path={db_filename!r},
        window_size=(1024, 680),
    )
    app.run()


if __name__ == "__main__":
    main()
'''

STARTER_TEST = '''from decimal import Decimal

from app import Customer, Product, SalesOrder, SalesReport
from decui import Database


def test_order_round_trip():
    db = Database.memory()
    customer = db.save(Customer(name="Test customer"))
    product = db.save(Product(name="Test product"))
    order = SalesOrder(customer=customer.id)
    order.Items.add(product=product.id, quantity=2, price=Decimal("12.50"))
    db.post(order)

    restored = db.get(SalesOrder, order.id)
    assert restored is not None
    assert restored.is_posted
    assert restored.customer == customer.id
    assert restored.Items[0]["product"] == product.id
    assert restored.Items.total("amount") == Decimal("25.00")

    day = restored.date.date()
    rows = SalesReport().execute(db, date_from=day, date_to=day)
    assert [row["total"] for row in rows] == [Decimal("25.00")]
'''

STARTER_README_TEMPLATE = """# {project_name}

Пример учёта заказов на базе [DECUI](https://github.com/leokud/decui).

## Запуск

Запускайте команды из каталога с `app.py` в окружении с установленным DECUI.

```bash
decui run
decui check
decui test
```

Создайте клиента и товар. Откройте «Заказы клиентов», выберите клиента,
добавьте строку товара с количеством и ценой и нажмите «Провести и закрыть».
Откройте «Отчет по продажам» и сформируйте отчёт за дату заказа.

`decui check` проверяет схему. Тест в `test_app.py` проводит заказ в базе
в памяти и проверяет сумму сохранённой строки. Для сборки приложения
выполните `decui build`.

Файл `.gitignore` исключает локальную базу, кэши и результаты сборки.
"""

STARTER_GITIGNORE = """# Python
__pycache__/
*.pyc
.pytest_cache/
.venv/

# Локальные базы SQLite и их журналы
*.db
*.db-*
*.sqlite3
*.sqlite3-*

# Сборки PyInstaller
/build/
/dist/
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
    candidates = (file_arg,) if file_arg is not None else ("app.py", "main.py")
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    if file_arg is not None:
        print(f"Файл «{file_arg}» не найден. Проверьте путь к приложению.", file=sys.stderr)
    else:
        print(
            "В текущем каталоге нет app.py или main.py. "
            "Перейдите в каталог приложения или укажите путь к его Python-файлу.",
            file=sys.stderr,
        )
    return None


def cmd_new(args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not name:
        print("Укажите имя каталога, например: decui new warehouse --template basic", file=sys.stderr)
        return 1

    project_dir = Path(name)
    template = getattr(args, "template", "orders")
    if template == "basic":
        app, test, readme = BASIC_APP, BASIC_TEST, BASIC_README
    else:
        slug = project_dir.name.lower().replace("-", "_").replace(" ", "_")
        app = STARTER_APP_TEMPLATE.format(project_name=project_dir.name, db_filename=f"{slug}.db")
        test, readme = STARTER_TEST, STARTER_README_TEMPLATE

    try:
        if project_dir.exists() and (not project_dir.is_dir() or any(project_dir.iterdir())):
            print(f"Выберите новый или пустой каталог. Путь «{project_dir}» уже занят.", file=sys.stderr)
            return 1
        project_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in (
            ("app.py", app),
            ("test_app.py", test),
            ("README.md", readme.format(project_name=project_dir.name)),
            (".gitignore", STARTER_GITIGNORE),
        ):
            with (project_dir / filename).open("x", encoding="utf-8") as project_file:
                project_file.write(content)
    except OSError as exc:
        print(f"Не удалось создать проект «{project_dir}»: {exc}", file=sys.stderr)
        return 1

    print(f"Проект создан в каталоге «{project_dir}».")
    print("Из этого каталога запустите приложение командой decui run или uv run decui run.")
    print("Команда decui check проверяет схему, decui test запускает тест.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    target = _resolve_entrypoint(args.file)
    if target is None:
        return 1

    print(f"Запускается {target}…", flush=True)
    return subprocess.call([sys.executable, target])


def _reference_errors(entities: list[type[Entity]], reports: list[type]) -> list[str]:
    from .fields import RefField
    from .movement import RefDimension, get_all_registers
    from .reports import RefParam

    registered = set(entities)
    errors: list[str] = []

    def check(path: str, reference: RefField | RefParam | RefDimension) -> None:
        try:
            target = reference.resolve_target()
        except KeyError:
            target = None
        if not isinstance(target, type) or target not in registered:
            name = getattr(reference.target, "__name__", reference.target)
            errors.append(
                f"{path}: цель ссылки «{name}» не зарегистрирована. "
                "Проверьте имя, декоратор и импорт модели."
            )

    for cls in entities:
        for name, field in cls._meta.fields.items():
            if isinstance(field, RefField):
                check(f"{cls.__name__}.{name}", field)
        for part_name, part in cls._meta.table_parts.items():
            for name, field in part.fields.items():
                if isinstance(field, RefField):
                    check(f"{cls.__name__}.{part_name}.{name}", field)

    for cls in reports:
        for name, param in cls._report_meta.params.items():
            if isinstance(param, RefParam):
                check(f"{cls.__name__}.{name}", param)

    for cls in get_all_registers():
        for name, dimension in cls._register_meta.dimensions.items():
            if isinstance(dimension, RefDimension):
                check(f"{cls.__name__}.{name}", dimension)

    return errors


def cmd_check(args: argparse.Namespace) -> int:
    """Validate entrypoint models, Ref targets, registers, and SQLite initialization without GUI."""
    target = _resolve_entrypoint(args.file)
    if target is None:
        return 1
    target_path = Path(target).resolve()

    spec = importlib.util.spec_from_file_location("_decui_check_target", target_path)
    if spec is None or spec.loader is None:
        print(f"Ошибка: не удалось загрузить модуль из {target_path}", file=sys.stderr)
        return 1

    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(target_path.parent))
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        print(f"Ошибка импорта {target}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return 1
    finally:
        sys.modules.pop(spec.name, None)
        sys.path.pop(0)

    from decui.orm import Database
    from decui.registry import get_all_entities, get_all_reports

    entities = get_all_entities()
    reports = get_all_reports()
    errors = _reference_errors(entities, reports)

    backup = getattr(args, "backup", None)
    if not errors:
        options = {
            "column_renames": getattr(mod, "_DECUI_COLUMN_RENAMES", None),
            "table_part_renames": getattr(mod, "_DECUI_TABLE_PART_RENAMES", None),
        }
        if backup is None:
            db = Database(**options)
            if not db._ready:
                errors.append(
                    f"Не удалось инициализировать базу данных: {db._last_error or 'неизвестная ошибка'}"
                )
        else:
            from decui.files import stage_decui_backup

            try:
                with TemporaryDirectory(prefix="decui-migration-") as temporary:
                    copy = Path(temporary) / "migration.sqlite3"
                    stage_decui_backup(Path(backup), copy)
                    db = Database(str(copy), **options)
                    try:
                        if not db._ready:
                            errors.append(f"Ошибка миграции: {db._last_error or 'неизвестная ошибка'}")
                        else:
                            with db._engine.connect() as connection:
                                violation = connection.exec_driver_sql("PRAGMA foreign_key_check").first()
                            if violation is not None:
                                errors.append(f"Нарушена ссылочная целостность: таблица «{violation[0]}».")
                            else:
                                for message in db._migration_messages:
                                    print(message)
                    finally:
                        db._engine.dispose()
            except (OSError, ValueError, TimeoutError, sqlite3.Error) as exc:
                errors.append(f"Не удалось проверить копию базы: {exc}")

    if errors:
        print("Проверка не пройдена:")
        for e in errors:
            print(f"  - {e}")
        return 1

    if backup is not None:
        print("Копия базы открыта, ссылочная целостность проверена.")
    print(
        f"Проверка пройдена ({target_path.name}): сущностей — {len(entities)}, отчётов — {len(reports)}."
    )
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    env = os.environ.copy()
    if sys.platform != "win32" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env.setdefault("QT_QPA_PLATFORM", "offscreen")

    test_args = [sys.executable, "-m", "pytest"] + (args.pytest_args or [])
    return subprocess.call(test_args, env=env)


def build_arguments(target: Path, *, name: str | None = None, dist_dir: Path | None = None) -> list[str]:
    """Build the arguments shared by the CLI and Studio."""
    build_cmd = [
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        f"--name={name or target.stem}",
        "--paths",
        str(Path(__file__).resolve().parents[1]),
        "--collect-all",
        "sqlalchemy",
    ]
    if dist_dir is not None:
        build_cmd.extend(["--distpath", str(dist_dir)])
    for hi in _HIDDEN_IMPORTS:
        build_cmd.extend(["--hidden-import", hi])
    build_cmd.append(str(target))
    return build_cmd


def cmd_build(args: argparse.Namespace) -> int:
    target = _resolve_entrypoint(args.file)
    if target is None:
        return 1

    build_cmd = [sys.executable, *build_arguments(Path(target))]

    print(f"Сборка исполняемого файла для {target}…")
    return subprocess.call(build_cmd)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="decui",
        description="Создание, запуск и проверка приложений DECUI",
        epilog="Первый проект можно создать командой decui new warehouse --template basic.",
    )
    parser.add_argument("--version", action="version", version=f"decui {VERSION}")

    subparsers = parser.add_subparsers(dest="command", help="Команды")

    subparsers.add_parser("studio", help="Открыть визуальный редактор DECUI Studio")

    p_new = subparsers.add_parser("new", help="Создать проект DECUI")
    p_new.add_argument("name", help="Путь к каталогу нового проекта")
    p_new.add_argument(
        "--template",
        choices=("basic", "orders"),
        default="orders",
        help="basic — один справочник для первого запуска, orders — заказы и отчёт (по умолчанию)",
    )

    entrypoint_help = "Путь к Python-файлу (по умолчанию app.py или main.py)"
    p_run = subparsers.add_parser("run", help="Запустить приложение")
    p_run.add_argument("file", nargs="?", help=entrypoint_help)

    p_check = subparsers.add_parser("check", help="Проверить модели, ссылки и схему базы")
    p_check.add_argument("file", nargs="?", help=entrypoint_help)
    p_check.add_argument("--backup", type=Path, help="Проверить обновление схемы на копии резервной базы")

    p_test = subparsers.add_parser("test", help="Запустить тесты через pytest")
    p_test.add_argument("pytest_args", nargs=argparse.REMAINDER, help="Дополнительные аргументы pytest")

    p_build = subparsers.add_parser("build", help="Собрать исполняемый файл")
    p_build.add_argument("file", nargs="?", help=entrypoint_help)

    argv = sys.argv[1:]
    if argv[:1] == ["test"] and argv[1:] not in (["-h"], ["--help"]):
        # argparse rejects options before the first positional in a REMAINDER.
        # Let pytest parse its own arguments in their original order.
        args = parser.parse_args(argv[:1])
        args.pytest_args = argv[1:]
    else:
        args = parser.parse_args(argv)

    if args.command == "studio":
        from decui.studio import run_studio

        sys.exit(run_studio())
    elif args.command == "new":
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
