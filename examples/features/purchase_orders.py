"""Учебный маршрут от карточки товара до отчёта по строкам.

В каталоге учебного проекта запустите копию файла с --demo --db data.sqlite3.
Из корня репозитория запустите python -m examples.features.purchase_orders --demo.
"""

import argparse
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from decui import (
    Column, Database, DateParam, Decimal as D, ERPApplication, Integer,
    MasterData, Ref, RefDimension, RefParam, Report, Resource, String, Transaction,
    get_latest, history_register, line_items, master_data, report, set_record, transaction,
)


@master_data
class Product(MasterData):
    __verbose_name__ = "Товары"
    __section__ = "Склад"

    name = String("Наименование", required=True)
    price = D("Цена", precision=10, scale=2, min_value=0)
    priority = Integer("Приоритет", default=3)


@master_data
class Supplier(MasterData):
    __verbose_name__ = "Поставщики"
    name = String("Наименование", required=True)


@history_register
class Prices:
    product = RefDimension(Product, "Товар")
    supplier = RefDimension(Supplier, "Поставщик")
    price = Resource("Цена")


@transaction
class PurchaseOrder(Transaction):
    __verbose_name__ = "Заказы поставщикам"
    supplier = Ref(Supplier, "Поставщик", required=True, related_list="Заказы поставщику")
    total = D("Итого", scale=2, read_only=True)

    @line_items
    class Items:
        product = Ref(Product, "Товар", required=True)
        quantity = Integer("Количество", min_value=1, default=1)
        unit_price = D("Цена на дату заказа", scale=2, read_only=True)
        amount = D("Сумма", scale=2, read_only=True)

    def on_validate(self):
        return [] if self.Items else ["Добавьте хотя бы одну строку заказа."]

    def before_save(self, db):
        prices = []
        for line, row in enumerate(self.Items, start=1):
            record = get_latest(
                db, Prices, on_date=self.date,
                product=row["product"], supplier=self.supplier,
            )
            if record is None or record["price"] is None or record["price"] < 0:
                product = db.get(Product, row["product"])
                name = product.name if product is not None else f"ID {row['product']}"
                raise ValueError(
                    f"Строка {line}, товар «{name}», {self.date:%d.%m.%Y}: "
                    "нет допустимой цены поставщика."
                )
            prices.append(record["price"])

        for row, price in zip(self.Items, prices, strict=True):
            row["unit_price"] = price
            row["amount"] = (row["quantity"] * price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        self.total = self.Items.total("amount")


@report
class PurchaseReport(Report):
    title = "Закупки по строкам"
    auto_run = True
    supplier = RefParam("Поставщик", Supplier, from_record=True)
    date_from = DateParam("С", default=date(2026, 11, 1))
    date_to = DateParam("По", default=date(2026, 11, 30))
    columns = [
        Column("Заказ", "number"),
        Column("Товар", "product"),
        Column("Сумма", "amount", aggregate="sum", format="{:,.2f}"),
    ]

    def execute(
        self, db: Database, *, date_from: date, date_to: date, supplier: int | None = None,
    ) -> list[dict]:
        rows = []
        filters = {"supplier": supplier} if supplier is not None else {}
        orders = db.filter(
            PurchaseOrder,
            date__date_range=(date_from, date_to),
            include_table_parts=True,
            **filters,
        )
        products = {product.id: product.name for product in db.list(Product)}
        for order in orders:
            for item in order.Items:
                rows.append(dict(
                    number=order.number,
                    product=products.get(item["product"], ""),
                    amount=item["amount"],
                    _entity_class=PurchaseOrder,
                    _entity_id=order.id,
                ))
        return rows


def seed_demo(db: Database):
    with db.transaction():
        if db.filter(PurchaseOrder, number="DEMO-NOV"):
            return
        products = db.list(Product)
        product = products[0] if products else db.save(Product(name="Кофе", price=Decimal("99")))
        north = db.save(Supplier(name="Север"))
        south = db.save(Supplier(name="Юг"))
        for supplier, day, price in (
            (north, date(2026, 11, 1), "12.50"),
            (north, date(2026, 12, 1), "15.00"),
            (south, date(2026, 11, 1), "0.00"),
        ):
            set_record(db, Prices, dimensions={"product": product.id, "supplier": supplier.id},
                       resources={"price": Decimal(price)}, period=day)
        for number, supplier, moment in (
            ("DEMO-NOV", north, datetime(2026, 11, 30, 23, 59, 59)),
            ("DEMO-JAN", south, datetime(2027, 1, 1)),
        ):
            order = PurchaseOrder(number=number, supplier=supplier.id, date=moment)
            order.Items.add(product=product.id, quantity=2)
            db.save(order, validate=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Пример учёта товаров и закупок")
    parser.add_argument("--demo", action="store_true", help="Добавить демонстрационные заказы")
    parser.add_argument("--db", help="Путь к базе SQLite учебного проекта")
    args = parser.parse_args()
    app = ERPApplication(title="Учёт товаров", db_path=args.db, app_id="warehouse", start=Product)
    if args.demo:
        seed_demo(app.database)
    raise SystemExit(app.run())
