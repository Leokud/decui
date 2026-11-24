# DECUI — Декларативный ERP-фреймворк для Python

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![PySide6](https://img.shields.io/badge/GUI-PySide6%20%28Qt6%29-green.svg)](https://wiki.qt.io/Qt_for_Python)
[![SQLAlchemy](https://img.shields.io/badge/ORM-SQLAlchemy%202.0%2B-red.svg)](https://www.sqlalchemy.org/)
[![Tests](https://img.shields.io/badge/Tests-pytest%2023%2F23%20passed-brightgreen.svg)]()
[![Web Tutorial](https://img.shields.io/badge/Web_Tutorial-GitHub_Pages-blueviolet.svg)](https://leokud.github.io/decui/)

**DECUI** — это легковесный декларативный (metadata-driven) Python-фреймворк для создания настольных учетных и ERP-систем.

> 🌐 **Интерактивный веб-учебник:**  
> Пройдите пошаговый курс в браузере: **[leokud.github.io/decui](https://leokud.github.io/decui/)**


Вы описываете **только структуру данных и бизнес-логику в виде Python-классов** — фреймворк автоматически генерирует:
- Таблицы базы данных в SQLite и модели SQLAlchemy
- Оконные формы создания и редактирования с валидацией (`AutoForm`)
- Списки записей с сортировкой и CRUD-операциями (`ListView`)
- Дерево навигации по разделам и модулям (`NavigationPanel`)
- Табличные части / спецификации с автокалькуляцией сумм (`@line_items`)
- Регистры накопления остатков и истории значений (`@ledger`, `@history_register`)
- Параметрические отчёты с автоматическими фильтрами, агрегацией и диаграммами (`@report`)
- Сборку в единый независимый файл `.exe` через PyInstaller

---

## 🎯 Для кого этот фреймворк?

- **Python-разработчикам:** Вам нужно быстро сделать рабочее настольное приложение для учета заказов, склада, клиентов или финансов? Не нужно вручную верстать виджеты Qt, писать SQL-запросы или связывать сигналы форм — фреймворк сделает это сам.
- **Разработчикам 1С и ERP-систем:** Вы найдете знакомые и проверенные концепции (справочники, документы, движения, регистры, проведение), но написанные на чистом, типизированном Python с поддержкой современных библиотек, Git и тестов.

---

## 📚 Терминология: Общепринятые понятия vs 1С

DECUI предоставляет удобные синонимы. Вы можете писать код в терминах классического бэкенда/ERP или в привычных терминах 1С:

| Общепринятый термин (ERP / Backend) | Термин 1С | DECUI API | Назначение |
|---|---|---|---|
| **Master Data / Lookup** | Справочник | `Catalog` / `MasterData`, `@catalog` / `@master_data` | Списки условно-постоянных объектов (товары, клиенты) |
| **Transaction / Document** | Документ | `Document` / `Transaction`, `@document` / `@transaction` | Хозяйственные операции, привязанные ко времени |
| **Line Items / Child Table** | Табличная часть | `@line_items` / `@tab_part` | Строки спецификаций внутри документа |
| **Form Action** | Команда | `@action` / `@command` | Кнопка на форме с привязанной бизнес-логикой |
| **Balance Ledger** | Регистр накопления | `@ledger` / `@accumulation` | Журнал учета остатков и оборотов (склад, касса) |
| **Value History** | Регистр сведений | `@history_register` / `@information` | Хранение параметров во времени (курсы валют, тарифы) |
| **Ledger Entry** | Движение | `LedgerEntry` / `Movement` | Единичная запись изменения баланса (`+` / `-`) |
| **Posting / Approval** | Проведение | `doc.post()`, `doc.submit()`, `doc.is_posted` | Фиксация документа и создание записей в регистрах |
| **Sequence Numbering** | Автонумерация | `__numbering__ = {"prefix": "INV", ...}` | Генерация номеров по шаблону (`INV-2026-000001`) |
| **Navigation Section** | Подсистема | `__section__ = "Склад/Товары"` | Группировка в левом дереве навигации |
| **Report** | Отчёт | `Report` / `QueryReport`, `@report` | Экран параметрического отчёта с агрегацией и графиком |

> Подробное руководство по концепциям учета доступно в [docs/11-concepts-guide.md](decui/docs/11-concepts-guide.md).

---

## ⚡ Быстрый старт: 10 строк кода

Создайте файл `app.py`:

```python
from decui import ERPApplication, MasterData, String, Decimal as D, master_data

@master_data
class Product(MasterData):
    __verbose_name__ = "Товары"
    __section__      = "Склад"

    name  = String("Наименование", required=True)
    price = D("Цена", precision=10, scale=2, min_value=0)

if __name__ == "__main__":
    app = ERPApplication(title="Учет товаров", db_path="warehouse.db")
    app.run()
```

Запустите:
```bash
python app.py
```

Фреймворк автоматически создаст таблицу SQLite, откроет главное окно, сформирует дерево меню «Склад → Товары», таблицу просмотра со списком и диалоговые окна создания и редактирования товаров.

---

## 🛠 Пример с транзакциями, строками и регистрами

```python
from decimal import Decimal
from decui import (
    ERPApplication, MasterData, Transaction, String, Integer, Decimal as D, Ref,
    master_data, transaction, line_items, action, ledger, Ledger, Dimension, Metric, LedgerEntry
)

# 1. Справочник товаров
@master_data
class Item(MasterData):
    name = String("Товар", required=True)
    price = D("Цена", precision=10, scale=2, default=Decimal("0.00"))

# 2. Регистр остатков склада
@ledger
class StockLedger(Ledger):
    product = Dimension("Товар")
    qty = Metric("Количество")

# 3. Документ продажи
@transaction
class SaleOrder(Transaction):
    __verbose_name__ = "Заказы покупателей"
    __section__ = "Продажи"

    customer = String("Покупатель", required=True)
    total = D("Итого", precision=10, scale=2, read_only=True, default=Decimal("0.00"))

    # Табличная часть со строками заказа
    @line_items
    class Items:
        product  = Ref(Item, "Товар", representation_field="name")
        quantity = Integer("Кол-во", default=1, min_value=1)
        price    = D("Цена", precision=10, scale=2)
        amount   = D("Сумма", precision=10, scale=2, read_only=True)

    # Кнопка пересчета суммы на форме
    @action("Пересчитать")
    def calc(self):
        self.total = sum(
            Decimal(str(r.get("quantity", 1))) * Decimal(str(r.get("price", 0)))
            for r in self._table_data.get("Items", [])
        )

    # Движения по складу при проведении заказа (списание товара)
    def get_ledger_entries(self):
        return [
            LedgerEntry(
                register=StockLedger,
                dimensions={"product": row["product"]},
                resources={"qty": row["quantity"]},
                is_income=False,  # списание со склада
            )
            for row in self._table_data.get("Items", [])
        ]
```

---

## 🚀 CLI-утилита `decui` и эргономика (DX)

Фреймворк включает CLI и инструменты для максимального удобства разработки:

```bash
# 1. Создать новый проект из шаблона
decui new my_erp

# 2. Запустить приложение
decui run

# 3. Запустить тесты
decui test

# 4. Собрать в исполняемый .exe файл
decui build
```

### Ключевые возможности DX:
- **Авто-названия полей (Auto-naming):** `name = String(required=True)` вместо дублирования `String("Name")`.
- **Коллекция спецификаций:** `doc.Items.add(...)` и `doc.Items.total("amount")` без ручных циклов.
- **Active Record:** `product.save(db)`, `product.delete(db)`, `Customer.filter(db, is_active=True)`.
- **Сериализация:** `entity.to_dict()` и `Entity.from_dict(data)` для легкого обмена данными.
- **Тестовые хелперы:** `Database.memory()` и `db.seed([e1, e2])`.

> Подробное описание всех возможностей доступно в [docs/12-dx-guide.md](decui/docs/12-dx-guide.md).

---

## 📦 Сборка в автономный .exe

Вы можете собрать готовый исполняемый файл без консольного окна, который можно запустить на любом компьютере без установки Python:

```bash
python build_exe.py
# Готовый бинарник будет сохранен в: dist/ERP (или dist/ERP.exe на Windows)
```

---

## 🧪 Запуск тестов

В проект включен набор тестов на базе `pytest`:

```bash
uv run pytest -v
```

Все тесты выполняются в Qt offscreen-режиме и покрывают ORM, генерацию моделей, валидацию полей, автонумерацию, расчет остатков и сборку интерфейса.

---

## 📖 Полная документация

| Документ | Описание |
|---|---|
| [**Интерактивный Веб-учебник**](https://leokud.github.io/decui/) | **Пошаговое интерактивное руководство в браузере (GitHub Pages)** |
| [12-dx-guide.md](decui/docs/12-dx-guide.md) | **Руководство по Developer Experience: CLI, авто-поля, Active Record** |
| [11-concepts-guide.md](decui/docs/11-concepts-guide.md) | **Концептуальное руководство: архитектура учета простыми словами** |
| [01-quickstart.md](decui/docs/01-quickstart.md) | Пошаговый старт, установка и первые шаги |
| [02-entities.md](decui/docs/02-entities.md) | Сущности (Master Data, Transaction), типы полей, валидация |
| [03-table-parts.md](decui/docs/03-table-parts.md) | Табличные части и спецификации строк (`@line_items`) |
| [04-commands.md](decui/docs/04-commands.md) | Действия и кнопки на форме (`@action` / `@command`) |
| [05-reports.md](decui/docs/05-reports.md) | Построение отчётов, параметры, форматирование |
| [06-numbering.md](decui/docs/06-numbering.md) | Автонумерация и шаблоны номеров |
| [07-building.md](decui/docs/07-building.md) | Сборка standalone .exe через PyInstaller и авто-миграции |
| [08-api.md](decui/docs/08-api.md) | Полный справочник всех классов, методов и функций API |
| [09-registers.md](decui/docs/09-registers.md) | Регистры накопления, регистры сведений и работа с остатками |
| [10-reports.md](decui/docs/10-reports.md) | Расширенные отчёты, агрегации и столбчатые диаграммы (`BarChart`) |
| [09-advanced.md](decui/docs/09-advanced.md) | Роли, права, последовательность проведения |

---

## 📄 Лицензия

MIT License.
