import os
from decimal import Decimal
import pytest

from PySide6.QtWidgets import QApplication

# Ensure offscreen Qt
os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


from decui import (
    Catalog,
    Document,
    Database,
    String,
    Decimal as D,
    Integer,
    Ref,
    catalog,
    document,
    command,
    tab_part,
    Report,
    Column,
    report,
)
from decui.ui.form import AutoForm
from decui.ui.listview import ListView
from decui.ui.navigation import NavigationPanel
from decui.ui.report_view import ReportView
from decui.ui.chart import BarChart


@catalog
class ItemUI(Catalog):
    __form_tabs__ = {"price": "Учёт"}
    __form_groups__ = {"price": "Стоимость"}
    name = String("Имя", required=True)
    price = D("Цена", precision=10, scale=2)

    @tab_part
    class Stock:
        available = Integer("Доступно")


@document
class OrderUI(Document):
    __form_table_tabs__ = {"Rows": "Позиции"}
    number = String("Номер")
    basis = Ref(ItemUI, "Основание", create_from="Создать заказ", basis_fields={"quoted_price": "price"},
                basis_tables={"Rows": {"source": "Stock", "columns": {"qty": "available"}}})
    quoted_price = D("Цена предложения", default=Decimal("1"))

    @tab_part
    class Rows:
        product = Ref(ItemUI, "Товар")
        qty = Integer("Кол-во")
        price = D("Цена")
        amount = D("Сумма")

    @command("Посчитать")
    def calc(self):
        for r in self._table_data.get("Rows", []):
            r["amount"] = Decimal(str(r.get("qty", 1))) * Decimal(str(r.get("price", 0)))


@report
class ItemReportUI(Report):
    title = "Тестовый отчет"
    columns = [Column("Имя", "name")]

    def execute(self, db, **kwargs):
        return [{"name": i.name} for i in db.list(ItemUI)]


def test_ui_components(qapp):
    db = Database(":memory:")
    item = ItemUI(name="Клавиатура", price=Decimal("3500.00"))
    db.save(item)

    # 1. Navigation Panel
    nav = NavigationPanel()
    assert nav.topLevelItemCount() > 0

    # 2. ListView
    lv = ListView(ItemUI, db)
    assert lv._table is not None
    assert len(lv._items) == 1

    # 3. AutoForm (Catalog)
    form_cat = AutoForm(ItemUI, db)
    form_cat.load_entity(item)
    assert form_cat._entity is not None
    box = form_cat._form_layout.groups["Учёт", "Стоимость"]
    assert box.isAncestorOf(form_cat._widgets["price"])
    assert not box.isAncestorOf(form_cat._widgets["name"])
    assert not box.isHidden()
    assert form_cat._form_layout.tabs.tabText(0) == "Учёт"
    assert form_cat._form_layout.tabs.widget(0).isAncestorOf(box)

    # 4. AutoForm (Document with Line Items)
    order = OrderUI(number="O-1")
    order._table_data["Rows"] = [{"product": item._id, "qty": 2, "price": Decimal("3500.00"), "amount": Decimal("0.00")}]
    order.calc()
    db.save(order)

    form_doc = AutoForm(OrderUI, db)
    form_doc.load_entity(order)
    assert "Rows" in form_doc._tab_widgets
    tabs = form_doc._form_layout.tabs
    assert tabs.tabText(0) == "Позиции"
    assert tabs.isTabVisible(0)
    assert tabs.widget(0).isAncestorOf(form_doc._tab_widgets["Rows"])

    # 5. ReportView
    rv = ReportView(ItemReportUI, db)
    rv._generate()
    assert rv._table.model() is not None

    # 6. BarChart
    chart = BarChart(labels=["A", "B"], values=[10.0, 20.0], title="Диаграмма")
    assert chart._title == "Диаграмма"



def test_create_on_basis_opens_draft_with_source_link(qapp, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    db = Database.memory()
    item = ItemUI(name="Бумага", price=Decimal("20"))
    item._table_data["Stock"] = [{"available": 4}, {"available": 7}]
    form = AutoForm(ItemUI, db)
    form.load_new()
    assert form._basis_menu.isEmpty()
    db.save(item)
    form.load_entity(item)
    action = next(action for action in form._basis_menu.actions() if action.text() == "Создать заказ")
    records = []
    form._widgets["price"].setValue(99)

    def inspect_draft():
        dialog = QApplication.activeModalWidget()
        try:
            draft = dialog.findChild(AutoForm)._entity
            records.append((type(draft), draft.id, draft.basis, draft.quoted_price,
                            [row.get("qty") for row in draft._table_data.get("Rows", [])]))
        finally:
            dialog.reject()

    QTimer.singleShot(0, inspect_draft)
    action.trigger()
    assert records == [(OrderUI, None, item.id, Decimal("20"), [4, 7])]
    monkeypatch.setattr(db, "can_read_field", lambda cls, field: cls != ItemUI or field not in {"price", "Stock.available"})
    QTimer.singleShot(0, inspect_draft)
    action.trigger()
    assert records[-1] == (OrderUI, None, item.id, Decimal("1"), [])
    assert db.list(OrderUI) == []
    monkeypatch.setattr(db, "allows", lambda cls, right: cls != OrderUI or right != "create")
    form.load_entity(item)
    assert form._basis_menu.isEmpty()


def test_catalog_csv_preview_and_atomic_import(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QComboBox, QDialogButtonBox, QFileDialog, QTableWidget

    from decui.csv_import import read_catalog_csv, save_catalog_csv

    db = Database.memory()
    csv = tmp_path / "products.csv"
    csv.write_text('Имя;Цена\n"Бумага; А4";20,50\nРучка;10,00\n', encoding="utf-8-sig")
    _, drafts = read_catalog_csv(csv, ItemUI, db)
    assert db.list(ItemUI) == []

    def fail_second(record, database):
        if record.name == "Ручка":
            raise ValueError("Проверка склада")

    with monkeypatch.context() as patch:
        patch.setattr(ItemUI, "before_save", fail_second)
        with pytest.raises(ValueError, match="Строка 3"):
            save_catalog_csv(db, drafts)
    assert db.list(ItemUI) == []

    view = ListView(ItemUI, db)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(csv), "CSV"))
    dimensions = []

    def confirm_preview():
        dialog = qapp.activeModalWidget()
        try:
            table = dialog.findChild(QTableWidget)
            dimensions.append((table.rowCount(), table.columnCount(), len(db.list(ItemUI))))
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    csv.write_text('Наименование;Цена;Служебное\n"Бумага; А4";20,50;x\nРучка;10,00;y\n', encoding="utf-8")

    def map_columns():
        dialog = qapp.activeModalWidget()
        try:
            table = dialog.findChild(QTableWidget)
            picker = table.cellWidget(0, 1)
            assert isinstance(picker, QComboBox)
            picker.setCurrentIndex(picker.findData("name"))
            QTimer.singleShot(0, confirm_preview)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, map_columns)
    view._import_csv()
    assert dimensions == [(2, 2, 0)]
    assert {row.name: row.price for row in db.list(ItemUI)} == {
        "Бумага; А4": Decimal("20.50"), "Ручка": Decimal("10.00"),
    }


