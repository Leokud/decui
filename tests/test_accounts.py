from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from decui import ERPApplication, current_role, set_role
from decui.ui.accounts import AccountDialog


def test_local_accounts_bootstrap_login_and_preview_export(tmp_path):
    from decui.studio_project import approval_template

    qt = QApplication.instance() or QApplication([])
    database = str(tmp_path / "accounts.sqlite3")
    set_role("previous")

    def create_first():
        dialog = qt.activeModalWidget()
        assert isinstance(dialog, AccountDialog) and dialog.mode == "first"
        dialog.login.setText("Owner")
        dialog.password.setText("test owner password")
        dialog.confirmation.setText("test owner password")
        dialog._submit()

    QTimer.singleShot(0, create_first)
    owner = ERPApplication(db_path=database, local_accounts=True)
    assert owner.account is not None and owner.account.login == "owner"
    assert current_role() == "admin"
    def create_reviewer():
        dialog = qt.activeModalWidget()
        assert isinstance(dialog, AccountDialog) and dialog.mode == "create"
        dialog.login.setText("Reviewer")
        dialog.role.setText("reviewer")
        dialog.password.setText("test reviewer password")
        dialog.confirmation.setText("test reviewer password")
        dialog._submit()

    menu = owner._account_menu
    QTimer.singleShot(0, create_reviewer)
    menu.actions()[0].trigger()
    with owner.database.session() as session:
        raw = session.connection().exec_driver_sql(
            "SELECT salt, password_hash FROM _erp_accounts WHERE login = 'reviewer'"
        ).one()
    assert len(raw.salt) == 32 and raw.password_hash != "test reviewer password"
    assert owner.main_window.menuBar().actions()[0].text() == "Учётные записи"

    def sign_in():
        dialog = qt.activeModalWidget()
        assert isinstance(dialog, AccountDialog) and dialog.mode == "login"
        dialog.login.setText("REVIEWER")
        dialog.password.setText("wrong password")
        dialog._submit()
        assert dialog.account is None and dialog.error.text()
        dialog.password.setText("test reviewer password")
        dialog._submit()

    QTimer.singleShot(0, sign_in)
    reviewer = ERPApplication(db_path=database, local_accounts=True)
    assert reviewer.account is not None and reviewer.account.role == "reviewer"
    assert current_role() == "reviewer"
    assert not reviewer.main_window.menuBar().actions()
    set_role("admin")

    project = approval_template()
    project.local_accounts = True
    project.preview_role = "reviewer"
    assert "local_accounts=True" in project.source()
    preview = project.source(preview_database=tmp_path / "preview.sqlite3")
    assert "set_role('reviewer')" in preview and "local_accounts=True" not in preview


