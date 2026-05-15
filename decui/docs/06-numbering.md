# Автонумерация

Формат серийных номеров как в 1С.

## Шаблоны

Доступные переменные в шаблоне:

| Переменная | Значение | Пример |
|---|---|---|
| `{prefix}` | Текстовый префикс | `INV` |
| `{seq:06d}` | Номер с ведущими нулями, 6 разрядов | `000001` |
| `{seq:08d}` | Номер с ведущими нулями, 8 разрядов | `00000001` |
| `{year}` | Год (из даты документа) | `2026` |
| `{month}` | Месяц (из даты документа) | `05` |

## Примеры

```python
@document
class Invoice(Document):
    __numbering__ = {
        "prefix":  "INV",
        "field":   "number",
        "pattern": "{prefix}-{year}-{seq:06d}",   # INV-2026-000001
    }
    number = String("Номер", read_only=True)
```

```python
@catalog
class Product(Catalog):
    __numbering__ = {
        "prefix":  "PRD",
        "field":   "code",
        "pattern": "{prefix}-{seq:08d}",  # PRD-00000001
    }
```

```python
# Чистый номер с годом, без префикса
__numbering__ = {
    "prefix":  "PAY",
    "field":   "number",
    "pattern": "{year}-{month}-{seq:04d}",  # 2026-05-0001
}
```

## По умолчанию

Если `pattern` не указан: `{seq:08d}` → `00000001`.

`field` = `"number"` для документов, `"code"` для справочников.