def test_catalog_csv_updates_by_key_and_rechecks_preview(qapp, tmp_path):
    from decui.csv_import import read_catalog_csv, save_catalog_csv

    @catalog
    class ImportedProduct(Catalog):
        sku = String("Артикул", unique=True)
        name = String("Название", required=True)
        price = D("Цена")

    db = Database.memory()
    product = db.save(ImportedProduct(sku="A", name="Бумага", price=Decimal("5")))
    path = tmp_path / "prices.csv"
    path.write_text("Артикул;Название;Цена\nA;;9\nB;Карандаш;3\n", encoding="utf-8")
    _, preview = read_catalog_csv(path, ImportedProduct, db, key="sku")
    assert [row.record.id for row in preview] == [product.id, None]
    product.name = "Бумага А4"
    db.save(product)
    save_catalog_csv(db, preview)
    updated = db.get(ImportedProduct, product.id)
    assert updated.name == "Бумага А4"
    assert updated.price == Decimal("9")
    assert db.filter(ImportedProduct, sku="B")[0].name == "Карандаш"

    path.write_text("sku;name;price\nA;;10\nC;Линейка;7\n", encoding="utf-8")
    _, preview = read_catalog_csv(path, ImportedProduct, db, key="sku")
    db.save(ImportedProduct(sku="C", name="Новая линейка", price=Decimal("8")))
    with pytest.raises(ValueError, match="Строка 3.*после предпросмотра"):
        save_catalog_csv(db, preview)
    assert db.get(ImportedProduct, product.id).price == Decimal("9")
    assert db.filter(ImportedProduct, sku="C")[0].price == Decimal("8")


def test_catalog_csv_resolves_reference_names_and_requires_id_for_duplicates(qapp, tmp_path, monkeypatch):
    from decui.csv_import import read_catalog_csv, save_catalog_csv

    @catalog
    class ImportCategory(Catalog):
        category_label = String("Подпись")

    @catalog
    class ProductWithCategory(Catalog):
        name = String("Имя")
        category = Ref(ImportCategory, "Категория", representation_field="category_label")

    db = Database.memory()
    office = db.save(ImportCategory(category_label="Канцелярия"))
    numeric = db.save(ImportCategory(category_label="123"))
    path = tmp_path / "categories.csv"
    path.write_text(f"Имя;Категория\nБумага;Канцелярия\nПапка;название:123\nРучка;{office.id}\n", encoding="utf-8")
    _, preview = read_catalog_csv(path, ProductWithCategory, db)
    save_catalog_csv(db, preview)
    assert {item.name: item.category for item in db.list(ProductWithCategory)} == {
        "Бумага": office.id, "Папка": numeric.id, "Ручка": office.id,
    }

    db.save(ImportCategory(category_label="Канцелярия"))
    with pytest.raises(ValueError, match="Строка 2.*нескольким записям"):
        read_catalog_csv(path, ProductWithCategory, db)

    monkeypatch.setattr(db, "can_read_field", lambda cls, name: cls is not ImportCategory or name != "category_label")
    with pytest.raises(ValueError, match="поиск по названию недоступен"):
        read_catalog_csv(path, ProductWithCategory, db)


def test_catalog_csv_template_omits_restricted_fields_and_can_be_imported(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QPushButton

    from decui.csv_import import read_catalog_csv, save_catalog_csv

    db = Database.memory()
    monkeypatch.setattr(db, "can_edit_field", lambda cls, name: cls is not ItemUI or name != "price")
    view = ListView(ItemUI, db)
    target = tmp_path / "template.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), "CSV"))
    button = next(button for button in view.findChildren(QPushButton) if button.text() == "CSV")
    action = next(action for action in button.menu().actions() if action.text() == "Сохранить шаблон CSV…")
    action.trigger()
    assert target.read_bytes() == b"\xef\xbb\xbfname\r\n"
    with target.open("a", encoding="utf-8", newline="") as file:
        file.write("Бумага\r\n")
    columns, rows = read_catalog_csv(target, ItemUI, db)
    assert columns == ["name"]
    save_catalog_csv(db, rows)
    assert [item.name for item in db.list(ItemUI)] == ["Бумага"]


def test_list_csv_exports_search_sorted_rows_without_restricted_fields(qapp, tmp_path, monkeypatch):
    import csv

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFileDialog, QPushButton

    db = Database.memory()
    for name in ("Бумага А4", "Папка А4", "=1+1 А4", "Ручка"):
        db.save(ItemUI(name=name, price=Decimal("7")))
    monkeypatch.setattr(db, "can_read_field", lambda cls, name: cls is not ItemUI or name != "price")
    view = ListView(ItemUI, db)
    view._search_edit.setText("А4")
    view._table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    target = tmp_path / "visible.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(target), "CSV"))
    button = next(button for button in view.findChildren(QPushButton) if button.text() == "CSV")
    action = next(action for action in button.menu().actions() if action.text() == "Выгрузить список…")
    action.trigger()
    with target.open(encoding="utf-8-sig", newline="") as file:
        assert list(csv.reader(file, delimiter=";")) == [["Имя"], ["Папка А4"], ["Бумага А4"], ["'=1+1 А4"]]


def test_list_exact_filters_combine_with_search_and_can_be_reset(qapp):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QDoubleSpinBox, QPushButton

    db = Database.memory()
    db.save(ItemUI(name="Бумага А4", price=Decimal("7")))
    db.save(ItemUI(name="Бумага А3", price=Decimal("9")))
    db.save(ItemUI(name="Папка", price=Decimal("7")))
    view = ListView(ItemUI, db)
    view._search_edit.setText("Бумага")

    def apply_price():
        dialog = qapp.activeModalWidget()
        try:
            dialog.findChild(QCheckBox, "filter_price").setChecked(True)
            dialog.findChild(QDoubleSpinBox).setValue(7)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, apply_price)
    view._edit_field_filters()
    assert view._proxy.rowCount() == 1
    assert view._proxy.index(0, 0).data() == "Бумага А4"
    view.refresh()
    assert view._proxy.rowCount() == 1

    def reset():
        dialog = qapp.activeModalWidget()
        try:
            next(btn for btn in dialog.findChildren(QPushButton) if btn.text() == "Сбросить").click()
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, reset)
    view._edit_field_filters()
    assert view._proxy.rowCount() == 2


