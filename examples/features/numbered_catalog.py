"""Каталог с автонумерацией и ограничениями.

Запуск:
    cd F:/projects/decui
    python examples/features/numbered_catalog.py
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from decui import catalog, Catalog, String, Integer


@catalog
class Employee(Catalog):
    __numbering__    = {"prefix": "EMP", "field": "code"}
    __verbose_name__ = "Сотрудники"

    code = String("Табельный номер", read_only=True)
    name = String("ФИО", required=True)
    age  = Integer("Возраст", min_value=18, max_value=99, default=25)


def _seed(db):
    if db.list(Employee):
        return
    db.save(Employee(name="Иванова Мария", age=32))
    db.save(Employee(name="Петров Дмитрий", age=45))
    emps = db.list(Employee)
    print(f"  Сотрудники: {emps[0].code}, {emps[1].code}")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="numbered.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
