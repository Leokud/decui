from __future__ import annotations

import csv
import html
from collections.abc import Collection
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from decui.accounts import AccountStore, current_account
from decui.csv_import import parse_tabular_rows
from decui.fields import BooleanField, Field, RefField
from decui.movement import RefDimension, get_all_registers
from decui.registry import get_all_entities, get_all_reports
from decui.reports import RefParam, Report
from decui.sequence import SequenceViolationError, current_role

from .editors import (
    create_editor,
    get_editor_value,
    populate_ref_editor,
    set_editor_value,
    show_ui_error,
)
from .listview import ListView
from .report_view import ReportView

if TYPE_CHECKING:
    from decui.entity import Entity
    from decui.orm import Database


class FieldLayout:
    """Place form rows into optional tabs and groups."""

    def __init__(self, panel: QWidget) -> None:
        self.root = QVBoxLayout(panel)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget(panel)
        self.pages: dict[str, QWidget] = {}
        self.groups: dict[tuple[str, str], QGroupBox] = {}
        self._layouts = {"": self.root}
        self._forms: dict[tuple[str, str], QFormLayout] = {}

    def _page_layout(self, tab: str) -> QVBoxLayout:
        if tab not in self._layouts:
            page = QWidget()
            self.pages[tab] = page
            layout = QVBoxLayout(page)
            layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            self._layouts[tab] = layout
            self.tabs.addTab(page, tab.replace("&", "&&"))
        return self._layouts[tab]

    def add_to_tab(self, tab: str, widget: QWidget) -> None:
        self._page_layout(tab).addWidget(widget)

    def section(self, tab: str, group: str) -> QFormLayout:
        layout = self._page_layout(tab)
        key = (tab, group)
        if key not in self._forms:
            form = self._new_field_layout()
            self._forms[key] = form
            if group:
                box = QGroupBox(group.replace("&", "&&"))
                self.groups[key] = box
                box.setLayout(form)
                layout.addWidget(box)
            else:
                layout.addLayout(form)
        return self._forms[key]

    def finish(self) -> None:
        self.root.addWidget(self.tabs)
        self.tabs.setVisible(bool(self.pages))

    def set_visible_sections(self, sections: set[tuple[str, str]]) -> None:
        for key, box in self.groups.items():
            box.setVisible(key in sections)
        visible_tabs = {name for name, _ in sections if name}
        for name, page in self.pages.items():
            self.tabs.setTabVisible(self.tabs.indexOf(page), name in visible_tabs)
        self.tabs.setVisible(bool(visible_tabs))

    @staticmethod
    def _new_field_layout() -> QFormLayout:
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setVerticalSpacing(10)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return form

