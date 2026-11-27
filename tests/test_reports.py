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


def test_report_requires_current_results_for_csv_and_refreshes_empty_results(tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from decui.ui.report_view import ReportView

    app = QApplication.instance() or QApplication([])
    db = Database.memory()
    db.seed([Client(name="Алексей"), Client(name="Борис")])
    view = ReportView(ClientReport, db)
    target = tmp_path / "clients.csv"
    exports = []
    errors = []

    def choose_file(*args):
        exports.append(args)
        return str(target), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_file)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *_args: errors.append(_args[-1]))
    field = view._param_widgets["prefix_filter"]
    view.show()
    view.activateWindow()
    field.setFocus()
    app.processEvents()
    try:
        assert not view._export_btn.isEnabled()
        field.setText("А")
        QTest.keyClick(field, Qt.Key.Key_F5)
        assert view._export_btn.isEnabled()
        assert [row["name"] for row in view._last_rows] == ["Алексей"]

        field.setText("Б")
        assert not view._result_notice.isHidden()
        assert not view._export_btn.isEnabled()
        view.refresh()
        assert [row["name"] for row in view._last_rows] == ["Алексей"]
        view._export_csv()
        assert not exports

        field.setText("А")
        assert view._result_notice.isHidden()
        assert view._export_btn.isEnabled()
        field.setText("Б")
        QTest.keyClick(field, Qt.Key.Key_F5)
        view._export_csv()
        assert len(exports) == 1
        content = target.read_text(encoding="utf-8-sig")
        assert "Борис" in content
        assert "Алексей" not in content

        def fail(*args, **kwargs):
            raise ValueError("Ошибка проверки")

        with monkeypatch.context() as patch:
            patch.setattr(view._report_instance, "execute", fail)
            field.setText("А")
            view._generate()
            assert not view._export_btn.isEnabled()
            field.setText("Б")
            assert view._export_btn.isEnabled()

            view._generate()
            assert errors and "Ошибка проверки" in errors[-1]
            assert "предыдущий результат" in view._result_notice.text()
            assert not view._export_btn.isEnabled()
            assert [row["name"] for row in view._last_rows] == ["Борис"]

        field.setText("В")
        view._generate()
        assert view._result_notice.isHidden()
        assert view._no_data_label.text() == "Нет данных по применённым параметрам."
        assert not view._export_btn.isEnabled()
        db.save(Client(name="Вера"))
        view.refresh()
        assert view._no_data_label.isHidden()
        assert view._export_btn.isEnabled()
        assert [row["name"] for row in view._last_rows] == ["Вера"]
    finally:
        view.close()


def test_report_marks_variants_dates_and_removed_references_as_unapplied():
    from PySide6.QtWidgets import QApplication

    from decui.ui.report_view import ReportView

    class ContextReport(Report):
        auto_run = True
        customer = RefParam("Клиент", Client)
        date = DateParam("Дата", required=False)
        columns = [Column("Клиент", "name")]

        def execute(self, db, customer=None, date=None):
            clients = db.filter(Client, id=customer) if customer is not None else db.list(Client)
            return [{"name": client.name} for client in clients]

    app = QApplication.instance() or QApplication([])
    db = Database.memory()
    client = db.save(Client(name="Алексей"))
    db.save(Client(name="Борис"))
    db.save_report_variant("ContextReport", "Все клиенты", {"params": {"customer": None, "date": None}})
    view = ReportView(ContextReport, db, initial_refs={"customer": client.id})
    assert view._export_btn.isEnabled()

    date_editor = view._param_widgets["date"]
    date_editor.checkbox.setChecked(True)
    assert not view._export_btn.isEnabled()
    date_editor.checkbox.setChecked(False)
    assert view._export_btn.isEnabled()

    view._apply_variant(view._variant_picker.findData("Все клиенты"))
    assert not view._export_btn.isEnabled()
    assert [row["name"] for row in view._last_rows] == ["Алексей"]
    customer_editor = view._param_widgets["customer"]
    customer_editor.setCurrentIndex(customer_editor.findData(client.id))
    assert view._export_btn.isEnabled()

    db.delete(client)
    view.refresh()
    assert customer_editor.currentData() is None
    assert not view._export_btn.isEnabled()
    assert not view._result_notice.isHidden()
    view._generate()
    assert view._export_btn.isEnabled()
    assert [row["name"] for row in view._last_rows] == ["Борис"]
    app.processEvents()


