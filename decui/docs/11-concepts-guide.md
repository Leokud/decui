# Руководство по концепциям DECUI: Для Python-разработчиков

> **DECUI** — это декларативный фреймворк для создания настольных учетных и ERP-систем на Python.  
> Если вы никогда не работали с 1С, SAP или другими крупными ERP — этот документ простыми словами объяснит архитектурные принципы, терминологию и паттерны, заложенные в DECUI.

---

## 1. Введение: Зачем нужен специализированный учетный фреймворк?

Обычные CRUD-приложения обычно строятся по принципу: *«Таблица в БД → Форма ввода → Список записей»*.
Однако в реальном бизнесе (торговля, производство, логистика, услуги) простого CRUD недостаточно. Возникают типичные задачи:
1. **Неизменяемость проведенных операций:** Нельзя просто «исправить задним числом» отгрузку товара или проводку денег — это нарушит баланс и историю.
2. **Мгновенный расчет остатков:** Узнавать остаток товара на складе суммированием миллионов строк чеков за 5 лет — слишком медленно.
3. **Строгая сквозная нумерация:** Чеки, накладные и счета должны иметь последовательные номера в разрезе префиксов и периодов (например, `INV-2026-000001`).
4. **Связка «Шапка + Строки»:** Счета и заказы всегда состоят из общих реквизитов (контрагент, склад, дата) и строк спецификации (товар, количество, цена, сумма).

В корпоративных системах (1С, SAP ERP, Microsoft Dynamics, Odoo) для этого выработаны проверенные временем паттерны. **DECUI переносит эти паттерны в элегантный Python-код.**

---

## 2. Сравнение терминологии

DECUI поддерживает как общепринятую инженерную терминологию (Master Data, Transaction, Line Items, Ledger), так и привычные термины 1С (Справочник, Документ, Табличная часть, Регистр).

| Концепция DECUI / ERP | Термин в 1С | Классы и декораторы в DECUI | Назначение |
|---|---|---|---|
| **Master Data / Lookup** | Справочник | `Catalog`, `MasterData`, `@catalog`, `@master_data` | Условно-постоянные сущности (товары, клиенты, сотрудники, склады) |
| **Transaction / Document** | Документ | `Document`, `Transaction`, `@document`, `@transaction` | Фиксация хозяйственного события во времени (счет, оплата, приказ) |
| **Line Items / Child Table** | Табличная часть | `@line_items`, `@tab_part`, `LineItems`, `TablePart` | Строки спецификации внутри документа (товары в чеке, услуги в акте) |
| **Form Action** | Команда | `@action`, `@command`, `Action`, `Command` | Кнопка на форме с привязанной бизнес-логикой (Рассчитать, Провести) |
| **Balance Ledger** | Регистр накопления | `@ledger`, `@balance_ledger`, `@accumulation` | Журнал изменений количественных/денежных остатков (склад, взаиморасчеты) |
| **Value History / Store** | Регистр сведений | `@history_register`, `@value_history`, `@information` | Хранилище периодических данных (курсы валют на дату, оклады, цены) |
| **Ledger Entry / Movement** | Движение | `LedgerEntry`, `Movement` | Единичная запись изменения баланса (приход `+`, расход `-`) |
| **Posting / Approval** | Проведение | `doc.post()`, `doc.submit()`, `doc.is_posted` | Перевод черновика документа в статус учтённого с фиксацией движений |
| **Dimension** | Измерение | `Dimension`, `RefDimension` | Аналитический разрез учета (в разрезе какого Склада, Товара, Валюты) |
| **Metric / Resource** | Ресурс | `Metric`, `Resource` | Числовой показатель, который суммируется (Количество, Сумма) |
| **Subsystem / Modules** | Подсистема | `__subsystem__`, `__section__`, `__category__` | Дерево навигации в левой панели приложения |
| **Sequence Numbering** | Автонумерация | `__numbering__`, `NumberingService` | Генерация последовательных номеров по шаблону |
| **Report / Query** | Отчёт | `Report`, `QueryReport`, `@report` | Параметрический отчёт с таблицей, агрегацией и графиками |

---

## 3. Ключевые архитектурные концепции

### 3.1. Нормативно-справочная информация (Master Data / Catalog)
Это списки объектов, которые многократно используются в операциях.

```python
from decui import master_data, MasterData, String, Decimal as D

@master_data
class Product(MasterData):
    __numbering__ = {"prefix": "PRD", "pattern": "{prefix}-{seq:06d}"}
    __verbose_name__ = "Товары"
    __section__ = "Склад/Справочники"

    code  = String("Артикул", read_only=True)
    name  = String("Наименование", required=True)
    price = D("Базовая цена", precision=10, scale=2, min_value=0)
```
- Фреймворк создаст таблицу в SQLite.
- Автоматически сгенерирует окно списка с поиском, пагинацией и сортировкой.
- Сгенерирует форму создания/редактирования с проверкой обязательных полей.

---

