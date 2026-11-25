from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from decui.studio_project import StudioField, StudioProject, StudioReport, goal_template


def test_studio_project_can_roundtrip_export_and_check(tmp_path):
    from decui.studio_project import StudioTablePart

    project = goal_template()
    ref = next(item for entity in project.entities for item in entity.fields if item.kind == "Ref")
    ref.create_from = "Создать на основании"
    ref.basis_fields = {"deadline": "deadline"}
    comment = next(field for field in project.entities[1].fields if field.name == "description")
    comment.visibility_field = "is_completed"
    comment.visibility_value = False
    comment.required_when_visible = True
    project.entities[0].table_parts.append(StudioTablePart("Milestones", "Этапы", [StudioField("name", "Название")]))
    project.entities[1].table_parts.append(StudioTablePart("Steps", "Шаги", [StudioField("title", "Шаг")]))
    ref.basis_tables = {"Steps": {"source": "Milestones", "columns": {"title": "name"}}}
    project.entities[0].fields[0].form_tab = "Подробности"
    project.entities[0].fields[0].form_group = "Основные сведения"
    project.reports.append(StudioReport("TaskList", "Все задачи", "Task", ["name", "deadline"], "-deadline", "goal"))
    project_file = tmp_path / "goals.decui.json"
    project.write(project_file)
    restored = StudioProject.read(project_file)
    assert restored == project
    entry = restored.export(tmp_path / "export")
    original = entry.read_bytes()
    generated = entry.read_text(encoding="utf-8")
    assert "__form_groups__" in generated
    assert "__form_tabs__" in generated
    assert "__form_visible_when__ = {'description': ('is_completed', False)}" in generated
    assert "__required_when_visible__ = ('description',)" in generated
    assert "create_from='Создать на основании'" in generated
    assert "basis_fields={'deadline': 'deadline'}" in generated
    assert "basis_tables={'Steps': {'source': 'Milestones', 'columns': {'title': 'name'}}}" in generated
    with pytest.raises(FileExistsError):
        restored.export(entry.parent)
    assert entry.read_bytes() == original
    result = subprocess.run(
        [sys.executable, "-m", "decui.cli", "check", str(entry)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True, text=True,
        check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 entities, 2 reports" in result.stdout


def test_studio_rejects_unresolved_references_and_duplicate_fields():
    project = goal_template()
    project.entities[1].fields[1].target = "Missing"
    with pytest.raises(ValueError, match="выберите объект"):
        project.source()
    project.entities[1].fields[1].target = "Goal"
    project.entities[1].fields.append(StudioField("name", "Дубликат"))
    with pytest.raises(ValueError, match="повторяющееся поле"):
        project.source()


def test_studio_document_with_table_part_roundtrips_through_sqlite(tmp_path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QLineEdit

    from decui.studio import StudioWindow
    from decui.studio_project import StudioEntity

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = StudioProject(entities=[
        StudioEntity("Product", "Товары", fields=[StudioField("name", "Название", required=True)]),
        StudioEntity("Invoice", "Накладные", kind="Документ", numbering_prefix="INV"),
    ])
    window._load_project()
    window.entities.setCurrentRow(1)

    def fill_dialog():
        dialog = app.activeModalWidget()
        name, label = dialog.findChildren(QLineEdit)[:2]
        name.setText("Items")
        label.setText("Товары накладной")
        dialog.findChild(QComboBox).setCurrentText("Позиции")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, fill_dialog)
    window._edit_table_part(None)
    selected = window.table_part.currentText()
    assert selected == "Товары накладной"
    window._field_list().extend([
        StudioField("product", "Товар", kind="Ref", required=True, target="Product"),
        StudioField("quantity", "Количество", kind="Integer", required=True, default="1"),
    ])

    path = tmp_path / "invoice.decui.json"
    window.project.write(path)
    project = StudioProject.read(path)
    assert project.entities[1].table_parts[0].form_tab == "Позиции"
    assert "__form_table_tabs__ = {'Items': 'Позиции'}" in project.source()
    assert project == window.project
    project.export(tmp_path)
    program = """
from pathlib import Path
from app import Invoice, Product
from decui import Database

db_path = str(Path('invoice.sqlite3').resolve())
db = Database(db_path)
product = db.save(Product(name='Бумага'))
invoice = db.save(Invoice(Items=[{'product': product.id, 'quantity': 3}]), validate=True)
assert invoice.number
fresh = Database(db_path)
restored = fresh.get(Invoice, invoice.id)
assert restored.Items[0]["product"] == product.id
assert restored.Items[0]["quantity"] == 3
fresh.delete(restored)
assert fresh.get(Invoice, invoice.id) is None
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": workspace}, capture_output=True, text=True, timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_studio_register_export_posts_and_rolls_back_reposting(tmp_path):
    from decui.studio_project import (
        StudioRegister,
        StudioRegisterMember,
        invoice_template,
    )

    project = invoice_template()
    project.registers.append(StudioRegister("PriceHistory", "Сведения", [
        StudioRegisterMember("product", "Товар", "Измерение", "Ссылка", "Product"),
        StudioRegisterMember("price", "Цена", "Ресурс", "Десятичное"),
    ]))
    project_file = tmp_path / "warehouse.decui.json"
    project.write(project_file)
    StudioProject.read(project_file).export(tmp_path)
    program = """
from decimal import Decimal
from PySide6.QtWidgets import QApplication, QMessageBox
from app import Invoice, PriceHistory, Product, Stock, WarehouseBalances
from decui import Database, get_balance, get_latest, set_record
from decui.ui.form import AutoForm

qt = QApplication([])
db = Database.memory()
product = db.save(Product(name='Бумага'))
set_record(db, PriceHistory, {'product': product.id}, {'price': Decimal('12.50')})
assert get_latest(db, PriceHistory, product=product.id)['price'] == Decimal('12.50')
invoice = Invoice(Items=[{'product': product.id, 'quantity': 3, 'price': Decimal('12.50')}])
form = AutoForm(Invoice, db)
form.load_entity(invoice)
assert form._run_post()
assert db.get(Invoice, invoice.id).is_posted
assert WarehouseBalances().execute(db) == [{'product': 'Бумага', 'quantity': 3}]
row = form._tab_widgets['Items']._table.cellWidget(0, 1)
row.setValue(7)
original_write = Invoice.write_movements
def broken_write(self, database):
    original_write(self, database)
    raise RuntimeError('movement failed')
Invoice.write_movements = broken_write
QMessageBox.critical = lambda *args: None
assert not form._run_post()
assert db.get(Invoice, invoice.id).Items[0]['quantity'] == 3
assert get_balance(db, Stock)[0]['quantity'] == 3
Invoice.write_movements = original_write
assert form._run_post()
assert get_balance(db, Stock)[0]['quantity'] == 7
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_studio_movement_editor_maps_typed_fields_and_roundtrips(tmp_path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QDialogButtonBox,
        QTableWidget,
    )

    from decui.studio import StudioWindow
    from decui.studio_project import invoice_template

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = invoice_template()
    window._load_project()
    window.entities.setCurrentRow(1)
    window.current.movements.clear()

    def fill_dialog():
        dialog = app.activeModalWidget()
        register, source, direction = dialog.findChildren(QComboBox)[:3]
        register.setCurrentText("Stock")
        source.setCurrentIndex(source.findData("Items"))
        direction.setCurrentText("Расход")
        table = dialog.findChild(QTableWidget)
        for index, field_name in enumerate(("row:product", "row:quantity")):
            box = table.cellWidget(index, 1)
            box.setCurrentIndex(box.findData(field_name))
        assert table.cellWidget(0, 1).findData("row:quantity") == -1
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, fill_dialog)
    window._edit_movement(-1)
    movement = window.current.movements[0]
    assert not movement.income
    assert movement.mapping == {"product": "row:product", "quantity": "row:quantity"}
    path = tmp_path / "warehouse.decui.json"
    window.project.write(path)
    loaded = StudioProject.read(path)
    entry = loaded.export(tmp_path)
    program = """
from app import Invoice, Product, Stock
from decui import Database, get_balance

db = Database.memory()
item = db.save(Product(name='Бумага'))
invoice = db.save(Invoice(Items=[{'product': item.id, 'quantity': 3}]))
invoice.write_movements(db)
assert get_balance(db, Stock)[0]['quantity'] == -3
"""
    assert entry.is_file()
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": workspace}, capture_output=True, text=True, timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_studio_register_report_editor_exports_dated_grouped_balances(tmp_path):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialogButtonBox,
        QLineEdit,
        QListWidget,
    )

    from decui.studio import StudioWindow
    from decui.studio_project import (
        StudioRegister,
        StudioRegisterMember,
        StudioReport,
        invoice_template,
    )

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = invoice_template()
    window.project.registers[0].members.append(
        StudioRegisterMember("warehouse", "Склад", "Измерение", "Строка", required=False)
    )
    window.project.registers.append(StudioRegister("Prices", "Сведения", [
        StudioRegisterMember("product", "Товар", "Измерение", "Ссылка", "Product"),
        StudioRegisterMember("price", "Цена", "Ресурс", "Десятичное"),
    ]))
    window.project.reports.append(StudioReport(
        "PriceHistory", "История цен", "Prices", ["period", "product", "price"],
        source_kind="register", as_of=True,
    ))
    window._load_project()

    def fill_dialog():
        dialog = app.activeModalWidget()
        name, title = dialog.findChildren(QLineEdit)
        name.setText("BalancesByWarehouse")
        title.setText("Остатки по складам")
        source, sort, ref = dialog.findChildren(QComboBox)
        source.setCurrentIndex(source.findData("register:Stock"))
        sort.setCurrentIndex(sort.findData("-quantity"))
        ref.setCurrentIndex(ref.findData("product"))
        group_list = dialog.findChildren(QListWidget)[1]
        for i in (0, 2):
            group_list.item(i).setCheckState(Qt.CheckState.Checked)
        for box in dialog.findChildren(QCheckBox):
            box.setChecked(True)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, fill_dialog)
    window._edit_report(-1)
    report = window.project.reports[-1]
    assert report.source_kind == "register" and report.totals and report.as_of
    assert report.ref_filter == "product"
    assert report.group_by == ["product", "warehouse"]
    path = tmp_path / "report.decui.json"
    window.project.write(path)
    StudioProject.read(path).export(tmp_path)

    program = """
from datetime import date
from decimal import Decimal
from app import BalancesByWarehouse, PriceHistory, Prices, Product, Stock, WarehouseBalances
from decui import Database, set_record
from PySide6.QtWidgets import QApplication
from decui.ui.report_view import ReportView

qt = QApplication([])
db = Database.memory()
paper = db.save(Product(name='Бумага'))
pens = db.save(Product(name='Ручки'))
early, late = date(2026, 10, 1), date(2026, 10, 2)
set_record(db, Stock, {'product': paper.id, 'warehouse': 'A'}, {'quantity': 3}, period=early)
set_record(db, Stock, {'product': paper.id, 'warehouse': 'B'}, {'quantity': 5}, period=early)
set_record(db, Stock, {'product': paper.id, 'warehouse': 'A'}, {'quantity': 7}, period=late)
set_record(db, Stock, {'product': pens.id, 'warehouse': 'B'}, {'quantity': 2}, period=early)
dated = BalancesByWarehouse().execute(db, through=early, selected=paper.id)
assert dated == [
    {'product': 'Бумага', 'quantity': 5, 'warehouse': 'B', '_group_keys': {'product': paper.id}},
    {'product': 'Бумага', 'quantity': 3, 'warehouse': 'A', '_group_keys': {'product': paper.id}},
]
assert BalancesByWarehouse.columns[1].aggregate == 'sum'
view = ReportView(BalancesByWarehouse, db)
view._show_results(dated)
model = view._table.model()
assert not view._table.isSortingEnabled()
assert model.rowCount() == 6
assert model.index(0, 0).data() == 'Товар: Бумага'
assert model.index(0, 1).data() == '8'
assert model.index(1, 2).data() == '    Склад: B'
assert model.index(5, 0).data() == 'Итого'
assert model.index(5, 1).data() == '8'
assert BalancesByWarehouse.to_csv(dated).splitlines()[-1] == 'Итого;8;'
assert WarehouseBalances().execute(db, selected=paper.id) == [{'product': 'Бумага', 'quantity': 15}]
set_record(db, Prices, {'product': paper.id}, {'price': Decimal('4.50')}, period=early)
set_record(db, Prices, {'product': paper.id}, {'price': Decimal('6.50')}, period=late)
assert PriceHistory().execute(db, through=early) == [
    {'period': early, 'product': 'Бумага', 'price': Decimal('4.50')},
]
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"}, capture_output=True, text=True, timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_grouped_report_keeps_reference_identity_and_detail_drilldown():
    from typing import ClassVar

    from PySide6.QtWidgets import QApplication

    from decui import Column, Database, Report
    from decui.ui.report_view import ReportView

    class ProductReport(Report):
        group_by = ("product",)
        columns: ClassVar[list[Column]] = [
            Column("Товар", "product"),
            Column("Количество", "quantity", aggregate="sum"),
        ]

    qt = QApplication.instance() or QApplication([])
    view = ReportView(ProductReport, Database.memory())
    details = [
        {
            "product": "Бумага", "quantity": quantity, "_group_keys": {"product": product_id},
            "_entity_class": ProductReport, "_entity_id": product_id,
        }
        for product_id, quantity in ((1, 3), (2, 5))
    ]
    view._show_results(details)
    model = view._table.model()
    assert [model.index(row, 1).data() for row in (0, 2, 4)] == ["3", "5", "8"]
    opened = []
    view.drill_down_requested.connect(lambda entity, entity_id: opened.append((entity, entity_id)))
    view._on_double_click(model.index(2, 0))
    view._on_double_click(model.index(3, 0))
    assert opened == [(ProductReport, 2)]
    qt.processEvents()


def test_studio_approval_roundtrip_and_posting_gate(tmp_path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import (
        QApplication,
        QDialogButtonBox,
        QPlainTextEdit,
        QTableWidget,
    )

    from decui.studio import StudioWindow
    from decui.studio_project import approval_template

    qt = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = approval_template()
    window.project.preview_role = "reviewer"
    window._load_project()
    window.entities.setCurrentRow(1)

    def edit_route():
        dialog = qt.activeModalWidget()
        status_input = dialog.findChild(QPlainTextEdit)
        assert status_input.toPlainText().splitlines()[0] == "Черновик"
        steps = dialog.findChild(QTableWidget)
        steps.item(0, 0).setText("Отправить заявку")
        steps.item(2, 3).setText("reviewer, lead")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, edit_route)
    window._edit_workflow()
    path = tmp_path / "approval.decui.json"
    window.project.write(path)
    project = StudioProject.read(path)
    assert project == window.project
    entry = project.export(tmp_path)
    assert "set_role(" not in entry.read_text(encoding="utf-8")
    assert "set_role('reviewer')" in project.source(preview_database=tmp_path / "preview.sqlite3")
    program = """
