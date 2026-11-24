from decimal import Decimal
import datetime
import pytest

from decui import (
    Catalog,
    Document,
    MasterData,
    Lookup,
    Transaction,
    String,
    Integer,
    Decimal as D,
    Boolean,
    catalog,
    document,
    master_data,
    transaction,
    command,
    action,
    tab_part,
    line_items,
)


def test_catalog_and_aliases():
    @catalog
    class Item(Catalog):
        name = String("Название", required=True)
        price = D("Цена", precision=10, scale=2)

    assert Item._meta.is_catalog
    assert not Item._meta.is_document
    assert "name" in Item._meta.fields
    assert "price" in Item._meta.fields

    item = Item(name="Test", price=Decimal("100.00"))
    assert item.name == "Test"
    assert item.price == Decimal("100.00")
    assert item._id is None

    # Test alias MasterData / master_data
    @master_data
    class Supplier(MasterData):
        title = String("Поставщик")

    assert Supplier._meta.is_catalog
    s = Supplier(title="Global Corp")
    assert s.title == "Global Corp"


def test_document_and_line_items_and_actions():
    @document
    class Order(Document):
        number = String("Номер")
        total = D("Итого", default=Decimal("0.00"))

        @line_items
        class Items:
            product = String("Товар")
            qty = Integer("Количество", default=1)
            price = D("Цена", default=Decimal("0.00"))

        @action("Пересчитать")
        def calculate(self):
            tot = Decimal("0.00")
            for row in self._table_data.get("Items", []):
                q = Decimal(str(row.get("qty", 0)))
                p = row.get("price", Decimal("0.00"))
                tot += q * p
            self.total = tot

    assert Order._meta.is_document
    assert "Items" in Order._meta.table_parts
    assert "calculate" in Order._meta.commands

    order = Order(number="ORD-001")
    assert order.number == "ORD-001"
    assert not order.is_posted
    assert not order.posted

    # Lifecycle methods
    order.post()
    assert order.is_posted
    assert order.posted

    order.unpost()
    assert not order.is_posted

    order.submit()
    assert order.is_posted

    order.cancel()
    assert not order.is_posted

    # Line items
    order._table_data["Items"] = [
        {"product": "Apples", "qty": 2, "price": Decimal("50.00")},
        {"product": "Bananas", "qty": 3, "price": Decimal("30.00")},
    ]
    order.calculate()
    assert order.total == Decimal("190.00")


def test_transaction_alias():
    @transaction
    class Waybill(Transaction):
        note = String("Примечание")

    wb = Waybill(note="Delivery")
    assert wb._meta.is_document
    assert wb.note == "Delivery"
    wb.submit()
    assert wb.is_posted
