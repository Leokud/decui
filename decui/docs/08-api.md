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
    report, Report, QueryReport, Column,
    StringParam, IntegerParam, NumberParam, DecimalParam, BooleanParam, DateParam, RefParam,

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
    argv=None                     # Аргументы командной строки sys.argv
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

db.save(entity)              # INSERT (если id is None) или UPDATE + сохранение строк спецификаций
db.get(Class, id)            # Получить объект по первичному ключу со всеми строками спецификаций
db.list(Class)               # Получить список всех объектов (краткая выгрузка без тяжелых строк)
db.filter(Class, **kwargs)   # Выборка объектов по значениям полей
db.delete(entity)            # Удалить объект и каскадно удалить строки спецификаций
db.seed([e1, e2, ...])       # Пакетное сохранение списка объектов
```

---

## 3. Сущности

### Базовый класс `Entity` (Active Record & Helpers)
Все сущности (`MasterData`, `Transaction`, `Catalog`, `Document`) наследуют методы:
- `entity.save(db)` — сохранить сущность в базу данных.
- `entity.delete(db)` — удалить сущность из базы данных.
- `Class.get(db, id)` — получить сущность по первичному ключу `id`.
- `Class.all(db)` — получить список всех сущностей данного класса.
- `Class.filter(db, **kwargs)` — отфильтровать записи по значениям полей.
- `entity.to_dict(include_table_parts=True)` — сериализовать в `dict`.
- `Class.from_dict(data)` — восстановить экземпляр из словаря.

### Коллекция строк спецификаций (`LineItemsCollection`)
Табличные части (`@line_items` / `@tab_part`) предоставляют методы:
- `doc.Items.add(**kwargs)` — валидирует и добавляет новую строку.
- `doc.Items.total(field="amount")` — вычисляет сумму указанного поля по всем строкам.
- Стандартные операции `list`: индексация (`doc.Items[0]`), итерация, `len()`, `doc.Items.clear()`.

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
| `String(name=None)` | Строка (`QLineEdit`) | `required`, `max_length`, `read_only`, `default` |
| `Integer(name=None)` | Целое число (`QSpinBox`) | `required`, `min_value`, `max_value`, `default` |
| `Decimal(name=None)` | Десятичное число (`QDoubleSpinBox`) | `precision`, `scale`, `min_value`, `max_value`, `currency` |
| `Boolean(name=None)` | Флаг (`QCheckBox`) | `default` |
| `Date(name=None)` | Дата (`QDateEdit`) | `required`, `default` |
| `DateTime(name=None)` | Дата и время (`QDateTimeEdit`) | `required`, `default` |
| `Time(name=None)` | Время (`QTimeEdit`) | `required`, `default` |
| `Choice(name=None)` | Множественный выбор (`QGroupBox + QCheckBox`) | `choices=[("CODE", "Label")]`, `separator="|"` |
| `Ref(Target, name=None)`| Внешний ключ (`QComboBox`) | `representation_field`, `auto_fill`, `required` |

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
- `get_latest(db, register, on_date=None, **dim_filters)` — срез последних.
- `get_records(db, register, order_by=..., limit=..., **filters)` — список записей.

---

## 6. Отчёты (`@report` / `QueryReport`)

```python
@report
class StockReport(QueryReport):
    title = "Остатки товаров"
    columns = [
        Column("Товар", "product", width=250),
        Column("Остаток", "qty", width=120, align="right", aggregate="sum"),
    ]

    # Параметры фильтра
    date_to = DateParam("На дату", default=datetime.date.today)
    min_qty = IntegerParam("Мин. остаток", default=0)

    def execute(self, db, date_to=None, min_qty=0, **kwargs):
        # Возвращает список словарей со строками
        return [...]
```