import sqlite3
from decimal import Decimal
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidget
from app import Invoice, Product, Stock
from decui import Database, get_balance, set_role
from decui.ui.form import AutoForm

qt = QApplication([])
db = Database("approval.sqlite3")
product = db.save(Product(name='Бумага'))
invoice = db.save(Invoice(Items=[{'product': product.id, 'quantity': 3, 'price': Decimal('1')}]))
assert invoice.approval_status == 'Черновик'
stale = db.get(Invoice, invoice.id)
form = AutoForm(Invoice, db)
form.load_entity(invoice)
assert form._workflow_actions.currentText() == 'Отправить заявку'

QMessageBox.critical = lambda *args: None
assert not form._run_post()
assert not db.get(Invoice, invoice.id).is_posted
assert get_balance(db, Stock)[0]['quantity'] == 0

invoice.approval_status = 'Согласовано'
try:
    db.save(invoice)
except ValueError as exc:
    assert 'не разрешён' in str(exc)
else:
    raise AssertionError('Forbidden direct status update succeeded')
invoice.approval_status = 'Черновик'
form._run_workflow_action()
assert db.get(Invoice, invoice.id).approval_status == 'На согласовании'
try:
    db.transition(stale, 'Отправить заявку')
except ValueError as exc:
    assert 'изменился' in str(exc)
