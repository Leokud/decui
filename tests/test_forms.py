"""Comprehensive tests for DECUI declarative/programmable forms, Form Designer UI, and Pyrefly LSP IDE."""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QWidget,
)

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from decui import (
    Boolean,
    Catalog,
    Choice,
    Database,
    Decimal as D,
    Document,
    EntityForm,
    Form,
    FormButton,
    FormColumn,
    FormDivider,
    FormField,
    FormGroup,
    FormLabel,
    FormRelated,
    FormRow,
    FormTab,
    FormTable,
    FormTabs,
    FormWidget,
    Integer,
    Ref,
    String,
    catalog,
    command,
    document,
    form_for,
    tab_part,
)
from decui.studio import StudioWindow
from decui.studio_form_designer import FormDesignerWidget
from decui.studio_import import import_python_project
from decui.studio_lsp import StudioLspBridge
from decui.studio_project import StudioEntity, StudioField, StudioFormConfig, StudioFormNode, StudioProject
from decui.studio_tools import FormPreviewDialog
from decui.ui.form import AutoForm
from decui.ui.python_editor import PythonEditor


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ---------------------------------------------------------------------------
# 1. Runtime Custom Forms (Declarative Form(...) & EntityForm)
# ---------------------------------------------------------------------------


@catalog
class FormClient(Catalog):
    __verbose_name__ = "Клиенты (форма)"

    name = String("Имя", required=True, placeholder="Введите ФИО или компанию")
    kind = Choice("Тип", choices=[("ORG", "Юрлицо"), ("IND", "Физлицо")], default="ORG")
    inn = String("ИНН", placeholder="10 цифр")
    phone = String("Телефон")
    note = String("Заметка", multiline=True)

    __form__ = Form(
        FormLabel("Карточка клиента", style="heading"),
        FormRow(
            FormColumn("name", "kind"),
            FormColumn(
                FormField("inn", label="ИНН организации", visible_when=("kind", "ORG")),
                FormField("phone", placeholder="+7 (000) 000-00-00"),
            ),
        ),
        FormDivider(),
        FormGroup("Дополнительно", "note", collapsible=True, collapsed=True),
    )


@document
class CustomInvoice(Document):
    __verbose_name__ = "Счёт (кастомная форма)"
    __numbering__ = {"prefix": "CI", "field": "number"}

    client = Ref(FormClient, "Клиент", required=True)
    need_delivery = Boolean("Доставка")
    address = String("Адрес")
    total = D("Итого", default=Decimal("0.00"), read_only=True)
    extra_flag = String("Скрываемое / остаточное поле", read_only=False)

    @tab_part
    class Lines:
        __verbose_name__ = "Строки"
        title = String("Позиция", required=True)
        qty = Integer("Кол-во", default=1)
        price = D("Цена", default=Decimal("0.00"))
        amount = D("Сумма", default=Decimal("0.00"), read_only=True)

        __auto_calc__ = {"amount": "qty * price"}
        __totals__ = {"total": "sum(amount)"}

    @command("Подтвердить")
    def confirm(self) -> None:
        self._data["posted"] = True


@form_for(CustomInvoice)
class CustomInvoiceForm(EntityForm):
    layout = Form(
        FormLabel("Пользовательский документ", style="info"),
        FormRow("number", "date", "posted"),
        FormTabs(
            FormTab(
                "Основное",
                "client",
                FormTable("Lines", title="Состав счёта", min_height=180),
                FormRow("total", FormButton("confirm", label="Подтвердить заказ", primary=True)),
            ),
            FormTab(
                "Доставка",
                "need_delivery",
                FormField("address", visible_when=("need_delivery", True)),
                FormWidget(lambda ctrl: QLabel("Пользовательский виджет", objectName="custom_form_widget")),
            ),
        ),
        include_remaining=True,
        show_actions=False,
    )

    events_seen: list[str]

    def build_extra(self, layout) -> None:
        self.events_seen = []
        self._banner = QLabel("Инициализация", objectName="extra_banner")
        layout.addWidget(self._banner)

    def on_open(self) -> None:
        self.events_seen.append("on_open")
        self._banner.setText("Форма открыта")

    def on_field_changed(self, field_name: str, value: object) -> None:
        self.events_seen.append(f"field:{field_name}={value}")
        if field_name == "need_delivery":
            self.set_enabled("address", bool(value))
            self._banner.setText("Доставка включена" if value else "Самовывоз")

    def on_table_changed(self, part_name: str) -> None:
        self.events_seen.append(f"table:{part_name}")

    def on_before_save(self) -> bool | None:
        self.events_seen.append("before_save")
        if self.get_value("need_delivery") and not (self.get_value("address") or "").strip():
            self._banner.setText("Укажите адрес доставки!")
            return False
        return True


