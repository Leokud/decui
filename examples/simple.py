"""
Демонстрационный пример DECUI: справочник «Товары» + документ «Счёт» + отчёты.

Запуск:
    cd F:/projects/decui
    python examples/simple.py

При первом запуске создаются демо-данные (3 товара, 1 счёт с 3 строками).

Интерфейс после загрузки:
    Справочники → Товары       — список, Новый / Изменить / Удалить
    Документы   → Счета        — список, кнопки те же
    Отчёты      → Список товаров — нажать «Сформировать»
    Отчёты      → Продажи      — выбрать даты → «Сформировать»

Работа со счётом:
    1. Откройте «Счета» → нажмите «Новый»
    2. Выберите товар, заполните количество и цену в таблице
    3. Нажмите «Рассчитать»  — пересчитает суммы строк и итог
    4. Нажмите «Провести»    — рассчитает + установит флаг «Проведён»
    5. Нажмите «Сохранить и закрыть»

Команды «Провести» / «Рассчитать» / «Снять проведение»
автоматически сохраняют документ после выполнения.
"""

import sys
from pathlib import Path

# Добавляем корень проекта в sys.path, чтобы `import decui` работал
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from decimal import Decimal
from datetime import datetime

from decui import (
    # Декораторы
    catalog,
    document,
    command,
    tab_part,
    report,
    # Базовые классы
    Catalog,
    Document,
    Report,
    # Дескрипторы полей
    String,
    Integer,
    Decimal as D,
    Ref,
    # Отчёты
    Column,
    DateParam,
)


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ЧАСТЬ 1: ОПРЕДЕЛЕНИЕ СУЩНОСТЕЙ                                     ║
# ║  Пользователь пишет ТОЛЬКО это — остальное генерирует фреймворк.    ║
# ╚══════════════════════════════════════════════════════════════════════╝

# ── 1.1. Справочник ──────────────────────────────────────────────────

@catalog
class Product(Catalog):
    """Товары — простой справочник с автонумерацией.

    Поля:
      code  — автозаполняется из нумератора (PRD-000001)
      name  — обязательное строковое поле
      price — десятичное число с двумя знаками после запятой
    """

    __numbering__    = {"prefix": "PRD", "field": "code"}
    __verbose_name__ = "Товары"      # отображается в навигации

    code  = String("Код", read_only=True)
    name  = String("Наименование", required=True)
    price = D("Цена", precision=10, scale=2, required=True)


# ── 1.2. Документ с табличной частью и командами ────────────────────

@document
class Invoice(Document):
    """Счёт на оплату — документ с табличной частью и бизнес-командами.

    Унаследовано от Document (автоматически):
      date    — DateTimeField("Дата")     — сейчас
      number  — StringField("Номер")      — INV-000001
      posted  — BooleanField("Проведён")  — False

    Собственные поля:
      total    — итоговая сумма, read_only (заполняется командой)

    Табличная часть Items (товары указаны только здесь):
      product  — ссылка на товар (выпадающий список)
      quantity — количество
      price    — цена
      amount   — сумма = quantity x price

    Команды (кнопки на форме, после выполнения сохраняют документ):
      Рассчитать       — пересчитать amount в строках и total документа
      Провести         — рассчитать + posted = True
      Снять проведение — posted = False
    """

    __numbering__    = {"prefix": "INV", "field": "number"}
    __verbose_name__ = "Счета"
    __defaults__ = {
        "Items": lambda db: [
            {
                "product": db.list(Product)[0]._id if db.list(Product) else None,
                "quantity": 1,
                "price": db.list(Product)[0].price if db.list(Product) else 0,
                "amount": 0,
            },
        ],
    }

    # Поля
    number = String("Номер", read_only=True)
    total  = D("Сумма", precision=10, scale=2,
               read_only=True, default=0)

    # ── Табличная часть ──
    @tab_part
    class Items:
        """Позиции счёта: товар, количество, цена, сумма."""
        product  = Ref(Product, "Товар", required=True,
                       representation_field="name")
        quantity = Integer("Количество", required=True, default=1)
        price    = D("Цена", precision=10, scale=2, required=True)
        amount   = D("Сумма", precision=10, scale=2,
                     read_only=True, default=0)

    # ── Команды ──
    # self._data[...]       — доступ к полям документа
    # self._table_data[...] — доступ к строкам табличной части

    @command("Рассчитать")
    def calc(self) -> None:
        """Пересчитать amount в каждой строке и total документа."""
        total = Decimal("0")
        for row in self._table_data.get("Items", []):
            qty = Decimal(str(row.get("quantity", 0)))
            prc = row.get("price", Decimal("0"))
            if not isinstance(prc, Decimal):
                prc = Decimal(str(prc))
            row["amount"] = qty * prc
            total += row["amount"]
        self._data["total"] = total

    @command("Провести")
    def post(self) -> None:
        """Рассчитать и установить флаг «Проведён»."""
        self.calc()
        self._data["posted"] = True

    @command("Снять проведение")
    def unpost(self) -> None:
        """Снять флаг «Проведён»."""
        self._data["posted"] = False


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ЧАСТЬ 2: ОТЧЁТЫ                                                    ║
# ╚══════════════════════════════════════════════════════════════════════╝