else:
    raise AssertionError('Action on stale copy succeeded')

form._workflow_actions.setCurrentText('Вернуть на доработку')
form._run_workflow_action()
assert invoice.approval_status == 'Черновик'
form._run_workflow_action()
assert form._workflow_actions.findText('Согласовать') == -1
before = db.workflow_history(invoice)
try:
    db.transition(invoice, 'Согласовать')
except PermissionError:
    pass
else:
    raise AssertionError('Restricted named action succeeded')
invoice.approval_status = 'Согласовано'
try:
    db.save(invoice)
except PermissionError:
    pass
else:
    raise AssertionError('Restricted direct state update succeeded')
assert db.workflow_history(invoice) == before
invoice.approval_status = 'На согласовании'
set_role('reviewer')
form.load_entity(invoice)
assert form._workflow_actions.findText('Согласовать') >= 0
form._workflow_actions.setCurrentText('Согласовать')
form._run_workflow_action()
assert form._workflow_actions.isHidden() and not form._workflow_box.isHidden()
assert invoice.approval_status == 'Согласовано'
assert form._run_post()
assert get_balance(db, Stock)[0]['quantity'] == 3
assert invoice.copy_for_edit(db).approval_status == 'Черновик'
reopened = Database("approval.sqlite3")
events = reopened.workflow_history(reopened.get(Invoice, invoice.id))
assert [(e.source, e.target, e.action) for e in events] == [
    ('На согласовании', 'Согласовано', 'Согласовать'),
    ('Черновик', 'На согласовании', 'Отправить заявку'),
    ('На согласовании', 'Черновик', 'Вернуть на доработку'),
    ('Черновик', 'На согласовании', 'Отправить заявку'),
    (None, 'Черновик', None),
]
assert all(e.occurred_at.tzinfo is not None for e in events)
assert [e.role for e in events] == ['reviewer', 'admin', 'admin', 'admin', 'admin']

def check_history():
    dialog = qt.activeModalWidget()
    table = dialog.findChild(QTableWidget)
    assert table.rowCount() == 5
    assert table.item(0, 1).text() == 'Согласовать'
    assert table.item(0, 4).text() == 'reviewer'
    dialog.accept()
QTimer.singleShot(0, check_history)
form._show_workflow_history()

draft = db.save(Invoice())
try:
    with db.transaction():
        db.transition(draft, 'Отправить заявку')
        assert len(db.workflow_history(draft)) == 2
        raise RuntimeError('rollback')
except RuntimeError:
    pass
