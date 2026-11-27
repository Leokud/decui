# Табличные части

Табличная часть (line items) — строки внутри документа.

## Определение

```python
@tab_part
class Items:
    product  = Ref(Product, "Товар", required=True,
                   representation_field="name")
    quantity = Integer("Количество", required=True, default=1)
    price    = D("Цена", precision=10, scale=2, required=True)
    amount   = D("Сумма", precision=10, scale=2,
                 read_only=True, default=0)
```

## Как это работает

- Вложенный класс с декоратором `@tab_part`
- Поля — те же типы, что у справочников/документов
- В форме отображается как таблица с кнопками «+ Добавить строку / − Удалить»
- В БД — отдельная таблица `{родитель}_{имя_тч}` с FK + CASCADE DELETE
- Каждое поле — колонка, каждая строка — одна запись

## Доступ из кода

`db.list()` и `db.filter()` по умолчанию читают шапки записей. Перед
редактированием, командой или проведением загрузите запись со строками
через `db.get(Класс, id)` либо передайте `include_table_parts=True` в выборку.
Попытка записать одну шапку вызовет `ValueError`.
Чтобы удалить все строки, очистите коллекцию у полностью загруженной записи и сохраните её.

```python
# product — сохранённый товар, order — документ со строками Items.
order.Items.add(product=product.id, quantity=2, price=Decimal("12.50"))
for row in order.Items:
    row["amount"] = row["quantity"] * row["price"]
total = order.Items.total("amount")
```

В [заказе поставщику](../../examples/features/purchase_orders.py)
`order.Items.add(product=product.id, quantity=2)` добавляет строку.
`before_save(db)` перебирает `self.Items` и записывает цену в `unit_price`.
Соглашение для автоподстановки текущей цены в редакторе использует колонку
`price`. В учебном заказе `unit_price` заполняет правило сохранения.
После присвоения сумм `self.Items.total("amount")` даёт итог документа.

## Имена полей в табличной части

Не должны совпадать с именами полей документа-родителя.
