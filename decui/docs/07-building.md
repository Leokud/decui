# Сборка .exe

## Быстрая сборка

```bash
pip install pyinstaller
python build_exe.py
# Результат: dist/ERP/ERP.exe  (57 МБ, single-file)
```

## Структура проекта для сборки

```
my_app/
├── main.py          # Точка входа
├── entities.py      # Справочники и документы
├── reports.py       # Отчёты
└── build_exe.py     # Скрипт сборки
```

```python
# main.py
from decui.app import ERPApplication
import entities
import reports

app = ERPApplication(db_path="data.db")
app.run()
```

```bash
python build_exe.py
dist/my_app/my_app.exe
```

## Параметры сборки

В `build_exe.py` можно менять:

- `--windowed` → `--console` — показать окно консоли (для отладки)
- `--onefile` → `--onedir` — папка вместо одного файла
- `--name "МояERP"` — имя файла

## Зависимости в .exe

- PySide6 (Qt — 45+ МБ)
- SQLAlchemy
- sqlite3 (встроен в Python)

## При первом запуске

.exe создаёт `erp_demo.db` в своей папке. Если структура сущностей изменилась
(добавлены/удалены поля), фреймворк автоматически пересоздаёт таблицы —
можно не удалять БД вручную.

Старые данные при этом теряются (схема хешируется, при несовпадении — DROP ALL).
