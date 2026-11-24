import datetime
from decimal import Decimal
import pytest

from decui import (
    Column,
    DateParam,
    IntegerParam,
    DecimalParam,
    BooleanParam,
    StringParam,
    RefParam,
    Report,
    QueryReport,
    report,
    Database,
    Catalog,
    catalog,
    String,
)
from decui.reports import calc_aggregate


def test_calc_aggregate():
    values = ["10.5", "20", "30.5", "invalid", ""]
    assert calc_aggregate(values, "count") == "3"
    assert calc_aggregate(values, "sum") == "61.0"
    assert calc_aggregate(values, "avg") == "20.33333333333333333333333333"
    assert calc_aggregate(values, "min") == "10.5"
    assert calc_aggregate(values, "max") == "30.5"


@catalog
class Client(Catalog):
    name = String("Имя")


@report
class ClientReport(QueryReport):
    title = "Список клиентов"
    columns = [
        Column("ID", "id", width=60),
        Column("Имя", "name", width=200),
    ]

    prefix_filter = StringParam("Префикс имени")
    min_id = IntegerParam("Мин ID", default=0)

    def execute(self, db, prefix_filter=None, min_id=0, **kwargs):
        clients = db.list(Client)
        results = []
        for c in clients:
            if min_id and c._id < min_id:
                continue
            if prefix_filter and not (c.name or "").startswith(prefix_filter):
                continue
            results.append({"id": str(c._id), "name": c.name or ""})
        return results


def test_report_execution():
    db = Database(":memory:")
    c1 = Client(name="Алексей")
    c2 = Client(name="Андрей")
    c3 = Client(name="Борис")
    db.save(c1)
    db.save(c2)
    db.save(c3)

    rep = ClientReport()
    assert rep._report_meta.title == "Список клиентов"
    assert "prefix_filter" in rep._report_meta.params
    assert "min_id" in rep._report_meta.params

    rows = rep.execute(db, prefix_filter="А", min_id=0)
    assert len(rows) == 2
    assert rows[0]["name"] == "Алексей"
    assert rows[1]["name"] == "Андрей"
