"""Подсистемы (__subsystem__) — вложенная навигация.

Запуск:
    cd F:/projects/decui
    python examples/features/subsystem.py
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from decui import catalog, report, Catalog, Report, String, Column


@catalog
class AgeInterval(Catalog):
    __subsystem__    = "Расписание/Справочники"
    __verbose_name__ = "Возрасты"
    name = String("Название", required=True)


@catalog
class Teacher(Catalog):
    __subsystem__    = "Расписание/Справочники"
    __verbose_name__ = "Преподаватели"
    name = String("ФИО", required=True)


@catalog
class Circle(Catalog):
    __subsystem__    = "Расписание/Кружки"
    __verbose_name__ = "Кружки"
    name = String("Название", required=True)


@report
class Schedule(Report):
    __subsystem__ = "Расписание/Отчёты"
    title = "Расписание"
    columns = [Column("Название", "name")]

    def execute(self, db, **kw):
        return [{"name": c.name} for c in db.list(Circle)]


def _seed(db):
    if db.list(AgeInterval):
        return
    db.save(AgeInterval(name="3-5 лет"))
    db.save(AgeInterval(name="5-7 лет"))
    db.save(Teacher(name="Иванова"))
    db.save(Circle(name="Рисование"))
    db.save(Circle(name="Английский"))
    print("  Возрасты: 2, Преподаватели: 1, Кружки: 2")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="subsystem.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
