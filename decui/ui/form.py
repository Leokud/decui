from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
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

from decui.fields import DecimalField, Field, IntegerField, RefField

from .editors import (
    create_editor,
    get_editor_value,
    populate_ref_editor,
    set_editor_value,
)

if TYPE_CHECKING:
    from decimal import Decimal
    from decui.entity import Entity
    from decui.orm import Database


class AutoForm(QScrollArea):
    """Auto-generated form — adapts buttons to catalog vs document."""

    closed = Signal()

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

        self.setWidgetResizable(True)
        try:
            self._build_ui()
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Ошибка создания формы: {exc}")

    def _build_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel(f"<b>{self._entity_class._meta.verbose_name}</b>")
        title.setStyleSheet("font-size: 16px;")
        layout.addWidget(title)

        # Fields
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        for name, field in self._entity_class._meta.fields.items():
            if not field.visible:
                continue
            editor = create_editor(field)
            self._widgets[name] = editor
            label_text = field.display_name
            if field.required:
                label_text += " *"
            form.addRow(QLabel(label_text + ":"), editor)
        layout.addLayout(form)

        # Table parts
        for tp_name, tp in self._entity_class._meta.table_parts.items():
            tw = _TablePartWidget(tp, self._db)
            self._tab_widgets[tp_name] = tw
            layout.addWidget(tw)

        layout.addStretch()

        # Buttons
        cmd_layout = QHBoxLayout()
        cmd_layout.addStretch()

        # Command buttons (user-defined)
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

    def _build_doc_buttons(self, layout: QHBoxLayout) -> None:
        """Documents: only Провести / Провести и закрыть / Закрыть."""

        post_btn = QPushButton("Провести и закрыть")
        post_btn.clicked.connect(self._doc_post_and_close)
        layout.addWidget(post_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self._doc_close)
        layout.addWidget(close_btn)

    def _build_catalog_buttons(self, layout: QHBoxLayout) -> None:
        """Catalogs: Сохранить / Сохранить и закрыть / Закрыть."""
        save_btn = QPushButton("Сохранить")
        save_btn.clicked.connect(self._save_catalog)
        layout.addWidget(save_btn)

        save_close_btn = QPushButton("Сохранить и закрыть")
        save_close_btn.clicked.connect(self._catalog_save_and_close)
        layout.addWidget(save_close_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.closed.emit)
        layout.addWidget(close_btn)

    # ── Public API ──

    @property
    def entity(self) -> Entity | None:
        return self._entity

    def load_entity(self, entity: Entity) -> None:
        self._entity = entity
        self._editing = entity._id is not None
        self._populate_widgets()
        self._refresh_ref_editors()

    def load_new(self, defaults: dict[str, Any] | None = None) -> None:
        merged: dict[str, Any] = {}
        # Apply __defaults__ from entity metadata (compile-time defaults)
        for k, v in self._entity_class._meta.defaults.items():
            val: Any = v(self._db) if callable(v) else v
            if k in self._entity_class._meta.table_parts:
                # Table part data goes into load_new's kwargs as the table part name
                merged[k] = val
            elif k in self._entity_class._meta.fields:
                merged[k] = val
        # Override with explicit defaults passed to this method
        if defaults:
            merged.update(defaults)
        self._entity = self._entity_class(**merged)
        self._editing = False
        self._populate_widgets()
        self._refresh_ref_editors()

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
            set_editor_value(widget, self._entity_class._meta.fields[name], value)
        for tp_name, tw in self._tab_widgets.items():
            tw.load_rows(self._entity._table_data.get(tp_name, []))

    def _refresh_ref_editors(self) -> None:
        for name, widget in self._widgets.items():
            field = self._entity_class._meta.fields.get(name)
            if isinstance(field, RefField):
                target_class = field.resolve_target()
                items = self._db.list(target_class)
                rep = field.representation_field
                populate_ref_editor(widget, items, rep)
                # Re-apply value from entity data after populate
                if self._entity is not None:
                    val = self._entity._data.get(name)
                    if val is not None:
                        idx = widget.findData(val, Qt.ItemDataRole.UserRole)
                        if idx >= 0:
                            widget.setCurrentIndex(idx)
        self._connect_auto_fill()
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
            if fill_widget is None:
                continue
            fill_field = self._entity_class._meta.fields.get(fill_field_name)
            if fill_field is None:
                continue
            try:
                widget.currentIndexChanged.connect(
                    lambda idx, n=name, f=field, fw=fill_widget, ff=fill_field: (
                        self._on_ref_auto_fill(n, f, fw, ff)
                    )
                )
            except Exception:
                pass

    def _on_ref_auto_fill(
        self,
        ref_name: str,
        ref_field: RefField,
        fill_widget: QWidget,
        fill_field_def: Field,
    ) -> None:
        """Auto-fill a field from the referenced entity."""
        ref_widget = self._widgets.get(ref_name)
        if ref_widget is None:
            return
        ref_id = get_editor_value(ref_widget, ref_field)
        if ref_id is None:
            return
        target = ref_field.resolve_target()
        entity = self._db.get(target, ref_id)
        if entity is None:
            return
        val = entity._data.get(ref_field.auto_fill)
        if val is not None:
            set_editor_value(fill_widget, fill_field_def, val)

    def _widgets_to_entity(self) -> None:
        if self._entity is None:
            return
        for name, widget in self._widgets.items():
            field = self._entity_class._meta.fields[name]
            self._entity._data[name] = get_editor_value(widget, field)
        for tp_name, tw in self._tab_widgets.items():
            self._entity._table_data[tp_name] = tw.get_rows()

    # ── Save (shared) ──

    def _save(self) -> bool:
        if self._entity is None:
            return False
        self._widgets_to_entity()
        for name, field in self._entity_class._meta.fields.items():
            errs = field.validate(self._entity._data.get(name))
            if errs:
                QMessageBox.warning(self, "Ошибка заполнения", "\n".join(errs))
                return False
        try:
            self._db.save(self._entity)
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Ошибка сохранения: {exc}")
            return False

    def _save_entity_as_is(self) -> bool:
        """Save entity without re-reading widgets (preserves command changes)."""
        if self._entity is None:
            return False
        try:
            self._db.save(self._entity)
            return True
        except Exception:
            return False

    # ── Document button handlers (auto-save always) ──

    def _run_post(self) -> None:
        """Execute the 'Провести' command, save entity AS-IS."""
        if self._entity is None:
            return
        self._widgets_to_entity()
        found = False
        for _name, cmd in self._entity_class._meta.commands.items():
            if cmd.name in ("Провести",) or cmd.func.__name__ in ("post",):
                cmd.execute(self._entity)
                found = True
                break
        if not found:
            self._entity._data["posted"] = True
        self._save_entity_as_is()
        if hasattr(self._entity, "get_movements") and self._entity.get_movements():
            self._entity.clear_movements(self._db)
            self._entity.write_movements(self._db)

    def _doc_post_and_close(self) -> None:
        self._run_post()
        self.closed.emit()

    def _doc_close(self) -> None:
        """Read widgets, save, then close."""
        if self._entity is not None:
            self._widgets_to_entity()
            self._save_entity_as_is()
        self.closed.emit()

    # ── Catalog button handlers ──

    def _save_catalog(self) -> None:
        self._save()

    def _catalog_save_and_close(self) -> None:
        if self._save():
            self.closed.emit()

    # ── Command handler (for user-defined @command buttons) ──

    def _execute_command(self, cmd_name: str) -> None:
        if self._entity is None:
            return
        self._widgets_to_entity()
        cmd = self._entity_class._meta.commands.get(cmd_name)
        if cmd is None:
            return
        try:
            cmd.execute(self._entity)
            self._db.save(self._entity)
            # Write register movements after save (entity has _id)
            if hasattr(self._entity, "get_movements") and self._entity.get_movements():
                self._entity.clear_movements(self._db)
                self._entity.write_movements(self._db)
            self._populate_widgets()
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"{cmd.name}: {exc}")