def test_list_filter_operators_use_literal_search_and_numeric_comparison(qapp):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QCheckBox, QComboBox, QDialogButtonBox, QDoubleSpinBox, QLineEdit

    db = Database.memory()
    for name, price in (("Скидка 10%", 9), ("Скидка 12%", 5), ("Скидка 20", 9)):
        db.save(ItemUI(name=name, price=Decimal(price)))
    view = ListView(ItemUI, db)

    def apply_operators():
        dialog = qapp.activeModalWidget()
        try:
            dialog.findChild(QCheckBox, "filter_name").setChecked(True)
            text_operator = dialog.findChild(QComboBox, "operator_name")
            text_operator.setCurrentIndex(text_operator.findData("literal_contains"))
            dialog.findChild(QLineEdit).setText("%")
            dialog.findChild(QCheckBox, "filter_price").setChecked(True)
            number_operator = dialog.findChild(QComboBox, "operator_price")
            number_operator.setCurrentIndex(number_operator.findData("gt"))
            dialog.findChild(QDoubleSpinBox).setValue(5)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, apply_operators)
    view._edit_field_filters()
    assert [item.name for item in view._items] == ["Скидка 10%"]


def test_list_range_filter_includes_and_restores_both_bounds(qapp):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QCheckBox, QComboBox, QDialogButtonBox, QDoubleSpinBox

    db = Database.memory()
    for price in (4, 5, 6, 7, 8):
        db.save(ItemUI(name=str(price), price=Decimal(price)))
    view = ListView(ItemUI, db)

    def configure_range():
        dialog = qapp.activeModalWidget()
        try:
            dialog.findChild(QCheckBox, "filter_price").setChecked(True)
            operator = dialog.findChild(QComboBox, "operator_price")
            operator.setCurrentIndex(operator.findData("between"))
            lower, upper = dialog.findChildren(QDoubleSpinBox)
            lower.setValue(5)
            upper.setValue(7)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, configure_range)
    view._edit_field_filters()
    assert sorted(item.name for item in view._items) == ["5", "6", "7"]

    bounds = []

    def check_restored_range():
        dialog = qapp.activeModalWidget()
        try:
            bounds.extend(editor.value() for editor in dialog.findChildren(QDoubleSpinBox))
        finally:
            dialog.reject()

    QTimer.singleShot(0, check_restored_range)
    view._edit_field_filters()
    assert bounds == [5, 7]


def test_list_empty_state_and_reset_keep_related_scope(qapp):
    db = Database.memory()
    first = db.save(ItemUI(name="Первый товар"))
    second = db.save(ItemUI(name="Второй товар"))
    order = db.save(OrderUI(number="А-1", basis=first.id))
    db.save(OrderUI(number="Б-1", basis=second.id))

    view = ListView(OrderUI, db, filters={"basis": first.id})
    view._search_edit.setText("Несуществующий заказ")
    assert view._table.isHidden()
    assert not view._empty_label.isHidden()
    assert "ничего не найдено" in view._empty_label.text()
    assert not view._reset_button.isHidden()

    view._reset_button.click()
    assert view._search_edit.text() == ""
    assert view._reset_button.isHidden()
    assert view._empty_label.isHidden()
    assert [item.id for item in view._items] == [order.id]

    db.delete(order)
    view.refresh()
    assert view._table.isHidden()
    assert "пока нет записей" in view._empty_label.text()
    assert view._reset_button.isHidden()

    listing = ListView(OrderUI, db)
    listing._field_filters = {"number": ("eq", "А-1")}
    listing.refresh()
    assert listing._proxy.rowCount() == 0
    assert "ничего не найдено" in listing._empty_label.text()
    assert not listing._reset_button.isHidden()

    listing._search_edit.setText("А")
    listing._reset_button.click()
    assert listing._search_edit.text() == ""
    assert listing._field_filters == {}
    assert listing._proxy.rowCount() == 1
    assert listing._filters_button.text() == "Отбор…"


