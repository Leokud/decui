from __future__ import annotations

import argparse
import datetime
from decimal import Decimal
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QSpinBox

from decui import (
    Boolean,
    Catalog,
    Choice,
    ChoiceParam,
    Column,
    Database,
    Date,
    DateParam,
    DateTime,
    Decimal as D,
    Dimension,
    Document,
    HistoryRegister,
    Integer,
    Ledger,
    LedgerEntry,
    MasterData,
    Metric,
    QueryReport,
    Ref,
    RefDimension,
    ReminderDialog,
    ReminderService,
    SequenceViolationError,
    String,
    Time,
    Transaction,
    catalog,
    check_sequence,
    clear_movements,
    document,
    get_latest,
    get_ledger_balance,
    get_records,
    history_register,
    ledger,
    line_items,
    master_data,
    report,
    set_record,
    transaction,
    write_movements,
)
from decui.reports import calc_aggregate
from decui.ui.chart import BarChart
from decui.ui.editors import create_editor, get_editor_value, set_editor_value
from decui.ui.form import AutoForm
from decui.ui.listview import ListView
from decui.ui.report_view import ReportView


@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ── Models for audit tests ─────────────────────────────────────────────

@master_data
class AuditCategory(MasterData):
    __verbose_name__ = "Категории аудита"
    name: str = String("Название", required=True, max_length=20)


@master_data
class AuditItem(MasterData):
    name: str = String("Название", required=True)
    category: int | None = Ref(AuditCategory, "Категория", representation_field="name")
    qty: int | None = Integer("Кол-во")
    price: Decimal = D("Цена", scale=2, default=Decimal("0.00"), currency="RUB")
    active: bool = Boolean("Активен", default=True)
    priority: str = Choice("Приоритет", choices=[("LOW", "Низкий"), ("HIGH", "Высокий")], multi=False, default="LOW")


@transaction
class BaseTPDoc(Transaction):
    note: str = String("Примечание")

    @line_items
    class Rows:
        time_slot: datetime.time = Time("Время", required=True)
        qty: int = Integer("Кол-во", min_value=1, default=1)
        price: Decimal = D("Цена", scale=2, default=Decimal("0.00"))
        amount: Decimal = D("Сумма", scale=2, default=Decimal("0.00"))


@transaction
class SubDocA(BaseTPDoc):
    extra_a: str = String("Доп A")


@transaction
class SubDocB(BaseTPDoc):
    __numbering__ = {"prefix": "DOCB", "field": "number", "pattern": "{prefix}-{year}-{month}-{seq:04d}"}
    __sequence__ = True
    extra_b: str = String("Доп B")


@ledger
class AuditLedger(Ledger):
    item = RefDimension(AuditItem, "Товар")
    qty = Metric("Количество", field_type=Decimal)


@history_register
class AuditPrices(HistoryRegister):
    item = RefDimension(AuditItem, "Товар")
    price = Metric("Цена", field_type=Decimal)


@report
class NoColsReport(QueryReport):
    title = "Отчет без columns"
    mode = ChoiceParam("Режим", [("A", "Режим А"), ("B", "Режим B")], default="A")
    opt_date = DateParam("Фильтр даты")

    def execute(self, db, mode=None, opt_date=None, **kw):
        if mode == "EMPTY":
            return []
        return [
            {"k": "Строка 1", "v": 10},
            {"k": "Строка 2", "v": 2},
        ]


@report
class TotalsReport(QueryReport):
    title = "Отчет с итогами"
    columns = [
        Column("Имя", "name", width=210, align="left"),
        Column("Сумма", "total", width=130, align="right", aggregate="sum", prefix="$", format="{:,.2f}"),
    ]

    def execute(self, db, **kw):
        return [
            {"name": "Alpha", "total": Decimal("100.00")},
            {"name": "Beta", "total": Decimal("25.50")},
        ]


# ── 1. Core Fields & Entity Inheritance & Validation ───────────────────

