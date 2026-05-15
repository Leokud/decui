# Отчёты

Отчёты — декларативные запросы с автогенерированной формой параметров.

## Определение

```python
from decui import report, Report, Column, DateParam, RefParam

@report
class SalesReport(Report):
    title = "Продажи"
    columns = [
        Column("Дата",   "date",   width=160),
        Column("Товар",  "product", width=200),
        Column("Сумма",  "total",  width=120, align="right"),
    ]

    date_from = DateParam("Дата с", required=True,
                          default=lambda: date(2020, 1, 1))
    date_to   = DateParam("Дата по", required=True,
                          default=lambda: date(2030, 12, 31))

    def execute(self, db, date_from, date_to, **kw):
        rows = []
        for inv in db.list(Invoice):
            rows.append({
                "date":   inv.date.strftime("%d.%m.%Y %H:%M"),
                "product": db.get(Product, inv.customer).name,
                "total":  str(inv.total or "0"),
            })
        return rows
```

## Параметры отчёта

| Дескриптор | Виджет | Параметры |
|---|---|---|
| `DateParam("Метка", required=True)` | QDateEdit | `default=lambda: date(2020,1,1)` |
| `RefParam("Метка", Entity)` | QComboBox | `representation_field="name"` |
| `StringParam("Метка")` | QLineEdit | — |

Параметры с `required=True` проверяются перед выполнением.

Значение по умолчанию для `DateParam`:
```python
date_from = DateParam("Дата с", required=True,
                      default=lambda: date(2020, 1, 1))
```

Если default не указан — используется сегодняшняя дата (может отсекать данные).

## Правила

- `execute(db, **params)` возвращает `list[dict]`
- Ключи dict должны совпадать с `field` в `Column`
- Если данных нет — отчёт показывает «Нет данных за выбранный период»
- Кнопка «Сформировать» запускает выполнение

## Агрегация (итоги)

```python
Column("Сумма", "total", width=120, align="right", aggregate="sum")
```

| aggregate | Описание |
|---|---|
| `"sum"` | Сумма |
| `"avg"` | Среднее |
| `"count"` | Количество |
| `"min"` | Минимум |
| `"max"` | Максимум |

Строка «Итого» добавляется автоматически внизу таблицы. Над таблицей — панель
с итоговыми значениями.

## Форматирование значений

```python
Column("Цена", "price", format="{:,.2f}", suffix=" руб.")
Column("Доля", "share", format="{:.1%}", prefix="~")
```

| Параметр | Пример | Результат |
|---|---|---|
| `format="{:,.2f}"` | `1234.5` → `1,234.50` | Тысячные разделители, 2 знака |
| `suffix=" руб."` | `100` → `100 руб.` | Текст после значения |
| `prefix="$"` | `50` → `$50` | Текст перед значением |

Формат применяется к значениям в таблице, к строке «Итого» и к панели сводки.