def test_declarative_form_rendering_placeholder_groups_and_save(qapp, tmp_path):
    db = Database(str(tmp_path / "decl.db"))

    form = AutoForm(FormClient, db)
    form.load_new()
    try:
        name_ed = form.get_field_widget("name")
        phone_ed = form.get_field_widget("phone")
        inn_ed = form.get_field_widget("inn")
        assert isinstance(name_ed, QLineEdit)
        assert isinstance(phone_ed, QLineEdit)
        assert name_ed.placeholderText() == "Введите ФИО или компанию"
        assert phone_ed.placeholderText() == "+7 (000) 000-00-00"
        assert inn_ed is not None and inn_ed.isVisibleTo(form)

        # Conditional visibility switches inn off when kind changes to IND
        form.set_field_value("kind", "IND")
        assert not inn_ed.isVisibleTo(form)
        form.set_field_value("kind", "ORG")
        assert inn_ed.isVisibleTo(form)

        # Collapsible FormGroup exists and started collapsed
        groups = [g for g in form.findChildren(QGroupBox) if g.title() == "Дополнительно"]
        assert len(groups) == 1
        assert groups[0].isCheckable() and not groups[0].isChecked()

        # Fill and save record
        form.set_field_value("name", "ООО Альфа")
        form.set_field_value("inn", "7700112233")
        assert form._save() is True
        saved = db.list(FormClient)
        assert len(saved) == 1 and saved[0].name == "ООО Альфа"
    finally:
        form.close()
        form.deleteLater()


def test_entity_form_hooks_tabs_buttons_and_before_save_block(qapp, tmp_path):
    db = Database(str(tmp_path / "invoice.db"))

    client = FormClient(name="Покупатель", kind="ORG")
    db.save(client)

    form = AutoForm(CustomInvoice, db)
    form.load_new()
    try:
        ctrl = form.controller
        assert isinstance(ctrl, CustomInvoiceForm)
        assert "on_open" in ctrl.events_seen
        assert form.findChild(QLabel, "extra_banner").text() == "Форма открыта"
        assert form.findChild(QLabel, "custom_form_widget") is not None

        # Remaining field from include_remaining=True was rendered
        assert form.get_field_widget("extra_flag") is not None

        # show_actions=False hides standard save button, while custom FormButton is present
        custom_btns = [b for b in form.findChildren(QPushButton) if b.text() == "Подтвердить заказ"]
        assert len(custom_btns) == 1
        assert custom_btns[0].property("primary") is True

        # Switch delivery on via widget -> field hook triggers, empty address blocks save via on_before_save
        client_widget = form.get_field_widget("client")
        assert isinstance(client_widget, QComboBox)
        form.set_field_value("client", client._id)
        deliv_cb = form.get_field_widget("need_delivery")
        assert isinstance(deliv_cb, QCheckBox)
        deliv_cb.setChecked(True)
        assert "field:need_delivery=True" in ctrl.events_seen
        assert form.findChild(QLabel, "extra_banner").text() == "Доставка включена"
        assert form._save() is False
        assert form.findChild(QLabel, "extra_banner").text() == "Укажите адрес доставки!"

        # Provide address and save + run custom confirm button
        form.set_field_value("address", "ул. Пушкина, 1")
        tbl = form.get_table_widget("Lines")
        assert tbl is not None
        assert tbl.title() == "Состав счёта"
        tbl.load_rows([{"title": "Настройка", "qty": 2, "price": Decimal("500.00"), "amount": Decimal("1000.00")}])
        assert form._save() is True
        custom_btns[0].click()
        reloaded = db.get(CustomInvoice, form._entity._id)
        assert reloaded is not None and reloaded.posted is True
        assert reloaded.address == "ул. Пушкина, 1"
    finally:
        form.close()
        form.deleteLater()


