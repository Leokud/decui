# Отчёты

Система декларативных отчётов с автогенерированной формой параметров,
агрегацией, форматированием и итогами.

---

## Определение

```python
from decui import report, Report, Column, DateParam, RefParam

@report
class SalesReport(Report):
    title = "Продажи по товарам"
    columns = [
        Column("Товар",  "product",   width=200),
        Column("Кол-во", "qty",       width=80,  align="right"),
        Column("Сумма",  "total",     width=120, align="right",
               aggregate="sum", format="{:,.2f}", suffix=" руб."),
    ]

    date_from = DateParam("Дата с", required=True,
                          default=lambda: date(2020, 1, 1))
    date_to   = DateParam("Дата по", required=True,
                          default=lambda: date(2030, 12, 31))
    product   = RefParam("Товар", Product, representation_field="name")

    def execute(self, db, date_from, date_to, product=None, **kw):
        invoices = db.filter(
            Invoice,
            posted=True,
            date__date_range=(date_from, date_to),
            include_table_parts=True,
        )
        products = {p.id: p.name for p in db.list(Product)}
        rows = []
        for inv in invoices:
            for item in inv.Items:
                if product and item.get("product") != product:
                    continue
                rows.append({
                    "product": products.get(item.get("product"), "-"),
                    "qty": item.get("quantity", 0),
                    "total": item.get("amount", 0),
                    "_entity_class": Invoice,
                    "_entity_id": inv.id,
                })
        return rows
```

---

## Параметры отчёта

Параметры автоматически превращаются в поля на форме отчёта.

| Дескриптор | Виджет | Параметры |
|---|---|---|
| `DateParam("Метка", required=True)` | QDateEdit | `default=lambda: date(2020,1,1)` |
| `RefParam("Метка", Entity, ...)` | QComboBox | `representation_field="name"`, `from_record=True` |
| `StringParam("Метка")` | QLineEdit | `required`, `default` |
| `IntegerParam("Метка")` | QSpinBox | `min_value`, `max_value`, `default` |
| `DecimalParam("Метка")` | QDoubleSpinBox | `precision`, `scale`, `default` |
| `BooleanParam("Метка")` | QCheckBox | `default` |
| `ChoiceParam("Метка", choices=[...])` | QComboBox | `choices`, `default` |

### DateParam

```python
date_from = DateParam("Дата с", required=True,
                      default=lambda: date(2020, 1, 1))
```

Если `default` не указан — используется сегодняшняя дата. Широкий диапазон
по умолчанию (2020–2030) гарантирует, что данные не будут отсечены.

### RefParam

```python
product = RefParam("Товар", Product, representation_field="name")
```

Если параметр не обязательный (`required=False`), его можно оставить пустым
для выборки по всем значениям.