def test_list_search_keyboard_and_refresh_keep_editing_target(qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    confirmations = []

    def decline_delete(*args):
        confirmations.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", decline_delete)

    db = Database.memory()
    other = db.save(ItemUI(name="Альбом"))
    selected = db.save(ItemUI(name="Бумага"))
    view = ListView(ItemUI, db)
    view.show()
    view.activateWindow()
    view._table.setFocus()
    qapp.processEvents()
    try:
        QTest.keyClick(view._table, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        assert view._search_edit.hasFocus()
        view._search_edit.setText("Бумага")
        QTest.keyClick(view._search_edit, Qt.Key.Key_Escape)
        assert view._search_edit.text() == ""

        view._search_edit.setText("Бумага")
        edited = []
        view.edit_requested.connect(lambda item: edited.append(item.id))
        QTest.keyClick(view._search_edit, Qt.Key.Key_Return)
        assert view._table.hasFocus()
        assert view._selected_entity().id == selected.id
        assert edited == []

        view._search_edit.setFocus()
        view._search_edit.setCursorPosition(0)
        QTest.keyClick(view._search_edit, Qt.Key.Key_Delete)
        assert view._search_edit.text() == "умага"
        assert confirmations == []
        assert db.get(ItemUI, selected.id) is not None
        assert db.get(ItemUI, other.id) is not None

        view._reset_button.click()
        view._table.setFocus()
        view._table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        db.save(ItemUI(name="Абажур"))
        QTest.keyClick(view._table, Qt.Key.Key_F5)
        assert view._selected_entity().id == selected.id
        QTest.keyClick(view._table, Qt.Key.Key_Return)
        assert edited == [selected.id]
    finally:
        view.close()


@pytest.mark.parametrize("save_on_exit", [True, False])
def test_main_window_keeps_unsaved_card_open_until_user_can_leave(qapp, monkeypatch, save_on_exit):
    from PySide6.QtWidgets import QMessageBox

    from decui.app import _MainWindow

    buttons = QMessageBox.StandardButton
    last_answer = buttons.Save if save_on_exit else buttons.Discard
    answers = iter((buttons.Cancel, buttons.Save, last_answer))
    warnings = []

    def choose(prompt):
        assert prompt.button(buttons.Save).text() == "Сохранить"
        assert prompt.button(buttons.Discard).text() == "Не сохранять"
        assert prompt.escapeButton() is prompt.button(buttons.Cancel)
        return next(answers)

    monkeypatch.setattr(QMessageBox, "exec", choose)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(_args[-1]))
    db = Database.memory()
    item = db.save(ItemUI(name="Бумага"))
    window = _MainWindow(db)
    window._on_edit(item)
    window.show()
    form = window._active_widget
    field = form._widgets["name"]
    try:
        field.setText("Папка")
        window._on_navigate(OrderUI)
        assert window._active_widget is form
        assert field.text() == "Папка"

        field.clear()
        assert not window.close()
        assert window.isVisible()
        assert warnings and "Имя" in warnings[-1]
        assert db.get(ItemUI, item.id).name == "Бумага"

        field.setText("Папка")
        assert window.close()
        assert db.get(ItemUI, item.id).name == ("Папка" if save_on_exit else "Бумага")
    finally:
        window.hide()


def test_modal_form_scopes_save_shortcut_and_protects_draft_on_escape(qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    from decui.ui.form import _RecordDialog

    buttons = QMessageBox.StandardButton
    answers = iter((buttons.Cancel, buttons.Cancel, buttons.Save))
    monkeypatch.setattr(QMessageBox, "exec", lambda prompt: next(answers))
    db = Database.memory()
    parent = AutoForm(OrderUI, db)
    parent.load_new()
    parent._widgets["number"].setText("Заказ в работе")
    parent.show()
    dialog = _RecordDialog(ItemUI, db, parent)
    dialog.form.load_new()
    dialog.setModal(True)
    dialog.show()
    editor = dialog.form._widgets["name"]
    editor.setFocus()
    qapp.processEvents()
    try:
        editor.setText("Бумага")
        QTest.keySequence(editor, QKeySequence(QKeySequence.StandardKey.Save))
        assert dialog.isVisible()
        assert not dialog.form.has_unsaved_changes()
        assert parent.has_unsaved_changes()
        assert len(db.list(ItemUI)) == 1
        assert db.list(OrderUI) == []

        editor.setText("Папка")
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        assert dialog.isVisible()
        assert editor.text() == "Папка"
        assert db.list(ItemUI)[0].name == "Бумага"

        assert not dialog.close()
        assert dialog.isVisible()
        assert dialog.close()
        assert not dialog.isVisible()
        assert [item.name for item in db.list(ItemUI)] == ["Папка"]
    finally:
        dialog.hide()
        parent.hide()


def test_main_window_restores_list_after_editing_record(qapp):
    from PySide6.QtCore import Qt

    from decui.app import _MainWindow

    db = Database.memory()
    db.save(ItemUI(name="Бумага А4", price=Decimal("7")))
    selected = db.save(ItemUI(name="Бумага А3", price=Decimal("9")))
    db.save(ItemUI(name="Бумага А5", price=Decimal("4")))
    db.save(ItemUI(name="Папка", price=Decimal("12")))

    window = _MainWindow(db)
    window._on_navigate(ItemUI)
    listing = window._active_widget
    assert isinstance(listing, ListView)
    listing._field_filters = {"price": ("between", (Decimal("7"), Decimal("9")))}
    listing._search_edit.setText("Бумага")
    listing.refresh()
    listing._table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    listing._table.selectRow(0)
    listing.edit_requested.emit(listing._selected_entity())

    form = window._active_widget
    assert isinstance(form, AutoForm)
    assert form._entity.id == selected.id
    form.closed.emit()

    restored = window._active_widget
    assert isinstance(restored, ListView)
    assert restored._search_edit.text() == "Бумага"
    assert restored._filters_button.text() == "Отбор (1)…"
    assert restored._proxy.rowCount() == 2
    assert restored._proxy.index(0, 0).data() == "Бумага А3"
    assert restored._selected_entity().id == selected.id
    window.close()


def test_list_preferences_survive_reopen_and_are_isolated_by_account(qapp, tmp_path):
    from PySide6.QtCore import Qt

    from decui.accounts import Account, current_account, set_current_account
    from decui.app import _MainWindow

    path = str(tmp_path / "preferences.sqlite3")
    db = Database(path)
    db.save(ItemUI(name="Бумага", price=Decimal("7")))
    db.save(ItemUI(name="Папка", price=Decimal("9")))
    previous = current_account()
    try:
        set_current_account(Account("alice", "editor"))
        first = _MainWindow(db)
        first._on_navigate(ItemUI)
        listing = first._active_widget
        assert isinstance(listing, ListView)
        listing._field_filters = {"price": ("between", (Decimal("6.75"), Decimal("7.25")))}
        listing._search_edit.setText("Бумага")
        listing.refresh()
        listing._table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        first.close()

        reopened = _MainWindow(Database(path))
        reopened._on_navigate(ItemUI)
        listing = reopened._active_widget
        assert isinstance(listing, ListView)
        assert listing._search_edit.text() == "Бумага"
        assert listing._field_filters == {"price": ("between", (Decimal("6.75"), Decimal("7.25")))}
        assert listing._table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        assert listing._proxy.rowCount() == 1
        reopened.close()

        set_current_account(Account("bob", "editor"))
        other = _MainWindow(Database(path))
        other._on_navigate(ItemUI)
        listing = other._active_widget
        assert isinstance(listing, ListView)
        assert listing._search_edit.text() == ""
        assert listing._field_filters == {}
        assert listing._proxy.rowCount() == 2
        other.close()
    finally:
        set_current_account(previous)


def test_named_list_variants_restore_settings_and_are_private(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from decui.accounts import Account, current_account, set_current_account

    path = str(tmp_path / "variants.sqlite")
    db = Database(path)
    db.save(ItemUI(name="Бумага А4", price=Decimal("7")))
    db.save(ItemUI(name="Бумага А3", price=Decimal("9")))
    db.save(ItemUI(name="Папка", price=Decimal("9")))
    previous = current_account()
    try:
        set_current_account(Account("alice", "editor"))
        view = ListView(ItemUI, db)
        view._search_edit.setText("Бумага")
        view._field_filters = {"price": ("between", (Decimal("8.50"), Decimal("9.50")))}
        view.refresh()
        view._table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_kw: ("Бумага со скидкой", True))
        view._save_list_variant()

        reopened = ListView(ItemUI, Database(path))
        monkeypatch.setattr(QInputDialog, "getItem", lambda *_a, **_kw: ("Бумага со скидкой", True))
        reopened._load_list_variant()
        assert reopened._search_edit.text() == "Бумага"
        assert reopened._proxy.rowCount() == 1
        assert reopened._proxy.index(0, 0).data() == "Бумага А3"
        assert reopened.list_state().sort_field == "price"
        assert reopened.list_state().sort_order == Qt.SortOrder.DescendingOrder

        set_current_account(Account("bob", "editor"))
        assert db.list_variants(ItemUI) == {}
        set_current_account(Account("alice", "editor"))
        monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Yes)
        reopened._delete_list_variant()
        assert db.list_variants(ItemUI) == {}
        assert reopened._proxy.rowCount() == 1
    finally:
        set_current_account(previous)


def test_form_visibility_follows_checkbox_and_preserves_hidden_value(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from decui import Boolean

    @catalog
    class ShipmentForm(Catalog):
        __form_visible_when__ = {"address": ("delivery", True)}
        __required_when_visible__ = ("address",)
        __form_tabs__ = {"address": "Доставка"}
        delivery = Boolean("Нужна доставка", default=False)
        address = String("Адрес доставки")

    db = Database.memory()
    entity = db.save(ShipmentForm(delivery=False, address="Лесная, 4"))
    form = AutoForm(ShipmentForm, db)
    form.load_entity(entity)
    assert not form._form_layout.tabs.isTabVisible(0)
    form._widgets["delivery"].setChecked(True)
    assert form._form_layout.tabs.isTabVisible(0)
    assert form._widgets["address"].text() == "Лесная, 4"
    assert form._field_labels["address"].text() == "Адрес доставки *:"
    alerts = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: alerts.append(_args[-1]))
    form._widgets["address"].clear()
    assert not form._save()
    assert alerts == ["Адрес доставки обязательно"]
    with pytest.raises(ValueError, match="Адрес доставки"):
        db.save(ShipmentForm(delivery=True), validate=True)
    db.save(ShipmentForm(delivery=False), validate=True)
    form._widgets["address"].setText("Лесная, 4")
    form._widgets["delivery"].setChecked(False)
    assert not form._form_layout.tabs.isTabVisible(0)
    assert form._save()
    assert db.get(ShipmentForm, entity.id).address == "Лесная, 4"

    monkeypatch.setattr(db, "can_read_field", lambda cls, name: cls is not ShipmentForm or name != "delivery")
    form._widgets["delivery"].setChecked(True)
    assert not form._form_layout.tabs.isTabVisible(0)


def test_table_validation_opens_tab_and_selects_invalid_cell(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    @document
    class DispatchForm(Document):
        __form_tabs__ = {"description": "Общее"}
        __form_table_tabs__ = {"Lines": "Позиции"}
        description = String("Описание")

        @tab_part
        class Lines:
            product = String("Товар", required=True)

    db = Database.memory()
    form = AutoForm(DispatchForm, db)
    form.load_new({"Lines": [{"product": "Бумага"}, {}]})
    tabs = form._form_layout.tabs
    tabs.setCurrentWidget(form._form_layout.pages["Общее"])
    alerts = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: alerts.append(_args[-1]))
    assert not form._save()
    assert "стр. 2" in alerts[0]
    assert tabs.currentWidget() is form._form_layout.pages["Позиции"]
    table = form._tab_widgets["Lines"]._table
    assert (table.currentRow(), table.currentColumn()) == (1, 0)
    assert not db.list(DispatchForm)


def test_table_pastes_spreadsheet_rows_and_leaves_table_unchanged_on_error(qapp, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QPushButton

    @document
    class PasteOrder(Document):
        @tab_part
        class Rows:
            product = Ref(ItemUI, "Товар")
            quantity = Integer("Количество")
            price = D("Цена")
            amount = D("Сумма", read_only=True)

    db = Database.memory()
    paper = db.save(ItemUI(name="Бумага"))
    form = AutoForm(PasteOrder, db)
    form.load_new()
    part = form._tab_widgets["Rows"]
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *_args: ("Бумага\t2\t5,50\nБумага\t4\t3", True))
    next(button for button in part.findChildren(QPushButton) if button.text() == "Вставить строки…").click()
    rows = part.get_rows()
    assert [(row["product"], row["quantity"], row["amount"]) for row in rows] == [
        (paper.id, 2, Decimal("11.00")), (paper.id, 4, Decimal("12.00")),
    ]
    with pytest.raises(ValueError, match="Строка 2"):
        part._paste_rows("Бумага\t5\nБумага\tне число")
    assert part.get_rows() == rows
    assert db.list(PasteOrder) == []


def _ref_options(combo):
    return [combo.itemData(i) for i in range(combo.count())]


def test_related_refresh_updates_clean_lines_and_keeps_draft_ref(qapp):
    db = Database.memory()
    first = db.save(ItemUI(name="Бумага"))
    second = db.save(ItemUI(name="Папка"))
    order = db.save(OrderUI(Rows=[{"product": first.id, "qty": 1}]))
    form = AutoForm(OrderUI, db)
    form.load_by_id(order.id)
    part = form._tab_widgets["Rows"]

    order.Rows[0]["qty"] = 5
    db.save(order)
    form.refresh_related_data()
    assert part.get_rows()[0]["qty"] == 5
    assert not form.has_unsaved_changes()

    combo = part._table.cellWidget(0, 0)
    combo.setCurrentIndex(combo.findData(second.id))
    form.refresh_related_data()
    assert form.has_unsaved_changes()
    assert form._save()
    stored = db.get(OrderUI, order.id)
    assert (stored.Rows[0]["product"], stored.Rows[0]["qty"]) == (second.id, 5)


def test_dependent_ref_keeps_drafts_and_filters_quick_create(qapp, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QPushButton

    from decui import Boolean
    from decui import registry

    monkeypatch.setattr(registry, "_entity_registry", {})

    @catalog
    class Buyer(Catalog):
        name = String()

    @catalog
    class Address(Catalog):
        name = String()
        street = String()
        owner = Ref(Buyer, required=True)
        disabled = Boolean(default=False)
        zone = Integer(default=0)

    @catalog
    class Entrance(Catalog):
        name = String()
        address = Ref(Address)

    @document
    class Delivery(Document):
        entrance = Ref(Entrance, choice_filter={"address": "address"})
        address = Ref(Address, auto_fill="street", choice_filter={"owner": "buyer", "disabled": "disabled", "zone": "zone"})
        street = String()
        buyer = Ref(Buyer)
        disabled = Boolean(default=False)
        zone = Integer(default=0)
        notes = String()

    db = Database.memory()
    first = db.save(Buyer(name="Первый"))
    second = db.save(Buyer(name="Второй"))
    north = db.save(Address(name="Север", street="Лесная", owner=first.id))
    south = db.save(Address(name="Юг", owner=second.id))
    db.save(Address(name="Закрыт", owner=first.id, disabled=True))
    db.save(Address(name="Другой район", owner=first.id, zone=1))
    entry = db.save(Entrance(name="Вход", address=north.id))
    saved = db.save(Delivery(buyer=first.id, address=north.id, entrance=entry.id))
    form = AutoForm(Delivery, db)
    combo = form._widgets["address"]
    choice = form._widgets["buyer"]
    entrance = form._widgets["entrance"]
    plus = form._field_controls["address"].findChild(QPushButton)

    form.load_new()
    assert _ref_options(combo) == [None]
    assert not plus.isEnabled()
    choice.setCurrentIndex(choice.findData(first.id))
    assert _ref_options(combo) == [None, north.id]
    combo.setCurrentIndex(combo.findData(north.id))
    assert _ref_options(entrance) == [None, entry.id]
    assert form._form_value("street") == "Лесная"
    choice.setEditText("Нет такого покупателя")
    assert _ref_options(combo) == [None] and _ref_options(entrance) == [None]
    assert form._form_value("street") is None
    assert form._form_value("buyer") is None
    choice.setEditText("Второй")
    assert _ref_options(combo) == [None, south.id]
    assert form._form_value("buyer") == second.id

    form.load_new({"buyer": first.id, "address": north.id, "entrance": entry.id})
    assert combo.currentData() == north.id
    assert entrance.currentData() == entry.id

    form.load_by_id(saved.id)
    assert combo.currentData() == north.id
    assert not form.has_unsaved_changes()
    form.refresh_related_data()
    assert combo.currentData() == north.id
    choice.setCurrentIndex(choice.findData(second.id))
    assert _ref_options(combo) == [None, south.id]
    assert combo.currentData() is None and _ref_options(entrance) == [None]
    assert db.get(Delivery, saved.id).address == north.id
    choice.setCurrentIndex(choice.findData(first.id))
    assert _ref_options(combo) == [None, north.id]
    assert combo.currentData() is None
    assert db.get(Delivery, saved.id).address == north.id
    choice.setCurrentIndex(0)
    assert _ref_options(combo) == [None]
    choice.setCurrentIndex(choice.findData(first.id))
    assert plus.isEnabled()

    def create_address():
        dialog = QApplication.activeModalWidget()
        try:
            child = dialog.findChild(AutoForm)
            assert child.entity.owner == first.id and child.entity.zone == 0
            child._widgets["name"].setText("Новый адрес")
            child._catalog_save_and_close()
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, create_address)
    plus.click()
    created = db.filter(Address, name="Новый адрес")[0]
    assert combo.currentData() == created.id
    assert _ref_options(combo) == [None, north.id, created.id]
    assert db.get(Delivery, saved.id).address == north.id

    def create_other_address():
        dialog = QApplication.activeModalWidget()
        child = dialog.findChild(AutoForm)
        buyer = child._widgets["owner"]
        buyer.setCurrentIndex(buyer.findData(second.id))
        child._widgets["name"].setText("Адрес другого покупателя")
        child._catalog_save_and_close()
        if dialog.isVisible():
            dialog.reject()

    QTimer.singleShot(0, create_other_address)
    plus.click()
    assert len(db.filter(Address, owner=second.id)) == 2
    assert combo.currentData() == created.id
    assert _ref_options(combo) == [None, north.id, created.id]
    assert form._save()
    assert db.get(Delivery, saved.id).address == created.id

    draft = db.save(Delivery(buyer=first.id))
    form.load_by_id(draft.id)
    choice.setCurrentIndex(choice.findData(second.id))
    draft.address = north.id
    db.save(draft)
    form.refresh_related_data()
    assert _ref_options(combo) == [None, *(row.id for row in db.filter(Address, owner=second.id))]
    assert form._save()
    stored = db.get(Delivery, draft.id)
    assert stored.buyer == second.id and stored.address is None


def test_dependent_ref_preserves_legacy_without_disclosing_names(qapp, monkeypatch):
    from decui import Boolean, registry
    from decui.accounts import Account, current_account, set_current_account

    monkeypatch.setattr(registry, "_entity_registry", {})

    @catalog
    class PrivateBuyer(Catalog):
        __permissions__ = {"read": ["clerk"]}
        name = String()

    @catalog
    class PrivateAddress(Catalog):
        __owner_field__ = "login"
        __field_read_roles__ = {"code": []}
        login = String(visible=False)
        name = String()
        owner = Ref(PrivateBuyer)
        code = Integer(default=0)

    @document
    class PrivateDelivery(Document):
        __field_read_roles__ = {"private_buyer": []}
        __form_visible_when__ = {"buyer": ("show_buyer", True)}
        buyer = Ref(PrivateBuyer)
        show_buyer = Boolean(default=True)
        private_buyer = Ref(PrivateBuyer)
        address = Ref(PrivateAddress, choice_filter={"owner": "buyer"})
        other_address = Ref(PrivateAddress, choice_filter={"owner": "private_buyer"})
        coded_address = Ref(PrivateAddress, choice_filter={"owner": "buyer", "code": "code"})
        code = Integer(default=0)
        notes = String()

        @tab_part
        class Rows:
            buyer = Ref(PrivateBuyer)

    db = Database.memory()
    previous = current_account()
    try:
        set_current_account(Account("boss", "admin"))
        buyer = db.save(PrivateBuyer(name="Покупатель"))
        wrong = db.save(PrivateBuyer(name="Другой"))
        private = db.save(PrivateAddress(name="Секретный адрес", owner=wrong.id))
        legacy = db.save(PrivateDelivery(buyer=wrong.id, address=private.id))
        private.owner = buyer.id
        db.save(private)
        saved = db.save(PrivateDelivery(buyer=buyer.id, private_buyer=buyer.id, address=private.id,
                              other_address=private.id, coded_address=private.id, Rows=[{"buyer": buyer.id}]))
        form = AutoForm(PrivateDelivery, db)
        form.load_by_id(legacy.id)
        combo = form._widgets["address"]
        assert _ref_options(combo) == [None]
        assert "Сохранённая ссылка" in combo.lineEdit().placeholderText()
        assert not form.has_unsaved_changes()
        assert form._save()
        assert db.get(PrivateDelivery, legacy.id).address == private.id
        form._widgets["buyer"].setCurrentIndex(form._widgets["buyer"].findData(buyer.id))
        assert combo.currentData() == private.id

        set_current_account(Account("employee", "clerk"))
        public = db.save(PrivateAddress(name="Доступный адрес", owner=buyer.id))
        form.load_by_id(saved.id)
        assert _ref_options(combo) == [None, public.id]
        assert _ref_options(form._widgets["other_address"]) == [None]
        assert _ref_options(form._widgets["coded_address"]) == [None]
        assert "Секретный адрес" not in combo.currentText()
        assert not form.has_unsaved_changes()
        form._widgets["notes"].setText("Комментарий")
        assert form._save()
        assert db.get(PrivateDelivery, saved.id).address == private.id
        form._widgets["show_buyer"].setChecked(False)
        assert _ref_options(combo) == [None]
        form._widgets["show_buyer"].setChecked(True)
        assert _ref_options(combo) == [None, public.id]
        combo.setCurrentIndex(combo.findData(public.id))
        assert not combo.toolTip()
        assert form._save()
        assert db.get(PrivateDelivery, saved.id).address == public.id

        set_current_account(Account("guest", "guest"))
        form.load_by_id(saved.id)
        assert _ref_options(combo) == [None]
        assert "Доступный адрес" not in combo.currentText()
        form.refresh_related_data()
        form._widgets["notes"].setText("Новая заметка")
        assert form._save()
        stored = db.get(PrivateDelivery, saved.id)
        assert stored.buyer == stored.Rows[0]["buyer"] == buyer.id
        form._widgets["buyer"].setCurrentIndex(0)
        form._tab_widgets["Rows"]._table.cellWidget(0, 0).setCurrentIndex(0)
        assert form._save()
        stored = db.get(PrivateDelivery, saved.id)
        assert stored.buyer is None and stored.Rows[0]["buyer"] is None
    finally:
        set_current_account(previous)


@pytest.mark.parametrize("case, path, message", [
    ("source", "InvalidChoice.address", "неизвестное поле источника"),
    ("target", "InvalidChoice.address", "неизвестное поле отбора"),
    ("type", "InvalidChoice.address", "несовместимые поля"),
    ("cycle", "InvalidChoice.address", "цикл зависимостей"),
    ("table", "InvalidChoice.Rows.address", "только в шапке"),
])
def test_dependent_ref_invalid_metadata(monkeypatch, case, path, message):
    from decui import registry

    monkeypatch.setattr(registry, "_entity_registry", {})

    @catalog
    class First(Catalog):
        name = String()

    @catalog
    class Second(Catalog):
        first = Ref(First)
        link = Ref("Second")

    @document
    class InvalidChoice(Document):
        customer = Ref(Second)
        address = Ref(Second, choice_filter={
            "source": {"first": "missing"},
            "target": {"missing": "customer"},
            "type": {"first": "customer"},
            "cycle": {"link": "other"},
            "table": {},
        }[case])
        other = Ref(Second, choice_filter={"link": "address"} if case == "cycle" else {})

        @tab_part
        class Rows:
            address = Ref(Second, choice_filter={"link": "customer"} if case == "table" else {})

    db = Database.memory()
    assert not db._ready
    assert f"{path}:" in db._last_error and message in db._last_error


def test_ui_error_formatting_hides_raw_sql_and_preserves_details(qapp, monkeypatch):
    import sqlite3

    from PySide6.QtWidgets import QMessageBox
    from sqlalchemy.exc import IntegrityError

    from decui.ui.editors import format_ui_error, show_ui_error

    raw_unique = "Ошибка SQL при сохранении: (sqlite3.IntegrityError) UNIQUE constraint failed: item.code\n[SQL: INSERT INTO item]"
    msg_unique = format_ui_error(RuntimeError(raw_unique))
    assert "уникальными значениями" in msg_unique
    assert "SQL" not in msg_unique and "INSERT" not in msg_unique

    sa_fk = IntegrityError("DELETE FROM item", {}, sqlite3.IntegrityError("FOREIGN KEY constraint failed"))
    msg_fk = format_ui_error(sa_fk)
    assert "используется в других документах" in msg_fk
    assert "DELETE FROM" not in msg_fk

    msg_locked = format_ui_error(sqlite3.OperationalError("database is locked"))
    assert "временно занята" in msg_locked

    assert format_ui_error(ValueError("Понятная бизнес-ошибка")) == "Понятная бизнес-ошибка"

    shown = []

    def capture_box(box):
        shown.append((box.text(), box.detailedText()))
        return int(QMessageBox.StandardButton.Ok)

    monkeypatch.setattr(QMessageBox, "exec", capture_box)
    show_ui_error(None, "Ошибка", RuntimeError(raw_unique), prefix="Ошибка сохранения: ")
    assert len(shown) == 1
    assert shown[0][0].startswith("Ошибка сохранения: ")
    assert "уникальными значениями" in shown[0][0]
    assert "SQL" not in shown[0][0]
    assert "INSERT INTO item" in shown[0][1]

    db = Database.memory()
    item = db.save(ItemUI(name="Товар"))
    view = ListView(ItemUI, db)
    view._table.selectRow(0)
    warnings = []
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a: warnings.append(_a[-1]))
    monkeypatch.setattr(db, "delete", lambda _ent: (_ for _ in ()).throw(RuntimeError(raw_unique)))
    view._on_action("Delete")
    assert len(warnings) == 1
    assert "уникальными значениями" in warnings[0]
    assert "INSERT" not in warnings[0]
    assert db.get(ItemUI, item.id) is not None