assert db.get(Invoice, draft.id).approval_status == draft.approval_status == 'Черновик'
assert len(db.workflow_history(draft)) == 1

direct = db.get(Invoice, draft.id)
direct.approval_status = 'На согласовании'
db.save(direct)
assert db.workflow_history(direct)[0].action is None
deleted_id = draft.id
db.delete(direct)
replacement = db.save(Invoice())
assert replacement.id == deleted_id
assert len(db.workflow_history(replacement)) == 1

with sqlite3.connect('approval.sqlite3') as sqlite:
    sqlite.execute('ALTER TABLE _erp_workflow_events DROP COLUMN role')
    sqlite.execute("UPDATE _erp_config SET value = 'prior-schema' WHERE key = 'schema_hash'")
upgraded = Database('approval.sqlite3')
old_events = upgraded.workflow_history(upgraded.get(Invoice, invoice.id))
assert len(old_events) == 5 and all(e.role is None for e in old_events)
assert upgraded.workflow_history(upgraded.save(Invoice()))[0].role == 'reviewer'
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_print_form_exports_saved_document_and_filters_hidden_values(tmp_path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit

    from decui.studio import StudioWindow
    from decui.studio_project import invoice_template

    qt = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = invoice_template()
    window.project.local_accounts = True
    window.project.entities[1].table_parts[0].fields[2].read_roles = ["pricing"]
    window._load_project()
    window.entities.setCurrentRow(1)

    def update_form():
        dialog = qt.activeModalWidget()
        dialog.findChild(QLineEdit).setText("Накладная склада")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, update_form)
    window._edit_print_form()
    project_file = tmp_path / "print.decui.json"
    window.project.write(project_file)
    StudioProject.read(project_file).export(tmp_path)
    program = """
from decimal import Decimal
from pathlib import Path
from PySide6.QtWidgets import QApplication
from app import Invoice, Product
from decui import Database
from decui.accounts import Account, set_current_account

qt = QApplication([])
db = Database.memory()
set_current_account(Account('pricing', 'pricing'))
product = db.save(Product(name='Бумага <А4>'))
document = db.save(Invoice(Items=[{'product': product.id, 'quantity': 4, 'price': Decimal('125.32')}]))
print_form = document.__print_form__
document.Items[0]['price'] = Decimal('999')
html = print_form.render_html(db, document)
assert 'Накладная склада' in html
assert 'Бумага &lt;А4&gt;' in html
assert '125.32' in html and '999' not in html
set_current_account(Account('reader', 'reader'))
html = print_form.render_html(db, document)
assert 'Цена' not in html and '125.32' not in html
print_form.export_pdf(db, document, 'invoice.pdf')
assert Path('invoice.pdf').read_bytes().startswith(b'%PDF-')
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_studio_build_uses_export_settings_and_keeps_existing_executable(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from decui import studio

    qt = QApplication.instance() or QApplication([])
    window = studio.StudioWindow()
    window.project = goal_template()
    window.project.preview_role = "preview-user"
    window._load_project()

    def mock_builder(source, *, name, dist_dir):
        generated = source.read_text(encoding="utf-8")
        assert "preview.sqlite3" not in generated
        assert "preview-user" not in generated
        filename = name + (".exe" if sys.platform == "win32" else "")
        output = dist_dir / filename
        return [
            "-c",
            (
                "from pathlib import Path; "
                f"p = Path({str(output)!r}); p.parent.mkdir(); "
                "p.write_bytes(b'executable'); p.chmod(0o755)"
            ),
        ]

    monkeypatch.setattr(studio, "build_arguments", mock_builder)
    destination = tmp_path / "application"
    window._start_project("build", destination)
    assert window.process.waitForFinished(15_000)
    assert destination.read_bytes() == b"executable"
    if sys.platform != "win32":
        assert os.access(destination, os.X_OK)
    assert window.preview_dir is None
    destination.write_bytes(b"existing")
    window._start_project("build", destination)
    assert "уже существует" in window.console.toPlainText()
    assert destination.read_bytes() == b"existing"
    qt.processEvents()


def test_studio_jumps_to_local_and_generated_python_errors():
    from PySide6.QtWidgets import QApplication

    from decui.studio import StudioWindow

    qt = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = goal_template()
    window._load_project()
    window.entities.setCurrentRow(1)
    window.code.setPlainText("\n\ndef broken(:\n    pass")
    window.check()
    assert window.diagnostic_button.isEnabled()
    window.diagnostic_button.click()
    assert window.tabs.currentIndex() == 0
    assert window.code.textCursor().selectedText() == "def broken(:"
    assert window.code.textCursor().blockNumber() == 2

    window.code.clear()
    window.logic.setPlainText("raise RuntimeError('runtime test')")
    window.check()
    assert window.process.waitForFinished(15_000)
    assert window.diagnostic_button.isEnabled()
    assert "runtime test" in window.console.toPlainText()
    window.diagnostic_button.click()
    assert window.tabs.currentIndex() == 1
    assert window.logic.textCursor().selectedText() == "raise RuntimeError('runtime test')"

    window.entities.setCurrentRow(1)
    window.code.setPlainText("\n\ndef broken(self):\n    raise RuntimeError('inside object')")
    window.logic.setPlainText("Task().broken()")
    window.check()
    assert window.process.waitForFinished(15_000)
    assert "inside object" in window.console.toPlainText()
    window.diagnostic_button.click()
    assert window.tabs.currentIndex() == 0
    assert window.code.textCursor().blockNumber() == 3
    assert window.code.textCursor().selectedText().strip() == "raise RuntimeError('inside object')"

    window.code.clear()
    window.diagnostic_button.click()
    assert window.tabs.currentWidget() is window.source
    assert window.source.textCursor().selectedText().strip() == "raise RuntimeError('inside object')"
    assert window.preview_dir is None
    qt.processEvents()


def test_studio_stages_backup_without_touching_original_or_bypassing_login(tmp_path):
    import sqlite3

    from PySide6.QtWidgets import QApplication

    from decui.files import stage_decui_backup
    from decui.studio import StudioWindow

    qt = QApplication.instance() or QApplication([])
    project = goal_template()
    bootstrap = tmp_path / "bootstrap"
    project.export(bootstrap)
    archive = tmp_path / "snapshot.sqlite3"
    setup = """
from app import Goal
from decui import Database
import sys

