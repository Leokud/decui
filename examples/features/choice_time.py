"""Choice (множественный выбор) + Time (время).

Запуск:
    cd F:/projects/decui
    python examples/features/choice_time.py
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from datetime import time
from decui import catalog, Catalog, String, Time, Choice

_DAYS = [
    ("ПН", "Пн"), ("ВТ", "Вт"), ("СР", "Ср"),
    ("ЧТ", "Чт"), ("ПТ", "Пт"),
]


@catalog
class WorkSchedule(Catalog):
    __verbose_name__ = "Графики работы"
    name      = String("Название", required=True)
    work_days = Choice("Рабочие дни", choices=_DAYS, required=True)
    start     = Time("Начало", default=lambda: time(9, 0))
    end       = Time("Окончание", default=lambda: time(18, 0))


def _seed(db):
    if db.list(WorkSchedule):
        return
    ws = WorkSchedule(name="Стандартный", work_days="ПН|ВТ|СР|ЧТ|ПТ",
                      start=time(9, 0), end=time(18, 0))
    db.save(ws)
    print(f"  График: {ws.name} ({ws.work_days})")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="choice_time.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
