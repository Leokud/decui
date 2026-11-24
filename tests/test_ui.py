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
    name = String("Имя", required=True)
    price = D("Цена", precision=10, scale=2)


@document
class OrderUI(Document):
    number = String("Номер")

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

    # 4. AutoForm (Document with Line Items)
    order = OrderUI(number="O-1")
    order._table_data["Rows"] = [{"product": item._id, "qty": 2, "price": Decimal("3500.00"), "amount": Decimal("0.00")}]
    order.calc()
    db.save(order)

    form_doc = AutoForm(OrderUI, db)
    form_doc.load_entity(order)
    assert "Rows" in form_doc._tab_widgets

    # 5. ReportView
    rv = ReportView(ItemReportUI, db)
    rv._generate()
    assert rv._table.model() is not None

    # 6. BarChart
    chart = BarChart(labels=["A", "B"], values=[10.0, 20.0], title="Диаграмма")
    assert chart._title == "Диаграмма"
