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

```python
# Чтение
for row in self._table_data.get("Items", []):
    print(row["product"], row["quantity"], row["price"])

# Запись
self._table_data["Items"] = [
    {"product": 1, "quantity": 10, "price": Decimal("500"), "amount": Decimal("0")},
]

# Добавление строки
self._table_data.setdefault("Items", []).append(
    {"product": 1, "quantity": 1, "price": Decimal("100"), "amount": Decimal("0")}
)
```

## Имена полей в табличной части

Не должны совпадать с именами полей документа-родителя.