def test_report_variants_restore_parameters_after_reopen_and_are_private(tmp_path, monkeypatch):
    from PySide6.QtCore import QDate
    from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

    from decui.accounts import Account, current_account, set_current_account
    from decui.ui.report_view import ReportView

    class VariantReport(Report):
        prefix = StringParam("Префикс")
        date = DateParam("На дату", required=False)

    app = QApplication.instance() or QApplication([])
    path = str(tmp_path / "variants.sqlite")
    previous_account = current_account()
    try:
        set_current_account(Account("alice", "viewer"))
        first = ReportView(VariantReport, Database(path))
        first._param_widgets["prefix"].setText("А")
        date = first._param_widgets["date"]
        date.date_edit.setDate(QDate(2026, 11, 25))
        date.checkbox.setChecked(True)
        monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("На ноябрь", True))
        first._save_variant()

        second = ReportView(VariantReport, Database(path))
        second._apply_variant(1)
        assert second._param_widgets["prefix"].text() == "А"
        assert second._param_widgets["date"].value() == datetime.date(2026, 11, 25)

        set_current_account(Account("bob", "viewer"))
        other = ReportView(VariantReport, Database(path))
        assert other._variant_picker.count() == 1

        set_current_account(Account("alice", "viewer"))
        second._variant_picker.setCurrentIndex(1)
        monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
        second._delete_variant()
        assert ReportView(VariantReport, Database(path))._variant_picker.count() == 1
    finally:
        set_current_account(previous_account)
        app.processEvents()


def test_report_variant_columns_apply_to_table_and_csv(tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QListWidget, QMessageBox, QPushButton

    from decui.ui.report_view import ReportView

    class SalesVariant(Report):
        columns = [Column("Страна", "country"), Column("Город", "city"), Column("Сумма", "total", aggregate="sum")]
        group_by = ("country",)

    app = QApplication.instance() or QApplication([])
    db = Database.memory()
    db.save_report_variant("SalesVariant", "Старый вариант", {
        "params": {}, "columns": ["country", "total"],
    })
    rows = [{"country": "Россия", "city": "Москва", "total": 5}]
    view = ReportView(SalesVariant, db)
    view._show_results(rows)
    observed = []

    def choose_columns():
        dialog = app.activeModalWidget()
        try:
            options = dialog.findChild(QListWidget)
            observed.append(bool(options.item(0).flags() & Qt.ItemFlag.ItemIsUserCheckable))
            options.item(1).setCheckState(Qt.CheckState.Unchecked)
            options.setCurrentRow(0)
            down = next(button for button in dialog.findChildren(QPushButton) if button.text() == "Ниже")
            down.click()
            down.click()
        finally:
            dialog.accept()

    QTimer.singleShot(0, choose_columns)
    view._edit_columns()
    assert observed == [False]
    assert view._table.model().columnCount() == 2
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("Итоги", True))
    view._save_variant()

    restored = ReportView(SalesVariant, db)
    restored._apply_variant(restored._variant_picker.findData("Итоги"))
    restored._show_results(rows)
    assert restored._table.model().columnCount() == 2
    assert restored._table.model().headerData(0, Qt.Orientation.Horizontal) == "Сумма"
    target = tmp_path / "report.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    restored._export_csv()
    assert target.read_text(encoding="utf-8-sig").splitlines() == [
        "Сумма;Страна", "5;Страна: Россия", "5;Россия", "5;Итого",
    ]
    restored._apply_variant(restored._variant_picker.findData("Старый вариант"))
    assert restored._table.model().columnCount() == 2
    assert restored._table.model().headerData(0, Qt.Orientation.Horizontal) == "Страна"


