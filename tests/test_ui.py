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