# ═══════════════════════════════════════════════════════════════════════
# Table part editor — with auto-fill price + auto-calc
# ═══════════════════════════════════════════════════════════════════════

class _TablePartWidget(QGroupBox):
    """Editable sub-table with auto-calculation."""

    _CONVENTION_PRICE = "price"
    _CONVENTION_QTY = "quantity"
    _CONVENTION_AMOUNT = "amount"

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
        self._loading = False  # guards against auto-signal during load
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
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)

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
        self._rows_data = list(rows)
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
                    if isinstance(widget, QWidget):
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
                set_editor_value(editor, field, value)
                if isinstance(field, RefField) and self._db is not None:
                    target_class = field.resolve_target()
                    items = self._db.list(target_class)
                    populate_ref_editor(editor, items, field.representation_field)
                    if value is not None:
                        idx = editor.findData(value, Qt.ItemDataRole.UserRole)
                        if idx >= 0:
                            editor.setCurrentIndex(idx)
                self._table.setCellWidget(row_idx, col_idx, editor)
            self._connect_row_signals(row_idx, col_names)

    def _connect_row_signals(self, row_idx: int, col_names: list[str]) -> None:
        """Connect auto-fill and auto-calc signals for a table row."""
        editors: dict[str, QWidget] = {}
        for col_idx, name in enumerate(col_names):
            editors[name] = self._table.cellWidget(row_idx, col_idx)

        # Auto-fill price from catalog when product RefField changes
        for name, field in self._tp.fields.items():
            if isinstance(field, RefField) and self._db is not None:
                price_editor = editors.get(self._CONVENTION_PRICE)
                if price_editor is None:
                    continue
                ref_editor = editors[name]
                if isinstance(ref_editor, QWidget):
                    try:
                        ref_editor.currentIndexChanged.connect(
                            lambda idx, e=ref_editor, pe=price_editor, f=field: (
                                self._on_product_changed(e, pe, f)
                            )
                        )
                    except Exception:
                        pass

        # Auto-calc amount when quantity or price changes
        qty_editor = editors.get(self._CONVENTION_QTY)
        price_editor = editors.get(self._CONVENTION_PRICE)
        amt_editor = editors.get(self._CONVENTION_AMOUNT)
        if qty_editor and price_editor and amt_editor:
            for src in (qty_editor, price_editor):
                try:
                    if hasattr(src, "valueChanged"):
                        src.valueChanged.connect(
                            lambda val, q=qty_editor, p=price_editor, a=amt_editor: (
                                self._auto_calc(q, p, a)
                            )
                        )
                except Exception:
                    pass

    def _on_product_changed(
        self,
        combo: QWidget,
        price_editor: QWidget,
        ref_field: RefField,
    ) -> None:
        """Auto-fill price from the selected product."""
        if self._db is None or self._loading:
            return
        from decimal import Decimal as PyDec
        prod_id = get_editor_value(combo, ref_field)
        if prod_id is None:
            return
        target_class = ref_field.resolve_target()
        product = self._db.get(target_class, prod_id)
        if product is None:
            return
        product_price = getattr(product, self._CONVENTION_PRICE, None)
        if product_price is not None:
            set_editor_value(price_editor, self._tp.fields[self._CONVENTION_PRICE], product_price)

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

    def _delete_row(self) -> None:
        current = self._table.currentRow()
        if 0 <= current < len(self._rows_data):
            self._read_table()
            del self._rows_data[current]
            self._loading = True
            self._rebuild_table()
            self._loading = False