db = Database.memory()
with db.session() as session:
    connection = session.connection()
    connection.exec_driver_sql("CREATE TABLE backup_marker (value TEXT)")
    connection.exec_driver_sql("INSERT INTO backup_marker VALUES ('original')")
db.backup(sys.argv[1])
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", setup, str(archive)], cwd=bootstrap,
        env={**os.environ, "PYTHONPATH": workspace}, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    original = archive.read_bytes()

    window = StudioWindow()
    window.project = project
    window.project.preview_role = "test-role"
    window.project.logic = (
        "def configure_app(app: ERPApplication) -> None:\n"
        "    with app.database.session() as session:\n"
        "        cursor = session.connection().exec_driver_sql('SELECT value FROM backup_marker')\n"
        "        assert cursor.scalar() == 'original'\n"
        "        session.connection().exec_driver_sql(\"UPDATE backup_marker SET value = 'preview'\")\n"
        "    print('Working copy opened', flush=True)\n"
        "    raise SystemExit(0)"
    )
    window._load_project()
    window._start_project("backup", backup=archive)
    assert window.process.waitForFinished(15_000)
    assert "Working copy opened" in window.console.toPlainText()
    assert archive.read_bytes() == original
    assert window.preview_dir is None
    assert "set_role('test-role')" not in window._entry_snapshot[1]

    with sqlite3.connect(archive) as connection:
        connection.execute("CREATE TABLE _erp_accounts (login TEXT PRIMARY KEY)")
    staged = tmp_path / "working.sqlite3"
    assert stage_decui_backup(archive, staged)
    generated = window.project.source(preview_database=staged, backup_has_accounts=True)
    assert "local_accounts=True" in generated and "set_role(" not in generated
    qt.processEvents()


def test_studio_initial_data_resolves_dependencies_and_loads_only_once(tmp_path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QTableWidget

    from decui.studio import StudioWindow
    from decui.studio_project import invoice_template

    qt = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = invoice_template()
    window._load_project()

    def edit_seed():
        dialog = qt.activeModalWidget()
        dialog.findChild(QTableWidget).item(0, 1).setText("Бумага для склада")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, edit_seed)
    window._edit_seed(0)
    window.project.initial_data.reverse()
    spec = tmp_path / "warehouse.decui.json"
    window.project.write(spec)
    project = StudioProject.read(spec)
    assert project.initial_data[0].key == "delivery"
    assert "first_run=_seed_initial_data" in project.source()
    assert "first_run=_seed_initial_data" not in project.source(
        preview_database=tmp_path / "backup.sqlite3", backup_has_accounts=False,
    )
    project.export(tmp_path)
    program = """
from app import Invoice, Product, _seed_initial_data
from decui import Database

db = Database('warehouse.sqlite3')
assert db.initialize(_seed_initial_data)
record = db.get(Invoice, db.list(Invoice)[0].id)
paper = db.filter(Product, name='Бумага для склада')[0]
assert record.Items[0]['product'] == paper.id
assert record.Items[0]['quantity'] == 12
assert not db.initialize(_seed_initial_data)
assert len(db.list(Product)) == 2
assert len(db.list(Invoice)) == 1
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": workspace}, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_initial_data_rejects_circular_references():
    from decui.studio_project import StudioEntity, StudioSeed

    project = StudioProject(
        entities=[StudioEntity("Category", "Категории", fields=[
            StudioField("parent", "Родитель", kind="Ref", target="Category"),
        ])],
        initial_data=[
            StudioSeed("one", "Category", {"parent": "two"}),
            StudioSeed("two", "Category", {"parent": "one"}),
        ],
    )
    with pytest.raises(ValueError, match="по кругу"):
        project.source()



def test_field_dialog_updates_form_preview_before_saving():
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLineEdit, QTabWidget

    from decui.studio import StudioWindow

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = goal_template()
    window._load_project()
    window.entities.setCurrentRow(0)
    window.saved = repr(window.project)
    window._field_list()[0].form_tab = "До изменения"
    observed = []

    def edit_draft():
        dialog = app.activeModalWidget()
        try:
            tabs = dialog.findChild(QTabWidget)
            observed.append(tabs.tabText(tabs.currentIndex()))
            dialog.findChild(QLineEdit, "field_form_tab").setText("После изменения")
            tabs = dialog.findChild(QTabWidget)
            observed.append(tabs.tabText(tabs.currentIndex()))
        finally:
            dialog.reject()

    QTimer.singleShot(0, edit_draft)
    window._field_dialog(0)
    assert observed == ["До изменения", "После изменения"]
    assert window._field_list()[0].form_tab == "До изменения"
    window.saved = repr(window.project)
    window.close()



def test_studio_field_order_follows_header_moves_and_survives_reload(tmp_path):
    from PySide6.QtWidgets import QApplication

    from decui.studio import StudioWindow
    from decui.studio_project import StudioEntity, StudioTablePart

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    main_fields = [StudioField(name, name) for name in ("first", "second", "third")]
    part_fields = [StudioField(name, name) for name in ("item", "quantity")]
    window.project = StudioProject(entities=[
        StudioEntity("Example", "Пример", fields=main_fields, table_parts=[
            StudioTablePart("Rows", "Строки", part_fields),
        ]),
    ])
    window._load_project()
    window.entities.setCurrentRow(0)
    window.fields.verticalHeader().moveSection(0, 2)
    assert window.fields.currentRow() == 2
    window._move_selected_field(-1)
    assert [window.fields.item(row, 0).text() for row in range(3)] == ["second", "first", "third"]
    window.table_part.setCurrentIndex(1)
    window.fields.verticalHeader().moveSection(1, 0)
    path = tmp_path / "order.decui.json"
    window.project.write(path)
    project = StudioProject.read(path)
    entity = project.entities[0]
    assert [field.name for field in entity.fields] == ["second", "first", "third"]
    assert [field.name for field in entity.table_parts[0].fields] == ["quantity", "item"]
    source = project.source()
    assert source.index("second:") < source.index("first:") < source.index("third:")
    assert source.index("quantity:") < source.index("item:")
    window.saved = repr(window.project)
    window.close()
    app.processEvents()



def test_studio_object_editor_scrolls_on_small_screen():
    from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea

    from decui.studio import StudioWindow

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = goal_template()
    window._load_project()
    window.entities.setCurrentRow(1)
    window.resize(800, 600)
    window.show()
    app.processEvents()
    assert (window.width(), window.height()) == (800, 600)
    page = window.tabs.widget(0)
    assert isinstance(page, QScrollArea)
    assert page.horizontalScrollBar().maximum() == 0
    assert page.verticalScrollBar().maximum() > 0
    page.ensureWidgetVisible(window.code)
    app.processEvents()
    assert page.viewport().rect().contains(window.code.geometry().translated(
        window.editor.mapTo(page.viewport(), window.editor.rect().topLeft())
    ))
    buttons = [button for button in window.editor.findChildren(QPushButton) if button.isVisible()]
    assert all(not left.geometry().intersects(right.geometry())
               for index, left in enumerate(buttons) for right in buttons[index + 1:])
    window.saved = repr(window.project)
    window.close()


def test_order_invoice_template_creates_and_posts_invoice_from_seed_order(tmp_path):
    from decui.studio_project import order_invoice_template

    project = order_invoice_template()
    path = tmp_path / "orders.decui.json"
    project.write(path)
    StudioProject.read(path).export(tmp_path)
    program = """
