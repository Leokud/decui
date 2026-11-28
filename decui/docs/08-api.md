# Полный справочник API DECUI

Все ключевые классы, декораторы и функции экспортируются напрямую из пакета `decui`:

```python
from decui import (
    # Приложение и база данных
    ERPApplication, Database,

    # Декораторы сущностей
    catalog, master_data, lookup,
    document, transaction,
    tab_part, line_items, child_table,
    command, action,

    # Декораторы регистров
    accumulation, ledger, balance_ledger,
    information, history_register, value_history,

    # Базовые классы
    Entity, Catalog, MasterData, Lookup,
    Document, Transaction, TablePart, LineItems,
    Register, Ledger, AccumulationRegister, InformationRegister, HistoryRegister, ValueHistory,
    Movement, LedgerEntry, Dimension, RefDimension, Resource, Metric,

    # Дескрипторы полей
    String, Integer, Decimal, Boolean, Date, DateTime, Time, Choice, Ref, Field,

    # Работа с регистрами и движениями
    get_balance, get_ledger_balance, get_records, get_latest, get_slice_last, set_record,
    write_movements, clear_movements,

    # Отчёты и параметры
    report, Report, QueryReport, Column, register_report_rows,
    StringParam, IntegerParam, NumberParam, DecimalParam, BooleanParam, DateParam, ChoiceParam, RefParam,

    # Согласование и печатные формы
    Workflow, Transition, WorkflowEvent, PrintForm, PrintTable,

    # Последовательности и права
    SequenceViolationError, check_sequence, define_role, has_permission, current_role, set_role,

    # Напоминания и уведомления
    ReminderService, ReminderDialog, celebrate_goal_completed,
)
```

---

## 1. Запуск приложения (`ERPApplication`)

```python
app = ERPApplication(
    title="Моя ERP Система",       # Заголовок главного окна
    db_path="erp.db",             # Путь к SQLite (или ":memory:")
    app_id=None,                  # Идентификатор для локального каталога данных (DECUI/<app_id>)
    db_filename="data.sqlite3",   # Имя файла БД при использовании app_id
    start=None,                   # Сущность или отчёт, открываемый сразу после запуска
    first_run=None,               # Callable(db) — однократное начальное заполнение пустой БД
    local_accounts=False,         # Включить локальный вход и учётные записи пользователей
    column_renames=None,          # Словарь переименований колонок при авто-миграции
    table_part_renames=None,      # Словарь переименований табличных частей при авто-миграции
    window_size=(1200, 800),      # Размер главного окна
    argv=None,                    # Аргументы командной строки sys.argv
)

# Подключение сервиса напоминаний о дедлайнах
app.enable_reminders([Task], date_field="deadline", notify_before_minutes=60)

app.run()                         # Запуск цикла событий Qt
```

- `app.main_window`: главное окно `QMainWindow`.
- `app.database`: экземпляр `Database`.

---

## 2. База данных (`Database`)

```python
db = Database("erp.db")
# Или база данных в памяти (для тестов):
db = Database.memory()

db.save(entity, validate=False) # INSERT / UPDATE + строки спецификаций + хуки before_save/after_save
db.get(Class, id)            # Получить объект по первичному ключу со всеми строками спецификаций
db.list(Class, include_table_parts=False)  # Получить список доступных записей
db.filter(Class, order_by=None, limit=None, include_table_parts=False, **kwargs)
db.delete(entity)            # Удалить объект, строки спецификаций и движения регистров документа
db.delete_by_id(Class, id)   # Удалить доступный объект по ID
db.seed([e1, e2, ...])       # Пакетное сохранение списка объектов

db.post(doc)                 # Проверить права/последовательность, выполнить проведение, сохранить и обновить движения
db.unpost(doc)               # Снять проведение и очистить движения документа
db.execute_command(entity, "method_name")  # Выполнить @action/@command и сохранить в транзакции

db.transition(entity, "Согласовать")       # Выполнить переход маршрута Workflow и записать событие
db.workflow_history(entity)                # Получить историю WorkflowEvent по записи
db.backup("/path/to/backup.sqlite3")       # Сохранить согласованную резервную копию SQLite

db.allows(Class, "create")                 # Проверить права текущей роли на операцию
db.can_read_field(Class, "price")          # Проверить право чтения поля/колонки
db.can_edit_field(Class, "price")          # Проверить право изменения поля/колонки
```

---

При прямом вызове `db.save(entity, validate=True)` флаг включает проверку полей и
`on_validate()`. `before_save(db)` вызывается при сохранении независимо от
флага. Шапка и строки сохраняются в одной транзакции. `db.get(Class, id)`
возвращает доступную запись со строками или `None`. Для нескольких записей
передайте `include_table_parts=True` в `db.list()` или `db.filter()`. По
умолчанию эти методы загружают только шапки.

