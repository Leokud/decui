# Руководство по Developer Experience (DX) в DECUI

DECUI спроектирован так, чтобы разработка учетных систем на Python была быстрой, понятной и не требовала рутинного шаблонного кода (boilerplate). В этой главе описаны возможности для улучшения эргономики работы разработчика.

---

## 1. Консольная утилита `decui` (CLI)

Фреймворк поставляется со встроенной CLI-утилитой `decui`, доступной из терминала или через `python -m decui.cli`.

### Создание нового проекта: `decui new <имя>`
Команда генерирует готовое к запуску приложение с настроенным каталогом товаров, клиентами, заказами, строками спецификаций, отчетом и SQLite БД:

```bash
decui new my_erp
cd my_erp
```

Созданная структура:
- `app.py` — полный исполняемый код приложения со справочниками, документами и отчетами.
- `README.md` — краткие инструкции по запуску и тестированию.

### Запуск приложения: `decui run [файл]`
Автоматически находит `app.py` или `main.py` и запускает его:

```bash
decui run
# или указать конкретный файл
decui run custom_app.py
```

### Запуск тестов: `decui test`
Запускает тестовый набор через `pytest`, автоматически настраивая headless-режим Qt (`QT_QPA_PLATFORM=offscreen`) на Linux:

```bash
decui test
# можно передавать аргументы pytest
decui test -v -k "test_invoice"
```

### Сборка дистрибутива: `decui build [файл]`
Собирает оконное приложение в единый исполняемый файл через PyInstaller:

```bash
decui build
```

---

## 2. Автоматический вывод названий полей (Auto-naming)

Раньше для каждого поля требовалось обязательно указывать русское или английское отображаемое имя:
```python
# Старый синтаксис
name = String("Наименование", required=True)
unit_price = Decimal("Цена за единицу", scale=2)
```

Теперь аргумент `display_name` опционален. Если имя опущено, DECUI автоматически формирует читабельную подпись из имени атрибута:

```python
# Современный краткий синтаксис:
name = String(required=True)                  # Подпись: "Name"
unit_price = Decimal(scale=2, currency="RUB") # Подпись: "Unit price"
customer_phone = String()                     # Подпись: "Customer phone"
is_active = Boolean(default=True)             # Подпись: "Is active"
customer = Ref(Customer, required=True)       # Подпись: "Customer"
```

> **Совет:** Явное отображаемое имя по-прежнему поддерживается первым позиционным аргументом: `name = String("Название товара", required=True)`.

---

## 3. Коллекция строк спецификаций (`LineItemsCollection`)

Спецификации документов (`@line_items` / `@tab_part`) теперь оборачиваются в `LineItemsCollection`. Это сохраняет 100% совместимость с обычным `list`, добавляя методы `.add()` и `.total()`:

### Добавление строк через `.add(**kwargs)`
Автоматически валидирует и приводит типы значений в соответствии с дескрипторами:

```python
order = SalesOrder()

# Быстрое добавление строк
order.Items.add(
    product=product_id,
    quantity=5,
    price=Decimal("120.00"),
    amount=Decimal("600.00"),
)
```

### Подсчет итогов через `.total(field)`
Больше не нужно писать циклы для суммирования:

```python
total_sum = order.Items.total("amount")   # Decimal("600.00")
total_qty = order.Items.total("quantity") # 5
```

### Совместимость с обычными списками
Коллекция наследует стандартный `list`, поэтому все привычные операции Python работают «из коробки»:

```python
len(order.Items)          # Количество строк
order.Items[0]            # Доступ по индексу
for row in order.Items:   # Итерация
    print(row["amount"])
order.Items.clear()       # Очистка строк
```

---

## 4. Паттерн Active Record и методы сущностей

Вы можете использовать привычный объектный стиль работы с базой данных вместо вызовов `db.save(entity)`:

```python
# 1. Сохранение
product = Product(name="Монитор 27\"", unit_price=Decimal("25000.00"))
product.save(db)
print(product._id)  # ID назначен автоматически

# 2. Получение по ID (включая строки спецификаций)
loaded = Product.get(db, 1)

# 3. Список всех записей
all_products = Product.all(db)

# 4. Фильтрация по значениям полей
active_items = Product.filter(db, is_active=True)

# 5. Удаление
product.delete(db)
```

---

## 5. Сериализация и десериализация (`to_dict` / `from_dict`)

Сущности легко сериализуются в стандартные словари Python (например, для JSON API, логирования или импорта/экспорта):

```python
# Экспорт в словарь (включая строки спецификаций)
data = order.to_dict()
# {
#     "id": 1,
#     "number": "SO-2026-00001",
#     "customer": 42,
#     "Items": [
#         {"product": 10, "quantity": 2, "price": Decimal("100.00"), "amount": Decimal("200.00")}
#     ]
# }

# Восстановление объекта из словаря
restored = SalesOrder.from_dict(data)
```

---

## 6. Утилиты для тестирования и Database Helpers

Для написания модульных тестов и фикстур добавлены методы быстрого создания БД и заполнения данных (seed):

```python
import pytest
from decui import Database

@pytest.fixture
def db():
    # Создание быстрой базы данных в памяти
    return Database.memory()

def test_sales_flow(db):
    # Пакетное сохранение тестовых данных
    products = db.seed([
        Product(name="Товар 1", unit_price=Decimal("100")),
        Product(name="Товар 2", unit_price=Decimal("200")),
    ])
    
    # Фильтрация
    found = db.filter(Product, name="Товар 1")
    assert len(found) == 1
```