### 3.2. Документы и строки (Transactions & Line Items)
Документ отражает факт события в определенный момент времени (`date`).
Каждый `Document` по умолчанию содержит:
- `date` — временная метка события.
- `number` — строковый уникальный номер.
- `posted` — булевый флаг (`True`, если документ проведен / согласован).

```python
from decui import transaction, Transaction, String, Ref, Decimal as D, Integer, line_items, action

@transaction
class Invoice(Transaction):
    __numbering__ = {"prefix": "INV", "pattern": "{prefix}-{year}-{seq:06d}"}
    __verbose_name__ = "Счета покупателям"
    __section__ = "Продажи"

    number   = String("Номер", read_only=True)
    customer = String("Покупатель", required=True)
    total    = D("Итого", precision=10, scale=2, read_only=True, default=0)

    # Строки спецификации (Line Items)
    @line_items
    class Items:
        product  = Ref(Product, "Товар", representation_field="name")
        quantity = Integer("Кол-во", default=1, min_value=1)
        price    = D("Цена", precision=10, scale=2)
        amount   = D("Сумма", precision=10, scale=2, read_only=True)

    # Бизнес-действие на форме
    @action("Рассчитать")
    def calculate(self):
        total = 0
        for row in self._table_data.get("Items", []):
            qty = row.get("quantity", 1)
            prc = row.get("price", 0)
            row["amount"] = qty * prc
            total += row["amount"]
        self.total = total
```

---

### 3.3. Проведение (Posting) и Регистры (Ledgers)
Самая важная концепция учета: **документ сам по себе не хранит остатки — он порождает записи в регистрах (движения)**.

- **Черновик:** менеджер заполняет счет, редактирует цены. Документ сохранен, но остатки на складе не изменились.
- **Проведение (Post):** менеджер нажимает «Провести». Документ помечается как `posted = True`, а его метод `get_ledger_entries()` создает записи движения.
- **Отмена проведения (Unpost):** если документ отменен, все его движения удаляются из регистров.

#### Описание регистра остатков (Balance Ledger):
```python
from decui import ledger, Ledger, Dimension, Metric

@ledger
class WarehouseStock:
    warehouse = Dimension("Склад")
    product   = Dimension("Товар")
    quantity  = Metric("Количество")
```

#### Запись движений при проведении:
```python
from decui import LedgerEntry

@transaction
class GoodsReceipt(Transaction):
    ...
    def get_ledger_entries(self):
        # При проведении документа на склад поступает товар (is_income=True)
        return [
            LedgerEntry(
                register=WarehouseStock,
                dimensions={"warehouse": self.warehouse, "product": row["product"]},
                resources={"quantity": row["quantity"]},
                is_income=True,
            )
            for row in self._table_data.get("Items", [])
        ]
```

#### Запрос актуальных остатков:
```python
from decui import get_ledger_balance

# Мгновенный расчет остатков по всем товарам на конкретном складе
balances = get_ledger_balance(db, WarehouseStock, warehouse=1, group_by=["product"])
for b in balances:
    print(f"Товар #{b['product']}: остаток {b['quantity']} шт.")
```

---

### 3.4. Периодические параметры (Value History / Information Registers)
Используются для величин, меняющихся во времени: курсы валют, ставки налогов, прайс-листы.

```python
from decui import history_register, Dimension, Resource, set_record, get_latest

@history_register
class ExchangeRate:
    currency = Dimension("Валюта")
    rate     = Resource("Курс")

# Запись нового курса
set_record(db, ExchangeRate, dimensions={"currency": "USD"}, resources={"rate": 92.50})

# Получение самого свежего курса (срез последних)
latest_usd = get_latest(db, ExchangeRate, currency="USD")
print(f"Текущий курс USD: {latest_usd['rate']}")
```

---

### 3.5. Отчёты (Reports & Dashboards)
Отчеты строятся декларативно: параметры формы фильтрации + список колонок с агрегацией (`sum`, `avg`, `count`, `min`, `max`).

```python
from decui import report, QueryReport, Column, DateParam, RefParam

@report
class SalesSummary(QueryReport):
    title = "Сводка продаж за период"
    columns = [
        Column("Дата", "date", width=120),
        Column("Товар", "product", width=200),
        Column("Кол-во", "qty", width=90, align="right", aggregate="sum"),
        Column("Сумма", "total", width=130, align="right", aggregate="sum", suffix=" руб."),
    ]

    date_from = DateParam("Период с", required=True)
    date_to   = DateParam("Период по", required=True)

    def execute(self, db, date_from, date_to, **params):
        # Бизнес-логика отчета
        return [
            {"date": "2026-05-01", "product": "Ноутбук", "qty": 5, "total": 350000.00},
            {"date": "2026-05-02", "product": "Мышь", "qty": 10, "total": 15000.00},
        ]
```

---

## 4. Сборка готового приложения в .exe

Для распространения приложения пользователям не нужен установленный Python:
```bash
python build_exe.py
# Результат: dist/ERP (или dist/ERP.exe на Windows)
```
PyInstaller упаковывает Qt, SQLite и логику в единый автономный файл.