class AutoForm(QScrollArea):
    """Auto-generated form — validates fields & line items, tracks dirty state, supports Ref quick-create."""

    closed = Signal()
    data_changed = Signal(str)  # emits short status message

    def __init__(
        self,
        entity_class: type,
        database: Database,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._entity_class = entity_class
        self._db = database
        self._entity: Entity | None = None
        self._widgets: dict[str, Any] = {}
        self._tab_widgets: dict[str, Any] = {}
        self._is_doc = entity_class._meta.is_document
        self._related_views: list[QWidget] = []
        self._related_id: int | None = None
        self._initial_snapshot: dict[str, Any] | None = None
        self._choice_criteria: dict[str, dict[str, Any] | None] = {}
        self._choice_create_buttons: dict[str, QPushButton] = {}

        self.setWidgetResizable(True)
        try:
            self._build_ui()
        except Exception as exc:
            show_ui_error(self, "Ошибка", exc, prefix="Ошибка создания формы: ")

    def _build_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        safe_name = html.escape(self._entity_class._meta.verbose_name)
        title = QLabel(f"<b>{safe_name}</b>")
        title.setProperty("heading", True)
        title.setWordWrap(True)
        layout.addWidget(title)

        # Fields
        self._field_panel = QWidget()
        self._field_controls: dict[str, QWidget] = {}
        self._field_labels: dict[str, QLabel] = {}
        self._field_layouts: dict[str, QFormLayout] = {}
        self._form_layout = FieldLayout(self._field_panel)
        for name, field in self._entity_class._meta.fields.items():
            if not field.visible:
                continue
            tab = self._entity_class.__form_tabs__.get(name, "")
            group = self._entity_class.__form_groups__.get(name, "")
            form = self._form_layout.section(tab, group)
            self._field_layouts[name] = form
            editor = create_editor(field)
            self._widgets[name] = editor
            label_text = field.display_name
            if field.required:
                label_text += " *"

            label = QLabel(label_text + ":")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setBuddy(editor)
            self._field_labels[name] = label

            if isinstance(field, RefField) and not field.read_only:
                row_wrap = QWidget()
                row_hl = QHBoxLayout(row_wrap)
                row_hl.setContentsMargins(0, 0, 0, 0)
                row_hl.addWidget(editor, stretch=1)
                add_ref_btn = QPushButton("+")
                add_ref_btn.setProperty("compact", True)
                add_ref_btn.setFixedWidth(36)
                add_ref_btn.setToolTip(f"Создать новый «{field.display_name}»")
                add_ref_btn.clicked.connect(
                    lambda checked=False, n=name, f=field: self._quick_create_ref(n, f)
                )
                if field.choice_filter:
                    self._choice_create_buttons[name] = add_ref_btn
                    add_ref_btn.setEnabled(False)
                row_hl.addWidget(add_ref_btn)
                self._field_controls[name] = row_wrap
                form.addRow(label, row_wrap)
            else:
                self._field_controls[name] = editor
                form.addRow(label, editor)
        layout.addWidget(self._field_panel)

        # Table parts
        self._table_tabs: set[str] = set()
        for tp_name, tp in self._entity_class._meta.table_parts.items():
            tw = _TablePartWidget(tp, self._db, entity_class=self._entity_class, part_name=tp_name)
            self._tab_widgets[tp_name] = tw
            tab = self._entity_class.__form_table_tabs__.get(tp_name)
            if tab:
                self._form_layout.add_to_tab(tab, tw)
                self._table_tabs.add(tab)
            else:
                layout.addWidget(tw)
        self._form_layout.finish()

        self._basis_button = QPushButton("Создать на основании")
        self._basis_menu = QMenu(self._basis_button)
        self._basis_button.setMenu(self._basis_menu)
        self._basis_button.hide()
        record_actions = QHBoxLayout()
        record_actions.addWidget(self._basis_button)
        self._reports_button = QPushButton("Отчёты")
        self._reports_menu = QMenu(self._reports_button)
        self._reports_button.setMenu(self._reports_menu)
        self._reports_button.hide()
        record_actions.addWidget(self._reports_button)
        self._unpost_button: QPushButton | None = None
        self._movements_button: QPushButton | None = None
        if self._is_doc:
            self._unpost_button = QPushButton("Отменить проведение")
            self._unpost_button.clicked.connect(self._unpost_document)
            self._unpost_button.hide()
            record_actions.addWidget(self._unpost_button)
            self._movements_button = QPushButton("Движения")
            self._movements_button.clicked.connect(self._show_document_movements)
            self._movements_button.hide()
            record_actions.addWidget(self._movements_button)
        record_actions.addStretch()
        layout.addLayout(record_actions)

        self._related_layout = QVBoxLayout()
        layout.addLayout(self._related_layout)
        layout.addStretch()

        self._workflow_box: QWidget | None = None
        if self._entity_class.__workflow__ is not None:
            self._workflow_box = QWidget()
            approval = QHBoxLayout(self._workflow_box)
            approval.setContentsMargins(0, 0, 0, 0)
            approval.addWidget(QLabel("Согласование"))
            self._workflow_actions = QComboBox()
            approval.addWidget(self._workflow_actions)
            self._workflow_apply = QPushButton("Выполнить")
            self._workflow_apply.clicked.connect(self._run_workflow_action)
            approval.addWidget(self._workflow_apply)
            approval.addStretch(1)
            history = QPushButton("История")
            history.clicked.connect(self._show_workflow_history)
            approval.addWidget(history)
            self._workflow_assign = QPushButton("Назначить согласующего")
            self._workflow_assign.clicked.connect(self._assign_workflow)
            self._workflow_assign.hide()
            approval.addWidget(self._workflow_assign)
            self._workflow_box.hide()
            layout.addWidget(self._workflow_box)

        # Buttons
        self._write_buttons: list[QPushButton] = []
        self._post_button: QPushButton | None = None
        self._print_button: QPushButton | None = None
        cmd_layout = QHBoxLayout()
        cmd_layout.addStretch()

        for cmd_name, cmd in self._entity_class._meta.commands.items():
            if not self._db.allows_command(self._entity_class, cmd_name):
                continue
            btn = QPushButton(cmd.name)
            btn.clicked.connect(
                lambda checked=False, n=cmd_name: self._execute_command(n)
            )
            self._write_buttons.append(btn)
            cmd_layout.addWidget(btn)

        if self._is_doc:
            self._build_doc_buttons(cmd_layout)
        else:
            self._build_catalog_buttons(cmd_layout)

        layout.addLayout(cmd_layout)

        # Connect auto-fill signals once
        self._connect_auto_fill()
        for name in {condition[0] for condition in self._entity_class.__form_visible_when__.values()}:
            control = self._widgets.get(name)
            if isinstance(control, QCheckBox):
                control.toggled.connect(self._refresh_field_visibility)
            elif isinstance(control, QComboBox):
                control.currentIndexChanged.connect(self._refresh_field_visibility)
        self._connect_choice_filters()

        # Shortcuts
        save_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Save), self)
        save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save_shortcut.activated.connect(self._save)

    def _build_doc_buttons(self, layout: QHBoxLayout) -> None:
        draft_btn = QPushButton("Сохранить")
        draft_btn.clicked.connect(self._save_draft)
        self._write_buttons.append(draft_btn)
        layout.addWidget(draft_btn)

        post_btn = QPushButton("Провести и закрыть")
        post_btn.setProperty("primary", True)
        post_btn.clicked.connect(self._doc_post_and_close)
        self._post_button = post_btn
        layout.addWidget(post_btn)

        if self._entity_class.__print_form__ is not None:
            self._print_button = QPushButton("Печатная форма")
            self._print_button.clicked.connect(self._preview_print_form)
            layout.addWidget(self._print_button)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self._request_close)
        layout.addWidget(close_btn)

    def _build_catalog_buttons(self, layout: QHBoxLayout) -> None:
        save_btn = QPushButton("Сохранить")
        save_btn.clicked.connect(self._save_catalog)
        self._write_buttons.append(save_btn)
        layout.addWidget(save_btn)

        save_close_btn = QPushButton("Сохранить и закрыть")
        save_close_btn.setProperty("primary", True)
        save_close_btn.clicked.connect(self._catalog_save_and_close)
        self._write_buttons.append(save_close_btn)
        layout.addWidget(save_close_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self._request_close)
        layout.addWidget(close_btn)

    # ── Quick create Ref item in modal dialog ──

    def _quick_create_ref(self, field_name: str, field: RefField) -> None:
        target_cls = field.resolve_target()
        if not self._db.allows(target_cls, "create"):
            return
        defaults = self._choice_filter_values(field_name, field) if field.choice_filter else {}
        if defaults is None:
            return
        dlg = _RecordDialog(target_cls, self._db, self)
        dlg.setWindowTitle(f"Новый: {target_cls._meta.verbose_name}")
        dlg.resize(520, 420)
        dlg.form.load_new(defaults)
        dlg.exec()
        entity = dlg.form.entity
        created_id = entity.id if entity is not None else None
        dlg.deleteLater()
        self._refresh_ref_editors()
        self._refresh_choice_editors()
        if created_id is not None:
            combo = self._widgets.get(field_name)
            if isinstance(combo, QComboBox):
                idx = combo.findData(created_id, Qt.ItemDataRole.UserRole)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    # ── Public API ──

    @property
    def entity(self) -> Entity | None:
        return self._entity

    def has_unsaved_changes(self) -> bool:
        if self._entity is None or self._initial_snapshot is None:
            return False
        current = self._capture_current_values()
        return current != self._initial_snapshot

    def _capture_current_values(self) -> dict[str, Any]:
        snap: dict[str, Any] = {}
        for name in self._widgets:
            snap[name] = self._form_value(name)
        for tp_name, tw in self._tab_widgets.items():
            snap[f"__tp__{tp_name}"] = [dict(r) for r in tw.get_rows()]
        return snap

    def _mark_clean(self) -> None:
        self._initial_snapshot = self._capture_current_values()

    def load_entity(self, entity: Entity) -> None:
        self._entity = entity
        self._refresh_ref_editors()
        self._populate_widgets()
        self._mark_clean()

    def load_new(self, defaults: dict[str, Any] | None = None) -> None:
        merged: dict[str, Any] = {}
        for k, v in self._entity_class._meta.defaults.items():
            val: Any = v(self._db) if callable(v) else v
            if k in self._entity_class._meta.table_parts:
                merged[k] = val
            elif k in self._entity_class._meta.fields:
                merged[k] = val
        if defaults:
            merged.update(defaults)
        self._entity = self._entity_class(**merged)
        self._refresh_ref_editors()
        self._populate_widgets()
        self._mark_clean()

    def load_by_id(self, id: int) -> None:
        entity = self._db.get(self._entity_class, id)
        if entity is not None:
            self.load_entity(entity)

    # ── Widget <-> Entity ──

    def _populate_widgets(self) -> None:
        if self._entity is None:
            return
        for name, widget in self._widgets.items():
            value = self._entity._data.get(name)
            field = self._entity_class._meta.fields[name]
            if name not in self._entity_class._meta.choice_refs:
                with QSignalBlocker(widget):
                    set_editor_value(widget, field, value, preserve_unavailable=self._entity.id is not None)
        self._refresh_choice_editors(from_entity=self._entity_class._meta.choice_refs.keys())
        for tp_name, tw in self._tab_widgets.items():
            tw.load_rows(self._entity._table_data.get(tp_name, []))
        self._populate_related_lists()
        self._refresh_basis_actions()
        self._refresh_report_actions()
        self._show_workflow_actions()
        editable = self._db.can_edit(self._entity)
        self._field_panel.setEnabled(editable)
        if self._print_button is not None:
            self._print_button.setEnabled(self._entity._id is not None)
        for name, control in self._field_controls.items():
            field = self._entity_class._meta.fields[name]
            control.setEnabled(not field.read_only and self._db.can_edit_field(self._entity_class, name))
        self._refresh_field_visibility()
        if self._post_button is not None:
            self._post_button.setEnabled(self._db.can_post(self._entity))
        if self._unpost_button is not None:
            self._unpost_button.setVisible(
                self._entity.id is not None and bool(self._entity._data.get("posted"))
                and self._db.can_post(self._entity)
            )
        if self._movements_button is not None:
            self._movements_button.setVisible(
                self._entity.id is not None and bool(get_all_registers())
                and not self._db.masked_fields(self._entity_class)
                and self._db.allows(self._entity_class, "read")
            )
        for widget in [*self._tab_widgets.values(), *self._related_views, *self._write_buttons]:
            widget.setEnabled(editable)

    def _refresh_field_visibility(self) -> None:
        visible_groups = {(tab, "") for tab in self._table_tabs}
        for name, control in self._field_controls.items():
            readable = self._field_is_visible(name)
            self._field_layouts[name].setRowVisible(control, readable)
            if name in self._entity_class.__required_when_visible__:
                field = self._entity_class._meta.fields[name]
                self._field_labels[name].setText(field.display_name + (" *:" if readable else ":"))
            if readable:
                visible_groups.add((
                    self._entity_class.__form_tabs__.get(name, ""),
                    self._entity_class.__form_groups__.get(name, ""),
                ))
        self._form_layout.set_visible_sections(visible_groups)

    def _field_is_visible(self, name: str) -> bool:
        if name not in self._widgets or not self._db.can_read_field(self._entity_class, name):
            return False
        condition = self._entity_class.__form_visible_when__.get(name)
        if condition is None:
            return True
        source, expected = condition
        return (
            source in self._widgets and self._db.can_read_field(self._entity_class, source)
            and get_editor_value(self._widgets[source], self._entity_class._meta.fields[source]) == expected
        )

    def _preview_print_form(self) -> None:
        entity = self._entity
        if entity is None or entity.__print_form__ is None:
            return
        config = entity.__print_form__
        try:
            content = config.render_html(self._db, entity)
        except (ValueError, PermissionError) as exc:
            show_ui_error(self, "Печатная форма", exc, warning=True)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(config.title)
        dialog.resize(720, 580)
        layout = QVBoxLayout(dialog)
        preview = QTextBrowser()
        preview.setHtml(content)
        layout.addWidget(preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(dialog.reject)
        export = buttons.addButton("Сохранить PDF", QDialogButtonBox.ButtonRole.ActionRole)

        def save_pdf() -> None:
            path, _ = QFileDialog.getSaveFileName(dialog, "Сохранить PDF", "", "PDF (*.pdf)")
            if path:
                try:
                    config.export_pdf(self._db, entity, path if path.lower().endswith(".pdf") else path + ".pdf")
                except (OSError, ValueError, PermissionError) as exc:
                    show_ui_error(dialog, "Сохранение PDF", exc, warning=True)

        export.clicked.connect(save_pdf)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    def _show_workflow_actions(self) -> None:
        if self._workflow_box is None or self._entity is None:
            return
        route = self._entity.__workflow__
        if route is None:
            return
        account = current_account()
        assignee = self._entity._data.get(route.assignee_field) if route.uses_assignment else None
        self._workflow_assign.setVisible(route.uses_assignment and account is not None and account.role == "admin")
        actions = (
            route.available_actions(
                self._entity._data.get(route.field), current_role(), account.login if account else None, assignee,
            )
            if self._entity._id is not None else ()
        )
        if not self._db.can_edit(self._entity):
            actions = tuple(step for step in actions if step.assigned_only)
        self._workflow_actions.clear()
        self._workflow_actions.addItems([step.label for step in actions])
        self._workflow_actions.setVisible(bool(actions))
        self._workflow_apply.setVisible(bool(actions))
        self._workflow_box.setVisible(self._entity._id is not None)

    def _assign_workflow(self) -> None:
        if self._entity is None:
            return
        if self.has_unsaved_changes():
            QMessageBox.warning(self, "Согласование", "Сначала сохраните изменения в карточке.")
            return
        try:
            logins = [item.login for item in AccountStore(self._db).list_accounts()]
            if not logins:
                raise ValueError("Создайте учётную запись согласующего.")
            route = self._entity.__workflow__
            current = self._entity._data.get(route.assignee_field) if route else None
            index = logins.index(current) if current in logins else 0
            login, accepted = QInputDialog.getItem(
                self, "Назначение согласующего", "Учётная запись", logins, index, False,
            )
            if accepted:
                self._db.assign_workflow(self._entity, login)
                self._populate_widgets()
                self._mark_clean()
                self.data_changed.emit(f"Назначен согласующий: {login}")
        except Exception as exc:
            show_ui_error(self, "Согласование", exc, warning=True)

    def _show_workflow_history(self) -> None:
        if self._entity is None:
            return
        try:
            events = self._db.workflow_history(self._entity)
        except Exception as exc:
            show_ui_error(self, "История согласования", exc, warning=True)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("История согласования")
        dialog.resize(810, 340)
        layout = QVBoxLayout(dialog)
        if events:
            table = QTableWidget(len(events), 6)
            table.setHorizontalHeaderLabels(["Время", "Действие", "Предыдущий статус", "Новый статус", "Роль", "Пользователь"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.verticalHeader().setDefaultSectionSize(32)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            for row, event in enumerate(events):
                action = event.action or ("Создание записи" if event.source is None else "Изменение статуса")
                values = (
                    event.occurred_at.astimezone().strftime("%d.%m.%Y %H:%M:%S"),
                    action, event.source or "—", event.target, event.role or "—", event.login or "—",
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    table.setItem(row, column, item)
            layout.addWidget(table)
        else:
            layout.addWidget(QLabel("Сохранённых изменений статуса пока нет."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    def _run_workflow_action(self) -> None:
        if self._entity is None:
            return
        label = self._workflow_actions.currentText()
        if not label:
            return
        if self._db.can_edit(self._entity):
            self._widgets_to_entity()
        try:
            self._entity = self._db.transition(self._entity, label)
        except Exception as exc:
            show_ui_error(self, "Согласование", exc, warning=True)
            self._show_workflow_actions()
            return
        self._populate_widgets()
        self._mark_clean()
        self.data_changed.emit(f"Выполнено «{label}»")

    def _refresh_basis_actions(self) -> None:
        self._basis_menu.clear()
        if self._entity is not None and self._entity._id is not None:
            for target in get_all_entities():
                if not self._db.allows(target, "create"):
                    continue
                for name, field in target._meta.fields.items():
                    if (
                        isinstance(field, RefField) and field.create_from
                        and field.resolve_target() is self._entity_class
                        and self._db.can_edit_field(target, name)
                    ):
                        self._basis_menu.addAction(
                            field.create_from,
                            lambda _checked=False, cls=target, link=name: self._create_from_basis(cls, link),
                        )
        self._basis_button.setVisible(not self._basis_menu.isEmpty())

    def _refresh_report_actions(self) -> None:
        self._reports_menu.clear()
        if self._entity is not None and self._entity.id is not None and self._db.allows(self._entity_class, "read"):
            for report in get_all_reports():
                params = tuple(
                    name for name, param in report._report_meta.params.items()
                    if isinstance(param, RefParam) and param.from_record
                    and param.target in (self._entity_class, self._entity_class._meta.name)
                )
                if params:
                    action = self._reports_menu.addAction(report._report_meta.title.replace("&", "&&"))
                    action.triggered.connect(
                        lambda checked=False, cls=report, names=params: self._open_record_report(cls, names)
                    )
        self._reports_button.setVisible(not self._reports_menu.isEmpty())

    def _open_record_report(self, report: type[Report], params: tuple[str, ...]) -> None:
        if self._entity is None or self._entity.id is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(report._report_meta.title)
        dialog.resize(900, 600)
        try:
            view = ReportView(report, self._db, dialog, initial_refs=dict.fromkeys(params, self._entity.id))

            def open_record(cls: type, record_id: int) -> None:
                self._open_related_form(cls, record_id=record_id, parent=dialog)
                view.refresh()

            view.drill_down_requested.connect(open_record)
            layout = QVBoxLayout(dialog)
            layout.addWidget(view, stretch=1)
            close = QPushButton("Закрыть")
            close.clicked.connect(dialog.reject)
            layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
            dialog.exec()
        except Exception as exc:
            show_ui_error(self, "Не удалось открыть отчёт", exc, warning=True)
        finally:
            dialog.deleteLater()

    def _create_from_basis(self, target: type, link: str) -> None:
        source = self._entity
        if source is None or source._id is None:
            return
        if not self._db.allows(target, "create") or not self._db.can_edit_field(target, link):
            return
        saved = self._db.get(self._entity_class, source._id)
        if saved is None:
            QMessageBox.warning(self, "Запись недоступна", "Исходная запись больше недоступна.")
            return
        reference = target._meta.fields.get(link)
        if not isinstance(reference, RefField) or not reference.create_from or reference.resolve_target() is not self._entity_class:
            return
        defaults: dict[str, Any] = {link: source._id}
        protected = {link, "id", "number", "date", "posted", (target.__numbering__ or {}).get("field")}
        for name, source_name in reference.basis_fields.items():
            field = target._meta.fields.get(name)
            original = self._entity_class._meta.fields.get(source_name)
            if (
                name in protected or not self._basis_fields_match(field, original)
                or not self._db.can_edit_field(target, name)
                or not self._db.can_read_field(self._entity_class, source_name)
            ):
                continue
            defaults[name] = deepcopy(saved._data.get(source_name))
        for name, mapping in reference.basis_tables.items():
            destination = target._meta.table_parts.get(name)
            source_part = self._entity_class._meta.table_parts.get(mapping["source"])
            if destination is None or source_part is None:
                continue
            columns = {
                col: original for col, original in mapping["columns"].items()
                if self._basis_fields_match(destination.fields.get(col), source_part.fields.get(original))
                and self._db.can_edit_field(target, f"{name}.{col}")
                and self._db.can_read_field(self._entity_class, f"{mapping['source']}.{original}")
            }
            if columns:
                defaults[name] = [
                    {col: deepcopy(row.get(original)) for col, original in columns.items()}
                    for row in saved._table_data.get(mapping["source"], [])
                ]
        self._open_related_form(target, defaults=defaults)

    @staticmethod
    def _basis_fields_match(field: Field | None, original: Field | None) -> bool:
        if field is None or field.read_only or type(field) is not type(original):
            return False
        if isinstance(field, RefField):
            return isinstance(original, RefField) and original.resolve_target() is field.resolve_target()
        return True

    def _populate_related_lists(self) -> None:
        entity = self._entity
        entity_id = entity._id if entity is not None else None
        if entity_id == self._related_id:
            return
        self._related_id = entity_id
        for view in self._related_views:
            self._related_layout.removeWidget(view)
            view.deleteLater()
        self._related_views.clear()
        if entity_id is None:
            return
        related_lists = {
            (target, field): title
            for title, target, field in getattr(self._entity_class, "__related_lists__", ())
        }
        for target in get_all_entities():
            for ref_field, field_def in target._meta.fields.items():
                if (
                    isinstance(field_def, RefField) and field_def.related_list
                    and field_def.resolve_target() is self._entity_class
                ):
                    related_lists.setdefault((target, ref_field), field_def.related_list)
        for (target, ref_field), title in related_lists.items():
            if not self._db.can_read_field(target, ref_field):
                continue
            group = QGroupBox(title)
            layout = QVBoxLayout(group)
            view = ListView(target, self._db, filters={ref_field: entity_id})
            view.setMinimumHeight(230)
            view.create_requested.connect(
                lambda cls, f=ref_field, pk=entity_id: self._open_related_form(cls, defaults={f: pk})
            )
            view.edit_requested.connect(lambda item: self._open_related_form(type(item), item))
            view.copy_requested.connect(lambda item: self._open_related_form(type(item), item, copy=True))
            view.data_changed.connect(self._reload_parent)
            layout.addWidget(view)
            self._related_layout.addWidget(group)
            self._related_views.append(group)

    def refresh_related_data(self) -> None:
        """Refresh related records and unchanged fields after an external edit."""
        self._refresh_ref_editors()
        self._refresh_choice_editors()
        for group in self._related_views:
            view = group.findChild(ListView)
            if view is not None:
                view.refresh()
        self._reload_parent()

    def _reload_parent(self) -> None:
        if self._entity is None or self._entity._id is None:
            return
        updated = self._db.get(self._entity_class, self._entity._id)
        if updated is not None:
            unchanged = set()
            for name, widget in self._widgets.items():
                field = self._entity_class._meta.fields[name]
                current = self._form_value(name)
                if field.read_only or current == (self._initial_snapshot or {}).get(name):
                    unchanged.add(name)
                    if name not in self._entity_class._meta.choice_refs:
                        with QSignalBlocker(widget):
                            set_editor_value(widget, field, updated._data.get(name), preserve_unavailable=True)
            self._entity = updated
            for name, ref in self._entity_class._meta.choice_refs.items():
                if any(src in self._widgets and src not in unchanged for src in ref.choice_filter.values()):
                    unchanged.discard(name)
            self._refresh_choice_editors(from_entity=unchanged)
            if self._initial_snapshot is not None:
                for name in unchanged:
                    self._initial_snapshot[name] = self._form_value(name)
                for part, table in self._tab_widgets.items():
                    key = f"__tp__{part}"
                    if table.get_rows() == self._initial_snapshot[key]:
                        table.load_rows(updated._table_data[part])
                        self._initial_snapshot[key] = [dict(row) for row in table.get_rows()]
            self._refresh_field_visibility()

    def _open_related_form(
        self, cls: type, item: Entity | None = None, *, defaults: dict[str, Any] | None = None,
        copy: bool = False, record_id: int | None = None, parent: QWidget | None = None,
    ) -> None:
        try:
            dialog = _RecordDialog(cls, self._db, parent if parent is not None else self)
            dialog.setWindowTitle(cls._meta.verbose_name)
            dialog.resize(620, 450)
            if item is None and record_id is None:
                dialog.form.load_new(defaults)
            else:
                key = item.id if item is not None else record_id
                full = self._db.get(cls, key) if key is not None else None
                if full is None:
                    raise ValueError("Запись была удалена.")
                dialog.form.load_entity(full.copy_for_edit(self._db) if copy else full)
            dialog.exec()
            dialog.deleteLater()
            self.refresh_related_data()
        except Exception as exc:
            show_ui_error(self, "Не удалось открыть форму", exc, warning=True)

    def _refresh_ref_editors(self) -> None:
        for name, widget in self._widgets.items():
            field = self._entity_class._meta.fields.get(name)
            if isinstance(field, RefField) and not field.choice_filter and isinstance(widget, QComboBox):
                target_class = field.resolve_target()
                items = self._db.list(target_class)
                value = self._form_value(name)
                with QSignalBlocker(widget):
                    populate_ref_editor(widget, items, field.representation_field)
                    set_editor_value(widget, field, value, preserve_unavailable=(
                        self._entity is not None and self._entity.id is not None
                        and self._entity._data.get(name) == value
                    ))
        for tw in self._tab_widgets.values():
            tw.refresh_refs(self._db)

    def _choice_filter_values(self, name: str, field: RefField) -> dict[str, Any] | None:
        if not self._field_is_visible(name):
            return None
        target = field.resolve_target()
        values: dict[str, Any] = {}
        for target_name, source in field.choice_filter.items():
            if not self._field_is_visible(source) or not self._db.can_read_field(target, target_name):
                return None
            source_field = self._entity_class._meta.fields[source]
            value = get_editor_value(self._widgets[source], source_field)
            if value is None or value == "":
                return None
            if isinstance(source_field, RefField) and self._db.get(source_field.resolve_target(), value) is None:
                return None
            values[target_name] = value
        return values

    def _refresh_choice_editors(self, *, from_entity: Collection[str] = (), changed: bool = False) -> None:
        entity = self._entity
        if entity is None:
            return
        for name, field in self._entity_class._meta.choice_refs.items():
            combo = self._widgets.get(name)
            if not isinstance(combo, QComboBox):
                continue
            loading = name in from_entity
            value = entity._data.get(name) if loading else self._form_value(name)
            criteria = self._choice_filter_values(name, field)
            if changed and criteria == self._choice_criteria.get(name):
                continue
            button = self._choice_create_buttons.get(name)
            if button is not None:
                button.setEnabled(criteria is not None and self._db.allows(field.resolve_target(), "create"))
            items = [] if criteria is None else self._db.filter(
                field.resolve_target(), **{f"{key}__eq": val for key, val in criteria.items()}
            )
            previous_selection = get_editor_value(combo, field)
            with QSignalBlocker(combo):
                populate_ref_editor(combo, items, field.representation_field)
                set_editor_value(combo, field, value, preserve_unavailable=(
                    entity.id is not None and value == entity._data.get(name)
                    and (loading or self._choice_criteria.get(name) == criteria)
                ))
            self._choice_criteria[name] = criteria
            if changed and previous_selection != get_editor_value(combo, field):
                self._on_ref_auto_fill(name, field)
                self._refresh_field_visibility()

    def _connect_choice_filters(self) -> None:
        conditions = self._entity_class.__form_visible_when__
        sources = {
            name for field in self._entity_class._meta.choice_refs.values()
            for name in field.choice_filter.values()
        }
        watched = sources | self._entity_class._meta.choice_refs.keys()
        watched |= {conditions[name][0] for name in watched if name in conditions}

        def refresh(*args: Any) -> None:
            self._refresh_choice_editors(changed=True)

        for name in watched:
            widget = self._widgets.get(name)
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(refresh)
                if widget.isEditable():
                    widget.editTextChanged.connect(refresh)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(refresh)
            elif isinstance(widget, (QLineEdit, QPlainTextEdit)):
                widget.textChanged.connect(refresh)
            elif isinstance(widget, QDateTimeEdit):
                widget.dateTimeChanged.connect(refresh)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.valueChanged.connect(refresh)
            elif isinstance(widget, QGroupBox):
                for checkbox in widget.findChildren(QCheckBox):
                    checkbox.toggled.connect(refresh)

    def _form_value(self, name: str) -> Any:
        widget = self._widgets[name]
        return get_editor_value(widget, self._entity_class._meta.fields[name], preserve_unavailable=True)

    def _connect_auto_fill(self) -> None:
        """Connect RefField editors to auto-fill target fields."""
        for name, widget in self._widgets.items():
            field = self._entity_class._meta.fields.get(name)
            if not isinstance(field, RefField) or not field.auto_fill:
                continue
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(
                    lambda idx, n=name, f=field: self._on_ref_auto_fill(n, f)
                )

    def _on_ref_auto_fill(self, ref_name: str, ref_field: RefField) -> None:
        """Auto-fill or clear target field when referenced entity changes."""
        destination = ref_field.auto_fill
        if not destination or destination not in self._widgets:
            return
        if not self._db.can_edit_field(self._entity_class, destination):
            return
        fill_widget = self._widgets[destination]
        fill_field_def = self._entity_class._meta.fields[destination]
        ref_id = get_editor_value(self._widgets[ref_name], ref_field)
        if ref_id is None:
            set_editor_value(fill_widget, fill_field_def, None)
            return
        target = ref_field.resolve_target()
        entity = self._db.get(target, ref_id)
        if entity is None:
            return
        val = entity._data.get(destination)
        if val is not None:
            set_editor_value(fill_widget, fill_field_def, val)

    def _widgets_to_entity(self) -> None:
        if self._entity is None:
            return
        for name in self._widgets:
            field = self._entity_class._meta.fields[name]
            if not field.read_only and self._db.can_edit_field(self._entity_class, name):
                self._entity._data[name] = self._form_value(name)
        for tp_name, tw in self._tab_widgets.items():
            rows = tw.get_rows()
            self._entity._table_data[tp_name] = rows

    # ── Validation & Save ──

    def _validate_all(self) -> bool:
        """Validate header fields, table part rows, and entity custom rules."""
        if self._entity is None:
            return False
        first_bad_field: str | None = None
        for name, field in self._entity_class._meta.fields.items():
            if name in getattr(self._entity, "_masked_fields", ()):
                continue
            w = self._widgets.get(name)
            if w is not None:
                w.setStyleSheet("")
            errs = self._entity._validate_field(name, field)
            if errs and w is not None and first_bad_field is None:
                first_bad_field = name
                w.setStyleSheet("border: 1px solid #dc2626;")

        all_errs = self._entity.validate()
        if all_errs:
            if first_bad_field is not None:
                page = self._form_layout.pages.get(self._entity_class.__form_tabs__.get(first_bad_field, ""))
                if page is not None:
                    self._form_layout.tabs.setCurrentWidget(page)
                widget = self._widgets[first_bad_field]
                self.ensureWidgetVisible(widget)
                widget.setFocus()
            else:
                for part, row, column, _ in self._entity._table_validation_errors():
                    table = self._tab_widgets.get(part)
                    if table is None or not table._column_readable(column):
                        continue
                    page = self._form_layout.pages.get(self._entity_class.__form_table_tabs__.get(part, ""))
                    if page is not None:
                        self._form_layout.tabs.setCurrentWidget(page)
                    cell = table.focus_cell(row, column)
                    self.ensureWidgetVisible(cell if cell is not None else table)
                    break
            QMessageBox.warning(self, "Ошибка заполнения", "\n".join(all_errs))
            return False
        return True

    def _save(self) -> bool:
        if self._entity is None or not self._db.can_edit(self._entity):
            return False
        self._widgets_to_entity()
        if not self._validate_all():
            return False
        try:
            self._db.save(self._entity)
            self._show_save_message()
            self._populate_widgets()
            self._mark_clean()
            self.data_changed.emit(f"Сохранено: {self._entity}")
            return True
        except Exception as exc:
            show_ui_error(self, "Ошибка", exc, prefix="Ошибка сохранения: ")
            return False

    def _show_save_message(self) -> None:
        message = getattr(self._entity, "_save_message", None)
        if message and self._entity is not None:
            object.__setattr__(self._entity, "_save_message", None)
            QMessageBox.information(self, "Запись сохранена", message)

    # ── Document & Catalog button handlers ──

    def _save_draft(self) -> None:
        self._save()

    def _run_post(self) -> bool:
        """Validate, run post command, check sequence, persist, and write movements."""
        if self._entity is None or not self._db.can_post(self._entity):
            return False
        self._widgets_to_entity()
        if not self._validate_all():
            return False

        try:
            self._db.post(self._entity)
        except SequenceViolationError as exc:
            QMessageBox.warning(self, "Нарушение последовательности", str(exc))
            return False
        except Exception as exc:
            show_ui_error(self, "Ошибка проведения", exc)
            return False

        self._show_save_message()
        self._populate_widgets()
        self._mark_clean()
        self.data_changed.emit(f"Проведён: {self._entity}")
        return True

    def _doc_post_and_close(self) -> None:
        if self._run_post():
            self.closed.emit()

    def _show_document_movements(self) -> None:
        if self._entity is None:
            return
        try:
            movements = self._db.document_movements(self._entity)
            if not movements:
                QMessageBox.information(self, "Движения", "У документа нет движений.")
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("Движения документа")
            dialog.resize(680, 430)
            layout = QVBoxLayout(dialog)
            tabs = QTabWidget()
            layout.addWidget(tabs)
            for register, rows in movements.items():
                meta = register._register_meta
                columns = [("period", "Период")]
                columns.extend((name, member.name) for name, member in (meta.dimensions | meta.resources).items())
                references: dict[str, dict[int, str]] = {}
                for name, dimension in meta.dimensions.items():
                    if isinstance(dimension, RefDimension):
                        ids = {row[name] for row in rows if row[name] is not None}
                        if ids:
                            target = dimension.resolve_target()
                            rep = dimension.representation_field
                            references[name] = {
                                entity.id: str(getattr(entity, rep, None) or entity) if rep else str(entity)
                                for entity in self._db.filter(target, id__in=ids) if entity.id is not None
                            }
                table = QTableWidget(len(rows), len(columns))
                table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
                table.setHorizontalHeaderLabels([label for _, label in columns])
                for row_index, row in enumerate(rows):
                    for col_index, (name, _) in enumerate(columns):
                        value = row[name]
                        if value is not None and name in references:
                            value = references[name].get(value, "Недоступно")
                        table.setItem(row_index, col_index, QTableWidgetItem("" if value is None else str(value)))
                table.resizeColumnsToContents()
                tabs.addTab(table, meta.name)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            dialog.exec()
            dialog.deleteLater()
        except Exception as exc:
            show_ui_error(self, "Движения документа", exc, warning=True)

    def _unpost_document(self) -> None:
        if self._entity is None or self._entity.id is None or not self._db.can_post(self._entity):
            return
        prompt = "Отменить проведение и удалить движения документа?"
        if self.has_unsaved_changes():
            prompt += " Несохранённые изменения в форме будут сброшены."
        if QMessageBox.question(
            self, "Отменить проведение", prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            stored = self._db.unpost(self._entity)
        except Exception as exc:
            show_ui_error(self, "Ошибка отмены проведения", exc, warning=True)
            return
        self.load_entity(stored)
        self.data_changed.emit(f"Проведение отменено: {stored}")

    def _confirm_close(self) -> bool:
        if not self.has_unsaved_changes():
            return True
        save = QMessageBox.StandardButton.Save
        discard = QMessageBox.StandardButton.Discard
        cancel = QMessageBox.StandardButton.Cancel
        dialog = QMessageBox(
            QMessageBox.Icon.Question,
            "Несохранённые изменения",
            "Сохранить изменения в карточке?",
            save | discard | cancel,
            self,
        )
        dialog.button(save).setText("Сохранить")
        dialog.button(discard).setText("Не сохранять")
        dialog.button(cancel).setText("Продолжить редактирование")
        dialog.setDefaultButton(save)
        dialog.setEscapeButton(cancel)
        try:
            answer = dialog.exec()
        finally:
            dialog.deleteLater()
        return answer == discard or (answer == save and self._save())

    def _request_close(self) -> None:
        if self._confirm_close():
            self.closed.emit()

    def _doc_close(self) -> None:
        self._request_close()

    def _save_catalog(self) -> None:
        self._save()

    def _catalog_save_and_close(self) -> None:
        if self._save():
            self.closed.emit()

    # ── Command handler ──

    def _execute_command(self, cmd_name: str) -> None:
        if self._entity is None or not self._db.can_edit(self._entity):
            return
        self._widgets_to_entity()
        cmd = self._entity_class._meta.commands.get(cmd_name)
        if cmd is None:
            return
        try:
            self._db.execute_command(self._entity, cmd_name)
            self._populate_widgets()
            self._mark_clean()
            self.data_changed.emit(f"Выполнено «{cmd.name}»")
        except Exception as exc:
            show_ui_error(self, "Ошибка", exc, prefix=f"{cmd.name}: ")


# ═══════════════════════════════════════════════════════════════════════
# Table part editor — with auto-fill + auto-calc + centered booleans
# ═══════════════════════════════════════════════════════════════════════

class _RecordDialog(QDialog):
    """A modal record editor with the same close confirmation as the main window."""

    def __init__(self, entity_class: type, database: Database, parent: QWidget) -> None:
        super().__init__(parent)
        self.form = AutoForm(entity_class, database, self)
        self.form.closed.connect(self.accept)
        QVBoxLayout(self).addWidget(self.form)

    def reject(self) -> None:
        if self.form._confirm_close():
            super().reject()


class _TablePartWidget(QGroupBox):
    """Editable sub-table with auto-calculation and validation."""

    _CONVENTION_PRICE = "price"
    _CONVENTION_QTY = "quantity"
    _CONVENTION_AMOUNT = "amount"
    rows_changed = Signal()

    def __init__(
        self,
        table_part: Any,
        database: Database | None = None,
        parent: QWidget | None = None,
        *,
        entity_class: type | None = None,
        part_name: str = "",
    ) -> None:
        super().__init__(table_part.verbose_name, parent)
        self._tp = table_part
        self._db = database
        self._entity_class = entity_class
        self._part_name = part_name
        self._rows_data: list[dict[str, Any]] = []
        self._loading = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._table = QTableWidget()
        col_names = list(self._tp.fields.keys())
        self._table.setColumnCount(len(col_names))
        self._table.setHorizontalHeaderLabels(
            [self._tp.fields[n].display_name for n in col_names]
        )
        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setMinimumSectionSize(80)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        layout.addWidget(self._table)

        btn_layout = QHBoxLayout()
        self._add_button = QPushButton("+ Добавить строку")
        self._add_button.clicked.connect(self._add_row)
        self._add_button.setEnabled(self._can_add_rows())
        btn_layout.addWidget(self._add_button)
        self._paste_button: QPushButton | None = None
        if self._db is not None and self._paste_columns():
            self._paste_button = QPushButton("Вставить строки…")
            self._paste_button.clicked.connect(self._paste_rows_dialog)
            btn_layout.addWidget(self._paste_button)
        self._delete_button = QPushButton("- Удалить")
        self._delete_button.clicked.connect(self._delete_row)
        self._delete_button.setEnabled(self._can_remove_rows())
        btn_layout.addWidget(self._delete_button)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    # ── Public ──

    def _column_editable(self, name: str) -> bool:
        return self._entity_class is None or self._db is None or self._db.can_edit_field(
            self._entity_class, f"{self._part_name}.{name}",
        )

    def _column_readable(self, name: str) -> bool:
        return self._entity_class is None or self._db is None or self._db.can_read_field(
            self._entity_class, f"{self._part_name}.{name}",
        )

    def _can_add_rows(self) -> bool:
        return any(
            not field.read_only and self._column_readable(name) and self._column_editable(name)
            for name, field in self._tp.fields.items()
        )

    def _can_remove_rows(self) -> bool:
        return all(self._column_editable(name) for name in self._tp.fields)

    def load_rows(self, rows: list[dict[str, Any]]) -> None:
        self._loading = True
        self._rows_data = [dict(r) for r in rows]
        self._rebuild_table()
        self._add_button.setEnabled(self._can_add_rows())
        if self._paste_button is not None:
            self._paste_button.setEnabled(bool(self._paste_columns()))
        self._delete_button.setEnabled(self._can_remove_rows())
        self._loading = False

    def get_rows(self) -> list[dict[str, Any]]:
        self._read_table()
        return self._rows_data

    def focus_cell(self, row: int, name: str) -> QWidget | None:
        column = list(self._tp.fields).index(name)
        self._table.setCurrentCell(row, column)
        self._table.scrollTo(self._table.model().index(row, column))
        editor = self._table.cellWidget(row, column)
        if editor is not None and editor.isEnabled():
            editor.setFocus()
        else:
            self._table.setFocus()
        return editor

    def refresh_refs(self, db: Database) -> None:
        self._loading = True
        try:
            for col, field in enumerate(self._tp.fields.values()):
                if isinstance(field, RefField) and self._table.rowCount():
                    items = db.list(field.resolve_target())
                    for row in range(self._table.rowCount()):
                        widget = self._table.cellWidget(row, col)
                        if isinstance(widget, QComboBox):
                            value = get_editor_value(widget, field, preserve_unavailable=True)
                            with QSignalBlocker(widget):
                                populate_ref_editor(widget, items, field.representation_field)
                                set_editor_value(widget, field, value, preserve_unavailable=True)
        finally:
            self._loading = False

    # ── Internals ──

    def _rebuild_table(self) -> None:
        col_names = list(self._tp.fields.keys())
        self._table.setRowCount(len(self._rows_data))
        for index, name in enumerate(col_names):
            self._table.setColumnHidden(index, not self._column_readable(name))
        ref_cache: dict[type, list[Any]] = {}
        if self._db is not None and self._rows_data:
            for field in self._tp.fields.values():
                if isinstance(field, RefField):
                    target_class = field.resolve_target()
                    if target_class not in ref_cache:
                        ref_cache[target_class] = self._db.list(target_class)
        for row_idx, row_data in enumerate(self._rows_data):
            for col_idx, name in enumerate(col_names):
                field = self._tp.fields[name]
                editor = create_editor(field)
                editor.setEnabled(editor.isEnabled() and self._column_editable(name))
                value = row_data.get(name)
                if value is None:
                    value = field.default() if callable(field.default) else field.default
                if isinstance(field, RefField) and self._db is not None and isinstance(editor, QComboBox):
                    target_class = field.resolve_target()
                    populate_ref_editor(editor, ref_cache.get(target_class, ()), field.representation_field)
                set_editor_value(editor, field, value, preserve_unavailable=True)

                if isinstance(field, BooleanField) and isinstance(editor, QCheckBox):
                    wrap = QWidget()
                    hl = QHBoxLayout(wrap)
                    hl.setContentsMargins(0, 0, 0, 0)
                    hl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    hl.addWidget(editor)
                    self._table.setCellWidget(row_idx, col_idx, wrap)
                else:
                    self._table.setCellWidget(row_idx, col_idx, editor)
            self._connect_row_signals(row_idx, col_names)
        self._table.resizeColumnsToContents()
        self._table.resizeRowsToContents()

    def _connect_row_signals(self, row_idx: int, col_names: list[str]) -> None:
        """Connect auto-fill and auto-calc signals for a table row."""
        editors: dict[str, QWidget] = {}
        for col_idx, name in enumerate(col_names):
            cw = self._table.cellWidget(row_idx, col_idx)
            if cw is not None:
                editors[name] = cw

        # Auto-fill target column from RefField (honoring field.auto_fill or convention price)
        for name, field in self._tp.fields.items():
            if not isinstance(field, RefField) or self._db is None:
                continue
            ref_editor = editors.get(name)
            if not isinstance(ref_editor, QComboBox):
                continue

            target_col = field.auto_fill or (self._CONVENTION_PRICE if self._CONVENTION_PRICE in editors else None)
            if not target_col or target_col not in editors:
                continue
            target_editor = editors[target_col]
            target_field = self._tp.fields[target_col]
            ref_editor.currentIndexChanged.connect(
                lambda idx, e=ref_editor, te=target_editor, tf=target_field, f=field, tn=target_col: (
                    self._on_ref_row_changed(e, te, tf, f, tn)
                )
            )

        # Auto-calc amount when quantity or price changes
        qty_editor = editors.get(self._CONVENTION_QTY)
        price_editor = editors.get(self._CONVENTION_PRICE)
        amt_editor = editors.get(self._CONVENTION_AMOUNT)
        if qty_editor and price_editor and amt_editor:
            for src in (qty_editor, price_editor):
                if hasattr(src, "valueChanged"):
                    src.valueChanged.connect(
                        lambda val, q=qty_editor, p=price_editor, a=amt_editor: (
                            self._auto_calc(q, p, a)
                        )
                    )

    def _on_ref_row_changed(
        self,
        combo: QWidget,
        target_editor: QWidget,
        target_field: Field,
        ref_field: RefField,
        target_col_name: str,
    ) -> None:
        if self._db is None or self._loading or not self._column_editable(target_col_name):
            return
        ref_id = get_editor_value(combo, ref_field)
        if ref_id is None:
            return
        target_class = ref_field.resolve_target()
        obj = self._db.get(target_class, ref_id)
        if obj is None:
            return
        attr_name = ref_field.auto_fill or self._CONVENTION_PRICE
        val = getattr(obj, attr_name, None)
        if val is not None:
            set_editor_value(target_editor, target_field, val)

    def _auto_calc(
        self,
        qty_editor: QWidget,
        price_editor: QWidget,
        amt_editor: QWidget,
    ) -> None:
        """quantity x price = amount."""
        if self._loading or not self._column_editable(self._CONVENTION_AMOUNT):
            return
        from decimal import Decimal as PyDec

        qty = get_editor_value(qty_editor, self._tp.fields[self._CONVENTION_QTY])
        price = get_editor_value(price_editor, self._tp.fields[self._CONVENTION_PRICE])
        if qty is not None and price is not None:
            amount = PyDec(str(qty)) * PyDec(str(price))
            set_editor_value(amt_editor, self._tp.fields[self._CONVENTION_AMOUNT], amount)

    def _read_table(self) -> None:
        col_names = list(self._tp.fields.keys())
        for row_idx in range(self._table.rowCount()):
            if row_idx >= len(self._rows_data):
                self._rows_data.append({})
            row_data = self._rows_data[row_idx]
            for col_idx, name in enumerate(col_names):
                field = self._tp.fields[name]
                widget = self._table.cellWidget(row_idx, col_idx)
                if widget is not None and self._column_editable(name):
                    row_data[name] = get_editor_value(widget, field, preserve_unavailable=True)

    def _paste_columns(self) -> list[tuple[str, Field]]:
        return [
            (name, field) for name, field in self._tp.fields.items()
            if not field.read_only and self._column_readable(name) and self._column_editable(name)
        ]

    def _paste_rows_dialog(self) -> None:
        columns = ", ".join(field.display_name for _, field in self._paste_columns())
        text, accepted = QInputDialog.getMultiLineText(
            self, "Вставить строки", f"Вставьте ячейки без заголовков. Порядок колонок: {columns}",
        )
        if accepted:
            try:
                self._paste_rows(text)
            except (ValueError, csv.Error) as exc:
                show_ui_error(self, "Вставка строк", exc, warning=True)

    def _paste_rows(self, text: str) -> None:
        if self._db is None or not self.isEnabled():
            return
        columns = self._paste_columns()
        if not columns:
            return
        pasted = parse_tabular_rows(text, columns, self._db)
        if not pasted:
            return
        additions = [self._new_row_defaults() | values for values in pasted]
        self._read_table()
        start = len(self._rows_data)
        self._rows_data.extend(additions)
        self._loading = True
        self._rebuild_table()
        self._loading = False
        names = list(self._tp.fields)
        calculated = (self._CONVENTION_QTY, self._CONVENTION_PRICE, self._CONVENTION_AMOUNT)
        if all(name in names for name in calculated):
            for index, values in enumerate(pasted, start=start):
                if self._CONVENTION_AMOUNT not in values:
                    qty, price, amount = (self._table.cellWidget(index, names.index(name)) for name in calculated)
                    if qty is not None and price is not None and amount is not None:
                        self._auto_calc(qty, price, amount)
        self.focus_cell(start, columns[0][0])
        self.rows_changed.emit()

    def _new_row_defaults(self) -> dict[str, Any]:
        return {
            name: field.default() if callable(field.default) else field.default
            for name, field in self._tp.fields.items() if field.default is not None
        }

    def _add_row(self) -> None:
        if not self._can_add_rows():
            return
        self._read_table()
        self._rows_data.append(self._new_row_defaults())
        self._loading = True
        self._rebuild_table()
        self._loading = False
        row_idx = len(self._rows_data) - 1
        target_col = next(
            (
                name for name, field in self._tp.fields.items()
                if not field.read_only and self._column_readable(name) and self._column_editable(name)
            ),
            next(iter(self._tp.fields), None),
        )
        if target_col is not None and row_idx >= 0:
            self.focus_cell(row_idx, target_col)
        self.rows_changed.emit()

    def _find_active_row(self) -> int:
        current = self._table.currentRow()
        if 0 <= current < len(self._rows_data):
            return current
        focus = QApplication.focusWidget()
        if focus is not None:
            for r in range(self._table.rowCount()):
                for c in range(self._table.columnCount()):
                    cw = self._table.cellWidget(r, c)
                    if cw is not None and (cw is focus or cw.isAncestorOf(focus)):
                        return r
        return -1

    def _delete_row(self) -> None:
        if not self._can_remove_rows():
            return
        current = self._find_active_row()
        if 0 <= current < len(self._rows_data):
            self._read_table()
            del self._rows_data[current]
            self._loading = True
            self._rebuild_table()
            self._loading = False
            self.rows_changed.emit()
