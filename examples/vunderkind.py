"""
Центр развития детей «Вундеркинд» — Расписание / Набор / Оплата.

Запуск с консолью (для отладки):
    python examples/vunderkind.py

Запуск без консоли (собранный .exe):
    dist/ERP/ERP.exe

При первом запуске создаются демо-данные:
  - 5 возрастных интервалов          (ТЗ п.3)
  - 4 кружка с описанием и возрастом  (ТЗ п.4)
  - 3 преподавателя                   (ТЗ п.5)
  - 3 запуска кружков                 (ТЗ п.6)
"""

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from datetime import date, time, datetime

from decui import (
    catalog, document, command, report,
    Catalog, Document, Report,
    String, Ref, Date, Time, Choice,
    Column, DateParam, RefParam,
)

_DAYS = [
    ("ПН", "Понедельник"),
    ("ВТ", "Вторник"),
    ("СР", "Среда"),
    ("ЧТ", "Четверг"),
    ("ПТ", "Пятница"),
    ("СБ", "Суббота"),
    ("ВС", "Воскресенье"),
]


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  СПРАВОЧНИКИ                                                        ║
# ╚══════════════════════════════════════════════════════════════════════╝

@catalog
class AgeInterval(Catalog):
    """Возрастные интервалы (ТЗ п.3)."""
    __section__      = "Расписание"
    __verbose_name__ = "Возрастные интервалы"

    name = String("Название", required=True)


@catalog
class Circle(Catalog):
    """Кружки (ТЗ п.4)."""
    __section__      = "Расписание"
    __verbose_name__ = "Кружки"

    name         = String("Наименование", required=True)
    age_interval = Ref(AgeInterval, "Возрастной интервал",
                       required=True, representation_field="name")
    description  = String("Описание")


@catalog
class Teacher(Catalog):
    """Преподаватели (ТЗ п.5)."""
    __section__      = "Расписание"
    __verbose_name__ = "Преподаватели"

    name = String("ФИО", required=True)


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ДОКУМЕНТ                                                          ║
# ╚══════════════════════════════════════════════════════════════════════╝

@document
class ClubStart(Document):
    """Старт кружка (ТЗ п.6)."""

    __section__      = "Расписание"
    __verbose_name__ = "Старт кружка"
    __numbering__    = {"prefix": "СТАРТ", "field": "number"}
    __defaults__     = {
        "days": lambda db: "ПН|СР",
        "time": lambda db: time(10, 0),
    }

    number       = String("Номер", read_only=True)
    start_date   = Date("Дата приказа", required=True, default=date.today)
    circle       = Ref(Circle, "Кружок", required=True,
                       representation_field="name",
                       auto_fill="age_interval")
    age_interval = Ref(AgeInterval, "Возраст", read_only=True,
                       representation_field="name")
    period_from  = Date("Период с", required=True, default=date.today)
    period_to    = Date("Период по", required=True)
    teacher      = Ref(Teacher, "Преподаватель", required=True,
                       representation_field="name")
    days         = Choice("Дни недели", choices=_DAYS,
                          required=True)
    time         = Time("Время занятий", required=True,
                        default=lambda: time(10, 0))

    @command("Запустить")
    def start(self):
        """Запустить кружок (ТЗ п.7)."""
        self._data["posted"] = True


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ОТЧЁТ                                                             ║
# ╚══════════════════════════════════════════════════════════════════════╝

@report
class TeacherSchedule(Report):
    """График работы преподавателя (ТЗ п.8)."""

    title = "График работы преподавателя"
    columns = [
        Column("День недели", "day", width=120),
        Column("Время", "time", width=80),
        Column("Кружок", "circle", width=250),
        Column("Период с", "from_", width=100),
        Column("Период по", "to_", width=100),
    ]

    # Для демонстрации агрегации — добавьте колонку с aggregate:

    teacher   = RefParam("Преподаватель", Teacher,
                         representation_field="name", required=True)
    date_from = DateParam("Период с", required=True,
                          default=lambda: date(2020, 1, 1))
    date_to   = DateParam("Период по", required=True,
                          default=lambda: date(2030, 12, 31))

    def execute(self, db, teacher, date_from, date_to, **kw):
        rows = []
        for cs_summary in db.list(ClubStart):
            if teacher is not None and cs_summary.teacher != teacher:
                continue
            cs = db.get(ClubStart, cs_summary._id)
            if cs is None or not cs.posted:
                continue
            # Показываем если период работы кружка пересекается с фильтром
            if cs.period_from and date_to and cs.period_from > date_to:
                continue
            if cs.period_to and date_from and cs.period_to < date_from:
                continue
            circle = db.get(Circle, cs.circle)
            cname = circle.name if circle else f"#{cs.circle}"
            day_list = (cs.days or "").split("|")
            day_map = dict(_DAYS)
            for d in day_list:
                if not d:
                    continue
                rows.append({
                    "day": day_map.get(d, d),
                    "time": str(cs.time or ""),
                    "circle": cname,
                    "from_": str(cs.period_from or ""),
                    "to_": str(cs.period_to or ""),
                })
        return rows


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ДЕМО-ДАННЫЕ                                                        ║
# ╚══════════════════════════════════════════════════════════════════════╝

