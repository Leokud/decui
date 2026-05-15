"""Отчёт с агрегацией (sum/avg/min/max) и форматированием.

Запуск:
    cd F:/projects/decui
    python examples/features/report_aggregate.py
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from datetime import date
from decimal import Decimal
from decui import (
    catalog, document, tab_part, report,
    Catalog, Document, String, Integer, Decimal as D, Ref,
    Report, Column, DateParam,
)


@catalog
class Product(Catalog):
    __verbose_name__ = "Товары"
    name  = String("Название")
    price = D("Цена", min_value=0)


@document
class Invoice(Document):
    __verbose_name__ = "Продажи"
    total = D("Итого", read_only=True, default=0)

    @tab_part
    class Items:
        product  = Ref(Product, "Товар", representation_field="name")
        quantity = Integer("Кол-во", default=1)
        price    = D("Цена")
        amount   = D("Сумма", read_only=True, default=0)


@report
class SalesReport(Report):
    title = "Продажи"
    columns = [
        Column("Товар", "product", width=200),
        Column("Кол-во", "qty", width=80, align="right",
               aggregate="sum"),
        Column("Сумма", "total", width=120, align="right",
               aggregate="sum", format="{:,.2f}", suffix=" руб."),
        Column("Цена ср.", "avg_price", width=100, align="right",
               aggregate="avg", format="{:,.2f}"),
    ]
    date_from = DateParam("Дата с", required=True,
                          default=lambda: date(2020, 1, 1))
    date_to   = DateParam("Дата по", required=True,
                          default=lambda: date(2030, 12, 31))

    def execute(self, db, date_from, date_to, **kw):
        rows = []
        for inv in db.list(Invoice):
            full = db.get(Invoice, inv._id)
            if not full or not full.posted:
                continue
            for item in full._table_data.get("Items", []):
                prod = db.get(Product, item.get("product"))
                qty = Decimal(str(item.get("quantity", 0)))
                price = item.get("price", Decimal("0"))
                amount = item.get("amount", Decimal("0"))
                rows.append({
                    "product":   prod.name if prod else "-",
                    "qty":       str(qty),
                    "total":     str(amount),
                    "avg_price": str(price),
                })
        return rows


def _seed(db):
    if db.list(Product):
        return
    p1 = Product(name="Кофе", price=Decimal("800"))
    p2 = Product(name="Чай",  price=Decimal("300"))
    db.save(p1)
    db.save(p2)

    inv = Invoice()
    inv._table_data["Items"] = [
        {"product": p1._id, "quantity": 10, "price": Decimal("800"),
         "amount": Decimal("8000")},
        {"product": p2._id, "quantity": 20, "price": Decimal("300"),
         "amount": Decimal("6000")},
    ]
    inv._data["total"] = Decimal("14000")
    db.save(inv)
    print(f"  Создан счёт на 14000 руб.")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="report_agg.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