from decimal import Decimal
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidget, QTabWidget
from app import Invoice, SalesOrder, WarehouseBalances, _seed_initial_data
from decui import Database
from decui.ui.form import AutoForm

app = QApplication([])
db = Database.memory()
assert db.initialize(_seed_initial_data)
order = db.get(SalesOrder, db.list(SalesOrder)[0].id)
assert order.number == '00000001'
assert not db.list(Invoice)
form = AutoForm(SalesOrder, db)
form.load_entity(order)
action = next(action for action in form._basis_menu.actions() if action.text() == 'Создать накладную')
outcomes = []
errors = []
QMessageBox.warning = lambda *args: errors.append(str(args[-1]))
QMessageBox.critical = lambda *args: errors.append(str(args[-1]))

def post_invoice():
    dialog = app.activeModalWidget()
    try:
        invoice_form = dialog.findChild(AutoForm)
        outcomes.append(invoice_form._run_post())
    finally:
        dialog.accept()

QTimer.singleShot(0, post_invoice)
action.trigger()
assert outcomes == [True], errors
invoice = db.get(Invoice, db.list(Invoice)[0].id)
assert invoice.order_ref == order.id
assert invoice.comment == 'Доставить до обеда'
assert invoice.number == '00000001'
assert invoice.posted
assert [(row['quantity'], row['price']) for row in invoice.Items] == [
    (12, Decimal('380.00')), (40, Decimal('28.50')),
]
assert sorted(row['quantity'] for row in WarehouseBalances().execute(db)) == [12, 40]
assert sorted(row["quantity"] for rows in db.document_movements(invoice).values() for row in rows) == [12, 40]

movements_form = AutoForm(Invoice, db)
movements_form.load_entity(invoice)
displayed = []

def inspect_movements():
    dialog = app.activeModalWidget()
    try:
        tabs = dialog.findChild(QTabWidget)
        table = tabs.widget(0)
        assert isinstance(table, QTableWidget)
        displayed.append((tabs.tabText(0), sorted(
            (table.item(row, 1).text(), table.item(row, 2).text()) for row in range(table.rowCount())
        )))
    finally:
        dialog.reject()

QTimer.singleShot(0, inspect_movements)
movements_form._movements_button.click()
assert displayed == [("Stock", [("Бумага А4", "12"), ("Ручки синие", "40")])]

try:
    with db.transaction():
        db.unpost(invoice)
        assert not invoice.is_posted
        assert WarehouseBalances().execute(db) == []
        raise RuntimeError("Отменить операцию")
except RuntimeError:
    pass
assert invoice.is_posted
assert sorted(row['quantity'] for row in WarehouseBalances().execute(db)) == [12, 40]

invoice_form = AutoForm(Invoice, db)
invoice_form.load_entity(invoice)
invoice_form._widgets["comment"].setPlainText("Несохранённое изменение")
messages = []
def confirm(*args):
    messages.append(args[2])
    return QMessageBox.StandardButton.Yes
