from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from decui.fields import BooleanField, Field, RefField
from decui.registry import get_all_entities
from decui.sequence import SequenceViolationError

from .listview import ListView
from .editors import (
    create_editor,
    get_editor_value,
    populate_ref_editor,
    set_editor_value,
)

if TYPE_CHECKING:
    from decui.entity import Entity
    from decui.orm import Database


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

        self.setWidgetResizable(True)
        try:
            self._build_ui()
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Ошибка создания формы: {exc}")

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
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setVerticalSpacing(10)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for name, field in self._entity_class._meta.fields.items():
            if not field.visible:
                continue
            editor = create_editor(field)
            self._widgets[name] = editor
            label_text = field.display_name
            if field.required:
                label_text += " *"

            label = QLabel(label_text + ":")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setBuddy(editor)

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
                row_hl.addWidget(add_ref_btn)
                form.addRow(label, row_wrap)
            else:
                form.addRow(label, editor)
        layout.addLayout(form)

        # Table parts
        for tp_name, tp in self._entity_class._meta.table_parts.items():
            tw = _TablePartWidget(tp, self._db)
            self._tab_widgets[tp_name] = tw
            layout.addWidget(tw)

        self._related_layout = QVBoxLayout()
        layout.addLayout(self._related_layout)
        layout.addStretch()

        # Buttons
        cmd_layout = QHBoxLayout()
        cmd_layout.addStretch()

        for cmd_name, cmd in self._entity_class._meta.commands.items():
            btn = QPushButton(cmd.name)
            btn.clicked.connect(
                lambda checked=False, n=cmd_name: self._execute_command(n)
            )
            cmd_layout.addWidget(btn)

        if self._is_doc:
            self._build_doc_buttons(cmd_layout)
        else:
            self._build_catalog_buttons(cmd_layout)

        layout.addLayout(cmd_layout)

        # Connect auto-fill signals once
        self._connect_auto_fill()

        # Shortcuts
        QShortcut(
            QKeySequence("Ctrl+S"),
            self,
            self._save_draft if self._is_doc else self._save_catalog,
        )

    def _build_doc_buttons(self, layout: QHBoxLayout) -> None:
        draft_btn = QPushButton("Сохранить")
        draft_btn.clicked.connect(self._save_draft)
        layout.addWidget(draft_btn)

        post_btn = QPushButton("Провести и закрыть")
        post_btn.setProperty("primary", True)
        post_btn.clicked.connect(self._doc_post_and_close)
        layout.addWidget(post_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self._request_close)
        layout.addWidget(close_btn)

    def _build_catalog_buttons(self, layout: QHBoxLayout) -> None:
        save_btn = QPushButton("Сохранить")
        save_btn.clicked.connect(self._save_catalog)
        layout.addWidget(save_btn)

        save_close_btn = QPushButton("Сохранить и закрыть")
        save_close_btn.setProperty("primary", True)
        save_close_btn.clicked.connect(self._catalog_save_and_close)
        layout.addWidget(save_close_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self._request_close)
        layout.addWidget(close_btn)

    # ── Quick create Ref item in modal dialog ──

    def _quick_create_ref(self, field_name: str, field: RefField) -> None:
        target_cls = field.resolve_target()
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Новый: {target_cls._meta.verbose_name}")
        dlg.resize(520, 420)
        v = QVBoxLayout(dlg)
        sub_form = AutoForm(target_cls, self._db, dlg)
        sub_form.load_new()
        sub_form.closed.connect(dlg.accept)
        v.addWidget(sub_form)
        dlg.exec()
        created_id = sub_form.entity._id if sub_form.entity is not None else None
        self._refresh_ref_editors()
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
        for name, widget in self._widgets.items():
            field = self._entity_class._meta.fields[name]
            try:
                snap[name] = get_editor_value(widget, field)
            except Exception:
                snap[name] = None
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
            widget.blockSignals(True)
            set_editor_value(widget, self._entity_class._meta.fields[name], value)
            widget.blockSignals(False)
        for tp_name, tw in self._tab_widgets.items():
            tw.load_rows(self._entity._table_data.get(tp_name, []))
        self._populate_related_lists()

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
            for name, widget in self._widgets.items():
                field = self._entity_class._meta.fields[name]
                current = get_editor_value(widget, field)
                if field.read_only or current == (self._initial_snapshot or {}).get(name):
                    set_editor_value(widget, field, updated._data.get(name))
                    if self._initial_snapshot is not None:
                        self._initial_snapshot[name] = get_editor_value(widget, field)
            self._entity = updated

    def _open_related_form(
        self, cls: type, item: Entity | None = None, *, defaults: dict[str, Any] | None = None, copy: bool = False
    ) -> None:
        try:
            dialog = QDialog(self)
            dialog.setWindowTitle(cls._meta.verbose_name)
            dialog.resize(620, 450)
            layout = QVBoxLayout(dialog)
            form = AutoForm(cls, self._db, dialog)
            if item is None:
                form.load_new(defaults)
            else:
                full = self._db.get(cls, item._id) if item._id is not None else None
                if full is None:
                    raise ValueError("Запись была удалена.")
                form.load_entity(full.copy_for_edit(self._db) if copy else full)
            form.closed.connect(dialog.accept)
            layout.addWidget(form)
            dialog.exec()
            dialog.deleteLater()
            self.refresh_related_data()
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось открыть форму", str(exc))

    def _refresh_ref_editors(self) -> None:
        for name, widget in self._widgets.items():
            field = self._entity_class._meta.fields.get(name)
            if isinstance(field, RefField) and isinstance(widget, QComboBox):
                target_class = field.resolve_target()
                items = self._db.list(target_class)
                populate_ref_editor(widget, items, field.representation_field)
                if self._entity is not None:
                    val = self._entity._data.get(name)
                    if val is not None:
                        idx = widget.findData(val, Qt.ItemDataRole.UserRole)
                        if idx >= 0:
                            widget.setCurrentIndex(idx)
        for tw in self._tab_widgets.values():
            tw.refresh_refs(self._db)

    def _connect_auto_fill(self) -> None:
        """Connect RefField editors to auto-fill target fields."""
        for name, widget in self._widgets.items():
            field = self._entity_class._meta.fields.get(name)
            if not isinstance(field, RefField) or not field.auto_fill:
                continue
            fill_field_name = field.auto_fill
            fill_widget = self._widgets.get(fill_field_name)
            fill_field = self._entity_class._meta.fields.get(fill_field_name)
            if fill_widget is None or fill_field is None:
                continue
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(
                    lambda idx, n=name, f=field, fw=fill_widget, ff=fill_field: (
                        self._on_ref_auto_fill(n, f, fw, ff)
                    )
                )

    def _on_ref_auto_fill(
        self,
        ref_name: str,
        ref_field: RefField,
        fill_widget: QWidget,
        fill_field_def: Field,
    ) -> None:
        """Auto-fill or clear target field when referenced entity changes."""
        ref_widget = self._widgets.get(ref_name)
        if ref_widget is None:
            return
        ref_id = get_editor_value(ref_widget, ref_field)
        if ref_id is None:
            set_editor_value(fill_widget, fill_field_def, None)
            return
        target = ref_field.resolve_target()
        entity = self._db.get(target, ref_id)
        if entity is None:
            return
        if ref_field.auto_fill:
            val = entity._data.get(ref_field.auto_fill)
            if val is not None:
                set_editor_value(fill_widget, fill_field_def, val)

    def _widgets_to_entity(self) -> None:
        if self._entity is None:
            return
        for name, widget in self._widgets.items():
            field = self._entity_class._meta.fields[name]
            if not field.read_only:
                self._entity._data[name] = get_editor_value(widget, field)
        for tp_name, tw in self._tab_widgets.items():
            rows = tw.get_rows()
            self._entity._table_data[tp_name] = rows

    # ── Validation & Save ──

    def _validate_all(self) -> bool:
        """Validate header fields, table part rows, and entity custom rules."""
        if self._entity is None:
            return False
        first_bad_widget: QWidget | None = None
        for name, field in self._entity_class._meta.fields.items():
            w = self._widgets.get(name)
            if w is not None:
                w.setStyleSheet("")
            errs = field.validate(self._entity._data.get(name))
            if errs:
                if w is not None and first_bad_widget is None:
                    first_bad_widget = w
                    w.setStyleSheet("border: 1px solid #dc2626;")

        all_errs = self._entity.validate()
        if all_errs:
            if first_bad_widget is not None:
                first_bad_widget.setFocus()
            QMessageBox.warning(self, "Ошибка заполнения", "\n".join(all_errs))
            return False
        return True

    def _save(self) -> bool:
        if self._entity is None:
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
            QMessageBox.critical(self, "Ошибка", f"Ошибка сохранения: {exc}")
            return False

    def _save_entity_as_is(self) -> bool:
        """Save entity without re-reading widgets, validating and surfacing errors cleanly."""
        if self._entity is None:
            return False
        if not self._validate_all():
            return False
        try:
            self._db.save(self._entity)
            self._show_save_message()
            self._populate_widgets()
            self._mark_clean()
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Ошибка сохранения: {exc}")
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
        if self._entity is None:
            return False
        self._widgets_to_entity()
        if not self._validate_all():
            return False

        try:
            if hasattr(self._entity, "check_sequence"):
                self._entity.check_sequence(self._db)

            found = False
            for _name, cmd in self._entity_class._meta.commands.items():
                if cmd.name in ("Провести", "Submit", "Post") or cmd.func.__name__ in ("post", "submit"):
                    cmd.execute(self._entity)
                    found = True
                    break
            if not found:
                self._entity._data["posted"] = True

            if not self._save_entity_as_is():
                return False

            if self._entity._id is not None and hasattr(self._entity, "clear_movements"):
                self._entity.clear_movements(self._db)
                if getattr(self._entity, "is_posted", False):
                    self._entity.write_movements(self._db)

            self.data_changed.emit(f"Проведён: {self._entity}")
            return True
        except SequenceViolationError as exc:
            QMessageBox.warning(self, "Нарушение последовательности", str(exc))
            return False
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка проведения", str(exc))
            return False

    def _doc_post_and_close(self) -> None:
        if self._run_post():
            self.closed.emit()

    def _request_close(self) -> None:
        """Prompt user if form has unsaved edits before closing."""
        if self.has_unsaved_changes():
            ans = QMessageBox.question(
                self,
                "Несохранённые изменения",
                "Сохранить изменения перед закрытием?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.Yes:
                if not self._save():
                    return
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
        if self._entity is None:
            return
        self._widgets_to_entity()
        cmd = self._entity_class._meta.commands.get(cmd_name)
        if cmd is None:
            return
        try:
            cmd.execute(self._entity)
            if not self._validate_all():
                return
            self._db.save(self._entity)

            if self._entity._id is not None and hasattr(self._entity, "clear_movements"):
                self._entity.clear_movements(self._db)
                if getattr(self._entity, "is_posted", False):
                    self._entity.write_movements(self._db)

            self._populate_widgets()
            self._mark_clean()
            self.data_changed.emit(f"Выполнено «{cmd.name}»")
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"{cmd.name}: {exc}")


