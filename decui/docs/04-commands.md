# Команды

Команды — методы, которые становятся кнопками на форме документа.

## Определение

```python
@command("Рассчитать")
def calc(self):
    total = Decimal("0")
    for row in self._table_data.get("Items", []):
        row["amount"] = Decimal(str(row["quantity"])) * row["price"]
        total += row["amount"]
    self._data["total"] = total

@command("Провести")
def post(self):
    self.calc()
    self._data["posted"] = True

@command("Снять проведение")
def unpost(self):
    self._data["posted"] = False
```

## Как это работает

1. Пользователь нажимает кнопку на форме
2. Фреймворк копирует данные из виджетов в сущность
3. Выполняется метод команды
4. **Документ автоматически сохраняется** в БД
5. Виджеты на форме обновляются

## Доступные объекты внутри команды

| Выражение | Что даёт |
|---|---|
| `self._data["name"]` | Значение поля документа |
| `self._data["name"] = x` | Установить поле документа |
| `self._table_data["Items"]` | Список строк табличной части |
| `self._table_data["Items"][0]["amount"]` | Поле первой строки |
| `self.date`, `self.number` | Поля через __getattr__ (только чтение) |

## Команды сохраняют документ

После выполнения любой команды фреймворк вызывает `db.save(self._entity)`. Отдельно нажимать «Сохранить» не нужно.
