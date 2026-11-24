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


def test_ide_typing_and_annotations():
    @master_data
    class Student(MasterData):
        __verbose_name__ = "Студенты"
        __section__ = "Учёба"

        name: str
        age: int = 18
        gpa: Decimal = Decimal("4.00")
        is_active: bool = True
        birth_date: datetime.date

    # 1. Introspection methods on class
    fields = Student.fields()
    assert "name" in fields
    assert "age" in fields
    assert "gpa" in fields
    assert "is_active" in fields
    assert "birth_date" in fields

    assert Student.field_names() == ["name", "age", "gpa", "is_active", "birth_date"]

    # 2. Accessing field on class returns descriptor
    assert hasattr(Student, "name")
    assert Student.name == fields["name"]
    assert Student.age == fields["age"]

    # 3. ClassVar metadata
    assert Student.__verbose_name__ == "Студенты"
    assert Student.__section__ == "Учёба"

    # 4. Instantiation with kwargs
    s = Student(name="Иван", age=20, gpa=Decimal("4.75"), is_active=True, birth_date=datetime.date(2004, 5, 10))
    assert s.name == "Иван"
    assert s.age == 20
    assert s.gpa == Decimal("4.75")
    assert s.is_active is True
    assert s.birth_date == datetime.date(2004, 5, 10)

    # 5. Setting attribute via descriptor/entity
    s.name = "Пётр"
    s.age = 21
    assert s.name == "Пётр"
    assert s.age == 21
    assert s._data["name"] == "Пётр"
    assert s._data["age"] == 21

    # 6. Document built-in fields and types
    @document
    class Goal(Document):
        title: str

    assert "date" in Goal.fields()
    assert "number" in Goal.fields()
    assert "posted" in Goal.fields()
    assert "title" in Goal.fields()

    g = Goal(title="Пройти отбор")
    assert g.title == "Пройти отбор"
    assert isinstance(g.date, datetime.datetime)
    assert g.number == ""
    assert g.posted is False
    assert g.id is None
