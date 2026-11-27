# Отчёты

Отчёты — декларативные запросы с автогенерированной формой параметров.

## Отчёт по строкам за период

`DateParam` передаёт в `execute` объекты `datetime.date`.
`date__date_range=(date_from, date_to)` отбирает заказы в базе и включает
оба граничных дня целиком. Если начало позже окончания, форма показывает
сообщение об ошибке. С `include_table_parts=True` строки всех выбранных
заказов загружаются пакетно. При пустом результате `execute` возвращает
пустой список.

```python
@report
class PurchaseReport(Report):
    title = "Закупки по строкам"
    auto_run = True
    supplier = RefParam("Поставщик", Supplier, from_record=True)
    date_from = DateParam("С", default=date(2026, 11, 1))
    date_to = DateParam("По", default=date(2026, 11, 30))
    columns = [
        Column("Заказ", "number"),
        Column("Товар", "product"),
        Column("Сумма", "amount", aggregate="sum", format="{:,.2f}"),
    ]

    def execute(
        self, db: Database, *, date_from: date, date_to: date, supplier: int | None = None,
    ) -> list[dict]:
        rows = []
        filters = {"supplier": supplier} if supplier is not None else {}
        orders = db.filter(
            PurchaseOrder,
            date__date_range=(date_from, date_to),
            include_table_parts=True,
            **filters,
        )
        products = {product.id: product.name for product in db.list(Product)}
        for order in orders:
            for item in order.Items:
                rows.append(dict(
                    number=order.number,
                    product=products.get(item["product"], ""),
                    amount=item["amount"],
                    _entity_class=PurchaseOrder,
                    _entity_id=order.id,
                ))
        return rows
```

[Полный пример с двумя заказами](../../examples/features/purchase_orders.py)

## Отчёт из карточки

`supplier = RefParam("Поставщик", Supplier, from_record=True)` добавляет отчёт
в меню «Отчёты» сохранённой карточки `Supplier`. При открытии из карточки
параметр получает её идентификатор, а редактирование карточки продолжается
после закрытия отчёта. Фильтр можно поменять в самом отчёте.

При `auto_run = True` отчёт формируется сразу после заполнения параметров.
В `execute` используйте полученное значение при запросе. При открытии из
общей навигации действуют обычные значения по умолчанию.

Чтобы двойной щелчок по строке открывал связанную карточку, верните в
словаре строки `_entity_class` с классом записи и `_entity_id` с её
идентификатором.

## Параметры отчёта

| Дескриптор | Виджет | Параметры |
|---|---|---|
| `DateParam("Метка", required=True)` | QDateEdit | `default=lambda: date(2020,1,1)` |
| `RefParam("Метка", Entity)` | QComboBox | `representation_field="name"`, `from_record=True` |
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
- Ключи dict должны совпадать с `key` в `Column`
- Кнопка «Сформировать» и клавиша `F5` запускают отчёт.
- После изменения параметров прежний результат остаётся на экране с
  сообщением «Параметры изменены». CSV снова доступен после успешного
  формирования с новыми параметрами или после возврата к применённым значениям.
- При пустой выборке отчёт показывает «Нет данных по применённым параметрам».
  В отчёте без параметров появится сообщение «В отчёте пока нет данных».

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
