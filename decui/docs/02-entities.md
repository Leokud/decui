# Справочники, документы и поля

## Справочники (Catalog)

```python
from decui import catalog, Catalog, String, Decimal as D

@catalog
class Product(Catalog):
    __numbering__    = {"prefix": "PRD", "field": "code"}
    __verbose_name__ = "Товары"

    code  = String("Код", read_only=True)
    name  = String("Наименование", required=True)
    price = D("Цена", precision=10, scale=2, required=True)
```

## Документы (Document)

```python
from decui import document, Document, String, Ref, command, tab_part

@document
class Invoice(Document):
    __numbering__    = {"prefix": "INV", "field": "number"}
    __verbose_name__ = "Счета"

    number = String("Номер", read_only=True)
    total  = D("Сумма", read_only=True, default=0)

    @tab_part
    class Items:
        product  = Ref(Product, "Товар")
        quantity = Integer("Количество", default=1)
        price    = D("Цена", precision=10, scale=2)

    @command("Рассчитать")
    def calc(self):
        for row in self._table_data.get("Items", []):
            row["amount"] = Decimal(str(row["quantity"])) * row["price"]
```

## Поля, унаследованные от Document

| Поле | Тип | Отображается как |
|---|---|---|
| `date` | `DateTimeField` | «Дата» (дд.мм.гггг ЧЧ:ММ) |
| `number` | `StringField` | «Номер» |
| `posted` | `BooleanField` | «Проведён», read_only |

## Все типы полей

| Дескриптор | Qt-виджет | SQLite | Пример |
|---|---|---|---|
| `String("Имя")` | QLineEdit | VARCHAR | `name = String("Имя", required=True)` |
| `Integer("Кол-во", min_value=0)` | QSpinBox | INTEGER | `qty = Integer("Кол-во", default=1, min_value=0)` |
| `Decimal("Цена", 10, 2)` | QDoubleSpinBox | NUMERIC | `price = D("Цена", precision=10, scale=2)` |
| `Boolean("Флаг")` | QCheckBox | BOOLEAN | `done = Boolean("Готово")` |
| `Date("Дата")` | QDateEdit (дд.мм.гггг) | DATE | `created = Date("Дата")` |
| `DateTime("Момент")` | QDateTimeEdit (дд.мм.гггг ЧЧ:ММ) | DATETIME | `ts = DateTime("Время")` |
| `Time("Время")` | QTimeEdit (ЧЧ:ММ) | VARCHAR(5) | `t = Time("Время")` |
| `Choice("Дни")` | QGroupBox+QCheckBox | VARCHAR | `days = Choice("Дни", choices=[...])` |
| `Ref(Entity, "Метка")` | QComboBox | INTEGER FK | `product = Ref(Product, "Товар")` |

## Ограничения чисел

```python
qty   = Integer("Кол-во", min_value=0, max_value=9999, default=1)
price = Decimal("Цена", min_value=Decimal("0"), max_value=Decimal("999999"))
```

Значения автоматически клэмпятся в заданный диапазон.

## Choice — множественный выбор

```python
_DAYS = [("ПН","Понедельник"), ("ВТ","Вторник"), ("СР","Среда")]

days = Choice("Дни недели", choices=_DAYS, required=True)
```

Хранится как строка `"ПН|СР|ПТ"`. В форме — группа чекбоксов.

## Ref — автоподстановка

```python
circle       = Ref(Circle, "Кружок", auto_fill="age_interval")
age_interval = Ref(AgeInterval, "Возраст", read_only=True)
```

При выборе кружка поле `age_interval` автоматически заполняется значением из справочника.

## Общие параметры полей

```python
name = String(
    "Имя",                    # display_name — подпись на форме
    required=True,            # NOT NULL + валидация
    read_only=True,           # только просмотр
    default="что-то",         # значение по умолчанию (callable — тоже можно)
    unique=True,              # UNIQUE в БД
    indexed=True,             # INDEX в БД
)
```

## Разделы в навигации

```python
@catalog
class Circle(Catalog):
    __section__      = "Расписание"   # сущность появляется под этим узлом
    __verbose_name__ = "Кружки"
```

Если не указан — сущность попадает в общий корень.

## Стандартные значения для новых записей

```python
@document
class ClubStart(Document):
    __defaults__ = {
        "days": lambda db: "ПН|СР",
        "time": lambda db: time(10, 0),
    }
```

Значения применяются при создании новой записи. Callable получает Database.

## Имена в навигации

```python
__verbose_name__ = "Товары"   # вместо Product в дереве навигации
```

Если не указан — используется имя класса (Product, Invoice и т.д.).