# ═══════════════════════════════════════════════════════════════════════
# Table part editor — with auto-fill + auto-calc + centered booleans
# ═══════════════════════════════════════════════════════════════════════

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
    ) -> None:
        super().__init__(table_part.verbose_name, parent)
        self._tp = table_part
        self._db = database
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
        add_btn = QPushButton("+ Добавить строку")
        add_btn.clicked.connect(self._add_row)
        btn_layout.addWidget(add_btn)
        del_btn = QPushButton("- Удалить")
        del_btn.clicked.connect(self._delete_row)
        btn_layout.addWidget(del_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    # ── Public ──

    def load_rows(self, rows: list[dict[str, Any]]) -> None:
        self._loading = True
        self._rows_data = [dict(r) for r in rows]
        self._rebuild_table()
        self._loading = False

    def get_rows(self) -> list[dict[str, Any]]:
        self._read_table()
        return self._rows_data

    def refresh_refs(self, db: Database) -> None:
        self._loading = True
        col_names = list(self._tp.fields.keys())
        for row in range(self._table.rowCount()):
            for col, name in enumerate(col_names):
                field = self._tp.fields[name]
                if isinstance(field, RefField):
                    widget = self._table.cellWidget(row, col)
                    if isinstance(widget, QComboBox):
                        target_class = field.resolve_target()
                        items = db.list(target_class)
                        populate_ref_editor(widget, items, field.representation_field)
                        val = self._rows_data[row].get(name) if row < len(self._rows_data) else None
                        if val is not None:
                            idx = widget.findData(val, Qt.ItemDataRole.UserRole)
                            if idx >= 0:
                                widget.setCurrentIndex(idx)
        self._loading = False

    # ── Internals ──

    def _rebuild_table(self) -> None:
        col_names = list(self._tp.fields.keys())
        self._table.setRowCount(len(self._rows_data))
        for row_idx, row_data in enumerate(self._rows_data):
            for col_idx, name in enumerate(col_names):
                field = self._tp.fields[name]
                editor = create_editor(field)
                value = row_data.get(name)
                if value is None:
                    value = field.default() if callable(field.default) else field.default
                if isinstance(field, RefField) and self._db is not None and isinstance(editor, QComboBox):
                    target_class = field.resolve_target()
                    items = self._db.list(target_class)
                    populate_ref_editor(editor, items, field.representation_field)
                set_editor_value(editor, field, value)

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
        if self._db is None or self._loading:
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
        if self._loading:
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
                if widget is not None:
                    row_data[name] = get_editor_value(widget, field)

    def _add_row(self) -> None:
        self._read_table()
        new_row: dict[str, Any] = {}
        for name, field in self._tp.fields.items():
            if field.default is not None:
                new_row[name] = field.default() if callable(field.default) else field.default
        self._rows_data.append(new_row)
        self._loading = True
        self._rebuild_table()
        self._loading = False
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
        current = self._find_active_row()
        if 0 <= current < len(self._rows_data):
            self._read_table()
            del self._rows_data[current]
            self._loading = True
            self._rebuild_table()
            self._loading = False
            self.rows_changed.emit()
