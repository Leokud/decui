# Бизнес-логика, действия (@action) и валидация

В DECUI бизнес-логика описывается декларативно прямо внутри классов сущностей на чистом Python.

---

## 1. Действия и кнопки на форме (@action / @command)

Любой метод сущности, помеченный декоратором `@action("Текст кнопки")` (или `@command`), автоматически становится кнопкой в форме редактирования.

### Пример: калькуляция строк и управление статусами

```python
from decimal import Decimal
from decui import transaction, Transaction, String, Decimal as D, Integer, Ref, line_items, action

@transaction
class SalesOrder(Transaction):
    customer = String(required=True)
    total    = D(scale=2, read_only=True)
    status   = String(default="Черновик")

    @line_items
    class Items:
        product  = Ref(Product, required=True)
        quantity = Integer(default=1, min_value=1)
        price    = D(scale=2)
        amount   = D(scale=2, read_only=True)

    # 1. Кнопка расчёта сумм
    @action("Рассчитать суммы")
    def calculate_totals(self):
        for row in self.Items:
            qty = Decimal(str(row.get("quantity") or 0))
            price = Decimal(str(row.get("price") or 0))
            row["amount"] = qty * price
        
        # Подсчёт общего итога через встроенный метод total()
        self.total = self.Items.total("amount")

    # 2. Кнопка согласования / проведения
    @action("Утвердить заказ")
    def approve(self):
        self.calculate_totals()
        self.status = "Согласован"
        self.post() # стандартный метод проведения документа
```

### Как это работает:
1. Пользователь нажимает кнопку на форме.
2. Фреймворк автоматически считывает все введённые пользователем значения из виджетов в объект.
3. Выполняется метод действия.
4. **Объект автоматически сохраняется в базу данных (`db.save`)**.
5. Форма автоматически обновляет все поля и строки спецификаций на экране.

---

## 2. Проверка бизнес-правил и валидация

Вы можете выполнять валидацию данных прямо внутри методов или переопределив метод проверки. Если условие нарушено, достаточно выбросить исключение или вернуть предупреждение:

```python
import datetime

class GoalTask(MasterData):
    name = String(required=True)
    deadline = DateTime(required=True)
    is_completed = Boolean(default=False)

    @action("Завершить задачу")
    def complete_task(self):
        # Бизнес-проверка: нельзя закрыть задачу, если срок не наступил
        if self.deadline > datetime.datetime.now() + datetime.timedelta(days=30):
            raise ValueError("Нельзя досрочно закрывать задачу с дедлайном более 30 дней!")

        self.is_completed = True
```

---

## 3. Работа с датами и временем в бизнес-логике

DECUI полностью поддерживает работу с модулем стандартной библиотеки `datetime`:

| Дескриптор | Тип Python | Виджет в UI | Описание |
|---|---|---|---|
| `Date` | `datetime.date` | `QDateEdit` | Календарная дата со всплывающим календарём |
| `DateTime` | `datetime.datetime` | `QDateTimeEdit` | Дата и точное время (часы, минуты) |
| `Time` | `datetime.time` | `QTimeEdit` | Время суток (ЧЧ:ММ) |

### Вычисления со сроками и дедлайнами:
```python
import datetime

@action("Продлить на неделю")
def extend_deadline(self):
    current = self.deadline or datetime.datetime.now()
    # Стандартная арифметика дат Python
    self.deadline = current + datetime.timedelta(days=7)
```

---

## 4. Доступные свойства и методы внутри сущности

| Свойство / Метод | Описание |
|---|---|
| `self.field_name` | Чтение и запись значений полей |
| `self.Items` | Коллекция строк спецификации (`LineItemsCollection`) |
| `self.Items.add(...)` | Добавить новую строку в спецификацию |
| `self.Items.total("amount")` | Сумма по указанной колонке |
| `self.post()` / `self.unpost()` | Изменение статуса проведения документа |
| `self.is_posted` | Проверка, проведён ли документ |
| `self.to_dict()` | Преобразование объекта в стандартный словарь |

