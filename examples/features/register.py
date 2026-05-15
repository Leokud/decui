"""Регистр накопления + движения + get_balance.

Запуск:
    cd F:/projects/decui
    python examples/features/register.py
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from datetime import date
from decimal import Decimal
from decui import (
    catalog, document, command, tab_part, accumulation, report, Report, Column,
    Catalog, Document, String, Integer, Decimal as D, Ref,
    Dimension, RefDimension, Resource, Movement, get_balance,
)


# ── Справочник ──

@catalog
class Product(Catalog):
    __verbose_name__ = "Товары"
    name  = String("Название", required=True)
    price = D("Цена", min_value=0)


# ── Регистр ──

@accumulation
class Sales:
    period  = Dimension("Период", date)
    product = RefDimension(Product, "Товар")
    revenue = Resource("Выручка")


# ── Документ с движениями ──

@document
class Invoice(Document):
    __numbering__    = {"prefix": "INV", "field": "number"}
    __verbose_name__ = "Счета"
    total = D("Итого", read_only=True, default=0)

    @tab_part
    class Items:
        __verbose_name__ = "товары"
        product  = Ref(Product, "Товар", representation_field="name")
        quantity = Integer("Кол-во", default=1)
        price    = D("Цена", min_value=0)
        amount   = D("Сумма", read_only=True, default=0)

    @command("Провести")
    def post(self):
        t = Decimal("0")
        for r in self._table_data.get("Items", []):
            r["amount"] = Decimal(str(r.get("quantity", 0))) * r.get("price", 0)
            t += r["amount"]
        self._data["total"] = t
        self._data["posted"] = True

    def get_movements(self):
        return [
            Movement(register=Sales, period=date.today(),
                     dimensions={"product": item["product"]},
                     resources={"revenue": item["amount"]},
                     is_income=True)
            for item in self._table_data.get("Items", [])
        ]


# ── Отчёт ──

@report
class BalanceReport(Report):
    title = "Остатки по регистру"
    columns = [
        Column("Товар", "product", width=200),
        Column("Выручка", "revenue", width=120, align="right",
               format="{:,.2f}", suffix=" руб."),
    ]

    def execute(self, db, **kw):
        balance = get_balance(db, Sales)
        result = []
        for b in balance:
            pid = b.get("product")
            prod = db.get(Product, pid) if pid else None
            result.append({
                "product": prod.name if prod else "-",
                "revenue": str(b.get("revenue", 0)),
            })
        return result


# ── Демо-данные ──

def _seed(db):
    from decui.movement import write_movements
    if db.list(Product):
        return
    p = Product(name="Кофе", price=Decimal("500"))
    db.save(p)

    inv = Invoice()
    inv._table_data["Items"] = [
        {"product": p._id, "quantity": 10, "price": Decimal("500"),
         "amount": Decimal("0")},
    ]
    inv.post()
    db.save(inv)
    write_movements(db, inv.get_movements())

    bal = get_balance(db, Sales)
    rev = bal[0].get("revenue", 0) if bal else 0
    print(f"  {inv.number}: {inv.total} руб., выручка по регистру: {rev}")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="register.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
