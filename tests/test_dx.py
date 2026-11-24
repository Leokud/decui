from __future__ import annotations

import os
from decimal import Decimal
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


def test_cli_new_and_run(tmp_path):
    """decui CLI 'new' command scaffolds a runnable project."""
    from decui.cli import cmd_new, cmd_run
    import argparse

    target_dir = tmp_path / "my_erp"
    args = argparse.Namespace(name=str(target_dir))
    ret = cmd_new(args)
    assert ret == 0

    assert (target_dir / "app.py").is_file()
    assert (target_dir / "README.md").is_file()

    content = (target_dir / "app.py").read_text(encoding="utf-8")
    assert "ERPApplication" in content
    assert "SalesOrder" in content
