# Полный гайд по фреймворку DECUI

**DECUI** — декларативный (metadata-driven) фреймворк на Python 3.12+ для создания настольных учётных и ERP-приложений на базе **PySide6**, **SQLAlchemy 2.0+** и **SQLite**.

Вы описываете **предметную область и бизнес-правила в Python-классах**, а DECUI автоматически строит:
- схему SQLite и неразрушающую миграцию при добавлении полей и таблиц;
- дерево навигации, списки с поиском/сортировкой/отборами и карточки редактирования;
- табличные части с авторасчётом строк;
- регистры остатков (балансы) и регистры истории значений;
- маршруты согласования, роли/учётные записи и печатные формы;
- параметрические отчёты с итогами, группировками, диаграммами и выгрузкой в CSV;
- автономный исполняемый файл через PyInstaller.

---

## Оглавление

1. [Как устроен DECUI за 2 минуты](#1-как-устроен-decui-за-2-минуты)
2. [Два способа старта: код и DECUI Studio](#2-два-способа-старта-код-и-decui-studio)
3. [Модели данных: справочники и поля](#3-модели-данных-справочники-и-поля)
4. [Документы, табличные части и бизнес-логика](#4-документы-табличные-части-и-бизнес-логика)
5. [Регистры: остатки и периодические цены](#5-регистры-остатки-и-периодические-цены)
6. [Настройка интерфейса без Qt-кода](#6-настройка-интерфейса-без-qt-кода)
7. [Согласование, права доступа и печатные формы](#7-согласование-права-доступа-и-печатные-формы)
8. [Отчёты, группировки и дашборды](#8-отчёты-группировки-и-дашборды)
9. [Напоминания, тестирование, миграции и сборка](#9-напоминания-тестирование-миграции-и-сборка)
10. [Сквозной рабочий пример](#10-сквозной-рабочий-пример)
11. [Типичные ошибки и лучшие практики](#11-типичные-ошибки-и-лучшие-практики)

---

## 1. Как устроен DECUI за 2 минуты

```text
  Классы Python (@master_data, @transaction, @ledger, @report)
          │
          ▼  EntityMeta / RegisterMeta / ReportMeta
     Метаданные
          │
          ├──▶ Database (SQLite + изолированный declarative_base на экземпляр)
          │         └── CRUD, транзакции, проведение (post/unpost), регистры, авто-миграция
          │
          └──▶ ERPApplication (PySide6)
                    ├── NavigationPanel  (дерево разделов по __section__)
                    ├── ListView         (таблица, поиск Ctrl+F, отборы, CSV)
                    ├── AutoForm         (вкладки, группы, Ref +, табличные части)
                    └── ReportView       (параметры, итоги, группировки, CSV)
```

### Ключевые принципы
- **Единый источник правды:** описание класса определяет сразу таблицу БД, колонки списка, форму карточки и правила валидации.
- **Чистый Python и IDE-поддержка:** поддерживаются аннотации типов и `@dataclass_transform` — IDE подсказывает аргументы конструктора и типы полей.
- **Два стиля синтаксиса (полная совместимость):**
  - **Универсальный ERP-стиль:** `@master_data` / `MasterData`, `@transaction` / `Transaction`, `@line_items`, `@action`, `@ledger` / `Ledger`, `LedgerEntry`, `@history_register`, `@report` / `QueryReport`.
  - **1С-стиль:** `@catalog` / `Catalog`, `@document` / `Document`, `@tab_part`, `@command`, `@accumulation`, `Movement`, `@information`, `Report`.
- **Безопасные границы:** ошибки ограничений SQLite/SQLAlchemy (`UNIQUE`, `FOREIGN KEY`, `NOT NULL`, блокировка БД) автоматически переводятся в понятные русские сообщения без сырого SQL в интерфейсе.

---

## 2. Два способа старта: код и DECUI Studio

### Путь A — из шаблона CLI

```bash
uv sync --locked --inexact
uv run --locked decui new my_erp --template orders
cd my_erp
uv run --locked decui check
uv run --locked decui test -q
uv run --locked decui run
```

Основные команды CLI (`decui` или `python -m decui.cli`):
- `decui new <dir> --template basic|orders` — создаёт `app.py`, `test_app.py`, `README.md`, `.gitignore`.
- `decui check [file] [--backup copy.sqlite3]` — проверяет импорт, ссылки, инициализацию схемы или миграцию на копии базы.
- `decui test [pytest_args]` — запускает `pytest` (на Linux без дисплея автоматически включает `QT_QPA_PLATFORM=offscreen`).
- `decui run [file]` — запускает приложение (`app.py` или `main.py` по умолчанию).
- `decui build [file]` — собирает standalone-исполняемый файл через PyInstaller.

### Путь B — визуально в DECUI Studio

```bash
uv run --locked decui studio
```

В Studio можно проектировать справочники, документы (с шаблонами нумерации и `__sequence__`), табличные части, визуальные расчёты и проверки (`before_save` / `on_validate`), компоновку формы (вкладки, группы, скрытые поля, живой предпросмотр всей карточки), расширенные параметры полей (`Decimal` с валютой и точностью, множественный `Choice`, `auto_fill`, `representation_field`, `choice_filter`), регистры (с `representation_field`), движения (с выбором даты записи), отчёты (по строкам табличных частей, срез последних, фильтры, `from_record`, диаграмма `BarChart`, оформление колонок/строк и живой предпросмотр), маршруты согласования, права доступа, печатные формы, напоминания и начальные данные (включая захват из SQLite) — а также импортировать готовый `app.py`, видеть ER-схему связей с экспортом в PNG, проверять SQLite в инспекторе (CSV + SQL чтения), отменять/повторять правки структуры и собирать проект.

---

## 3. Модели данных: справочники и поля

Справочник (`MasterData` / `Catalog`) хранит условно-постоянные записи: товары, клиентов, склады, контрагентов.

```python
from decimal import Decimal
from decui import (
    Boolean, Choice, Decimal as D, Integer, MasterData, Ref, String, master_data
)

@master_data
class Warehouse(MasterData):
    __verbose_name__ = "Склады"
    __section__ = "Склад"

    name = String("Название", required=True, unique=True)
    is_main = Boolean("Основной", default=False)


@master_data
class Product(MasterData):
    __verbose_name__ = "Товары"
    __section__ = "Склад"
    __numbering__ = {"prefix": "PRD", "field": "code", "pattern": "{prefix}-{seq:05d}"}

    code = String("Код", read_only=True)
    name = String("Наименование", required=True)
    category = Choice(
        "Категория",
        choices=[("RAW", "Сырьё"), ("GOODS", "Товар"), ("SERVICE", "Услуга")],
        multi=False,
        default="GOODS",
    )
    price = D("Базовая цена", precision=12, scale=2, min_value=0, default=Decimal("0.00"))
    notes = String("Примечание", multiline=True)
```

### Основные типы полей (`decui/fields.py`)

| Дескриптор | Тип Python | Виджет в карточке | Частые параметры |
|---|---|---|---|
| `String` | `str` | `QLineEdit` / `QPlainTextEdit` | `max_length`, `multiline=True`, `required`, `unique`, `indexed` |
| `Integer` | `int` | `QSpinBox` | `min_value`, `max_value`, `default`, `required` |
| `Decimal` (`D`) | `Decimal` | `QDoubleSpinBox` | `precision`, `scale`, `min_value`, `max_value`, `currency` |
| `Boolean` | `bool` | `QCheckBox` | `default` |
| `Date` | `datetime.date` | `QDateEdit` | `default=datetime.date.today`, `required` |
| `DateTime` | `datetime.datetime` | `QDateTimeEdit` | `default=datetime.datetime.now`, `required` |
| `Time` | `datetime.time` | `QTimeEdit` | `default`, `required` |
| `Choice` | `str` | `QGroupBox` с чекбоксами или `QComboBox` | `choices=[("CODE", "Подпись")]`, `multi=True\|False`, `separator="\|"` |
| `Ref(Target)` | `int` (FK) | `QComboBox` с поиском и кнопкой `+` | `representation_field`, `auto_fill`, `related_list`, `choice_filter`, `on_delete` |

> **Подсказка:** `display_name` (первый аргумент) можно опустить — тогда подпись сгенерируется автоматически из имени атрибута (`unit_price = D(scale=2)` → `"Unit price"`). Поддерживается также стиль с аннотациями типов: `name: str = String("Название", required=True)` или просто `is_active: bool = True`.

### Возможности `Ref` для связанных данных

```python
@master_data
class Address(MasterData):
    __verbose_name__ = "Адреса"
    __section__ = "Продажи"

    owner = Ref(Customer, "Покупатель", required=True, related_list="Адреса покупателя")
    name = String("Адрес", required=True)
```

- `related_list="Адреса покупателя"` — в карточке сохранённого `Customer` появляется кнопка/вкладка со списком его адресов; при создании нового адреса оттуда владелец подставится автоматически.
- `choice_filter={"owner": "customer"}` — в документе `Order` выбор поля `address` автоматически фильтруется по выбранному `customer`, а кнопка `+` создаёт адрес сразу для этого покупателя.
- `auto_fill="phone"` — при выборе контрагента копирует значение одноимённого поля `phone` в текущую карточку.
- `on_delete="RESTRICT"` (по умолчанию) запрещает удаление используемой записи; `"CASCADE"` удаляет зависимые записи (для защищённых правами сущностей требуется удаление через API DECUI).

---

## 4. Документы, табличные части и бизнес-логика

Документ (`Transaction` / `Document`) фиксирует событие во времени и автоматически наследует три поля:
- `date` (`DateTimeField`) — дата и время документа (по умолчанию текущее время);
- `number` (`StringField`) — уникальный номер (автозаполняется через `__numbering__`);
- `posted` (`BooleanField`, `read_only=True`) — признак проведения (`doc.is_posted`).

```python
from decimal import Decimal, ROUND_HALF_UP
from decui import Integer, Ref, Transaction, action, line_items, transaction

@transaction
class SalesOrder(Transaction):
    __verbose_name__ = "Заказы клиентов"
    __section__ = "Продажи"
    __numbering__ = {"prefix": "SO", "field": "number", "pattern": "{prefix}-{year}-{seq:05d}"}
    __sequence__ = True  # запретить проведение задним числом после более поздних проведённых документов

    customer = Ref(Customer, "Клиент", required=True)
    warehouse = Ref(Warehouse, "Склад", required=True)
    total = D("Итого", scale=2, read_only=True, default=Decimal("0.00"))

    @line_items
    class Items:
        product = Ref(Product, "Товар", required=True, auto_fill="price")
        quantity = Integer("Количество", min_value=1, default=1)
        price = D("Цена", scale=2, min_value=0, default=Decimal("0.00"))
        amount = D("Сумма", scale=2, read_only=True, default=Decimal("0.00"))

    def on_validate(self) -> list[str]:
        return [] if self.Items else ["Добавьте хотя бы одну строку заказа."]

    def before_save(self, db) -> None:
        for row in self.Items:
            qty = Decimal(str(row.get("quantity") or 0))
            prc = Decimal(str(row.get("price") or 0))
            row["amount"] = (qty * prc).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self.total = self.Items.total("amount")

    @action("Провести")
    def post_order(self) -> None:
        self.post()
```

### Жизненный цикл сохранения и проведения

| Этап | Когда срабатывает | Что делать в хуке |
|---|---|---|
| `on_validate(self)` | В форме всегда; в Python при `db.save(doc, validate=True)` и `db.post(doc)` | Возвращать список строк ошибок `list[str]` (без побочных эффектов) |
| `before_save(self, db)` | При каждом сохранении (и в форме, и в Python) | Пересчёт итогов, заполнение цен, бизнес-проверки (`ValueError` откатит запись) |
| `after_save(self, db)` | После записи шапки и строк в транзакции | Дополнительные действия после фиксации |
| `before_delete(self, db)` / `after_delete(self, db)` | При удалении через `db.delete(obj)` | Проверки перед удалением или очистка ресурсов |
| `db.post(doc)` | Проведение из кода или кнопки проведения | Проверяет права, последовательность, запускает команду, сохраняет и переписывает движения регистров |
| `db.unpost(doc)` | Отмена проведения | Снимает `posted` и удаляет все движения документа |

> **Важно про табличные части:** `db.list(SalesOrder)` и `db.filter(SalesOrder, ...)` по умолчанию загружают только шапки. Чтобы работать со строками `doc.Items`, загружайте документ через `db.get(SalesOrder, id)` или передавайте `include_table_parts=True` в `db.list`/`db.filter`.

---

## 5. Регистры: остатки и периодические цены

Документы не хранят текущие остатки — они формируют движения в регистрах.

### Регистр накопления (`@ledger` / `@accumulation`)

```python
from decui import Ledger, LedgerEntry, Metric, RefDimension, get_ledger_balance, ledger

@ledger
class StockLedger(Ledger):
    warehouse = RefDimension(Warehouse, "Склад")
    product = RefDimension(Product, "Товар")
    qty = Metric("Количество")
```

В документе опишите метод `get_ledger_entries(self)` (или `get_movements(self)`):

```python
def get_ledger_entries(self):
    return [
        LedgerEntry(
            register=StockLedger,
            dimensions={"warehouse": self.warehouse, "product": row["product"]},
            resources={"qty": row["quantity"]},
            is_income=False,  # True = приход (+), False = расход (-)
        )
        for row in self.Items
    ]
```

Чтение остатков:

```python
balances = get_ledger_balance(db, StockLedger, warehouse=wh.id, group_by=["product"])
```

При перепроведении `db.post(doc)`, отмене `db.unpost(doc)` или удалении `db.delete(doc)` старые движения документа в регистрах автоматически очищаются в той же транзакции.

### Регистр сведений (`@history_register` / `@information`)

Хранит значения, зависящие от даты (прайс-листы, курсы валют, тарифы):

```python
from decui import RefDimension, Resource, get_latest, get_records, history_register, set_record

@history_register
class SupplierPrice:
    product = RefDimension(Product, "Товар")
    supplier = RefDimension(Supplier, "Поставщик")
    price = Resource("Цена")

# Запись цены на дату
set_record(
    db,
    SupplierPrice,
    dimensions={"product": product.id, "supplier": supplier.id},
    resources={"price": Decimal("120.00")},
    period=date(2026, 11, 1),
)

# Получение актуальной цены на дату документа
rec = get_latest(db, SupplierPrice, on_date=order.date, product=product.id, supplier=supplier.id)
current_price = rec["price"] if rec is not None else None
```

---

## 6. Настройка интерфейса без Qt-кода

Поведением списков и карточек управляют декларативные атрибуты класса:

| Атрибут | Пример | Что меняет в UI |
|---|---|---|
| `__section__` (`__subsystem__`) | `"Продажи/Заказы"` | Вложенность в левом дереве навигации |
| `__verbose_name__` | `"Заказы клиентов"` | Заголовок пункта меню, вкладки списка и окна |
| `__list_columns__` | `("number", "date", "customer", "total")` | Порядок и состав колонок в `ListView` |
| `__list_order__` | `"-date"` | Начальная сортировка списка (`-` по убыванию) |
| `__form_groups__` | `{"customer": "Основное", "total": "Итоги"}` | Объединение полей в `QGroupBox` |
| `__form_tabs__` | `{"notes": "Дополнительно"}` | Перенос полей (вместе с их группой) на вкладки |
| `__form_table_tabs__` | `{"Items": "Товары"}` | Перенос табличной части на отдельную вкладку |
| `__form_visible_when__` | `{"delivery_address": ("need_delivery", True)}` | Динамическое скрытие/показ полей в карточке |
| `__required_when_visible__` | `("delivery_address",)` | Обязательность заполнения, только когда поле показано |
| `__defaults__` | `{"warehouse": lambda db: ...}` | Вычисляемые значения по умолчанию при создании записи |

### Создание документа на основании (`create_from`)

Чтобы из карточки заказа быстро создавать накладную или оплату, добавьте в дочерний документ ссылку с `create_from`:

```python
invoice_source = Ref(
    SalesOrder,
    "Заказ-основание",
    create_from="Создать накладную",
    basis_fields={"warehouse": "warehouse", "customer": "customer"},
    basis_tables={"Items": {"source": "Items", "columns": {"product": "product", "quantity": "quantity", "price": "price"}}},
)
```

В сохранённой карточке `SalesOrder` появится меню «Создать на основании → Создать накладную», которое откроет новую форму с уже перенесёнными реквизитами и строками.

---

## 7. Согласование, права доступа и печатные формы

### Маршруты согласования (`Workflow`)

```python
from decui import Transition, Workflow

__workflow__ = Workflow(
    field="status",
    initial="Черновик",
    states=("Черновик", "На согласовании", "Утверждён"),
    transitions=(
        Transition("На согласование", "Черновик", "На согласовании", roles=("manager", "admin")),
        Transition("Утвердить", "На согласовании", "Утверждён", roles=("admin",), assigned_only=True),
    ),
    post_states=("Утверждён",),  # проводить можно только утверждённый документ
)
```

- В карточке сохранённого документа отображаются доступные текущей роли кнопки переходов и кнопка «История».
- Программно: `db.transition(doc, "На согласование")` и `db.workflow_history(doc)`.

### Локальные учётные записи и права

Передайте `local_accounts=True` в `ERPApplication(...)`, чтобы включить экран входа и управление пользователями через меню «Учётные записи». На классе сущности можно задать:
- `__owner_field__ = "author"` — доступ только владельцу записи и `admin` (плюс назначенному согласующему в рамках перехода);
- `__permissions__ = {"read": ("manager", "admin"), "create": ("manager", "admin"), "update": ("admin",), "delete": ("admin",), "post": ("admin",)}`;
- `__field_read_roles__` / `__field_write_roles__` — ограничение видимости и редактирования отдельных реквизитов и колонок (`"Items.cost"`);
- `__command_roles__` — ограничение кнопок `@action`.

### Печатные формы (`PrintForm`)

```python
from decui import PrintForm, PrintTable

__print_form__ = PrintForm(
    title="Заказ клиента",
    fields=("number", "date", "customer", "total"),
    tables=(PrintTable(part="Items", columns=("product", "quantity", "price", "amount")),),
)
```

В карточке сохранённой записи появляется кнопка «Печатная форма» с предпросмотром HTML и экспортом в PDF. Печать берёт данные из базы и автоматически уважает `__field_read_roles__`.

---

## 8. Отчёты, группировки и дашборды

```python
from datetime import date
from decui import Column, DateParam, QueryReport, RefParam, report

@report
class SalesReport(QueryReport):
    title = "Продажи по товарам"
    __section__ = "Отчёты"
    auto_run = True
    group_by = ("customer",)

    customer = RefParam("Клиент", Customer, from_record=True)
    date_from = DateParam("С", default=lambda: date.today().replace(day=1))
    date_to = DateParam("По", default=date.today)

    columns = [
        Column("Клиент", "customer", width=180),
        Column("Заказ", "number", width=120),
        Column("Товар", "product", width=200),
        Column("Кол-во", "qty", width=90, align="right", aggregate="sum"),
        Column("Сумма", "amount", width=130, align="right", aggregate="sum", format="{:,.2f}", suffix=" ₽"),
    ]

    def execute(self, db, *, date_from=None, date_to=None, customer=None, **_kw):
        filters = {"customer": customer} if customer is not None else {}
        orders = db.filter(
            SalesOrder,
            posted=True,
            date__date_range=(date_from, date_to),
            include_table_parts=True,
            **filters,
        )
        customers = {c.id: c.name for c in db.list(Customer)}
        products = {p.id: p.name for p in db.list(Product)}
        rows = []
        for order in orders:
            for item in order.Items:
                rows.append({
                    "customer": customers.get(order.customer, "—"),
                    "number": order.number or "",
                    "product": products.get(item.get("product"), "—"),
                    "qty": item.get("quantity", 0),
                    "amount": item.get("amount", 0),
                    "_entity_class": SalesOrder,
                    "_entity_id": order.id,
                })
        return rows
```

- `from_record=True` добавляет отчёт в меню «Отчёты» карточки `Customer` с подстановкой выбранного клиента.
- `_entity_class` и `_entity_id` открывают карточку документа по двойному клику на строке отчёта.
- Возвращайте в `execute()` числа (`int`/`Decimal`), а оформление задавайте в `Column(..., format=..., prefix=..., suffix=...)` — тогда корректно работают сортировка, агрегаты и экспорт в CSV.
- Для отчётов напрямую по регистрам используйте `register_report_rows(db, StockLedger, ["warehouse", "product", "qty"])`.
- Управляющие ключи оформления строк: `_style="completed"` (зачёркнутый серый), `_style="current"` (полужирный), `_color="#d32f2f"`, `_bold=True`.

---

## 9. Напоминания, тестирование, миграции и сборка

### Напоминания о сроках

```python
app = ERPApplication(title="Мой бизнес", app_id="my-erp", start=SalesOrder, first_run=seed_demo)
app.enable_reminders([Task], date_field="deadline", done_field="is_completed", notify_before_minutes=60)
```

### Быстрое тестирование без GUI и с GUI

```python
from decimal import Decimal
from decui import Database

def test_order_posts_and_writes_stock():
    db = Database.memory()
    wh = db.save(Warehouse(name="Основной"))
    prod = db.save(Product(name="Кофе", price=Decimal("500.00")))
    cust = db.save(Customer(name="ООО Ромашка"))

    order = SalesOrder(customer=cust.id, warehouse=wh.id)
    order.Items.add(product=prod.id, quantity=2, price=Decimal("500.00"))
    db.post(order)

    saved = db.get(SalesOrder, order.id)
    assert saved is not None and saved.is_posted
    assert saved.total == Decimal("1000.00")
```

### Эволюция схемы без потери данных

1. Добавление новых полей и таблиц выполняется автоматически при запуске `Database` (`ALTER TABLE ADD COLUMN` + `CREATE TABLE`).
2. Переименование колонки или табличной части задавайте явно:

```python
_DECUI_COLUMN_RENAMES = {"Product": {"old_name": "name"}}
_DECUI_TABLE_PART_RENAMES = {"SalesOrder": {"OldItems": "Items"}}

app = ERPApplication(
    app_id="my-erp",
    column_renames=_DECUI_COLUMN_RENAMES,
    table_part_renames=_DECUI_TABLE_PART_RENAMES,
)
```

3. Перед обновлением прогоните проверку на резервной копии:

```bash
decui check app.py --backup /path/to/backup.sqlite3
```

### Сборка дистрибутива

```bash
decui build app.py
```

---

## 10. Сквозной рабочий пример

Готовые рабочие примеры в репозитории, которые можно изучить и запустить сразу:
- [examples/simple.py](../../examples/simple.py) — базовый справочник, документ с табличной частью и отчёт;
- [examples/features/purchase_orders.py](../../examples/features/purchase_orders.py) — поставщики, регистр сведений `Prices` на дату заказа, `before_save`, отчёт из карточки;
- [examples/features/dependent_refs.py](../../examples/features/dependent_refs.py) — `related_list` и зависимый выбор `choice_filter`;
- [examples/vunderkind.py](../../examples/vunderkind.py) — расписание, выбор дней, проверка пересечений времени;
- [examples/future_goals.py](../../examples/future_goals.py) — задачи, дедлайны, напоминания, прогресс и стилизация отчётов.

---

## 11. Типичные ошибки и лучшие практики

1. **Точка входа под `if __name__ == "__main__":`** — не запускайте `ERPApplication(...).run()` на уровне модуля, иначе `decui check` и тесты откроют окно при импорте.
2. **Не теряйте табличные части при сохранении** — `db.list()` и `db.filter()` по умолчанию не грузят строки; используйте `db.get(Class, id)` или `include_table_parts=True`.
3. **Используйте `Decimal` для денег и количеств** — избегайте `float`, чтобы не ловить погрешности округления и типы SQLAlchemy `Numeric`.
4. **Не делайте N+1 запросов в `Report.execute()`** — загружайте справочники одним словарём `{x.id: x.name for x in db.list(Target)}`, а документы с строками через `include_table_parts=True`.
5. **Возвращайте числа в отчётах, а форматируйте в `Column`** — строки вида `"1 200 руб."` из `execute()` ломают сортировку и агрегацию.
6. **Храните бизнес-правила в `on_validate` / `before_save`** — тогда они работают одинаково и из формы UI, и при прямых вызовах `db.save` / `db.post` в тестах и скриптах.
7. **Используйте `app_id` вместо относительных путей к `.db`** — в собранном `.exe` база будет храниться в постоянном пользовательском каталоге, а не рядом со временной распаковкой.

---

## Подробная документация по темам

| Документ | Тема |
|---|---|
| [01-quickstart.md](01-quickstart.md) | Быстрый старт и шаблонный проект |
| [02-entities.md](02-entities.md) | Сущности, типы полей, `choice_filter`, PEP 681 |
| [03-table-parts.md](03-table-parts.md) | Табличные части и `LineItemsCollection` |
| [04-commands.md](04-commands.md) | Кнопки `@action` / `@command` и валидация |
| [05-reports.md](05-reports.md) | Отчёты за период и открытие из карточки |
| [06-numbering.md](06-numbering.md) | Шаблоны автонумерации |
| [07-building.md](07-building.md) | Сборка `.exe` и репетиция миграции `--backup` |
| [08-api.md](08-api.md) | Полный справочник API |
| [09-registers.md](09-registers.md) | Регистры накопления и сведений |
| [09-advanced.md](09-advanced.md) | Согласование `Workflow`, права, печать `PrintForm`, миграции |
| [10-reports.md](10-reports.md) | Агрегации, группировки и диаграммы `BarChart` |
| [11-concepts-guide.md](11-concepts-guide.md) | Архитектура учёта простыми словами |
| [12-dx-guide.md](12-dx-guide.md) | CLI, авто-имена, Active Record, тесты |
