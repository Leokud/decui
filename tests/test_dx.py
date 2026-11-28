from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from decimal import Decimal
from html import unescape
from pathlib import Path
import pytest

from decui import (
    Database,
    MasterData,
    Transaction,
    line_items,
    String,
    Integer,
    Decimal as D,
    Boolean,
    Ref,
    LineItemsCollection,
    master_data,
    transaction,
)


# ── Fixtures & Models ──────────────────────────────────────────────────

@master_data
class DXProduct(MasterData):
    name = String(required=True)
    unit_price = D(scale=2, currency="RUB")
    is_active = Boolean(default=True)
    custom_label = String("Специальное название")


@transaction
class DXInvoice(Transaction):
    number = String()
    product = Ref(DXProduct)

    @line_items
    class Items:
        product = Ref(DXProduct)
        quantity = Integer(default=1)
        price = D(scale=2)
        amount = D(scale=2)


@pytest.fixture
def db():
    return Database.memory()


# ── Tests ──────────────────────────────────────────────────────────────

def test_field_auto_naming():
    """Field display names should be inferred from attribute names when omitted."""
    fields = DXProduct._meta.fields
    assert fields["name"].display_name == "Name"
    assert fields["unit_price"].display_name == "Unit price"
    assert fields["is_active"].display_name == "Is active"
    # Explicit display name must be preserved
    assert fields["custom_label"].display_name == "Специальное название"


def test_line_items_collection():
    """LineItemsCollection provides .add() and .total() while retaining list behavior."""
    inv = DXInvoice(number="INV-001")
    assert isinstance(inv.Items, LineItemsCollection)
    assert len(inv.Items) == 0

    # Add items using kwargs
    row1 = inv.Items.add(quantity=2, price=Decimal("150.00"), amount=Decimal("300.00"))
    assert row1["quantity"] == 2
    assert row1["amount"] == Decimal("300.00")
    assert len(inv.Items) == 1

    inv.Items.add(quantity=1, price=Decimal("200.00"), amount=Decimal("200.00"))
    assert len(inv.Items) == 2

    # Test total calculation
    assert inv.Items.total("amount") == Decimal("500.00")
    assert inv.Items.total("quantity") == 3

    # Test list operations
    assert inv.Items[0]["amount"] == Decimal("300.00")
    assert [r["amount"] for r in inv.Items] == [Decimal("300.00"), Decimal("200.00")]

    inv.Items.clear()
    assert len(inv.Items) == 0


def test_active_record_methods(db):
    """Entity instances provide save(), delete(), get(), all(), filter()."""
    p1 = DXProduct(name="Apple", unit_price=Decimal("12.50"))
    p1.save(db)
    assert p1._id is not None

    p2 = DXProduct(name="Banana", unit_price=Decimal("8.00"))
    p2.save(db)

    # Fetch by ID
    fetched = DXProduct.get(db, p1._id)
    assert fetched is not None
    assert fetched.name == "Apple"
    assert fetched.unit_price == Decimal("12.50")

    # Fetch all
    all_products = DXProduct.all(db)
    assert len(all_products) >= 2
    names = [p.name for p in all_products]
    assert "Apple" in names
    assert "Banana" in names

    # Filter
    bananas = DXProduct.filter(db, name="Banana")
    assert len(bananas) == 1
    assert bananas[0].name == "Banana"

    # Delete
    p1.delete(db)
    assert DXProduct.get(db, p1._id) is None


def test_to_dict_and_from_dict():
    """to_dict and from_dict allow easy serialization and round-tripping."""
    p = DXProduct(name="Orange", unit_price=Decimal("15.00"), is_active=True)
    p_dict = p.to_dict()
    assert p_dict["name"] == "Orange"
    assert p_dict["unit_price"] == Decimal("15.00")
    assert p_dict["is_active"] is True

    # Reconstruct from dict
    restored = DXProduct.from_dict(p_dict)
    assert restored.name == "Orange"
    assert restored.unit_price == Decimal("15.00")

    # Document with line items
    inv = DXInvoice(number="INV-999")
    inv.Items.add(quantity=5, price=Decimal("10.00"), amount=Decimal("50.00"))
    inv_dict = inv.to_dict()
    assert inv_dict["number"] == "INV-999"
    assert len(inv_dict["Items"]) == 1
    assert inv_dict["Items"][0]["amount"] == Decimal("50.00")

    restored_inv = DXInvoice.from_dict(inv_dict)
    assert restored_inv.number == "INV-999"
    assert len(restored_inv.Items) == 1
    assert restored_inv.Items.total("amount") == Decimal("50.00")


def test_database_helpers(db):
    """Database.memory(), db.seed(), and db.filter() work as expected."""
    assert db._ready is True

    # Batch seed
    items = [
        DXProduct(name="Item 1", unit_price=Decimal("1.00")),
        DXProduct(name="Item 2", unit_price=Decimal("2.00")),
    ]
    seeded = db.seed(items)
    assert len(seeded) == 2
    assert all(item._id is not None for item in seeded)

    # Filter on db
    res = db.filter(DXProduct, name="Item 1")
    assert len(res) == 1
    assert res[0].name == "Item 1"


