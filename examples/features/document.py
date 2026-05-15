"""Документ с табличной частью, командами, __defaults__.

Запуск:
    cd F:/projects/decui
    python examples/features/document.py
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from decimal import Decimal
from decui import (
    catalog, document, command, tab_part,
    Catalog, Document, String, Integer, Decimal as D, Ref, Boolean,
    report, Report, Column,
)


# ── Справочник ──

@catalog
class Product(Catalog):
    __verbose_name__ = "Товары"
    name  = String("Наименование", required=True)
    price = D("Цена", precision=10, scale=2, min_value=0)


# ── Документ ──

@document
class Invoice(Document):
    __numbering__    = {"prefix": "INV", "field": "number"}
    __verbose_name__ = "Счета"
    __defaults__     = {"paid": False}

    number = String("Номер", read_only=True)
    paid   = Boolean("Оплачен")
    total  = D("Итого", read_only=True, default=0)

    @tab_part
    class Items:
        __verbose_name__ = "Позиции"
        product  = Ref(Product, "Товар", required=True,
                       representation_field="name")
        quantity = Integer("Количество", default=1, min_value=1)
        price    = D("Цена", min_value=0)
        amount   = D("Сумма", read_only=True, default=0)

    @command("Рассчитать")
    def calc(self):
        t = Decimal("0")
        for r in self._table_data.get("Items", []):
            r["amount"] = Decimal(str(r.get("quantity", 0))) * r.get("price", 0)
            t += r["amount"]
        self._data["total"] = t

    @command("Провести")
    def post(self):
        self.calc()
        self._data["posted"] = True

    @command("Снять проведение")
    def unpost(self):
        self._data["posted"] = False


# ── Отчёт ──

@report
class SalesReport(Report):
    title = "Продажи"
    columns = [
        Column("Товар", "product", width=200),
        Column("Кол-во", "qty", width=80, align="right", aggregate="sum"),
        Column("Сумма", "total", width=120, align="right", aggregate="sum",
               format="{:,.2f}", suffix=" руб."),
    ]

    def execute(self, db, **kw):
        rows = []
        for inv in db.list(Invoice):
            full = db.get(Invoice, inv._id)
            if not full or not full.posted:
                continue
            for item in full._table_data.get("Items", []):
                prod = db.get(Product, item.get("product"))
                rows.append({
                    "product": prod.name if prod else "-",
                    "qty": str(item.get("quantity", "")),
                    "total": str(item.get("amount", "")),
                })
        return rows


# ── Демо-данные ──

def _seed(db):
    if db.list(Product):
        return
    p1 = Product(name="Кофе", price=Decimal("500"))
    p2 = Product(name="Чай",  price=Decimal("300"))
    db.save(p1)
    db.save(p2)

    inv = Invoice()
    inv._table_data["Items"] = [
        {"product": p1._id, "quantity": 10, "price": Decimal("500"),
         "amount": Decimal("0")},
        {"product": p2._id, "quantity": 5,  "price": Decimal("300"),
         "amount": Decimal("0")},
    ]
    inv.calc()
    inv.post()
    db.save(inv)
    print(f"  Счёт {inv.number}: {inv.total} руб., проведён: {inv.posted}")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="document.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