def test_field_conversions_and_validation():
    from decui.fields import ChoiceField, DateField, RefField, StringField

    # DateField datetime -> date truncation
    df = DateField("Дата")
    d_conv = df.to_python(datetime.datetime(2026, 5, 15, 14, 30))
    assert type(d_conv) is datetime.date
    assert d_conv == datetime.date(2026, 5, 15)

    # StringField max_length enforcement
    sf = StringField("Код", max_length=5, required=True)
    assert len(sf.validate("ABCDEF")) == 1
    assert len(sf.validate("ABC")) == 0

    # ChoiceField required & allowed values
    cf = ChoiceField("Тег", choices=[("X", "X"), ("Y", "Y")], required=True)
    assert len(cf.validate("")) == 1
    assert len(cf.validate("Z")) == 1
    assert len(cf.validate("X|Y")) == 0

    # RefField rejects unsaved Entity
    rf = RefField(AuditCategory, "Категория")
    unsaved = AuditCategory(name="NewCat")
    with pytest.raises(ValueError):
        rf.to_python(unsaved)


def test_inherited_table_parts_independent_and_time_persists():
    db = Database.memory()
    assert SubDocA._meta.table_parts["Rows"] is not SubDocB._meta.table_parts["Rows"]
    assert SubDocA._meta.table_parts["Rows"].orm_model is not SubDocB._meta.table_parts["Rows"].orm_model

    a = SubDocA(note="Doc A")
    a.Items_rows = None  # ignore
    a.Rows.add(time_slot=datetime.time(14, 45), qty=3, price=Decimal("10.00"), amount=Decimal("30.00"))
    db.save(a)

    b = SubDocB(note="Doc B", date=datetime.datetime(2026, 3, 10, 9, 0))
    b.Rows.add(time_slot=datetime.time(9, 15), qty=2, price=Decimal("5.00"), amount=Decimal("10.00"))
    db.save(b)

    assert a.id is not None and a.id == a._id
    assert b.number == "DOCB-2026-03-0001"

    reloaded_a = db.get(SubDocA, a.id)
    assert reloaded_a is not None
    assert len(reloaded_a.Rows) == 1
    assert reloaded_a.Rows[0]["time_slot"] == datetime.time(14, 45)


def test_numbering_resets_per_period():
    db = Database.memory()
    d1 = SubDocB(note="Mar", date=datetime.datetime(2026, 3, 1, 10, 0))
    d2 = SubDocB(note="Apr", date=datetime.datetime(2026, 4, 1, 10, 0))
    d3 = SubDocB(note="Mar 2", date=datetime.datetime(2026, 3, 15, 10, 0))
    db.seed([d1, d2, d3])

    assert d1.number == "DOCB-2026-03-0001"
    assert d2.number == "DOCB-2026-04-0001"
    assert d3.number == "DOCB-2026-03-0002"


def test_database_filter_operators_and_hooks():
    db = Database.memory()
    cat = AuditCategory(name="Книги")
    db.save(cat)
    db.seed([
        AuditItem(name="Python Book", category=cat, price=Decimal("100.00"), qty=10, active=True),
        AuditItem(name="Qt Guide", category=cat, price=Decimal("250.00"), qty=2, active=False),
        AuditItem(name="SQL Cookbook", category=cat, price=Decimal("150.00"), qty=None, active=True),
    ])

    assert len(db.filter(AuditItem, price__gte=Decimal("150.00"))) == 2
    assert len(db.filter(AuditItem, name__icontains="book")) == 2
    assert len(db.filter(AuditItem, qty__isnull=True)) == 1
    ordered = db.filter(AuditItem, category=cat, order_by="-price", limit=2)
    assert [x.name for x in ordered] == ["Qt Guide", "SQL Cookbook"]

    with pytest.raises(ValueError):
        db.filter(AuditItem, unknown_col=123)

    # Validation & hooks
    bad = AuditItem(name="", price=Decimal("10.00"))
    with pytest.raises(ValueError):
        bad.save(db, validate=True)