def _seed_data(db):
    """Заполнение тестовыми данными (ТЗ — раздел «Заполнение»)."""
    if db.list(AgeInterval):
        return

    # ── п.3. Возрастные интервалы ──
    ages = [
        AgeInterval(name="Младший дошкольный (3-5 лет)"),
        AgeInterval(name="Старший дошкольный (5-7 лет)"),
        AgeInterval(name="Младший школьный (7-10 лет)"),
        AgeInterval(name="Средний школьный (10-14 лет)"),
        AgeInterval(name="Старший школьный (14-17 лет)"),
    ]
    for a in ages:
        db.save(a)
    print(f"  [OK] Возрастных интервалов: {len(ages)}")

    # ── п.4. Кружки (4 шт.) ──
    circles = [
        Circle(
            name="Юный художник",
            age_interval=ages[1]._id,
            description="Рисование, лепка, аппликация. Развитие мелкой "
                        "моторики и творческого мышления.",
        ),
        Circle(
            name="Робототехника",
            age_interval=ages[2]._id,
            description="Конструирование и программирование роботов "
                        "на базе Lego Mindstorms.",
        ),
        Circle(
            name="Английский язык",
            age_interval=ages[0]._id,
            description="Игровое изучение английского языка для самых "
                        "маленьких. Песни, стихи, мультфильмы.",
        ),
        Circle(
            name="Шахматы",
            age_interval=ages[2]._id,
            description="Обучение игре в шахматы, развитие логики "
                        "и стратегического мышления.",
        ),
    ]
    for c in circles:
        db.save(c)
    print(f"  [OK] Кружков: {len(circles)}")
    for c in circles:
        ai = db.get(AgeInterval, c.age_interval)
        print(f"       - {c.name} ({ai.name if ai else '?'})")

    # ── п.5. Преподаватели (3 шт.) ──
    teachers = [
        Teacher(name="Иванова Мария Сергеевна"),
        Teacher(name="Петров Дмитрий Алексеевич"),
        Teacher(name="Сидорова Елена Викторовна"),
    ]
    for t in teachers:
        db.save(t)
    print(f"  [OK] Преподавателей: {len(teachers)}")
    for t in teachers:
        print(f"       - {t.name}")

    # ── п.6. Старты кружков (3 шт.) ──
    starts = [
        ClubStart(
            start_date=date(2026, 9, 1),
            circle=circles[0]._id,
            age_interval=circles[0].age_interval,
            period_from=date(2026, 9, 1),
            period_to=date(2027, 5, 31),
            teacher=teachers[0]._id,
            days="ПН|СР",
            time=time(10, 0),
        ),
        ClubStart(
            start_date=date(2026, 9, 1),
            circle=circles[1]._id,
            age_interval=circles[1].age_interval,
            period_from=date(2026, 9, 1),
            period_to=date(2027, 5, 31),
            teacher=teachers[1]._id,
            days="ВТ|ЧТ",
            time=time(14, 0),
        ),
        ClubStart(
            start_date=date(2026, 9, 1),
            circle=circles[2]._id,
            age_interval=circles[2].age_interval,
            period_from=date(2026, 9, 1),
            period_to=date(2027, 5, 31),
            teacher=teachers[0]._id,
            days="ПН|ПТ",
            time=time(16, 0),
        ),
    ]
    for s in starts:
        s.start()
        db.save(s)
    print(f"  [OK] Стартов кружков: {len(starts)} (запущены)")
    for s in starts:
        tchr = db.get(Teacher, s.teacher)
        crc = db.get(Circle, s.circle)
        ai = db.get(AgeInterval, s.age_interval)
        print(f"       {s.number}: {crc.name if crc else '?'} [{ai.name if ai else '?'}] -> "
              f"{tchr.name if tchr else '?'} ({s.days} {s.time})")
    print()


# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ЗАПУСК                                                             ║
# ╚══════════════════════════════════════════════════════════════════════╝

def main() -> None:
    from decui.app import ERPApplication
    from decui.registry import get_all_entities, get_all_reports

    print("=" * 50)
    print("  Центр развития детей «Вундеркинд»")
    print("=" * 50)

    app = ERPApplication(db_path="vunderkind.db")

    if app.database._ready:
        print(f"\n  База данных: {app.database._path}")
        ents = [e._meta.verbose_name for e in get_all_entities()]
        reps = [r._report_meta.title for r in get_all_reports()]
        print(f"  Сущностей: {len(ents)} -> {', '.join(ents)}")
        print(f"  Отчётов:   {len(reps)} -> {', '.join(reps)}")
        _seed_data(app.database)
    else:
        print(f"\n  [ERR] База данных не инициализирована:")
        print(f"        {app.database._last_error}")

    print("  Запуск интерфейса...\n")
    app.run()


if __name__ == "__main__":
    main()