def test_report_view_csv_totals_variants_error_guard_and_shortcut(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QShortcut
    from PySide6.QtWidgets import QComboBox, QFileDialog, QInputDialog, QMessageBox

    from decui import RefParam

    class FormattedReport(Report):
        title = "Продажи"
        item = RefParam("Товар", ItemUI)
        columns = [
            Column("Товар", "name"),
            Column("Сумма", "total", aggregate="sum", format="{:,.2f}", suffix=" руб."),
            Column("Битый", "broken", format="{bad_key}"),
        ]

        def execute(self, db, item=None):
            return [{"name": "Бумага", "total": Decimal("10"), "broken": "5"}, {"name": "Папка", "total": Decimal("20"), "broken": "7"}]

    db = Database.memory()
    db.save(ItemUI(name="Бумага"))
    view = ReportView(FormattedReport, db)
    shortcuts = [sc for sc in view.findChildren(QShortcut) if sc.key().toString() == "F5"]
    assert len(shortcuts) == 1
    assert shortcuts[0].context() == Qt.ShortcutContext.WidgetWithChildrenShortcut
    ref_combo = view._param_widgets["item"]
    assert isinstance(ref_combo, QComboBox)
    assert [ref_combo.itemData(i) for i in range(ref_combo.count())].count(None) == 1

    view._generate()
    assert "30.00 руб." in view._summary_label.text()

    target_no_total = tmp_path / "no_total.csv"
    monkeypatch.setattr(QMessageBox, "information", lambda *_a: None)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a: (str(target_no_total), ""))
    view._aggregates = {"total": None}
    view._export_csv()
    assert not any("Итого" in line for line in target_no_total.read_text(encoding="utf-8-sig").splitlines())

    target_with_total = tmp_path / "with_total.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a: (str(target_with_total), ""))
    view._selected_columns = {"name", "total"}
    view._column_order = ["total", "name"]
    view._aggregates = {"total": "sum"}
    view._export_csv()
    assert target_with_total.read_text(encoding="utf-8-sig").splitlines() == [
        "Сумма;Товар",
        "10;Бумага",
        "20;Папка",
        "30;",
    ]

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_a: warnings.append(_a[-1]))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_kw: ("Тест", True))
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_kw: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        db, "report_variants",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("Ошибка SQL при чтении: [SQL: SELECT 1]")),
    )
    view._reload_variants()
    monkeypatch.setattr(
        db, "save_report_variant",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("Ошибка SQL при записи: [SQL: INSERT 1]")),
    )
    view._save_variant()
    view._variants = {"Тест": {}}
    view._variant_picker.addItem("Тест", "Тест")
    view._variant_picker.setCurrentIndex(view._variant_picker.findData("Тест"))
    monkeypatch.setattr(
        db, "delete_report_variant",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("Ошибка SQL при удалении: [SQL: DELETE 1]")),
    )
    view._delete_variant()
    assert len(warnings) == 3
    assert all("[SQL:" not in msg and "Ошибка при обращении к базе данных" in msg for msg in warnings)


