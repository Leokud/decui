import re
import runpy
from datetime import date, datetime
from decimal import Decimal
from html import unescape
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QDate, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from decui import Database, set_record
from decui.ui.form import AutoForm
from decui.ui.listview import ListView
from decui.ui.report_view import ReportView


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/features/purchase_orders.py"


@pytest.fixture
def lesson(monkeypatch):
    from sqlalchemy.orm import declarative_base

    from decui import movement, orm, registry

    monkeypatch.setattr(registry, "_entity_registry", {})
    monkeypatch.setattr(registry, "_report_registry", {})
    monkeypatch.setattr(movement, "_REGISTRY", {})
    base = declarative_base()
    for name, table in orm.Base.metadata.tables.items():
        if name.startswith("_erp_"):
            table.to_metadata(base.metadata)
    monkeypatch.setattr(orm, "Base", base)
    return SimpleNamespace(**runpy.run_path(str(EXAMPLE)))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_tutorial_copy_matches_example():
    page = (ROOT / "docs/index.html").read_text(encoding="utf-8")
    block = page.split('id="purchase-orders-example"', 1)[1]
    code = re.search(r'<code class="language-python">(.*?)</code>', block, re.S)
    assert code is not None
    assert unescape(code[1]) == EXAMPLE.read_text(encoding="utf-8").rstrip()


def test_saved_purchase_prices_and_rollback(qapp, monkeypatch, lesson):
    db = Database.memory()
    lesson.seed_demo(db)
    lesson.seed_demo(db)
    order = db.filter(lesson.PurchaseOrder, number="DEMO-NOV")[0]
    january = db.filter(lesson.PurchaseOrder, number="DEMO-JAN")[0]
    assert len(db.list(lesson.PurchaseOrder)) == 2
    assert not order.Items
    order = db.get(lesson.PurchaseOrder, order.id)
    january = db.get(lesson.PurchaseOrder, january.id)
    assert january.Items[0]["unit_price"] == january.total == Decimal("0.00")
    assert order.Items[0]["unit_price"] == Decimal("12.50")
    assert order.total == Decimal("25.00")

    db.save(lesson.PurchaseOrder(supplier=order.supplier))
    with pytest.raises(ValueError, match="хотя бы одну строку"):
        db.save(lesson.PurchaseOrder(supplier=order.supplier), validate=True)

    unpriced = db.save(lesson.Product(name="Без цены", price=Decimal("99")))
    order.Items[0]["quantity"] = 4
    order.Items.add(product=unpriced.id, quantity=1)
    with pytest.raises(ValueError, match="Строка 2, товар «Без цены», 30.11.2026"):
        db.save(order)
    assert order.Items[0]["amount"] == Decimal("25.00")

    form = AutoForm(lesson.PurchaseOrder, db)
    form.load_entity(order)
    alerts = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: alerts.append(args[-1]))
    assert not form._save()
    assert "Строка 2, товар «Без цены», 30.11.2026" in alerts[0]
    persisted = db.get(lesson.PurchaseOrder, order.id)
    assert len(persisted.Items) == 1
    assert persisted.Items[0]["quantity"] == 2
    assert persisted.Items.total("amount") == persisted.total == Decimal("25.00")

    persisted.date = datetime(2026, 12, 1)
    db.save(persisted)
    assert db.get(lesson.PurchaseOrder, persisted.id).total == Decimal("30.00")