def test_repr_and_str():
    """String representations should show meaningful names."""
    p = DXProduct(name="Widget")
    assert str(p) == "Widget"
    assert "Widget" in repr(p)

    inv = DXInvoice(number="INV-42")
    assert str(inv) == "INV-42"
    assert "INV-42" in repr(inv)


@pytest.mark.parametrize(
    ("template_args", "schema_summary", "test_name"),
    [
        ([], "сущностей — 3, отчётов — 1", "test_order_round_trip"),
        (["--template", "basic"], "сущностей — 1, отчётов — 0", "test_product_round_trip"),
    ],
)
def test_installed_cli_scaffolds_and_tests_project(tmp_path, template_args, schema_summary, test_name):
    name = "decui.exe" if sys.platform == "win32" else "decui"
    executable = Path(sysconfig.get_path("scripts")) / name
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"

    def cli(cwd, *args):
        result = subprocess.run(
            [str(executable), *args], cwd=cwd, env=env, timeout=30,
            capture_output=True, encoding="utf-8",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    assert cli(tmp_path, "--version").startswith("decui ")
    assert "decui test" in cli(tmp_path, "new", "my_erp", *template_args)
    project = tmp_path / "my_erp"
    assert (project / "README.md").is_file()
    ignore_rules = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"*.db", "*.sqlite3", "*.db-*", "*.sqlite3-*", "/dist/"} <= set(ignore_rules)
    assert schema_summary in cli(project, "check")
    assert "1 passed" in cli(project, "test", "-q", f"test_app.py::{test_name}")
    assert "pytest_args" in cli(project, "test", "--help")
    if template_args:
        tutorial = (Path(__file__).resolve().parents[1] / "docs/index.html").read_text(encoding="utf-8")
        example = tutorial.split('id="basic-app-example"', 1)[1].split(
            '<code class="language-python">', 1
        )[1].split("</code>", 1)[0]
        assert unescape(example) == (project / "app.py").read_text(encoding="utf-8").rstrip()


def test_cli_check_reports_all_unregistered_reference_targets(tmp_path):
    (tmp_path / "app.py").write_text(
        """from decui import (
    MasterData, Ref, RefDimension, RefParam, Report, Transaction,
    ledger, line_items, report, transaction,
)


class Unregistered(MasterData):
    pass


@transaction
class Order(Transaction):
    customer = Ref(Unregistered)

    @line_items
    class Items:
        product = Ref("MissingProduct")


@report
class Orders(Report):
    customer = RefParam("Клиент", "MissingCustomer")


@ledger
class Stock:
    product = RefDimension("MissingItem", "Товар")
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "decui.cli", "check"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )

    assert result.returncode == 1
    assert "Проверка не пройдена:" in result.stdout
    assert "Order.customer: цель ссылки «Unregistered»" in result.stdout
    assert "Order.Items.product: цель ссылки «MissingProduct»" in result.stdout
    assert "Orders.customer: цель ссылки «MissingCustomer»" in result.stdout
    assert "Stock.product: цель ссылки «MissingItem»" in result.stdout


def test_cli_check_and_build_russian_messages(tmp_path, monkeypatch, capsys):
    import argparse
    from decui import cli

    missing = tmp_path / "missing.py"
    assert cli.cmd_check(argparse.Namespace(file=str(missing))) == 1
    assert "не найден" in capsys.readouterr().err

    broken_app = tmp_path / "broken.py"
    broken_app.write_text("raise RuntimeError('сломанный импорт')\n", encoding="utf-8")
    assert cli.cmd_check(argparse.Namespace(file=str(broken_app))) == 1
    captured = capsys.readouterr()
    assert "Ошибка импорта" in captured.err

    # Database init failure inside decui check prints Russian error without raw traceback
    bad_schema_app = tmp_path / "bad_schema.py"
    bad_schema_app.write_text(
        "from decui import Catalog, Ref, catalog\n"
        "@catalog\n"
        "class BadA(Catalog):\n"
        "    link = Ref('BadA', choice_filter={'missing': 'link'})\n",
        encoding="utf-8",
    )
    bad_res = subprocess.run(
        [sys.executable, "-m", "decui.cli", "check", str(bad_schema_app)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert bad_res.returncode == 1
    assert "Проверка не пройдена:" in bad_res.stdout
    assert "Не удалось инициализировать базу данных:" in bad_res.stdout
    assert "Traceback" not in bad_res.stdout

    app_py = tmp_path / "app.py"
    app_py.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(cli.subprocess, "call", lambda _cmd: 0)
    assert cli.cmd_build(argparse.Namespace(file=str(app_py))) == 0
    assert "Сборка исполняемого файла для" in capsys.readouterr().out


def test_cli_handles_non_utf8_stdio_encoding(tmp_path):
    app_py = tmp_path / "app.py"
    app_py.write_text(
        "from decui import MasterData, String, master_data\n\n"
        "@master_data\n"
        "class Product(MasterData):\n"
        "    name = String('Наименование')\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    result = subprocess.run(
        [sys.executable, "-m", "decui.cli", "check", str(app_py)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Проверка пройдена (app.py):" in result.stdout

    missing_res = subprocess.run(
        [sys.executable, "-m", "decui.cli", "check", str(tmp_path / "missing.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert missing_res.returncode == 1
    assert "не найден" in missing_res.stderr
