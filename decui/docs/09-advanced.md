# Продвинутые возможности

Маршруты согласования, права доступа, локальные учётные записи, печатные формы,
последовательность документов и миграция схемы.

---

## Роли, права доступа и учётные записи

### Контекстная роль

```python
from decui import define_role, has_permission, current_role, set_role

define_role("admin", ["full_access"])
define_role("manager", ["edit_price", "view_report"])

set_role("manager")
if has_permission(current_role(), "edit_price"):
    ...
```

### Локальные учётные записи и матрица прав

Передайте `local_accounts=True` в `ERPApplication`, чтобы при первом запуске
создать администратора и требовать вход по логину/паролю. На уровне сущности
можно ограничить доступ:

- `__permissions__ = {"read": ("manager", "admin"), "create": ("manager", "admin"), "update": ("admin",), "delete": ("admin",), "post": ("admin",)}`
- `__owner_field__ = "owner"` — строковое поле владельца; доступ к записи остаётся у её создателя и роли `admin`
- `__field_read_roles__ = {"cost": ("admin",)}` — скрытие реквизитов или колонок (`"Items.cost"`) от остальных ролей
- `__field_write_roles__ = {"discount": ("manager", "admin")}` — ограничение редактирования поля или колонки
- `__command_roles__ = {"approve": ("admin",)}` — ограничение кнопок `@action`/`@command`

Проверить права программно можно через `db.allows(cls, action)`,
`db.can_read_field(cls, field)` и `db.can_edit_field(cls, field)`.

---

## Маршруты согласования (`Workflow`)

```python
from decui import Document, String, Transition, Workflow, document

@document
class Invoice(Document):
    status = String("Статус", default="Черновик", read_only=True)
    approval_assignee = String("Согласующий")

    __workflow__ = Workflow(
        field="status",
        initial="Черновик",
        states=("Черновик", "На согласовании", "Утверждён"),
        transitions=(
            Transition("Отправить", "Черновик", "На согласовании", roles=("manager", "admin")),
            Transition("Утвердить", "На согласовании", "Утверждён", roles=("admin",), assigned_only=True),
        ),
        post_states=("Утверждён",),
    )
```

- `db.transition(entity, "Отправить")` проверяет допустимость перехода, роль и (при `assigned_only=True`) совпадение вошедшего логина с `approval_assignee`, сохраняет изменение и записывает событие в историю.
- `db.workflow_history(entity)` возвращает список `WorkflowEvent(occurred_at, source, target, action, role, login)`.
- Параметр `post_states` запрещает проведение документа, пока его статус не входит в разрешённый список.

---

## Печатные формы (`PrintForm`)

```python
from decui import PrintForm, PrintTable

class Invoice(Document):
    ...
    __print_form__ = PrintForm(
        title="Счёт на оплату",
        fields=("number", "date", "customer"),
        tables=(PrintTable(part="Items", columns=("product", "quantity", "amount")),),
    )
```

Для сохранённой записи в карточке появляется кнопка «Печатная форма»
с просмотром HTML и экспортом в PDF. Печать читает актуальный снимок из БД
и учитывает `__field_read_roles__`.

---

## Автоматическая миграция схемы

Новые таблицы и столбцы добавляются при запуске. Для переименования
столбца или табличной части передайте `column_renames` или `table_part_renames`
в `ERPApplication` либо в `Database`. Ключ в `column_renames` обозначает
класс сущности или табличную часть, например `"Product"` или
`"PurchaseOrder.Items"`. Вложенный словарь задаёт пары старых и новых имён.

Если в классе `Product` поле `name` стало `title`, используйте один словарь
для запуска и проверки. `decui check` читает из точки входа
`_DECUI_COLUMN_RENAMES` и `_DECUI_TABLE_PART_RENAMES`. Studio задаёт их при
экспорте проекта с переименованиями.

```python
from decui import ERPApplication

_DECUI_COLUMN_RENAMES = {"Product": {"name": "title"}}

if __name__ == "__main__":
    app = ERPApplication(app_id="warehouse", column_renames=_DECUI_COLUMN_RENAMES)
    raise SystemExit(app.run())
```

Удаление столбца из существующей таблицы требует отдельной миграции.
Перед обновлением [проверьте схему на резервной копии](07-building.md#репетиция-обновления).

---

## Последовательность документов

```python
@document
class Invoice(Document):
    __sequence__ = True  # проверять хронологию при проведении
```

Проверка: если есть проведённый документ с более поздней датой,
проведение текущего блокируется (`SequenceViolationError`).

---

## Версионирование

Зарезервировано. В текущей версии не реализовано.

---

## Связанные документы

| Раздел | Документ |
|---|---|
| Регистры | [09-registers.md](09-registers.md) |
| Отчёты | [10-reports.md](10-reports.md) |