def test_purchase_related_create_and_report(qapp, monkeypatch, tmp_path, lesson):
    database = str(tmp_path / "warehouse.sqlite3")
    db = Database(database)
    lesson.seed_demo(db)
    north = db.filter(lesson.Supplier, name="Север")[0]
    source = AutoForm(lesson.Supplier, db)
    source.load_entity(north)
    view = source.findChild(ListView)
    created = []

    def save_related():
        dialog = QApplication.activeModalWidget()
        try:
            form = dialog.findChild(AutoForm)
            assert form.entity.supplier == north.id
            form.entity.date = datetime(2026, 12, 1)
            product = db.list(lesson.Product)[0]
            form.entity.Items.add(product=product.id, quantity=2)
            form.load_entity(form.entity)
            assert form._tab_widgets["Items"].get_rows()[0]["unit_price"] is None
            assert form._save()
            created.append(form.entity.id)
        finally:
            dialog.accept()

    QTimer.singleShot(0, save_related)
    view.create_requested.emit(lesson.PurchaseOrder)
    assert len(created) == 1
    db = Database(database)
    reopened = AutoForm(lesson.Supplier, db)
    reopened.load_by_id(north.id)
    assert created[0] in [item.id for item in reopened.findChild(ListView)._items]

    set_record(db, lesson.Prices, dimensions={"product": db.list(lesson.Product)[0].id, "supplier": north.id},
               resources={"price": Decimal("90")}, period=date(2026, 11, 30))
    report = ReportView(lesson.PurchaseReport, db)
    report._generate()
    assert [(row["number"], row["amount"]) for row in report._last_rows] == [("DEMO-NOV", Decimal("25.00"))]
    assert db.get(lesson.PurchaseOrder, created[0]).total == Decimal("30.00")

    widgets = report._param_widgets
    widgets["date_from"].setDate(QDate(2026, 10, 1))
    widgets["date_to"].setDate(QDate(2026, 10, 31))
    report._generate()
    assert report._last_rows == []
    widgets["date_to"].setDate(QDate(2026, 9, 30))
    alerts = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: alerts.append(args[-1]))
    report._generate()
    assert "Начало периода должно быть не позже его окончания." in alerts[0]


def test_report_opens_with_supplier_and_leaves_card_edits_intact(qapp, lesson):
    db = Database.memory()
    lesson.seed_demo(db)
    south = db.filter(lesson.Supplier, name="Юг")[0]
    source = AutoForm(lesson.Supplier, db)
    source.load_new()
    assert source._reports_menu.isEmpty()
    source.load_entity(south)
    source._widgets["name"].setText("Несохранённое название")
    observed = {}

    def inspect_order():
        dialog = QApplication.activeModalWidget()
        try:
            card = dialog.findChild(AutoForm)
            observed["opened_order"] = card.entity.number
        finally:
            dialog.accept()

    def inspect_report():
        dialog = QApplication.activeModalWidget()
        try:
            view = dialog.findChild(ReportView)
            observed["supplier"] = view._param_widgets["supplier"].currentData()
            observed["november"] = view._last_rows
            view._param_widgets["date_to"].setDate(QDate(2027, 1, 1))
            view._generate()
            observed["orders"] = [row["number"] for row in view._last_rows]
            QTimer.singleShot(0, inspect_order)
            view._on_double_click(view._proxy.index(0, 0))
        finally:
            dialog.accept()

    action, = source._reports_menu.actions()
    assert action.text() == "Закупки по строкам"
    QTimer.singleShot(0, inspect_report)
    action.trigger()

    assert observed == {
        "supplier": south.id, "november": [], "orders": ["DEMO-JAN"], "opened_order": "DEMO-JAN",
    }
    assert source.has_unsaved_changes()
    assert source._widgets["name"].text() == "Несохранённое название"
    assert db.get(lesson.Supplier, south.id).name == "Юг"
    general_view = ReportView(lesson.PurchaseReport, db)
    assert general_view._param_widgets["supplier"].currentData() is None
    assert [row["number"] for row in general_view._last_rows] == ["DEMO-NOV"]


def test_deleted_supplier_cannot_open_unfiltered_report(qapp, monkeypatch, lesson):
    db = Database.memory()
    supplier = db.save(lesson.Supplier(name="Удалённый"))
    source = AutoForm(lesson.Supplier, db)
    source.load_entity(supplier)
    db.delete(supplier)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))

    source._reports_menu.actions()[0].trigger()

    assert warnings == ["Запись для параметра «Поставщик» удалена или недоступна."]