def test_form_validation_activates_owning_form_tab(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)

    @catalog
    class TabRequiredCatalog(Catalog):
        code = String("Код")
        secret = String("Обязательно на вкладке", required=True)
        __form__ = Form(
            FormTabs(
                FormTab("Первая", "code"),
                FormTab("Вторая", "secret"),
            )
        )

    db = Database(str(tmp_path / "tab_val.db"))
    form = AutoForm(TabRequiredCatalog, db)
    form.load_new()
    try:
        tabs = form.findChild(QTabWidget)
        assert tabs is not None
        assert tabs.currentIndex() == 0
        # Saving with empty required field on tab 1 switches active tab to 1
        assert form._save() is False
        assert tabs.currentIndex() == 1
    finally:
        form.close()
        form.deleteLater()


# ---------------------------------------------------------------------------
# 2. StudioProject Serialization, Import, Rename Propagation & LSP Document
# ---------------------------------------------------------------------------


def test_studio_project_form_roundtrip_renames_and_lsp_mapping():
    project = StudioProject(
        title="Тест форм",
        app_id="form_test",
        entities=[
            StudioEntity(
                "Order",
                "Заказы",
                kind="Документ",
                fields=[
                    StudioField("customer", "Покупатель", required=True, placeholder="Имя"),
                    StudioField("kind", "Тип", kind="Choice", choices="B2B: Компания, B2C: Частное лицо", default="B2B"),
                    StudioField("inn", "ИНН", placeholder="10 цифр"),
                    StudioField("total", "Итого", kind="Decimal", default="0.00"),
                ],
                commands={"approve": "Согласовать"},
                code="def approve(self) -> None:\n    self.total = Decimal('10')\n",
                form=StudioFormConfig(
                    enabled=True,
                    include_remaining=False,
                    show_actions=True,
                    items=[
                        StudioFormNode("label", title="Карточка заказа", style="heading"),
                        StudioFormNode(
                            "row",
                            children=[
                                StudioFormNode("column", children=[StudioFormNode("field", target="customer", placeholder="ФИО")]),
                                StudioFormNode(
                                    "column",
                                    children=[StudioFormNode("field", target="inn", visibility_field="kind", visibility_value="B2B")],
                                ),
                            ],
                        ),
                        StudioFormNode(
                            "tabs",
                            children=[
                                StudioFormNode("tab", title="Финансы", children=[StudioFormNode("field", target="total")]),
                            ],
                        ),
                        StudioFormNode("button", target="approve", primary=True),
                    ],
                ),
                form_code="def on_open(self) -> None:\n    self.set_value('inn', '777')\n",
            )
        ],
    )

    import json

    # JSON round-trip preserves form, form_code, and placeholder
    reloaded = StudioProject.from_dict(json.loads(project.to_json()))
    ent = reloaded.entities[0]
    assert ent.form is not None and ent.form.enabled is True
    assert ent.form.items[1].children[0].children[0].placeholder == "ФИО"
    assert ent.form.items[1].children[1].children[0].visibility_value == "B2B"
    assert "def on_open" in ent.form_code

    # Code generation includes both Form(...) and @form_for(Order) class OrderForm(EntityForm)
    source = reloaded.source()
    assert "@form_for(Order)" in source
    assert "class OrderForm(EntityForm):" in source
    assert "placeholder='10 цифр'" in source
    assert "choices=[('B2B', 'Компания'), ('B2C', 'Частное лицо')]" in source
    assert "visible_when=('kind', 'B2B')" in source
    assert "FormButton('approve', primary=True)" in source

    # Import back into StudioProject preserves form structure & form_code losslessly
    imported, notes = import_python_project(source)
    assert all(note.startswith("Импортировано") for note in notes), notes
    imp_ent = imported.entities[0]
    assert imp_ent.form is not None and imp_ent.form.enabled is True
    assert "def on_open" in imp_ent.form_code
    assert imp_ent.fields[1].choices == "B2B: Компания, B2C: Частное лицо"
    assert imp_ent.fields[2].placeholder == "10 цифр"

    # Field rename propagates inside entity.form
    reloaded.rename_field_references(ent, "", "inn", "tax_id")
    assert ent.form.items[1].children[1].children[0].target == "tax_id"

    # lsp_document works even when form_code has syntax errors (incomplete typing like "self.")
    ent.form_code = "def on_open(self) -> None:\n    self.\n"
    text, spans = reloaded.lsp_document()
    assert "class OrderForm(EntityForm):" in text
    form_spans = [s for s in spans if s.location == "Код формы" and s.entity_name == "Order"]
    assert len(form_spans) == 1
    span = form_spans[0]
    assert span.col_offset == 4
    lines = text.splitlines()
    assert lines[span.generated_start - 1 + 1] == "        self."


