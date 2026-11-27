"""Отчёт с фильтром по справочнику (RefParam).

Запуск:
    cd F:/projects/decui
    python examples/features/report_refparam.py

В разделе «Отчёты» откройте «Сотрудники», выберите сотрудника и нажмите F5.
После смены фильтра снова нажмите F5, чтобы обновить таблицу и выгрузить CSV
с выбранным сотрудником.
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from decui import catalog, report, Catalog, String, Report, Column, RefParam


@catalog
class Employee(Catalog):
    __numbering__    = {"prefix": "EMP", "field": "code"}
    __verbose_name__ = "Сотрудники"
    code = String("Код", read_only=True)
    name = String("ФИО")


@report
class EmployeeReport(Report):
    title = "Сотрудники"
    columns = [
        Column("Код", "code", width=100),
        Column("ФИО", "name", width=250),
    ]
    employee = RefParam("Сотрудник", Employee, representation_field="name")

    def execute(self, db, employee, **kw):
        if employee:
            emp = db.get(Employee, employee)
            if emp:
                return [{"code": emp.code or "", "name": emp.name or ""}]
        return [{"code": e.code or "", "name": e.name or ""}
                for e in db.list(Employee)]


def _seed(db):
    if db.list(Employee):
        return
    db.save(Employee(name="Иванова Мария"))
    db.save(Employee(name="Петров Дмитрий"))
    emps = db.list(Employee)
    print(f"  Сотрудники: {emps[0].code}, {emps[1].code}")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="refparam.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
