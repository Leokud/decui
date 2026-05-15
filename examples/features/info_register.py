"""Регистр сведений (периодические значения). Пример курсов валют.

Запуск:
    cd F:/projects/decui
    python examples/features/info_register.py
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from decimal import Decimal
from decui import information, report, Report, Column, Dimension, Resource, Movement
from decui.movement import write_movements


@information
class CurrencyRate:
    currency = Dimension("Валюта")
    rate     = Resource("Курс")


@report
class RatesReport(Report):
    title = "Курсы валют"
    columns = [
        Column("Валюта", "currency", width=100),
        Column("Курс", "rate", width=120, align="right",
               format="{:,.2f}", suffix=" руб."),
    ]

    def execute(self, db, **kw):
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=db._engine)
        meta = CurrencyRate._register_meta
        mc = meta.orm_model
        if mc is None:
            return []
        with Session() as sess:
            rows = sess.query(mc).all()
        return [
            {"currency": getattr(r, "currency", "-"),
             "rate": str(getattr(r, "rate", 0))}
            for r in rows
        ]


def _seed(db):
    from decui.movement import clear_movements
    if not hasattr(CurrencyRate, "_register_meta"):
        return
    # Очищаем старые движения, чтобы не дублировать при повтоном запуске
    try:
        clear_movements(db, CurrencyRate)
    except Exception:
        pass
    write_movements(db, [
        Movement(register=CurrencyRate,
                 dimensions={"currency": "USD"},
                 resources={"rate": Decimal("92.50")}),
        Movement(register=CurrencyRate,
                 dimensions={"currency": "EUR"},
                 resources={"rate": Decimal("100.20")}),
    ])
    print("  Курсы: USD=92.50, EUR=100.20")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="info_reg.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