def test_non_destructive_schema_upgrade(tmp_path):
    db_file = tmp_path / "upgrade_test.db"
    db1 = Database(str(db_file))
    c = AuditCategory(name="Сохраняемая")
    db1.save(c)
    assert c.id is not None

    # Simulate adding a nullable column dynamically and reopening Database
    AuditCategory._meta.fields["notes"] = String("Заметки")
    AuditCategory._meta.fields["notes"].name = "notes"
    AuditCategory._meta.orm_model = None
    db2 = Database(str(db_file))
    assert db2._ready
    reloaded = db2.get(AuditCategory, c.id)
    assert reloaded is not None
    assert reloaded.name == "Сохраняемая"

    # Cleanup mutated test class field
    AuditCategory._meta.fields.pop("notes", None)
    AuditCategory._meta.orm_model = None


def test_sequence_check_and_registers_decimal_period():
    db = Database.memory()
    cat = AuditCategory(name="РегКат")
    db.save(cat)
    item = AuditItem(name="Деталь", category=cat)
    db.save(item)

    # Information register period ordering
    set_record(db, AuditPrices, dimensions={"item": item}, resources={"price": Decimal("200.50")}, period=datetime.date(2026, 6, 1))
    set_record(db, AuditPrices, dimensions={"item": item}, resources={"price": Decimal("99.99")}, period=datetime.date(2026, 5, 1))

    latest = get_latest(db, AuditPrices, item=item)
    assert latest is not None
    assert latest["price"] == Decimal("200.50")

    as_of_may = get_latest(db, AuditPrices, on_date=datetime.date(2026, 5, 15), item=item)
    assert as_of_may is not None
    assert as_of_may["price"] == Decimal("99.99")

    # Accumulation decimal precision + clear guard
    write_movements(db, [
        LedgerEntry(register=AuditLedger, dimensions={"item": item}, resources={"qty": Decimal("10.25")}, is_income=True, doc_id=1, doc_type="SubDocB"),
    ])
    clear_movements(db, AuditLedger, doc_id=None, doc_type=None)  # must be no-op guard
    bal = get_ledger_balance(db, AuditLedger, item=item)
    assert bal[0]["qty"] == Decimal("10.25")
    assert isinstance(bal[0]["qty"], Decimal)

    # Sequence check raises SequenceViolationError instead of Qt popup
    later = SubDocB(note="Later", date=datetime.datetime(2026, 12, 10, 12, 0))
    later.post()
    db.save(later)

    earlier = SubDocB(note="Earlier", date=datetime.datetime(2026, 12, 1, 12, 0))
    with pytest.raises(SequenceViolationError):
        check_sequence(db, earlier)


# ── 2. UI, Editors, Chart, ListView, ReportView, Reminders, CLI ───────

def test_editors_nullable_currency_single_choice(qapp):
    qty_f = AuditItem._meta.fields["qty"]
    qty_w = create_editor(qty_f)
    assert isinstance(qty_w, QSpinBox)
    assert get_editor_value(qty_w, qty_f) is None  # not -1000000000!

    price_f = AuditItem._meta.fields["price"]
    price_w = create_editor(price_f)
    assert isinstance(price_w, QDoubleSpinBox)
    assert price_w.suffix() == " RUB"
    assert price_w.minimum() < 0  # allows negative values when default=0

    prio_f = AuditItem._meta.fields["priority"]
    prio_w = create_editor(prio_f)
    assert isinstance(prio_w, QComboBox)
    set_editor_value(prio_w, prio_f, "HIGH")
    assert get_editor_value(prio_w, prio_f) == "HIGH"


def test_barchart_zero_and_negative_values(qapp):
    chart = BarChart(labels=["A", "B", "C"], values=[0.0, 0.0, 0.0], title="Нули")
    chart.resize(400, 220)
    pix = QPixmap(400, 220)
    chart.render(pix)  # Must not ZeroDivisionError

    chart_neg = BarChart(labels=["Profit", "Loss"], values=[50.0, -20.0])
    chart_neg.resize(400, 220)
    chart_neg.render(pix)