# ---------------------------------------------------------------------------
# 3. Visual FormDesignerWidget, Preview, Field Placeholder, Action Builder, IDE
# ---------------------------------------------------------------------------


def test_form_designer_presets_tree_ops_and_preview(qapp):
    project = StudioProject(
        entities=[
            StudioEntity(
                "Deal",
                "Сделки",
                kind="Документ",
                fields=[
                    StudioField("title", "Название", required=True, placeholder="Название сделки"),
                    StudioField("amount", "Сумма", kind="Decimal", default="0.00"),
                    StudioField("vip", "VIP", kind="Boolean"),
                ],
                commands={"close_deal": "Закрыть сделку"},
                code="def close_deal(self) -> None:\n    pass\n",
            )
        ]
    )
    designer = FormDesignerWidget()
    try:
        designer.load_entity(project.entities[0], project)
        assert not designer.enabled_check.isChecked()

        # Enable custom form and apply two_column preset
        designer.enabled_check.setChecked(True)
        designer._apply_preset("two_column")
        cfg = project.entities[0].form
        assert cfg is not None and cfg.enabled is True
        assert any(n.kind == "row" for n in cfg.items)
        assert any(n.kind == "column" or n.kind == "row" for n in cfg.items)

        # Apply standard / compact preset with existing table parts and open FormPreviewDialog
        designer._apply_preset("compact")
        assert cfg is not project.entities[0].form
        dlg = FormPreviewDialog(designer, project.entities[0], None, project=project)
        dlg.show()
        qapp.processEvents()
        dlg.close()
        dlg.deleteLater()

        # Edit form_code via designer's PythonEditor
        designer.form_code_editor.setPlainText("def on_open(self) -> None:\n    pass\n")
        assert "def on_open" in project.entities[0].form_code
    finally:
        designer.close()
        designer.deleteLater()


def test_studio_window_tabs_field_placeholder_source_sync(qapp):
    window = StudioWindow()
    try:
        # Tab invariants preserved, and "Форма" tab added right at index 2
        assert window.tabs.tabText(0) == "Объект"
        assert window.tabs.tabText(1) == "Приложение"
        assert window.tabs.tabText(2) == "Форма"
        assert window.tabs.widget(2) is window.form_designer

        window.load_template()
        window.tabs.setCurrentWidget(window.source)
        assert not window.source_edit_check.isChecked()
        assert window.source.isReadOnly()

        # Toggle direct app.py IDE mode, edit window title, and apply back to Studio
        window.source_edit_check.setChecked(True)
        assert not window.source.isReadOnly()
        src = window.source.toPlainText().replace("title='Мои цели'", "title='Мои задачи IDE'")
        window.source.setPlainText(src)
        assert window.source_apply_btn.isEnabled()
        assert window._apply_source_to_project() is True
        assert window.project.title == "Мои задачи IDE"
        assert window.title.text() == "Мои задачи IDE"

        # String placeholder stored cleanly on StudioEntity field
        first_entity = window.project.entities[0]
        first_entity.fields[0].placeholder = "Введите название цели"
        assert "placeholder='Введите название цели'" in window.project.source()
    finally:
        window._dirty = False
        window.saved = repr(window.project)
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------------------
# 4. Live Pyrefly LSP Client & StudioLspBridge End-to-End Test
# ---------------------------------------------------------------------------