`db.filter()` принимает точные значения и операторы в именах аргументов.
Доступны `__eq`, `__gt`, `__gte`, `__lt`, `__lte`, `__ne`, `__in`,
`__contains`, `__icontains`, `__literal_contains`, `__isnull`, `__between`
и `__date_range`. Для `Date` и `DateTime` передайте пару дат
`field__date_range=(start, end)`. Обе даты входят в период целиком.
`None` снимает ограничение с соответствующей стороны. Перевёрнутый
период вызывает `ValueError`.

## 3. Сущности

### Базовый класс `Entity` (Active Record & Helpers)
Все сущности (`MasterData`, `Transaction`, `Catalog`, `Document`) наследуют методы:
- `entity.save(db)` — сохранить сущность в базу данных.
- `entity.delete(db)` — удалить сущность из базы данных.
- `Class.get(db, id)` — получить сущность по первичному ключу `id`.
- `Class.all(db, include_table_parts=False)` — получить список доступных записей.
- `Class.filter(db, **kwargs)` — отфильтровать записи по значениям полей.
- `entity.to_dict(include_table_parts=True)` — сериализовать в `dict`.
- `Class.from_dict(data)` — восстановить экземпляр из словаря.

### Коллекция строк спецификаций (`LineItemsCollection`)
Табличные части (`@line_items` / `@tab_part`) предоставляют методы:
- `doc.Items.add(**kwargs)` — применяет значения по умолчанию, приводит типы полей и добавляет строку.
- `doc.Items.total(field="amount")` — вычисляет сумму указанного поля по всем строкам.
- Стандартные операции `list`: индексация (`doc.Items[0]`), итерация, `len()`, `doc.Items.clear()`.

Коллекцию можно перебирать через `for row in entity.Items`. Для расчёта итога
по сохранённым строкам загрузите документ через `db.get(Class, id)` или
передайте `include_table_parts=True` при выборке документов.

### `@catalog` / `@master_data` / `@lookup`

Регистрирует справочник / нормативно-справочную сущность.

```python
@master_data
class Customer(MasterData):
    __verbose_name__ = "Контрагенты"
    __section__      = "Продажи"
    __numbering__    = {"prefix": "CTR", "pattern": "{prefix}-{seq:06d}"}

    code = String(read_only=True)             # Автоматическое имя: "Code"
    name = String("Наименование", required=True) # Явное имя
```

### `@document` / `@transaction`

Регистрирует документ / транзакцию. Автоматически наследует поля:
- `date` (`DateTimeField`): момент фиксации операции.
- `number` (`StringField`): уникальный номер.
- `posted` (`BooleanField`): признак учтённости / проведения (`read_only`).

#### Методы и свойства `Document` / `Transaction`:
- `doc.post()` / `doc.submit()` — пометить документ как проведенный (`posted = True`).
- `doc.unpost()` / `doc.cancel()` — снять проведение (`posted = False`).
- `doc.is_posted` — булево свойство проверки статуса проведения.
- `doc.get_ledger_entries()` / `doc.get_movements()` — возвращает список `LedgerEntry` / `Movement` при проведении.
- `doc.write_movements(db)` — записать движения в регистры.
- `doc.clear_movements(db)` — удалить движения этого документа из регистров.

```python
@transaction
class Invoice(Transaction):
    __verbose_name__ = "Счета"
    __section__      = "Продажи"
    __numbering__    = {"prefix": "INV", "pattern": "{prefix}-{year}-{seq:06d}"}

    total = Decimal("Итого", read_only=True)

    @line_items
    class Items:
        product  = Ref(Product, "Товар")
        quantity = Integer("Кол-во", default=1)
        price    = Decimal("Цена")
        amount   = Decimal("Сумма", read_only=True)

    @action("Рассчитать")
    def calc(self):
        ...

    def get_ledger_entries(self):
        return [
            LedgerEntry(
                register=StockLedger,
                dimensions={"product": r["product"]},
                resources={"quantity": r["quantity"]},
                is_income=False,
            )
            for r in self._table_data.get("Items", [])
        ]
```

---

## 4. Поля (`Field`)

Все дескрипторы полей поддерживают автоматический вывод названий: первый аргумент `display_name` опционален. При его отсутствии имя генерируется из имени атрибута (`unit_price` → `"Unit price"`).

| Тип | Назначение | Параметры |
|---|---|---|
| `String(name=None)` | Строка (`QLineEdit` / `QPlainTextEdit`) | `required`, `max_length`, `multiline`, `read_only`, `default`, `unique`, `indexed` |
| `Integer(name=None)` | Целое число (`QSpinBox`) | `required`, `min_value`, `max_value`, `default`, `unique`, `indexed` |
| `Decimal(name=None)` | Десятичное число (`QDoubleSpinBox`) | `precision`, `scale`, `min_value`, `max_value`, `currency`, `default` |
| `Boolean(name=None)` | Флаг (`QCheckBox`) | `default` |
| `Date(name=None)` | Дата (`QDateEdit`) | `required`, `default` |
| `DateTime(name=None)` | Дата и время (`QDateTimeEdit`) | `required`, `default` |
| `Time(name=None)` | Время (`QTimeEdit`) | `required`, `default` |
| `Choice(name=None)` | Выбор (`QGroupBox+QCheckBox` или `QComboBox` при `multi=False`) | `choices=[("CODE", "Label")]`, `multi`, `separator="|"`, `required`, `default` |
| `Ref(Target, name=None)`| Внешний ключ (`QComboBox`) | `representation_field`, `auto_fill`, `on_delete`, `related_list`, `choice_filter`, `create_from`, `basis_fields`, `basis_tables`, `required` |

