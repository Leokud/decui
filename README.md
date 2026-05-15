# DECUI — Декларативный ERP-фреймворк

Python 3.12+ · PySide6 · SQLAlchemy · SQLite

Пользователь пишет **только метаданные и бизнес-логику** — фреймворк сам генерирует
SQL-модели, формы, списки, навигацию, табличные части и отчёты.

## Быстрый старт

```bash
pip install PySide6 sqlalchemy pyinstaller
cd decui

# Демо-пример: товары + счёт с табличной частью
python examples/simple.py

# Пример: центр развития детей (возрасты, кружки, расписание)
python examples/vunderkind.py
```

Сборка .exe:
```bash
python build_exe.py
# dist/ERP/ERP.exe  (single-file, без консоли)
```

## 10 строк — и всё готово

```python
from decui import catalog, Catalog, String, Decimal as D

@catalog
class Product(Catalog):
    name  = String("Наименование", required=True)
    price = D("Цена", precision=10, scale=2, required=True)
```

Фреймворк сам создаёт: таблицу в SQLite, форму для ввода, список для просмотра,
пункт в навигации, кнопки Сохранить / Закрыть.

## Возможности

| Возможность | Статус | Документация |
|---|---|---|
| Справочники (Catalog) | ✅ | [docs/02-entities.md](decui/docs/02-entities.md) |
| Документы (Document) | ✅ | [docs/02-entities.md](decui/docs/02-entities.md) |
| Табличные части (@tab_part) | ✅ | [docs/03-table-parts.md](decui/docs/03-table-parts.md) |
| Команды (@command) | ✅ | [docs/04-commands.md](decui/docs/04-commands.md) |
| Отчёты с параметрами (@report) | ✅ | [docs/05-reports.md](decui/docs/05-reports.md) |
| Автонумерация (1C-формат) | ✅ | [docs/06-numbering.md](decui/docs/06-numbering.md) |
| Сборка .exe (PyInstaller) | ✅ | [docs/07-building.md](decui/docs/07-building.md) |
| Регистры накопления | ✅ | [docs/09-advanced.md](decui/docs/09-advanced.md) |
| Регистры сведений | ✅ | [docs/09-advanced.md](decui/docs/09-advanced.md) |
| Движения по регистрам | ✅ | [docs/09-advanced.md](decui/docs/09-advanced.md) |
| Столбчатые диаграммы | ✅ | [docs/09-advanced.md](decui/docs/09-advanced.md) |
| Валютный учёт (currency) | ✅ | [docs/09-advanced.md](decui/docs/09-advanced.md) |
| Роли и права | ✅ | [docs/09-advanced.md](decui/docs/09-advanced.md) |
| Последовательность док-тов | ✅ | [docs/09-advanced.md](decui/docs/09-advanced.md) |
| Авто-миграция схемы БД | ✅ | [docs/07-building.md](decui/docs/07-building.md) |

## Архитектура

```
┌─────────────┐    ┌──────────────┐    ┌───────────────┐
│  Метаданные │───▶│  EntityMeta  │───▶│   Metadata    │
│  (классы)   │    │  (метакласс) │    │  (поля, тч,   │
│             │    │              │    │   команды)    │
└─────────────┘    └──────────────┘    └───────┬───────┘
                                              │
                    ┌─────────────────────────┼──────────┐
                    ▼                         ▼          ▼
            ┌──────────────┐         ┌────────────┐ ┌────────┐
            │  SQLAlchemy  │         │   AutoForm │ │ListView│
            │  (CREATE     │         │   (форма)  │ │(список)│
            │   TABLE)     │         └────────────┘ └────────┘
            └──────────────┘
```

## Принципы

- **Metadata-first** — класс описывает структуру, фреймворк строит UI
- **Чистый Qt** (QWidget), без QML и web-технологий
- **Композиция, не наследование** — каждый компонент заменяем
- **Без eval/exec** — статический анализ, PyInstaller без проблем
- **Без XML-форм** — только Python
- **Авто-миграция** — при изменении схемы данных таблицы пересоздаются

## Все типы полей

| Тип | Qt-виджет | Параметры |
|---|---|---|
| `String("Имя")` | QLineEdit | `required`, `max_length` |
| `Integer("Кол-во")` | QSpinBox | `min_value`, `max_value`, `default` |
| `Decimal("Цена")` | QDoubleSpinBox | `precision`, `scale`, `min_value`, `max_value`, `currency` |
| `Boolean("Флаг")` | QCheckBox | — |
| `Date("Дата")` | QDateEdit | `default` |
| `DateTime("Момент")` | QDateTimeEdit | `default` |
| `Time("Время")` | QTimeEdit | `default` |
| `Choice("Дни")` | QGroupBox+QCheckBox | `choices`, `separator` |
| `Ref(Entity, "Метка")` | QComboBox | `representation_field`, `auto_fill` |

## Полная документация

| Файл | Содержание |
|---|---|
| [docs/01-quickstart.md](decui/docs/01-quickstart.md) | Установка, запуск, примеры |
| [docs/02-entities.md](decui/docs/02-entities.md) | Справочники, документы, поля, разделы |
| [docs/03-table-parts.md](decui/docs/03-table-parts.md) | Табличные части |
| [docs/04-commands.md](decui/docs/04-commands.md) | Команды, бизнес-логика |
| [docs/05-reports.md](decui/docs/05-reports.md) | Отчёты, агрегация, форматирование |
| [docs/06-numbering.md](decui/docs/06-numbering.md) | Автонумерация (1C-формат) |
| [docs/07-building.md](decui/docs/07-building.md) | Сборка .exe, авто-миграция |
| [docs/08-api.md](decui/docs/08-api.md) | Полный справочник API |
| [docs/09-registers.md](decui/docs/09-registers.md) | Регистры накопления и сведений |
| [docs/10-reports.md](decui/docs/10-reports.md) | Отчёты: параметры, агрегация, формат, диаграммы |
| [docs/09-advanced.md](decui/docs/09-advanced.md) | Роли, миграция, последовательность |

## Для AI-ассистентов

`AGENTS.md` — полный контекст для LLM: архитектура, соглашения, частые ошибки.
`.clinerules` — правила для код-ревью и генерации кода.