def test_listview_ref_lookup_boolean_and_proxy_sorting(qapp):
    db = Database.memory()
    cat = AuditCategory(name="Канцелярия")
    db.save(cat)
    i1 = AuditItem(name="Ручка", category=cat, qty=2, active=True)
    i2 = AuditItem(name="Альбом", category=cat, qty=10, active=False)
    db.seed([i1, i2])

    lv = ListView(AuditItem, db)
    model = lv._table.model()
    assert model is not None

    # Ref renders category name (not raw integer id) and Boolean renders "Да"/"Нет"
    col_names = [f.display_name for f in AuditItem._meta.fields.values() if f.visible]
    cat_col = col_names.index("Категория")
    act_col = col_names.index("Активен")
    assert model.index(0, cat_col).data() == "Канцелярия"
    assert model.index(0, act_col).data() == "Да"
    assert model.index(1, act_col).data() == "Нет"

    # Search filter selects matching entity via proxy
    lv._search_edit.setText("Альбом")
    assert lv._table.model().rowCount() == 1
    lv._table.setCurrentIndex(lv._table.model().index(0, 0))
    sel = lv._selected_entity()
    assert sel is not None and sel.name == "Альбом"


def test_report_view_columns_totals_and_csv(qapp):
    db = Database.memory()
    assert calc_aggregate(["1,234.56", "1 000,00"], "sum") == "2234.56"

    # NoColsReport handles omitted columns & ChoiceParam & optional DateParam & empty clearing
    rv_nocols = ReportView(NoColsReport, db)
    rv_nocols._generate()
    assert not rv_nocols._table.isHidden()

    # Force empty mode -> hides table cleanly
    mode_w = rv_nocols._param_widgets["mode"]
    assert isinstance(mode_w, QComboBox)
    mode_w.addItem("Empty", "EMPTY")
    mode_w.setCurrentIndex(mode_w.count() - 1)
    rv_nocols._generate()
    assert rv_nocols._table.isHidden()
    assert rv_nocols._table.model() is None

    # TotalsReport: alignment, sorting keeps totals row at bottom, CSV export
    rv_tot = ReportView(TotalsReport, db)
    rv_tot._generate()
    m = rv_tot._table.model()
    assert m is not None
    # Total row should be row index 2
    assert m.index(2, 1).data() == "$125.50"
    rv_tot._table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    assert m.index(2, 1).data() == "$125.50"  # Still pinned at bottom!
    assert m.index(0, 0).data() == "Beta"

    csv_out = TotalsReport.to_csv(rv_tot._last_rows)
    assert "Имя;Сумма" in csv_out
    assert "Alpha;100.00" in csv_out


def test_autoform_dirty_and_reminder_retrigger(qapp):
    db = Database.memory()
    cat = AuditCategory(name="DirtyTest")
    db.save(cat)
    item = AuditItem(name="Исходное", category=cat)
    db.save(item)

    form = AutoForm(AuditItem, db)
    form.load_entity(item)
    assert form.has_unsaved_changes() is False

    form._widgets["name"].setText("Измененное")
    assert form.has_unsaved_changes() is True

    # Timezone-aware reminder check & postpone reset
    @master_data
    class TzTask(MasterData):
        name: str = String("Имя", required=True)
        deadline: datetime.datetime = DateTime("Срок")
        is_completed: bool = Boolean("Готово", default=False)

    t_db = Database.memory()
    aware_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=15)
    t = TzTask(name="UTC Task", deadline=aware_dt)
    t.save(t_db)

    svc = ReminderService(t_db, [TzTask], notify_before_minutes=60)
    found = svc.check_deadlines()
    assert len(found) == 1
    assert ("TzTask", t._id) in svc._dismissed_keys

    svc._on_entity_postponed(t)
    assert ("TzTask", t._id) not in svc._dismissed_keys


def test_cli_starter_template_check(tmp_path):
    from decui.cli import cmd_check, cmd_new

    proj = tmp_path / "demo_erp"
    assert cmd_new(argparse.Namespace(name=str(proj))) == 0
    assert cmd_check(argparse.Namespace(file=str(proj / "app.py"))) == 0