def test_table_part_caches_ref_queries_focuses_added_row_and_respects_role_rights(qapp, monkeypatch):
    db = Database.memory()
    item = db.save(ItemUI(name="Бумага", price=Decimal("10")))
    order = db.save(OrderUI(
        number="О-1",
        Rows=[
            {"product": item.id, "qty": 1, "price": Decimal("10")},
            {"product": item.id, "qty": 2, "price": Decimal("10")},
            {"product": item.id, "qty": 3, "price": Decimal("10")},
            {"product": item.id, "qty": 4, "price": Decimal("10")},
        ],
    ))

    list_calls = []
    orig_list = db.list

    def counting_list(cls, *args, **kwargs):
        list_calls.append(cls)
        return orig_list(cls, *args, **kwargs)

    form = AutoForm(OrderUI, db)
    part = form._tab_widgets["Rows"]
    monkeypatch.setattr(db, "list", counting_list)
    part.load_rows(order._table_data["Rows"])
    assert list_calls.count(ItemUI) == 1

    form.show()
    form.activateWindow()
    qapp.processEvents()
    try:
        part._add_button.click()
        qapp.processEvents()
        assert part._table.rowCount() == 5
        assert part._table.currentRow() == 4
        assert part._table.cellWidget(4, 0).hasFocus()
    finally:
        form.hide()

    monkeypatch.setattr(db, "can_edit_field", lambda cls, field: not field.startswith("Rows."))
    part.load_rows(order._table_data["Rows"])
    assert not part._add_button.isEnabled()
    part._add_row()
    assert part._table.rowCount() == 4


def test_date_time_editors_use_field_default_on_none(qapp):
    import datetime

    from decui import Date, DateTime, Time
    from decui.ui.editors import create_editor, get_editor_value, set_editor_value

    df = Date("Дата", default=datetime.date(2026, 5, 1))
    dw = create_editor(df)
    set_editor_value(dw, df, None)
    assert get_editor_value(dw, df) == datetime.date(2026, 5, 1)

    dtf = DateTime("Момент", default=lambda: datetime.datetime(2026, 5, 1, 14, 30))
    dtw = create_editor(dtf)
    set_editor_value(dtw, dtf, None)
    assert get_editor_value(dtw, dtf) == datetime.datetime(2026, 5, 1, 14, 30)

    tf = Time("Время", default=datetime.time(11, 15))
    tw = create_editor(tf)
    set_editor_value(tw, tf, datetime.time(8, 0))
    set_editor_value(tw, tf, None)
    assert get_editor_value(tw, tf) == datetime.time(11, 15)
