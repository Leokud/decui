from decimal import Decimal
import datetime
import pytest

from decui import (
    Catalog,
    Document,
    String,
    Integer,
    Decimal as D,
    Ref,
    catalog,
    document,
    tab_part,
    Database,
)


@catalog
class Category(Catalog):
    title = String("Категория", required=True)


@catalog
class ProductModel(Catalog):
    __numbering__ = {"prefix": "PRD", "field": "code", "pattern": "{prefix}-{seq:06d}"}
    code = String("Код", read_only=True)
    name = String("Название", required=True)
    price = D("Цена", precision=10, scale=2, default=Decimal("0.00"))
    category = Ref(Category, "Категория")


@document
class SaleDoc(Document):
    __numbering__ = {"prefix": "SALE", "field": "number", "pattern": "{prefix}-{seq:06d}"}
    number = String("Номер", read_only=True)
    customer = String("Покупатель")


    @tab_part
    class Lines:
        product = Ref(ProductModel, "Товар")
        quantity = Integer("Кол-во", default=1)
        amount = D("Сумма", precision=10, scale=2, default=Decimal("0.00"))


def test_database_crud():
    db = Database(":memory:")
    assert db._ready

    # Create & Save Category
    cat = Category(title="Электроника")
    db.save(cat)
    assert cat._id is not None
    cat_id = cat._id

    # Get Category
    loaded_cat = db.get(Category, cat_id)
    assert loaded_cat is not None
    assert loaded_cat.title == "Электроника"

    # Save Product with numbering and Ref
    prod = ProductModel(name="Ноутбук", price=Decimal("75000.00"), category=cat_id)
    db.save(prod)
    assert prod._id is not None
    assert prod.code.startswith("PRD-")

    # Update Product
    prod.price = Decimal("72000.00")
    db.save(prod)
    updated_prod = db.get(ProductModel, prod._id)
    assert updated_prod.price == Decimal("72000.00")

    # List
    all_products = db.list(ProductModel)
    assert len(all_products) == 1
    assert all_products[0].name == "Ноутбук"

    # Save Document with Line Items
    doc = SaleDoc(customer="Иван")
    doc._table_data["Lines"] = [
        {"product": prod._id, "quantity": 1, "amount": Decimal("72000.00")},
        {"product": prod._id, "quantity": 2, "amount": Decimal("144000.00")},
    ]
    db.save(doc)
    assert doc._id is not None
    assert doc.number.startswith("SALE-")

    # Reload document with table parts
    loaded_doc = db.get(SaleDoc, doc._id)
    assert loaded_doc is not None
    assert len(loaded_doc._table_data.get("Lines", [])) == 2

    # Delete
    db.delete(loaded_doc)
    assert db.get(SaleDoc, doc._id) is None
