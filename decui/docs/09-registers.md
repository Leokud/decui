# Регистры

Система учёта остатков, оборотов и периодических значений.

---

## Регистр накопления (AccumulationRegister)

Для складского, финансового и любого количественного учёта.

### Определение

```python
from decui import accumulation, Dimension, RefDimension, Resource

@accumulation
class GoodsBalance:
    period   = Dimension("Период", date)
    product  = RefDimension(Product, "Товар")
    warehouse = RefDimension(Warehouse, "Склад")
    quantity = Resource("Количество")
```

### Компоненты

| Тип | Описание | Пример |
|---|---|---|
| `Dimension("Имя", type)` | Измерение — аналитика | `date`, `str`, `int` |
| `RefDimension(Entity, "Имя")` | Ссылка на справочник | `RefDimension(Product, "Товар")` |
| `Resource("Имя")` | Числовой ресурс (Decimal) | `Resource("Сумма")` |

---

## Движения (Movements)

```python
from decui import Movement
from decui.movement import write_movements, clear_movements

# Запись прихода
write_movements(db, [
    Movement(
        register=GoodsBalance,
        period=date(2026, 6, 1),
        dimensions={"product": p._id, "warehouse": w._id},
        resources={"quantity": Decimal("100")},
        is_income=True,               # True = приход, False = расход
        doc_id=entity._id,            # ID документа-владельца
        doc_type="Invoice",           # Тип документа-владельца
    ),
])

# Очистка движений по документу
clear_movements(db, GoodsBalance, doc_id=entity._id, doc_type="Invoice")
```

### Параметры Movement

| Поле | Обязательность | Описание |
|---|---|---|
| `register` | да | Класс регистра |
| `period` | для накопления | Дата движения |
| `dimensions` | да | Значения измерений: `{"product": 1}` |
| `resources` | да | Значения ресурсов: `{"quantity": 100}` |
| `is_income` | да | `True` = приход, `False` = расход |
| `doc_id` | нет | ID документа для идентификации при перепроведении |
| `doc_type` | нет | Имя класса документа |

### Знак движения

Расход (`is_income=False`) записывается отрицательным числом:

```
Приход:  quantity=100  → в БД: +100
Расход:  quantity=30  → в БД: -30
Баланс:  SUM = 70
```

---

## Автоматические движения при проведении

Документ может автоматически записывать движения при проведении.
Для этого определите метод `get_movements()`:

```python
@document
class Invoice(Document):
    @command("Провести")
    def post(self):
        self.calc()
        self._data["posted"] = True

    def get_movements(self):
        return [
            Movement(
                register=GoodsBalance,
                period=self.date.date() if self.date else date.today(),
                dimensions={"product": item["product"],
                            "warehouse": item["warehouse"]},
                resources={"quantity": item["quantity"]},
                is_income=True,
            )
            for item in self._table_data.get("Items", [])
        ]
```

Фреймворк автоматически:
1. Вызывает `clear_movements()` при перепроведении
2. Вызывает `write_movements()` после проведения
3. Передаёт `doc_id` и `doc_type` для идентификации

---

## Получение баланса

```python
from decui import get_balance

# Все остатки
balance = get_balance(db, GoodsBalance)

# С фильтром по измерению
balance = get_balance(db, GoodsBalance, product=1)

# На дату (включает движения ДО этой даты)
balance = get_balance(db, GoodsBalance, on_date=date(2026, 6, 1))

# С группировкой
balance = get_balance(db, GoodsBalance, group_by=["warehouse"])
```

### Формат результата

```python
[
    {"product": 1, "warehouse": 2, "quantity": 70.0},
    {"product": 1, "warehouse": 3, "quantity": 30.0},
]
```

Без группировки:
```python
[{"quantity": 100.0}]  # SUM всех движений
```

---

## Регистр сведений (InformationRegister)

Для хранения периодических значений, не требующих суммирования (курсы валют,
цены поставщиков, ставки налогов).

### Определение

```python
from decui import information, Dimension, Resource

@information
class CurrencyRates:
    currency = Dimension("Валюта")
    rate     = Resource("Курс")
```

### Запись

```python
write_movements(db, [
    Movement(register=CurrencyRates,
             dimensions={"currency": "USD"},
             resources={"rate": Decimal("92.50")}),
])
```

### Чтение

Информационные регистры читаются прямым запросом к таблице (без суммирования):

```python
from sqlalchemy.orm import sessionmaker
Session = sessionmaker(bind=db._engine)
mc = CurrencyRates._register_meta.orm_model
with Session() as sess:
    rows = sess.query(mc).all()
    for r in rows:
        print(r.currency, r.rate)
```

---

## Идентификация документа

В таблицы регистров добавлены колонки:

| Колонка | Назначение |
|---|---|
| `doc_id` | `entity._id` документа, создавшего движение |
| `doc_type` | `type(entity).__name__`, например "Invoice" |

При перепроведении `clear_movements()` удаляет ТОЛЬКО движения текущего
документа, не затрагивая другие документы с теми же измерениями.

---

## Схема таблиц в БД

```sql
CREATE TABLE reg_goods_balance (
    id INTEGER PRIMARY KEY,
    period DATE NOT NULL,
    product INTEGER,            -- FK на product.id
    warehouse INTEGER,          -- FK на warehouse.id
    quantity NUMERIC(15,2),
    doc_type VARCHAR(50),
    doc_id INTEGER
);
```

Имя таблицы: `reg_` + имя регистра в snake_case.
