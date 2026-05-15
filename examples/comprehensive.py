
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from datetime import date
from decimal import Decimal

from decui import (
    # Сущности
    Catalog, Document, Entity,
    # Декораторы
    catalog, document, command, tab_part, report,
    # Поля
    String, Integer, Decimal as D, Date, Boolean, Ref,
    # Отчёты
    Report, Column, DateParam, RefParam, StringParam,
)


# ╔══════════════════════════════════════════════════════════════════════�-
# ║  СПРАВОЧНИКИ                                                        ║
# ╚══════════════════════════════════════════════════════════════════════╝

@catalog
class Product(Catalog):
    """Товары — справочник с автонумерацией."""
    __numbering__ = {"prefix": "PRD", "field": "code"}

    code = String("Код", read_only=True)
    name = String("Наименование", required=True)
    unit = String("Ед. изм.", default="шт.")
    price = D("Цена", precision=10, scale=2, required=True)


@catalog
class Customer(Catalog):
    """Контрагенты."""
    __numbering__ = {"prefix": "CTR", "field": "code"}

    code = String("Код", read_only=True)
    name = String("Наименование", required=True)
    inn = String("ИНН")


@catalog
class Warehouse(Catalog):
    """Склады."""
    __numbering__ = {"prefix": "WH", "field": "code"}

    code = String("Код", read_only=True)
    name = String("Наименование", required=True)


# ╔══════════════════════════════════════════════════════════════════════�-
# ║  ДОКУМЕНТЫ                                                          ║
# ╚══════════════════════════════════════════════════════════════════════╝

@document
class Invoice(Document):
    """Счёт на оплату — документ с табличной частью."""

    __numbering__ = {"prefix": "INV", "field": "number"}

    number  = String("Номер", read_only=True)
    customer = Ref(Customer, "Контрагент", required=True,
                   representation_field="name")
    warehouse = Ref(Warehouse, "Склад", representation_field="name")
    total   = D("Итого", precision=10, scale=2, read_only=True, default=0)

    # ── Табличная часть ──
    @tab_part
    class Items:
        product  = Ref(Product, "Товар", required=True,
                       representation_field="name")
        quantity = Integer("Количество", required=True, default=1)
        price    = D("Цена", precision=10, scale=2, required=True)
        amount   = D("Сумма", precision=10, scale=2, read_only=True, default=0)

    # ── Команды ──
    @command("Рассчитать")
    def calc(self) -> None:
        """Пересчитать суммы строк и итог."""
        total = Decimal("0")
        for row in self._table_data.get("Items", []):
            qty = Decimal(str(row.get("quantity", 0)))
            prc = row.get("price", Decimal("0"))
            if isinstance(prc, Decimal):
                amt = qty * prc
            else:
                amt = qty * Decimal(str(prc))
            row["amount"] = amt
            total += amt
        self._data["total"] = total

    @command("Провести")
    def post(self) -> None:
        self.calc()
        self._data["posted"] = True

    @command("Снять проведение")
    def unpost(self) -> None:
        self._data["posted"] = False


@document
class Payment(Document):
    """Платёжное поручение — простой документ без табличной части."""

    __numbering__ = {"prefix": "PAY", "field": "number"}

    number   = String("Номер", read_only=True)
    customer = Ref(Customer, "Контрагент", required=True,
                   representation_field="name")
    amount   = D("Сумма", precision=10, scale=2, required=True)

    @command("Провести")
    def post(self) -> None:
        self._data["posted"] = True

    @command("Снять проведение")
    def unpost(self) -> None:
        self._data["posted"] = False


# ╔══════════════════════════════════════════════════════════════════════�-
# ║  ОТЧЁТЫ                                                             ║
# ╚══════════════════════════════════════════════════════════════════════╝

@report
class ProductList(Report):
    """Список товаров с ценами."""
    title = "Список товаров"
    columns = [
        Column("Код", "code", width=100),
        Column("Наименование", "name", width=250),
        Column("Ед.", "unit", width=60),
        Column("Цена", "price", width=120, align="right"),
    ]

    def execute(self, db, **kw):
        return [
            {"code": p.code, "name": p.name, "unit": p.unit,
             "price": str(p.price) if p.price else "0"}
            for p in db.list(Product)
        ]


@report
class SalesReport(Report):
    """Продажи за период с детализацией."""
    title = "Продажи за период"
    columns = [
        Column("Дата", "date", width=100),
        Column("Номер", "number", width=120),
        Column("Контрагент", "customer", width=200),
        Column("Товар", "product", width=200),
        Column("Кол-во", "qty", width=80, align="right"),
        Column("Сумма", "amount", width=120, align="right"),
    ]

    date_from = DateParam("Дата с", required=True)
    date_to   = DateParam("Дата по", required=True)
    customer  = RefParam("Контрагент", Customer, representation_field="name")

    def execute(self, db, date_from, date_to, customer, **kw):
        rows = []
        # Load each invoice fully (with table parts)
        for inv_summary in db.list(Invoice):
            inv = db.get(Invoice, inv_summary._id)
            if inv is None:
                continue
            if inv.date:
                if date_from and inv.date < date_from:
                    continue
                if date_to and inv.date > date_to:
                    continue
            if customer and inv.customer != customer:
                continue

            inv_cust = db.get(Customer, inv.customer)
            cust_name = inv_cust.name if inv_cust else f"#{inv.customer}"

            items = inv._table_data.get("Items", [])
            if items:
                for row in items:
                    prod = db.get(Product, row.get("product"))
                    rows.append({
                        "date": str(inv.date),
                        "number": inv.number or "",
                        "customer": cust_name,
                        "product": prod.name if prod else f"#{row.get('product')}",
                        "qty": str(row.get("quantity", "")),
                        "amount": str(row.get("amount", "")),
                    })
            else:
                rows.append({
                    "date": str(inv.date),
                    "number": inv.number or "",
                    "customer": cust_name,
                    "product": "—",
                    "qty": "—",
                    "amount": str(inv.total or "0"),
                })
        return rows


# ╔══════════════════════════════════════════════════════════════════════�-
# ║  �-АПУСК                                                             ║
# ╚══════════════════════════════════════════════════════════════════════╝

def main() -> None:
    from decui.app import ERPApplication

    app = ERPApplication(db_path="erp_demo.db")
    app.run()


if __name__ == "__main__":
    main()
