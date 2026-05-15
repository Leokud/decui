# Продвинутые возможности

## Регистр накопления

Для учёта остатков и оборотов (товары, деньги).

```python
from decui import accumulation, Dimension, RefDimension, Resource, Movement

@accumulation
class GoodsBalance:
    period   = Dimension("Период", date)
    product  = RefDimension(Product, "Товар")
    warehouse = RefDimension(Warehouse, "Склад")
    quantity = Resource("Количество")
```

Запись движений:

```python
from decui import Movement

movement = Movement(
    register=GoodsBalance,
    period=date(2026, 6, 1),
    dimensions={"product": p._id, "warehouse": w._id},
    resources={"quantity": Decimal("100")},
)
write_movements(db, [movement])
```

Получение остатков:

```python
from decui import get_balance

# Все остатки по товару
balance = get_balance(db, GoodsBalance, group_by=["product"])
```

## Регистр сведений

Для хранения периодических значений (курсы валют, цены).

```python
from decui import information

@information
class CurrencyRates:
    currency = Dimension("Валюта")
    rate     = Resource("Курс")
```

## Проведение документа с движениями

Документ может автоматически записывать движения в регистры при проведении:

```python
@document
class Invoice(Document):
    @command("Провести")
    def post(self):
        self._data["posted"] = True

    def get_movements(self):
        return [
            Movement(
                register=GoodsBalance,
                period=self.date.date() if self.date else date.today(),
                dimensions={"product": self.product, "warehouse": self.warehouse},
                resources={"quantity": self.quantity},
            )
        ]
```

## Валютный учёт

```python
price = Decimal("Цена", precision=10, scale=2, currency="RUB")
```

Отображается как `1 234.50 RUB`.

## Диаграммы в отчётах

```python
from decui.ui.chart import BarChart

chart = BarChart(
    labels=["Янв","Фев","Мар"],
    values=[10, 25, 15],
    title="Продажи по месяцам",
    bar_color="#4A7BFF",
)
layout.addWidget(chart)
```

## Роли и права

```python
from decui.sequence import define_role, has_permission, set_role

define_role("admin", ["full_access"])
define_role("manager", ["edit_price", "view_report"])
define_role("guest", ["view_report"])

set_role("manager")
if has_permission("manager", "edit_price"):
    # разрешено
    pass
```

## Последовательность документов

```python
@document
class Invoice(Document):
    __sequence__ = True  # проверять хронологию при проведении
```

## Версионирование

Автоматически сохраняется история изменений при каждом сохранении сущности.