С `from_record=True` отчёт появляется в меню «Отчёты» сохранённой карточки
товара и открывается с заполненным фильтром. [Пример с поставщиком](05-reports.md#отчёт-из-карточки)

---

## Колонки (Column)

```python
Column(
    title="Сумма",        # Заголовок в таблице
    field="total",        # Ключ в dict из execute()
    width=120,            # Ширина в пикселях
    align="right",        # left / right / center
    aggregate="sum",      # sum / avg / count / min / max
    format="{:,.2f}",     # Python format string
    prefix="$",           # Текст перед значением
    suffix=" руб.",       # Текст после значения
)
```

### Параметры

| Параметр | Тип | По умолчанию | Описание |
|---|---|---|---|
| `title` | str | — | Заголовок колонки |
| `field` | str | — | Ключ в dict из `execute()` |
| `width` | int | 150 | Ширина колонки |
| `align` | str | `"left"` | Выравнивание текста |
| `aggregate` | str | None | `"sum"`, `"avg"`, `"count"`, `"min"`, `"max"` |
| `format` | str | None | Формат, например `"{:,.2f}"` |
| `prefix` | str | `""` | Текст перед значением |
| `suffix` | str | `""` | Текст после значения |

### Агрегация

| Значение | Описание | Пример |
|---|---|---|
| `"sum"` | Сумма | 100 + 200 = 300 |
| `"avg"` | Среднее | (100 + 200) / 2 = 150 |
| `"count"` | Количество | 2 |
| `"min"` | Минимум | 100 |
| `"max"` | Максимум | 200 |

### Форматирование

```python
Column("Цена", "price", format="{:,.2f}", suffix=" руб.")
# 1234.5 → "1,234.50 руб."

Column("Доля", "share", format="{:.1%}", prefix="~")
# 0.35 → "~35.0%"
```

---

## Метод execute() и группировка

```python
def execute(self, db, date_from, date_to, product=None, **kw):
    invoices = db.filter(
        Invoice,
        posted=True,
        date__date_range=(date_from, date_to),
        include_table_parts=True,
    )
    products = {p.id: p.name for p in db.list(Product)}
    rows = []
    for inv in invoices:
        for item in inv.Items:
            if product and item.get("product") != product:
                continue
            rows.append({
                "product": products.get(item.get("product"), "-"),
                "qty": item.get("quantity", 0),
                "total": item.get("amount", 0),
            })
    return rows
```

### Правила

- Принимает `db` и именованные параметры (совпадают с именами полей параметров)
- Возвращает `list[dict]` (для отчётов по регистрам можно использовать `register_report_rows(...)`)
- Ключи dict должны совпадать с `field` в `Column`
- Фильтровать только проведённые документы: `posted=True` в `db.filter(...)`
- Неиспользуемые параметры принимаются через `**kw`
- Для вложенных подытогов в классе отчёта задайте `group_by = ("category",)` или настройте группировку прямо в окне отчёта (варианты и порядок колонок сохраняются в БД для каждой учётной записи, доступен экспорт в CSV).
- Управляющие ключи в словаре строки: `_entity_class` + `_entity_id` (открытие карточки по двойному клику), `_style` (`"completed"`, `"current"`), `_color`, `_bold`.

---

## Панель сводки

После формирования отчёта над таблицей показывается панель:

```
Всего строк: 15 | Сумма: 54 321 руб. | Среднее: 3 621 руб.
```

Автоматически рассчитывается для колонок с `aggregate`.

---

## Строка «Итого»

Автоматически добавляется внизу таблицы для колонок с `aggregate`.
Значения форматируются с учётом `format`/`prefix`/`suffix`.

```
| Товар   | Кол-во | Сумма      |
|---------|--------|------------|
| Кофе    | 10     | 8 500 руб. |
| Чай     | 20     | 6 000 руб. |
| Итого:  | 30     | 14 500 руб.|  ← жирным шрифтом
```

---

## Столбчатые диаграммы

```python
from decui.ui.chart import BarChart

chart = BarChart(
    labels=["Янв", "Фев", "Мар"],
    values=[10, 25, 15],
    title="Продажи по месяцам",
    bar_color="#4A7BFF",
)
layout.addWidget(chart)
```

Параметры:

| Параметр | По умолчанию | Описание |
|---|---|---|
| `labels` | `[]` | Подписи под столбцами |
| `values` | `[]` | Высота столбцов |
| `title` | `""` | Заголовок над диаграммой |
| `bar_color` | `"#4A7BFF"` | Цвет столбцов (HEX) |

Минимальная высота виджета: 120px.

---

## Подсистемы для отчётов

```python
@report
class MyReport(Report):
    __subsystem__ = "Отчёты/Продажи"  # вложенная группировка в навигации
    title = "Продажи"
```

---

## Все примеры

| Файл | Описание |
|---|---|
| `examples/features/report_aggregate.py` | Агрегация + формат + suffix |
| `examples/features/report_refparam.py` | Фильтр по справочнику |
| `examples/features/subsystem.py` | Подсистемы + отчёт |
| `examples/simple.py` | Товары/счета + отчёт продаж |
| `examples/vunderkind.py` | Расписание преподавателей + отчёт |