### Атрибуты класса сущности (`__*__`)

| Атрибут | Назначение |
|---|---|
| `__verbose_name__`, `__section__` (`__subsystem__`) | Заголовок и раздел в дереве навигации |
| `__numbering__`, `__defaults__`, `__primary_key__` | Автонумерация, значения по умолчанию и первичный ключ |
| `__list_columns__`, `__list_order__` | Колонки и начальная сортировка списка `ListView` |
| `__form_groups__`, `__form_tabs__`, `__form_table_tabs__` | Группы полей и вкладки карточки/табличных частей |
| `__form_visible_when__`, `__required_when_visible__` | Условная видимость и обязательность полей формы |
| `__sequence__` | Проверка хронологической последовательности проведения документа |
| `__workflow__`, `__print_form__` | Маршрут согласования (`Workflow`) и печатная форма (`PrintForm`) |
| `__owner_field__`, `__permissions__`, `__field_read_roles__`, `__field_write_roles__`, `__command_roles__` | Разграничение прав по ролям и владельцу записи |

---

## 5. Регистры и движения

### Регистры остатков (`@ledger` / `@accumulation`)
```python
@ledger
class WarehouseStock(Ledger):
    warehouse = Dimension("Склад")
    product   = Dimension("Товар")
    qty       = Metric("Количество", field_type=Decimal)
```

- `get_ledger_balance(db, register, on_date=None, group_by=None, **filters)` — расчет остатков.

### Регистры сведений (`@history_register` / `@information`)
```python
@history_register
class PriceHistory(HistoryRegister):
    product = Dimension("Товар")
    price   = Resource("Цена", field_type=Decimal)
```

- `set_record(db, register, dimensions=..., resources=...)` — записать строку.
- `get_latest(db, register, on_date=None, **dim_filters)` — последняя запись со всеми заданными значениями измерений или `None`. Если передан `on_date`, учитываются записи по эту дату включительно. При совпадении дат выбирается запись с большим ID.
- `get_records(db, register, order_by=..., limit=..., **filters)` — список записей.

---

## 6. Отчёты (`@report` / `QueryReport`)

`Report.execute(db, **params)` получает значения параметров формы по именам
дескрипторов. Он возвращает список словарей с ключами из `Column`. Пустой
результат — `[]`. Условия периода задаются в методе. Для суммирования
возвращайте числа из базы и задавайте отображение через `Column.format`.

`RefParam("Поставщик", Supplier, from_record=True)` помещает отчёт в меню
«Отчёты» сохранённой карточки поставщика и подставляет её идентификатор
при открытии. Флаг по умолчанию равен `False`. При `auto_run=True`
формирование начинается после подстановки. [Отчёт из карточки](05-reports.md#отчёт-из-карточки)

```python
@report
class StockReport(QueryReport):
    title = "Остатки товаров"
    auto_run = True
    sortable = True
    group_by = ("warehouse",)
    chart = {"label": "product", "value": "qty", "title": "Остатки"}
    columns = [
        Column("Склад", "warehouse", width=160),
        Column("Товар", "product", width=250),
        Column("Остаток", "qty", width=120, align="right", aggregate="sum"),
    ]

    # Параметры фильтра
    date_to = DateParam("На дату", default=datetime.date.today)
    min_qty = IntegerParam("Мин. остаток", default=0)

    def execute(self, db, date_to=None, min_qty=0, **kwargs):
        # Возвращает список словарей со строками (для отчётов по регистрам см. register_report_rows(..., latest_only=True))
        return [...]
```

---

## 7. Согласование (`Workflow`) и печатные формы (`PrintForm`)

```python
from decui import PrintForm, PrintTable, Transition, Workflow

__workflow__ = Workflow(
    field="status",
    initial="Черновик",
    states=("Черновик", "На согласовании", "Утверждён"),
    transitions=(
        Transition("Отправить", "Черновик", "На согласовании", roles=("manager", "admin")),
        Transition("Утвердить", "На согласовании", "Утверждён", roles=("admin",), assigned_only=True),
    ),
    post_states=("Утверждён",),
)

__print_form__ = PrintForm(
    title="Заказ поставщику",
    fields=("number", "date", "supplier"),
    tables=(PrintTable(part="Items", columns=("product", "quantity", "amount")),),
)
```

Подробнее о правах, маршрутах согласования, печати и миграциях см. [09-advanced.md](09-advanced.md).