def test_live_pyrefly_lsp_diagnostics_completions_symbols_and_hover(qapp):
    project = StudioProject(
        entities=[
            StudioEntity(
                "Note",
                "Заметки",
                fields=[StudioField("title", "Заголовок", required=True)],
                code=(
                    "def helper(self) -> str:\n"
                    "    return 'ok'\n\n"
                    "def run_check(self) -> None:\n"
                    "    x = self.helper()\n"
                    "    y: int = 'bad_type'\n"
                ),
            )
        ]
    )
    bridge = StudioLspBridge(lambda: (project, None))
    ready_states: list[tuple[bool, str]] = []
    diags_seen: list[list] = []
    syms_seen: list[list] = []
    comps_seen: list[tuple[int, list]] = []

    bridge.status_changed.connect(lambda ok, msg: ready_states.append((ok, msg)))
    bridge.mapped_diagnostics.connect(lambda items: diags_seen.append(items))
    bridge.symbols_updated.connect(lambda items: syms_seen.append(items))
    bridge.completion_ready.connect(lambda req_id, items: comps_seen.append((req_id, items)))

    bridge.start()
    try:
        # Pump events up to 15s for pyrefly initialization + first publishDiagnostics
        import time

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not (ready_states and diags_seen and syms_seen):
            qapp.processEvents()
            time.sleep(0.05)

        assert any(ok for ok, _ in ready_states), f"LSP failed to initialize: {ready_states}"
        assert syms_seen and any(s.name == "Note" for s in syms_seen[-1])

        # Diagnostic on `y: int = 'bad_type'` must map back to entity "Note", "Код объекта"
        flat_diags = diags_seen[-1]
        note_diags = [d for d in flat_diags if d.location == "Код объекта" and d.entity_name == "Note"]
        assert note_diags, f"Expected mapped diagnostic on Note, got {flat_diags}"
        assert any(d.line == 5 for d in note_diags)  # 0-based line 5 -> `y: int = 'bad_type'`

        # Request completions after `self.` on a fresh line inside Note.run_check
        project.entities[0].code = (
            "def helper(self) -> str:\n"
            "    return 'ok'\n\n"
            "def run_check(self) -> None:\n"
            "    self.\n"
        )
        req_id = bridge.request_completion("Код объекта", "Note", 4, 9)
        assert req_id > 0
        comp_deadline = time.monotonic() + 10.0
        while time.monotonic() < comp_deadline and not any(rid == req_id for rid, _ in comps_seen):
            qapp.processEvents()
            time.sleep(0.05)

        matched = [items for rid, items in comps_seen if rid == req_id]
        assert matched, "Timed out waiting for LSP completions"
        labels = {item.label for item in matched[0]}
        assert "helper" in labels or "title" in labels
    finally:
        bridge.stop()


def test_examples_custom_forms_smoke(tmp_path):
    import subprocess
    import sys

    db_file = tmp_path / "ex.db"
    script = f"""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from decimal import Decimal
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox
from decui import Database
from decui.ui.form import AutoForm
from examples import custom_forms

app = QApplication([])
QMessageBox.warning = lambda *a, **k: QMessageBox.StandardButton.Ok
QMessageBox.critical = lambda *a, **k: QMessageBox.StandardButton.Ok

db = Database({str(db_file)!r})
client = custom_forms.Client(name="Тест Клиент", kind="ORG", inn="1234567890")
db.save(client)
srv = custom_forms.ServiceCatalog(name="Настройка", price=Decimal("1000.00"))
db.save(srv)

c_form = AutoForm(custom_forms.Client, db)
o_form = AutoForm(custom_forms.ServiceOrder, db)
c_form.load_entity(client)
assert c_form.get_field_widget("name") is not None

o_form.load_new()
assert o_form.controller is not None
o_form.set_field_value("client", client._id)
deliv = o_form.get_field_widget("need_delivery")
assert isinstance(deliv, QCheckBox)
deliv.setChecked(True)
o_form.set_field_value("address", "ул. Мира, 10")
tbl = o_form.get_table_widget("Lines")
assert tbl is not None
tbl.load_rows([{{"service": srv._id, "qty": 2, "price": Decimal("1000.00"), "amount": Decimal("2000.00")}}])
assert o_form._run_post() is True
saved = db.get(custom_forms.ServiceOrder, o_form._entity._id)
assert saved is not None and saved.posted is True
assert saved.status == "DONE"
assert saved.total == Decimal("2000.00")
c_form.close()
o_form.close()
"""
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
