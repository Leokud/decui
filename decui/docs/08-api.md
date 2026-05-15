# Полное API

## Импорт

```python
from decui import (
    # Декораторы
    catalog,           # @catalog       — справочник
    document,          # @document      — документ
    command,           # @command("Имя") — команда
    tab_part,          # @tab_part      — табличная часть
    report,            # @report        — отчёт

    # Базовые классы
    Entity,            # Базовый класс сущности
    Catalog,           # Базовый класс справочника
    Document,          # Базовый класс документа
    Report,            # Базовый класс отчёта

    # Поля
    String,            # Строка
    Integer,           # Целое (min_value, max_value)
    Decimal,           # Десятичное (precision, scale, min_value, max_value)
    Boolean,           # Да/Нет
    Date,              # Дата (дд.мм.гггг)
    DateTime,          # Дата+время (дд.мм.гггг ЧЧ:ММ)
    Time,              # Время (ЧЧ:ММ)
    Choice,            # Множественный выбор (чекбоксы)
    Ref,               # Ссылка (auto_fill, representation_field)

    # Параметры отчётов
    Column,            # Колонка таблицы отчёта
    DateParam,         # Параметр-дата (default)
    RefParam,          # Параметр-ссылка
    StringParam,       # Параметр-строка
)
```

## Декораторы

### `@catalog`

Регистрирует справочник. Класс должен наследовать `Catalog`.

### `@document`

Регистрирует документ. Класс должен наследовать `Document`.

### `@command(name)`

Делает метод кнопкой на форме. После выполнения сохраняет документ.

### `@tab_part`

Превращает вложенный класс в табличную часть.

### `@report`

Регистрирует отчёт. Класс должен наследовать `Report`.

## Базовые классы

### `Entity`

```python
class Product(Entity): ...
entity._data          # dict — значения полей
entity._table_data    # dict — строки табличных частей
entity._id            # int | None — первичный ключ
entity.name           # доступ к полю через __getattr__
```

### `Catalog(Entity)`

То же, что Entity. `_entity_type = "catalog"`.

### `Document(Entity)`

Добавляет поля: `date` (DateTime), `number` (String), `posted` (Boolean, read_only).

### `Report`

```python
class SalesReport(Report):
    title = "Название"
    columns = [Column(...)]
    def execute(self, db, **params): ...
```

## Класс-параметры сущностей

| Атрибут | Назначение |
|---|---|
| `__verbose_name__ = "Товары"` | Отображается в навигации |
| `__section__ = "Расписание"` | Группировка в навигации |
| `__numbering__ = {"prefix": "PRD"}` | Автонумерация |
| `__defaults__ = {"field": lambda db: ...}` | Значения по умолчанию |

## Database

```python
from decui.orm import Database

db = Database("data.db")   # или ":memory:"
db.save(entity)            # INSERT или UPDATE
db.get(Product, 1)         # SELECT по id (с табличными частями)
db.list(Product)           # SELECT все
db.delete(entity)          # DELETE
```

## Кнопки на форме

| Кнопка | Действие |
|---|---|
| **Сохранить** | Валидация → save() → остаться на форме |
| **Сохранить и закрыть** | Валидация → save() → сигнал closed → список |
| **Закрыть** | Сигнал closed → список (без сохранения) |

Для документов:
| **Провести и закрыть** | Команда → save → closed → список |
| **Закрыть** | save → closed → список (авто-сохранение) |