@report
class ProductReport(Report):
    """Список всех товаров с ценами. Без параметров."""

    title = "Список товаров"
    columns = [
        Column("Код", "code", width=120),
        Column("Наименование", "name", width=250),
        Column("Цена", "price", width=120, align="right"),
    ]

    def execute(self, db, **params):
        return [
            {
                "code": p.code or "",
                "name": p.name or "",
                "price": str(p.price) if p.price else "0",
            }
            for p in db.list(Product)
        ]


@report
class SalesReport(Report):
    """Продажи за период. Выводит построчную детализацию счетов.

    Параметры:
      date_from, date_to — обязательный период
    """

    title = "Продажи"
    columns = [
        Column("Дата", "date", width=160),
        Column("Номер", "number", width=120),
        Column("Товар", "product", width=200),
        Column("Кол-во", "qty", width=80, align="right"),
        Column("Сумма", "total", width=120, align="right"),
    ]

    date_from = DateParam("Дата с", required=True)
    date_to   = DateParam("Дата по", required=True)

    def execute(self, db, date_from, date_to, **kw):
        rows = []
        for inv_summary in db.list(Invoice):
            inv = db.get(Invoice, inv_summary._id)
            if inv is None:
                continue

            # Фильтр по дате
            if inv.date:
                dt = inv.date.replace(tzinfo=None)
                if date_from and dt < datetime.combine(
                    date_from, datetime.min.time()
                ):
                    continue
                if date_to and dt > datetime.combine(
                    date_to, datetime.max.time()
                ):
                    continue

            date_str = (
                inv.date.strftime("%d.%m.%Y %H:%M") if inv.date else ""
            )

            # Детализация по строкам табличной части
            items = inv._table_data.get("Items", [])
            if items:
                for item in items:
                    prod = db.get(Product, item.get("product"))
                    rows.append({
                        "date": date_str,
                        "number": inv.number or "",
                        "product": prod.name if prod else "-",
                        "qty": str(item.get("quantity", "")),
                        "total": str(item.get("amount", "")),
                    })
            else:
                rows.append({
                    "date": date_str,
                    "number": inv.number or "",
                    "product": "-",
                    "qty": "-",
                    "total": str(inv.total or "0"),
                })
        return rows


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ЧАСТЬ 3: ДЕМО-ДАННЫЕ                                               ║
# ╚══════════════════════════════════════════════════════════════════════╝

def _seed_data(db):
    """Заполнить БД демо-данными (вызывается при первом запуске).

    Демонстрирует программное создание сущностей:
      1. Создание товаров через конструктор + save
      2. Создание документа с табличной частью через _table_data
      3. Выполнение команд (calc → post)
      4. Сохранение документа
    """
    if db.list(Product):
        return  # данные уже есть

    # ── Товары ──
    coffee = Product(name="Кофе арабика", price=Decimal("850.00"))
    db.save(coffee)

    tea = Product(name="Чай зелёный", price=Decimal("350.00"))
    db.save(tea)

    sugar = Product(name="Сахар тростниковый", price=Decimal("120.00"))
    db.save(sugar)

    print("  -> Товары: Кофе, Чай, Сахар")
    print(f"     Коды: {coffee.code}, {tea.code}, {sugar.code}")

    # ── Счёт ──
    inv = Invoice()
    inv._table_data["Items"] = [
        {"product": coffee._id, "quantity": 10,
         "price": Decimal("850"), "amount": Decimal("0")},
        {"product": tea._id, "quantity": 5,
         "price": Decimal("350"), "amount": Decimal("0")},
        {"product": sugar._id, "quantity": 20,
         "price": Decimal("120"), "amount": Decimal("0")},
    ]

    inv.calc()
    inv.post()
    db.save(inv)

    print(f"  -> Счет N{inv.number} на сумму {inv.total} руб.")
    print(f"     Позиций: {len(inv._table_data['Items'])}")
    print(f"     Проведен: {inv.posted}")
    print()


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ЧАСТЬ 4: ЗАПУСК ПРИЛОЖЕНИЯ                                         ║
# ╚══════════════════════════════════════════════════════════════════════╝

def main() -> None:
    from decui.app import ERPApplication

    app = ERPApplication(db_path="erp_demo.db")
    _seed_data(app.database)
    app.run()


if __name__ == "__main__":
    main()