def test_custom_report_groups_keep_reference_identity_and_export_subtotals(tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QApplication, QComboBox, QFileDialog, QInputDialog, QListWidget, QMessageBox

    from decui.ui.report_view import ReportView

    class StoreReport(Report):
        columns = [Column("Склад", "store"), Column("Остаток", "total", aggregate="sum")]

    app = QApplication.instance() or QApplication([])
    db = Database.memory()
    rows = [
        {"store": "Главный", "total": 5, "_group_keys": {"store": 1}},
        {"store": "Главный", "total": 3, "_group_keys": {"store": 2}},
    ]
    view = ReportView(StoreReport, db)
    view._show_results(rows)
    assert view._table.isSortingEnabled()

    def group_by_store():
        dialog = app.activeModalWidget()
        try:
            dialog.findChild(QListWidget).item(0).setCheckState(Qt.CheckState.Checked)
        finally:
            dialog.accept()

    QTimer.singleShot(0, group_by_store)
    view._edit_groups()
    assert not view._table.isSortingEnabled()
    assert view._table.model().rowCount() == 5
    def use_average():
        dialog = app.activeModalWidget()
        try:
            editor = dialog.findChildren(QComboBox)[1]
            editor.setCurrentIndex(editor.findData("avg"))
        finally:
            dialog.accept()

    QTimer.singleShot(0, use_average)
    view._edit_totals()
    assert StoreReport.columns[1].aggregate == "sum"
    assert "Среднее" in view._summary_label.text()
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("По складам", True))
    view._save_variant()

    restored = ReportView(StoreReport, db)
    restored._apply_variant(restored._variant_picker.findData("По складам"))
    restored._show_results(rows)
    assert restored._table.model().rowCount() == 5
    target = tmp_path / "stores.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    restored._export_csv()
    assert target.read_text(encoding="utf-8-sig").splitlines() == [
        "Склад;Остаток", "Склад: Главный;5", "Главный;5",
        "Склад: Главный;3", "Главный;3", "Итого;4",
    ]


def test_report_variant_restores_sort_by_field_and_csv_matches(tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

    from decui.ui.report_view import ReportView

    class SortedReport(Report):
        columns = [Column("Товар", "name"), Column("Количество", "qty", aggregate="sum")]

    app = QApplication.instance() or QApplication([])
    db = Database.memory()
    rows = [{"name": "А", "qty": 2}, {"name": "Б", "qty": 10}, {"name": "В", "qty": 1}]
    view = ReportView(SortedReport, db)
    view._show_results(rows)
    assert view._export_rows() == rows
    view._table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert [row["qty"] for row in view._export_rows()] == [10, 2, 1]
    view._column_order = ["qty", "name"]
    view._show_results(rows)
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("По количеству", True))
    view._save_variant()

    restored = ReportView(SortedReport, db)
    restored._apply_variant(restored._variant_picker.findData("По количеству"))
    restored._show_results(rows)
    model = restored._table.model()
    assert [model.data(model.index(row, 0)) for row in range(model.rowCount())] == ["10", "2", "1", "13"]
    target = tmp_path / "sorted.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), ""))
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    restored._export_csv()
    assert target.read_text(encoding="utf-8-sig").splitlines() == ["Количество;Товар", "10;Б", "2;А", "1;В"]
    app.processEvents()


def test_register_report_rows_chunks_id_in_batches(monkeypatch):
    from decui import RefDimension, history_register, set_record
    from decui.movement import Resource
    from decui.reports import register_report_rows

    @history_register
    class ClientRates:
        client = RefDimension(Client, "Клиент")
        rate = Resource("Ставка", field_type=Decimal)

    db = Database.memory()
    clients = db.seed([Client(name=f"Клиент {i}") for i in range(1050)])
    for idx, client in enumerate(clients):
        set_record(
            db,
            ClientRates,
            dimensions={"client": client.id},
            resources={"rate": Decimal(idx + 1)},
            period=datetime.date(2026, 6, 1),
        )

    batch_sizes: list[int] = []
    orig_filter = db.filter

    def spy_filter(entity_class, **kwargs):
        if "id__in" in kwargs:
            batch_sizes.append(len(kwargs["id__in"]))
        return orig_filter(entity_class, **kwargs)

    monkeypatch.setattr(db, "filter", spy_filter)
    rows = register_report_rows(db, ClientRates, ["client", "rate"], order_by="rate")
    assert batch_sizes == [500, 500, 50]
    assert len(rows) == 1050
    assert rows[0]["client"] == "Клиент 0"
    assert rows[-1]["client"] == "Клиент 1049"


def test_report_csv_sanitizes_formula_injection_preserving_negative_numbers():
    class FormulaReport(Report):
        columns = [
            Column("Текст", "label"),
            Column("Сумма", "amount", aggregate="sum"),
        ]

    rows = [
        {"label": "=SUM(A1:A2)", "amount": Decimal("-12.50")},
        {"label": "+cmd|' /C calc'!A0", "amount": Decimal("2.50")},
    ]
    lines = FormulaReport.to_csv(rows).splitlines()
    assert lines[0] == "Текст;Сумма"
    assert lines[1] == "'=SUM(A1:A2);-12.50"
    assert lines[2] == "'+cmd|' /C calc'!A0;2.50"
