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
| `String("Имя")` | QLineEdit | VARCHAR | `name = String("Имя", required=True, placeholder="ООО …")` |
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

### Связанные записи и зависимый выбор ссылки

```python
# Address.owner — список адресов в карточке сохранённого Customer.
owner = Ref(Customer, "Покупатель", related_list="Адреса покупателя")

# Шапка Order — варианты адреса зависят от текущего редактора покупателя.
customer = Ref(Customer, "Покупатель")
address = Ref(Address, "Адрес доставки", choice_filter={"owner": "customer"})

# Вариант для копирования Customer.phone в Order.phone.
customer = Ref(Customer, "Покупатель", auto_fill="phone")
```

Ключ `choice_filter` — поле целевой записи, значение — поле текущей карточки.
Все пары сравниваются на равенство и объединяются через AND. Пустой или
недоступный источник оставляет пустой вариант. `False` и `0` участвуют в отборе.
При смене источника форма оставляет совместимый выбор, а несовместимый очищает.
Сохранённую ссылку вне текущего отбора карточка сохраняет до явной замены или
очистки. Её имя в списке скрыто, а редактор показывает пояснение.
Кнопка «+» заполняет поля нового объекта значениями отбора и выбирает его,
если он доступен и попадает в список после сохранения.

Для правил, которые должны действовать и при записи из Python, используйте
`before_save(db)`. В [примере доставки](../../examples/features/dependent_refs.py)
сохранение проверяет принадлежность адреса покупателю. В Studio у поля Ref
в шапке укажите `owner=customer` в «Отбор вариантов ссылки». Несколько пар
разделяются запятыми. Настройка поддерживается для реквизитов шапки.


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

---

## Поддержка IDE и Type Hints (PEP 681)

DECUI поддерживает современные возможности статического анализа в IDE (VS Code, PyCharm, Cursor):

### 1. Объявление полей с аннотациями типов:
```python
@master_data
class Student(MasterData):
    # Способ 1: Type hint + дескриптор (полный контроль и подсказки)
    name: str = String("Имя", required=True)
    age: int = Integer("Возраст", default=18)
    deadline: datetime.datetime = DateTime("Дедлайн")

    # Способ 2: Чистые type hints (минималистичный стиль как в dataclasses)
    is_active: bool = True
    score: int = 100
```

### 2. Подсказки метаданных класса:
При вводе `__` внутри класса IDE подсказывает поддерживаемые атрибуты:
- `__verbose_name__: str` — название в навигации и заголовках
- `__section__: str` (`__subsystem__: str`) — раздел в дереве навигации
- `__numbering__: dict` — правила автонумерации
- `__defaults__: dict` — значения по умолчанию для новых записей
- `__primary_key__: str` — имя первичного ключа
- `__list_columns__: tuple[str, ...]` и `__list_order__: str` — колонки и сортировка списка
- `__form_groups__: dict`, `__form_tabs__: dict`, `__form_table_tabs__: dict` — группировка и вкладки карточки
- `__form_visible_when__: dict` и `__required_when_visible__: tuple[str, ...]` — условная видимость и обязательность
- `__form__: Form | type[EntityForm]` и `__form_class__: type[EntityForm]` — декларативная компоновка и программируемый контроллер формы (`@form_for`)
- `__workflow__: Workflow` и `__print_form__: PrintForm` — маршрут согласования и печатная форма
- `__owner_field__: str`, `__permissions__: dict`, `__field_read_roles__: dict`, `__field_write_roles__: dict`, `__command_roles__: dict` — доступ по ролям и владельцу
- `__sequence__: bool` — контроль хронологии проведения документов

### 3. Интроспекция полей:
- `Student.field_names()` — список имен всех полей (`['name', 'age', ...]`)
- `Student.fields()` — словарь дескрипторов
- `Student.name` — дескриптор поля на уровне класса
- В конструкторе `Student(name=..., age=...)` IDE автоматически подсказывает сигнатуру благодаря `@dataclass_transform`!

---

## Кастомные формы (`Form` и `EntityForm`)

Помимо стандартной вертикальной карточки можно задать точную многоколоночную компоновку в коде или настроить её визуально на вкладке **«Форма»** в `DECUI Studio`. Рабочий пример находится в [examples/custom_forms.py](../../examples/custom_forms.py).

### 1. Декларативная компоновка (`__form__ = Form(...)`)

```python
from decui import (
    Catalog, Choice, Form, FormColumn, FormDivider,
    FormField, FormGroup, FormLabel, FormRow, String, catalog,
)

@catalog
class Client(Catalog):
    name  = String("Наименование", required=True, placeholder="ООО «Ромашка»")
    kind  = Choice("Тип", choices=[("ORG", "Организация"), ("IND", "Физлицо")], default="ORG")
    inn   = String("ИНН", placeholder="10 цифр")
    phone = String("Телефон")
    note  = String("Заметки", multiline=True)

    __form__ = Form(
        FormLabel("Карточка клиента", style="heading"),
        FormRow(
            FormColumn("name", "kind"),
            FormColumn(FormField("inn", visible_when=("kind", "ORG")), "phone"),
        ),
        FormDivider(),
        FormGroup("Заметки", "note", collapsible=True),
    )
```

Доступные элементы:
- `FormRow(...)`, `FormColumn(..., stretch=1)`, `FormGroup(title, ..., collapsible=True, collapsed=False)`
- `FormTabs(FormTab("Вкладка", ...))` — вкладки произвольной вложенности
- `FormField(name, label=..., placeholder=..., read_only=..., stretch=..., visible_when=...)`
- `FormTable(name, title=..., min_height=..., read_only=...)` — табличная часть документа/справочника
- `FormButton(command, label=..., primary=...)` — кнопка `@command`/`@action` или встроенного действия (`save`, `post`, `unpost`, `print`, `close`, `movements`)
- `FormLabel(text, style="normal"|"heading"|"muted"|"info"|"warning")`, `FormDivider()`, `FormRelated(...)`, `FormWidget(factory)`

### 2. Программируемый контроллер (`EntityForm` + `@form_for`)

```python
from decui import EntityForm, Form, FormButton, FormRow, FormTable, form_for

@form_for(ServiceOrder)
class ServiceOrderForm(EntityForm):
    layout = Form(
        FormRow("number", "date", "posted"),
        FormTable("Lines", title="Состав работ", min_height=220),
        FormRow("total", FormButton("calc", primary=True)),
    )

    def on_open(self) -> None: ...
    def on_field_changed(self, field_name: str, value: object) -> None: ...
    def on_table_changed(self, part_name: str) -> None: ...
    def on_before_save(self) -> bool | None: ...
    def build_extra(self, layout) -> None: ...
```

Методы контроллера `EntityForm`: `get_value`, `set_value`, `get_widget`, `get_table`, `set_visible`, `set_enabled`, `set_label`, `refresh`, `save`, `post`.