def test_assigned_reviewer_and_actor_history(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from decui.studio_project import approval_template

    project = approval_template()
    project.local_accounts = True
    project.entities[1].owner_only = True
    approval = project.entities[1].workflow.transitions[2]
    approval.assigned_only = True
    approval.roles = ["reviewer"]
    project.export(tmp_path)
    program = """
from PySide6.QtWidgets import QApplication, QInputDialog
from app import Invoice, Product
from decui import Database, set_role
from decui.accounts import AccountStore, set_current_account
from decui.ui.form import AutoForm

app = QApplication([])
db = Database('assigned.sqlite3')
store = AccountStore(db)
admin = store.create_first_admin('Owner', 'secret for owner')
reviewer = store.create('Alice', 'secret for alice', role='reviewer')
other = store.create('Bob', 'secret for bobby', role='reviewer')

def enter(account):
    set_current_account(account)
    set_role(account.role)

enter(admin)
product = db.save(Product(name="Бумага"))
invoice = db.save(Invoice(Items=[{"product": product.id, "quantity": 3}]))
form = AutoForm(Invoice, db)
form.load_entity(invoice)
original = QInputDialog.getItem
QInputDialog.getItem = lambda *args: ('alice', True)
form._assign_workflow()
QInputDialog.getItem = original
assert invoice.approval_assignee == 'alice'
assert db.workflow_history(invoice)[0].action == 'Назначен согласующий: alice'
db.transition(invoice, 'Отправить')

enter(other)
assert db.get(Invoice, invoice.id) is None
assert not db.workflow_history(invoice)
try:
    db.transition(invoice, 'Согласовать')
except (ValueError, PermissionError):
    pass
else:
    raise AssertionError('Wrong reviewer could approve')
invoice.approval_assignee = 'bob'
try:
    db.save(invoice)
except PermissionError:
    pass
else:
    raise AssertionError('Reviewer could change the assignment')
invoice_id = invoice.id

enter(reviewer)
invoice = db.get(Invoice, invoice_id)
assert invoice is not None and db.list(Invoice)[0].id == invoice_id
form.load_entity(invoice)
assert not form._field_panel.isEnabled()
assert all(not button.isEnabled() for button in form._write_buttons)
assert not form._run_post()
assert form._workflow_actions.currentText() == 'Согласовать'
invoice.Items[0]['quantity'] = 50
try:
    db.save(invoice)
except PermissionError:
    pass
else:
    raise AssertionError('Reviewer could change quantity')
form._run_workflow_action()
invoice = db.get(Invoice, invoice_id)
assert invoice.approval_status == 'Согласовано'
assert invoice.Items[0]['quantity'] == 3
assert not invoice.is_posted
try:
    db.delete(invoice)
except PermissionError:
    pass
else:
    raise AssertionError('Reviewer could delete the document')
assert db.workflow_history(invoice)[0].login == 'alice'
assert invoice.copy_for_edit(db).approval_assignee is None
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_studio_owner_policy_scopes_records_and_register_reports(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from decui.studio_project import invoice_template

    project = invoice_template()
    project.local_accounts = True
    project.preview_role = "admin"
    project.entities[1].owner_only = True
    assert "local_accounts=True" in project.source(preview_database=tmp_path / "preview.sqlite3")
    project.export(tmp_path)
    program = """
from app import Invoice, Product, Stock, WarehouseBalances
from decui import Database, get_balance, set_role
from decui.accounts import AccountStore, set_current_account

db = Database.memory()
store = AccountStore(db)
admin = store.create_first_admin('Admin', 'test admin password')
alice = store.create('Alice', 'test alice password', role='clerk')
bob = store.create('Bob', 'test bob password', role='clerk')
product = db.save(Product(name='Товар'))

set_current_account(alice)
first = db.save(Invoice(Items=[{'product': product.id, 'quantity': 7}]))
first.write_movements(db)
assert first.record_owner == 'alice'
set_current_account(bob)
second = db.save(Invoice(record_owner='alice', Items=[{'product': product.id, 'quantity': 3}]))
second.write_movements(db)
assert second.record_owner == 'bob'
set_role('admin')  # the separate workflow role does not grant access to the record
assert db.get(Invoice, first.id) is None
assert [item.id for item in db.list(Invoice)] == [second.id]
assert [item.id for item in db.filter(Invoice, order_by='id', limit=1)] == [second.id]
for action in (lambda: db.save(first), lambda: db.delete(first), lambda: first.write_movements(db)):
    try:
        action()
    except PermissionError:
        pass
    else:
        raise AssertionError("Another user's record was writable")
first.clear_movements(db)
assert WarehouseBalances().execute(db) == [{'product': 'Товар', 'quantity': 3}]
set_current_account(None)
assert db.list(Invoice) == []
assert get_balance(db, Stock)[0]['quantity'] == 0
set_current_account(admin)
assert len(db.list(Invoice)) == 2
assert WarehouseBalances().execute(db) == [{'product': 'Товар', 'quantity': 10}]
db.delete(first)
assert get_balance(db, Stock)[0]['quantity'] == 3
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_studio_role_matrix_checks_independent_operations(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QPushButton, QTableWidget

    from decui.studio import StudioWindow
    from decui.studio_project import invoice_template

    qt = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = invoice_template()
    window.project.local_accounts = True
    window._load_project()
    window.entities.setCurrentRow(1)

    def fill_matrix():
        dialog = qt.activeModalWidget()
        dialog.findChild(QCheckBox).setChecked(True)
        add = next(button for button in dialog.findChildren(QPushButton) if button.text() == "Добавить роль")
        table = dialog.findChild(QTableWidget)
        for row, (role, columns) in enumerate((("clerk", (1, 2, 3)), ("auditor", (1,)), ("remover", (1, 4)))):
            add.click()
            table.item(row, 0).setText(role)
            for column in columns:
                table.item(row, column).setCheckState(Qt.CheckState.Checked)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, fill_matrix)
    window._edit_permissions()
    assert window.current.permissions["update"] == ["clerk"]
    assert window.current.permissions["post"] == []
    window.project.export(tmp_path)

    program = """
from app import Invoice, Product, Stock
from decui import Database, get_balance
from decui.accounts import Account, set_current_account
from decui.ui.form import AutoForm
from PySide6.QtWidgets import QApplication

qt = QApplication([])
db = Database.memory()
product = db.save(Product(name='Paper'))
set_current_account(Account('owner', 'admin'))
posted = db.save(Invoice(posted=True, Items=[{'product': product.id, 'quantity': 5}]))
posted.write_movements(db)
set_current_account(Account('clerk', 'clerk'))
draft = db.save(Invoice())
assert db.can_edit(draft) and not db.can_post(draft)
form = AutoForm(Invoice, db)
form.load_entity(draft)
assert not form._post_button.isEnabled()
draft.posted = True
try:
    db.save(draft)
except PermissionError:
    pass
else:
    raise AssertionError('Posting without permission succeeded')
try:
    posted.write_movements(db)
except PermissionError:
    pass
else:
    raise AssertionError('Writing movements without permission succeeded')
posted.clear_movements(db)
assert get_balance(db, Stock)[0]['quantity'] == 5
set_current_account(Account('audit', 'auditor'))
assert len(db.list(Invoice)) == 2
assert not db.can_edit(posted)
try:
    db.save(Invoice())
except PermissionError:
    pass
else:
    raise AssertionError('Read-only role could create an invoice')
set_current_account(Account('other', 'other'))
assert not db.list(Invoice)
assert get_balance(db, Stock)[0]['quantity'] == 0
set_current_account(Account('remove', 'remover'))
assert db.can_delete(posted) and not db.can_edit(posted)
db.delete(posted)
assert get_balance(db, Stock)[0]['quantity'] == 0
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_studio_field_roles_protect_defaults_existing_values_and_copy(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QLineEdit

    from decui.studio import StudioWindow
    from decui.studio_project import StudioField, invoice_template

    qt = QApplication.instance() or QApplication([])
    window = StudioWindow()
    window.project = invoice_template()
    window.project.local_accounts = True
    window.project.entities[1].fields.append(StudioField("discount", "Скидка", kind="Integer", default="5"))
    window._load_project()
    window.entities.setCurrentRow(1)

    def fill_roles():
        dialog = qt.activeModalWidget()
        toggle = next(box for box in dialog.findChildren(QCheckBox) if box.text() == "Ограничить изменение по ролям")
        toggle.setChecked(True)
        roles = next(edit for edit in dialog.findChildren(QLineEdit) if edit.placeholderText() == "Пусто — только администратор")
        roles.setText("pricing")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, fill_roles)
    window._field_dialog(0)
    assert window.current.fields[0].write_roles == ["pricing"]
    window.project.export(tmp_path)

    program = """
from app import Invoice
from decui import Database
from decui.accounts import Account, set_current_account
from decui.ui.form import AutoForm
from PySide6.QtWidgets import QApplication

qt = QApplication([])
db = Database.memory()
set_current_account(Account('clerk', 'clerk'))
try:
    db.save(Invoice(discount=10))
except PermissionError:
    pass
else:
    raise AssertionError('Restricted value was overridden during creation')
invoice = db.save(Invoice())
assert invoice.discount == 5
form = AutoForm(Invoice, db)
form.load_entity(invoice)
assert not form._widgets['discount'].isEnabled()
assert form._save()
set_current_account(Account('pricing', 'pricing'))
fresh = db.get(Invoice, invoice.id)
form.load_entity(fresh)
assert form._widgets['discount'].isEnabled()
form._widgets['discount'].setValue(20)
assert form._save()
set_current_account(Account('clerk', 'clerk'))
assert db.get(Invoice, invoice.id).discount == 20
invoice.discount = 7
try:
    db.save(invoice)
except PermissionError:
    pass
else:
    raise AssertionError('Existing value was overridden')
fresh = db.get(Invoice, invoice.id)
assert db.save(fresh.copy_for_edit(db)).discount == 5
assert db.get(Invoice, invoice.id).discount == 20
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_studio_column_roles_preserve_saved_prices_and_allow_quantity_edits(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QLineEdit

    from decui.studio import StudioWindow
    from decui.studio_project import invoice_template

    qt = QApplication.instance() or QApplication([])
    studio = StudioWindow()
    studio.project = invoice_template()
    studio.project.local_accounts = True
    studio._load_project()
    studio.entities.setCurrentRow(1)
    studio.table_part.setCurrentIndex(1)

    def configure_column():
        dialog = qt.activeModalWidget()
        limit = next(box for box in dialog.findChildren(QCheckBox)
                     if box.text() == "Ограничить изменение по ролям")
        limit.setChecked(True)
        roles = next(edit for edit in dialog.findChildren(QLineEdit)
                     if edit.placeholderText() == "Пусто — только администратор")
        roles.setText("pricing")
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

    QTimer.singleShot(0, configure_column)
    studio._field_dialog(2)
    assert studio.current.table_parts[0].fields[2].write_roles == ["pricing"]
    studio.project.export(tmp_path)
    program = """
from decimal import Decimal
from PySide6.QtWidgets import QApplication
from app import Invoice, Product
from decui import Database
from decui.accounts import Account, set_current_account
from decui.ui.form import AutoForm

qt = QApplication([])
db = Database.memory()
product = db.save(Product(name='Бумага'))
set_current_account(Account('pricing', 'pricing'))
invoice = db.save(Invoice(Items=[{'product': product.id, 'quantity': 3, 'price': Decimal('9.50')}]))
set_current_account(Account('clerk', 'clerk'))
form = AutoForm(Invoice, db)
form.load_entity(db.get(Invoice, invoice.id))
table = form._tab_widgets['Items']
assert not table._table.cellWidget(0, 2).isEnabled()
assert not table._delete_button.isEnabled()
table._table.cellWidget(0, 1).setValue(4)
assert form._save()
existing = db.get(Invoice, invoice.id)
assert existing.Items[0]['quantity'] == 4 and existing.Items[0]['price'] == Decimal('9.50')
existing.Items[0]['price'] = Decimal('8')
try:
    db.save(existing)
except PermissionError:
    pass
else:
    raise AssertionError('Protected column was overwritten')
existing.Items.clear()
try:
    db.save(existing)
except PermissionError:
    pass
else:
    raise AssertionError('Protected row was removed')
table._add_row()
table._table.cellWidget(1, 0).setCurrentIndex(table._table.cellWidget(1, 0).findData(product.id))
assert form._save()
stored = db.get(Invoice, invoice.id)
assert [row['price'] for row in stored.Items] == [Decimal('9.50'), Decimal('0.00')]
clone = stored.copy_for_edit(db)
assert [row['price'] for row in clone.Items] == [Decimal('0.00'), Decimal('0.00')]
assert db.get(Invoice, invoice.id).Items[0]['price'] == Decimal('9.50')
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_hidden_header_and_line_values_are_masked_and_preserved(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from decui.studio_project import StudioField, StudioReport, invoice_template

    project = invoice_template()
    project.local_accounts = True
    invoice = project.entities[1]
    invoice.fields.append(StudioField("private_note", "Примечание", read_roles=["pricing"], required=True, default="—"))
    invoice.table_parts[0].fields[2].read_roles = ["pricing"]
    project.reports.append(StudioReport("Notes", "Примечания", "Invoice", ["private_note"], "private_note"))
    project.export(tmp_path)
    program = """
from decimal import Decimal
from PySide6.QtWidgets import QApplication
from app import Invoice, Notes, Product, Stock
from decui import Database, get_balance
from decui.accounts import Account, set_current_account
from decui.ui.form import AutoForm

qt = QApplication([])
db = Database.memory()
item = db.save(Product(name='Товар'))
pricing = Account('pricing', 'pricing')
set_current_account(pricing)
invoice = db.save(Invoice(
    posted=True, private_note='Скрытая запись',
    Items=[{'product': item.id, 'quantity': 4, 'price': Decimal('19.50')}],
))
invoice.write_movements(db)
set_current_account(Account('clerk', 'clerk'))
record = db.get(Invoice, invoice.id)
assert record.private_note is None and record.Items[0]['price'] is None
assert db.list(Invoice)[0].private_note is None
record = db.filter(Invoice, include_table_parts=True)[0]
assert record.private_note is None and record.Items[0]['price'] is None
assert Notes().execute(db)[0]['private_note'] is None
assert get_balance(db, Stock)[0]['quantity'] == 0
assert not db.can_post(record)
for kwargs in ({'private_note': 'Скрытая запись'}, {'order_by': 'private_note'}):
    try:
        db.filter(Invoice, **kwargs)
    except PermissionError:
        pass
    else:
        raise AssertionError('Hidden field could be queried')
form = AutoForm(Invoice, db)
form.load_entity(record)
assert form._widgets['private_note'].isHidden()
lines = form._tab_widgets['Items']
assert lines._table.isColumnHidden(2)
lines._table.cellWidget(0, 1).setValue(5)
assert form._save()
set_current_account(pricing)
stored = db.get(Invoice, invoice.id)
assert stored.private_note == 'Скрытая запись'
assert stored.Items[0]['price'] == Decimal('19.50')
assert stored.Items[0]['quantity'] == 5
"""
    workspace = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": workspace, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_backup_preserves_wal_data_and_requires_admin(tmp_path, monkeypatch):
    import sqlite3

    import pytest
    from PySide6.QtWidgets import QFileDialog

    from decui import Database
    from decui.accounts import (
        Account,
        AccountStore,
        current_account,
        set_current_account,
    )

    qt = QApplication.instance() or QApplication([])
    runtime = ERPApplication(db_path=str(tmp_path / "active.sqlite3"))
    db = runtime.database
    source = tmp_path / "active.sqlite3"
    live = sqlite3.connect(source)
    try:
        assert live.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        live.execute("CREATE TABLE IF NOT EXISTS backup_sample (value TEXT)")
        live.execute("INSERT INTO backup_sample VALUES ('сохранённая запись')")
        live.commit()

        chosen = tmp_path / "menu-copy"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(chosen), ""))
        runtime._data_menu.actions()[0].trigger()
        copy = chosen.with_suffix(".sqlite3")
        with sqlite3.connect(copy) as saved:
            assert saved.execute("SELECT value FROM backup_sample").fetchone() == ("сохранённая запись",)
        with pytest.raises(FileExistsError):
            db.backup(copy)
        with db.transaction(), pytest.raises(RuntimeError, match="Завершите"):
            db.backup(tmp_path / "during-transaction.sqlite3")

        admin = AccountStore(db).create_first_admin("owner", "password for owner")
        previous = current_account()
        try:
            set_current_account(Account("reader", "reader"))
            with pytest.raises(PermissionError):
                db.backup(tmp_path / "restricted.sqlite3")
            assert not (tmp_path / "restricted.sqlite3").exists()
            set_current_account(admin)
            archive = db.backup(tmp_path / "protected.sqlite3")
            assert AccountStore(Database(str(archive))).authenticate("owner", "password for owner") == admin
        finally:
            set_current_account(previous)
    finally:
        live.close()
    qt.processEvents()