QMessageBox.question = confirm
assert not invoice_form._unpost_button.isHidden()
invoice_form._unpost_button.click()
assert "Несохранённые изменения" in messages[0]
assert not invoice_form._entity.is_posted
assert invoice_form._unpost_button.isHidden()
assert not db.get(Invoice, invoice.id).is_posted
assert db.get(Invoice, invoice.id).comment == "Доставить до обеда"
assert WarehouseBalances().execute(db) == []
assert db.document_movements(invoice) == {}
"""
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr



def test_studio_welcome_opens_example_and_local_guide(monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication, QPushButton

    from decui.studio import StudioWindow

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.resize(800, 600)
    window.show()
    app.processEvents()
    assert window.welcome.isVisible()
    assert not window.editor.isVisible()
    urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: not urls.append(url))
    next(button for button in window.findChildren(QPushButton) if button.text() == "Учебник").click()
    assert urls[0].isLocalFile() and Path(urls[0].toLocalFile()).name == "index.html"
    next(button for button in window.welcome.findChildren(QPushButton) if "Заказы и накладные" in button.text()).click()
    assert window.project.title == "Заказы и накладные"
    assert window.editor.isVisible()
    assert not window.welcome.isVisible()
    window.saved = repr(window.project)
    window.close()



def test_studio_renaming_metadata_updates_dependent_settings():
    from decui.studio_project import order_invoice_template

    project = order_invoice_template()
    _, order, invoice = project.entities
    report = StudioReport("OrderNotes", "Заказы", order.name, ["instructions"], "-instructions", group_by=["instructions"])
    project.reports.append(report)
    for entity, part, old, new in (
        (order, "", "instructions", "delivery_notes"),
        (invoice, "", "comment", "delivery_comment"),
        (order, "Lines", "ordered_qty", "requested"),
        (invoice, "Items", "quantity", "received"),
    ):
        fields = next(table.fields for table in entity.table_parts if table.name == part) if part else entity.fields
        next(item for item in fields if item.name == old).name = new
        project.rename_field_references(entity, part, old, new)
    for entity, old, new in ((order, "Lines", "Detail"), (invoice, "Items", "Receipt")):
        entity.table_parts[0].name = new
        project.rename_table_references(entity, old, new)

    link = invoice.fields[0]
    assert link.basis_fields == {"delivery_comment": "delivery_notes"}
    assert link.basis_tables["Receipt"] == {
        "source": "Detail", "columns": {"product": "product", "received": "requested", "price": "price"},
    }
    seed = project.initial_data[-1]
    assert seed.values == {"delivery_notes": "Доставить до обеда"}
    assert seed.tables["Detail"][0]["requested"] == "12"
    assert report.columns == report.group_by == ["delivery_notes"]
    assert report.order_by == "-delivery_notes"
    assert invoice.movements[0].table_part == "Receipt"
    assert invoice.movements[0].mapping["quantity"] == "row:received"
    assert invoice.print_form.tables["Receipt"] == ["product", "received", "price"]
    compile(project.source(), "app.py", "exec")


def test_studio_column_migration_from_dialog_survives_export(tmp_path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QPushButton, QTableWidget

    from decui.studio import StudioWindow
    from decui.studio_project import order_invoice_template

    qt = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = order_invoice_template()
    window._load_project()
    observations = []

    def configure(owner_name, old_name, new_name):
        dialog = qt.activeModalWidget()
        try:
            next(button for button in dialog.findChildren(QPushButton) if button.text() == "Добавить строку").click()
            table = dialog.findChild(QTableWidget)
            owner = table.cellWidget(0, 0)
            target = table.cellWidget(0, 2)
            assert isinstance(owner, QComboBox) and isinstance(target, QComboBox)
            owner.setCurrentText(owner_name)
            target.setCurrentText(new_name)
            table.item(0, 1).setText(old_name)
            observations.append(target.currentText())
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, lambda: configure("SalesOrder.Lines", "previous_qty", "ordered_qty"))
    window._edit_column_renames()
    QTimer.singleShot(0, lambda: configure("SalesOrder", "PreviousLines", "Lines"))
    window._edit_column_renames(table_parts=True)
    assert observations == ["ordered_qty", "Lines"]
    path = tmp_path / "orders.decui.json"
    window.project.write(path)
    project = StudioProject.read(path)
    assert project.table_part_renames == {"SalesOrder": {"PreviousLines": "Lines"}}
    assert project.column_renames == {"SalesOrder.Lines": {"previous_qty": "ordered_qty"}}
    for options in ({}, {"preview_database": tmp_path / "copy.sqlite3", "backup_has_accounts": False}):
        assert "_DECUI_TABLE_PART_RENAMES = {'SalesOrder': {'PreviousLines': 'Lines'}}" in project.source(**options)
        assert "_DECUI_COLUMN_RENAMES = {'SalesOrder.Lines': {'previous_qty': 'ordered_qty'}}" in project.source(**options)
    with pytest.raises(ValueError, match="проверьте старые и новые имена"):
        project.validate_column_renames({"SalesOrder.Lines": {"product": "ordered_qty"}})
    window.saved = repr(window.project)
    window.close()


def test_studio_checks_migration_on_copy_without_opening_app(tmp_path):
    import sqlite3
    from contextlib import closing

    from PySide6.QtWidgets import QApplication

    from decui.studio import StudioWindow
    from decui.studio_project import StudioEntity

    app = QApplication.instance() or QApplication([])
    archive = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(archive)) as connection, connection:
        connection.executescript("""
            CREATE TABLE _erp_config (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO _erp_config VALUES ('schema_hash', 'legacy');
            CREATE TABLE buyer (id INTEGER PRIMARY KEY, previous_name TEXT);
            INSERT INTO buyer VALUES (1, 'Покупатель');
        """)
    window = StudioWindow()
    window.project = StudioProject(
        entities=[StudioEntity("Buyer", "Покупатели", fields=[StudioField("name", "Имя")])],
        column_renames={"Buyer": {"previous_name": "name"}},
        logic="def configure_app(app):\n    raise RuntimeError('GUI must not start')",
    )
    window._load_project()
    original = archive.read_bytes()
    window._start_project("migration", backup=archive)
    assert window.process.waitForFinished(15_000)
    assert "Копия базы открыта" in window.console.toPlainText()
    assert "переименована колонка «previous_name» → «name»" in window.console.toPlainText()
    assert window.process.exitCode() == 0
    assert window.preview_dir is None
    assert archive.read_bytes() == original

    with closing(sqlite3.connect(archive)) as connection, connection:
        connection.execute("ALTER TABLE buyer ADD COLUMN name TEXT")
    original = archive.read_bytes()
    window._start_project("migration", backup=archive)
    assert window.process.waitForFinished(15_000)
    assert "существуют оба столбца" in window.console.toPlainText()
    assert window.process.exitCode() == 1
    assert archive.read_bytes() == original
    with closing(sqlite3.connect(archive)) as connection, connection:
        connection.execute("ALTER TABLE buyer DROP COLUMN previous_name")
    original = archive.read_bytes()
    window._start_project("migration", backup=archive)
    assert window.process.waitForFinished(15_000)
    assert window.process.exitCode() == 0
    assert "колонка «name» уже существует" in window.console.toPlainText()
    assert archive.read_bytes() == original
    window.saved = repr(window.project)
    window.close()
    app.processEvents()


def test_studio_list_configuration_exports_visible_order_and_default_sort(tmp_path):
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QListWidget

    from decui.studio import StudioWindow
    from decui.studio_project import StudioEntity

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = StudioProject(entities=[
        StudioEntity("PriceItem", "Товары", fields=[
            StudioField("name", "Название"),
            StudioField("price", "Цена", "Integer"),
            StudioField("note", "Заметка"),
        ]),
    ])
    window._load_project()

    def configure():
        dialog = app.activeModalWidget()
        try:
            fields = dialog.findChild(QListWidget)
            fields.item(2).setCheckState(Qt.CheckState.Unchecked)
            fields.insertItem(0, fields.takeItem(1))
            sort, direction = dialog.findChildren(QComboBox)
            sort.setCurrentIndex(sort.findData("price"))
            direction.setCurrentIndex(1)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, configure)
    window._edit_list_columns()
    path = tmp_path / "prices.decui.json"
    window.project.write(path)
    project = StudioProject.read(path)
    assert project.entities[0].list_columns == ["price", "name"]
    assert project.entities[0].list_order == "-price"
    project.export(tmp_path)
    program = """
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from app import PriceItem
from decui import Database
from decui.ui.listview import ListView

app = QApplication([])
db = Database.memory()
db.save(PriceItem(name="Бумага", price=3, note="Первая"))
db.save(PriceItem(name="Папка", price=10, note="Вторая"))
view = ListView(PriceItem, db)
model = view._table.model()
assert [model.headerData(i, Qt.Orientation.Horizontal) for i in range(model.columnCount())] == ["Цена", "Название"]
assert model.data(model.index(0, 0)) == "10"
"""
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    window.saved = repr(window.project)
    window.close()


def test_studio_exposes_plain_method_as_action(tmp_path):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QPushButton, QTableWidget

    from decui.studio import StudioWindow
    from decui.studio_project import StudioEntity

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = StudioProject(local_accounts=True, entities=[
        StudioEntity("ActionCatalog", "Карточки", fields=[StudioField("name", "Имя")], code=(
            "def mark(self):\n    self.name = 'Готово'\n\n"
            "def needs_argument(self, value):\n    self.name = value"
        )),
    ])
    window._load_project()
    offered = []

    def configure():
        dialog = app.activeModalWidget()
        try:
            next(button for button in dialog.findChildren(QPushButton) if button.text() == "Добавить действие").click()
            table = dialog.findChild(QTableWidget)
            method = table.cellWidget(0, 0)
            assert isinstance(method, QComboBox)
            offered.extend(method.itemText(index) for index in range(method.count()))
            table.item(0, 1).setText("Отметить")
            table.item(0, 2).setText("accountant")
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, configure)
    window._edit_commands()
    assert offered == ["mark"]
    assert window.project.entities[0].commands == {"mark": "Отметить"}
    assert window.project.entities[0].command_roles == {"mark": ["accountant"]}
    source = window.project.source()
    assert "mark = command('Отметить')(mark)" in source
    path = tmp_path / "actions.decui.json"
    window.project.write(path)
    StudioProject.read(path).export(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", """from app import ActionCatalog
from decui import Database
from decui.accounts import Account, set_current_account
from decui.ui.form import AutoForm
from PySide6.QtWidgets import QApplication, QPushButton
app = QApplication([])
db = Database.memory()
set_current_account(Account("clerk", "clerk"))
draft = ActionCatalog(name="Черновик")
form = AutoForm(ActionCatalog, db)
form.load_entity(draft)
assert not any(button.text() == "Отметить" for button in form.findChildren(QPushButton))
try:
    db.execute_command(draft, "mark")
except PermissionError:
    pass
else:
    raise AssertionError("Unauthorized command accepted")
assert draft.name == "Черновик"
set_current_account(Account("bookkeeper", "accountant"))
record = db.execute_command(draft, "mark")
assert db.get(ActionCatalog, record.id).name == "Готово"
"""],
        cwd=tmp_path, capture_output=True, text=True, timeout=15, check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    window.saved = repr(window.project)
    window.close()


def test_studio_can_create_action_method_and_cancel_draft(monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QInputDialog, QPushButton

    from decui.studio import StudioWindow
    from decui.studio_project import StudioEntity

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = StudioProject(entities=[StudioEntity("Product", "Товары")])
    window._load_project()
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("mark", True))

    def create(save):
        dialog = app.activeModalWidget()
        try:
            next(button for button in dialog.findChildren(QPushButton) if button.text() == "Создать метод…").click()
            which = QDialogButtonBox.StandardButton.Save if save else QDialogButtonBox.StandardButton.Cancel
            dialog.findChild(QDialogButtonBox).button(which).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, lambda: create(False))
    window._edit_commands()
    assert window.code.toPlainText() == ""
    assert window.project.entities[0].commands == {}

    QTimer.singleShot(0, lambda: create(True))
    window._edit_commands()
    assert window.code.toPlainText() == "def mark(self) -> None:\n    pass\n"
    assert window.code.textCursor().selectedText() == "pass"
    assert window.project.entities[0].commands == {"mark": "Новое действие"}
    assert "mark = command('Новое действие')(mark)" in window.project.source()
    window.saved = repr(window.project)
    window.close()


def test_studio_python_editor_indents_suites_and_highlights_multiline_strings():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextCursor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from decui.ui.python_editor import PythonEditor

    app = QApplication.instance() or QApplication([])
    editor = PythonEditor()
    editor.setPlainText('def handle(self):  # комментарий')
    editor.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText().endswith("\n    ")
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert editor.toPlainText().endswith("\n        ")

    editor.setPlainText('    text = "# строка:"')
    editor.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert editor.toPlainText().endswith("\n    ")

    editor.setPlainText('text = """Первая\n# часть строки\n"""\nreturn 3')
    editor._highlighter.rehighlight()
    app.processEvents()
    middle = editor.document().findBlockByNumber(1).layout().formats()
    assert [(span.format.foreground().color().name(), span.length) for span in middle] == [("#17653e", 14)]


def test_preview_shows_command_traceback_while_process_is_running():
    import time

    from PySide6.QtCore import QProcess
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from decui.studio import StudioWindow
    from decui.studio_project import StudioEntity

    app = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = StudioProject(
        entities=[StudioEntity(
            "PreviewItem", "Товары",
            fields=[StudioField("name", "Имя")],
            code="def fail(self):\n    raise RuntimeError('Ошибка тестовой команды')",
            commands={"fail": "Проверка"},
        )],
        logic="""def configure_app(app):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox

    def click():
        QMessageBox.critical = lambda *args: None
        window = app.main_window
        window._on_create(PreviewItem)
        window._active_widget._execute_command("fail")

    QTimer.singleShot(0, click)
""",
    )
    window._load_project()
    window.preview()
    try:
        deadline = time.monotonic() + 8
        while "RuntimeError: Ошибка тестовой команды" not in window.console.toPlainText() and time.monotonic() < deadline:
            QTest.qWait(30)
        assert window.process.state() == QProcess.ProcessState.Running
        assert "RuntimeError: Ошибка тестовой команды" in window.console.toPlainText()
        assert window.diagnostic_button.isEnabled()
        window.diagnostic_button.click()
        assert window.tabs.currentIndex() == 0
        assert window.code.textCursor().selectedText().strip() == "raise RuntimeError('Ошибка тестовой команды')"
    finally:
        if window.process.state() != QProcess.ProcessState.NotRunning:
            window.process.kill()
            window.process.waitForFinished(5000)
        window.saved = repr(window.project)
        window.close()
        app.processEvents()
