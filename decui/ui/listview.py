from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from decui.fields import (
    BooleanField,
    ChoiceField,
    DateField,
    DateTimeField,
    DecimalField,
    Field,
    IntegerField,
    RefField,
    TimeField,
)

if TYPE_CHECKING:
    from decui.entity import Entity
    from decui.orm import Database


_ACTION_LABELS: dict[str, str] = {
    "New": "Новый",
    "Edit": "Изменить",
    "Copy": "Копировать",
    "Delete": "Удалить",
    "Refresh": "Обновить",
}
DEFAULT_ACTIONS = ("New", "Edit", "Copy", "Delete", "Refresh")


class _ListSortProxy(QSortFilterProxyModel):
    """Proxy model providing safe multi-type sorting on UserRole and full-row text filtering."""

    def lessThan(self, source_left: QModelIndex, source_right: QModelIndex) -> bool:
        l_val = self.sourceModel().data(source_left, Qt.ItemDataRole.UserRole)
        r_val = self.sourceModel().data(source_right, Qt.ItemDataRole.UserRole)
        if l_val is None and r_val is None:
            return False
        if l_val is None:
            return True
        if r_val is None:
            return False
        try:
            return bool(l_val < r_val)
        except TypeError:
            return str(l_val).lower() < str(r_val).lower()


class ListView(QWidget):
    """Auto-generated list / table view with live search, sorting, Ref labels, and CRUD actions."""

    entity_selected = Signal(object)
    create_requested = Signal(type)
    edit_requested = Signal(object)
    copy_requested = Signal(object)
    data_changed = Signal()

    def __init__(
        self,
        entity_class: type,
        database: Database,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._entity_class = entity_class
        self._db = database
        self._items: list[Any] = []
        self._action_buttons: dict[str, QPushButton] = {}
        self._proxy = _ListSortProxy(self)
        self._proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)

        self._build_ui()
        try:
            self.refresh()
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", str(exc))

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # -- Toolbar --
        toolbar = QHBoxLayout()
        for action in DEFAULT_ACTIONS:
            label = _ACTION_LABELS.get(action, action)
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked=False, a=action: self._on_action(a))
            self._action_buttons[action] = btn
            toolbar.addWidget(btn)

        toolbar.addStretch()

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("🔍 Поиск (Ctrl+F)...")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMaximumWidth(260)
        self._search_edit.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self._search_edit)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet("padding: 0 8px; opacity: 0.75;")
        toolbar.addWidget(self._count_label)

        layout.addLayout(toolbar)

        # -- Table --
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self._table)

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+F"), self, self._search_edit.setFocus)
        QShortcut(QKeySequence("F5"), self, self.refresh)
        QShortcut(QKeySequence("Delete"), self._table, lambda: self._on_action("Delete"))
        QShortcut(QKeySequence("Return"), self._table, self._on_double_click)

        self._update_button_states()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload data from the database and rebuild the table."""
        try:
            self._items = self._db.list(self._entity_class)
            self._rebuild_model()
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", str(exc))

    def _build_ref_lookup(self, visible_fields: list[tuple[str, Field]]) -> dict[str, dict[int, str]]:
        """Batch-fetch FK labels so list cells display human-readable names cleanly."""
        cache: dict[str, dict[int, str]] = {}
        for name, field in visible_fields:
            if not isinstance(field, RefField):
                continue
            try:
                target_cls = field.resolve_target()
                items = self._db.list(target_cls)
                rep = field.representation_field
                mapping: dict[int, str] = {}
                for target_obj in items:
                    if target_obj._id is None:
                        continue
                    if rep and hasattr(target_obj, rep):
                        raw_rep = getattr(target_obj, rep)
                        lbl = str(raw_rep) if raw_rep is not None else str(target_obj)
                    else:
                        lbl = str(target_obj)
                    mapping[target_obj._id] = lbl
                cache[name] = mapping
            except Exception:
                cache[name] = {}
        return cache

    @staticmethod
    def _format_cell(
        val: Any,
        name: str,
        field: Field,
        is_doc: bool,
        ref_lookup: dict[str, dict[int, str]],
    ) -> tuple[str, Any]:
        """Return (display_text, sort_key) for a cell."""
        if val is None:
            return "", None

        if isinstance(field, BooleanField):
            bval = bool(val)
            if name == "posted" and is_doc:
                text = "Проведён" if bval else "Не проведён"
            else:
                text = "Да" if bval else "Нет"
            return text, int(bval)

        if isinstance(field, RefField):
            label = ref_lookup.get(name, {}).get(val) if isinstance(val, int) else None
            disp = label if label is not None else str(val)
            return disp, disp.lower()

        if isinstance(field, ChoiceField):
            disp = field.format_display(str(val))
            return disp, disp.lower()

        if isinstance(field, DateTimeField):
            if isinstance(val, datetime.datetime):
                return val.strftime("%d.%m.%Y %H:%M"), val.timestamp()
            return str(val), str(val)

        if isinstance(field, DateField):
            if isinstance(val, datetime.datetime):
                return val.strftime("%d.%m.%Y"), val.date().toordinal()
            if isinstance(val, datetime.date):
                return val.strftime("%d.%m.%Y"), val.toordinal()
            return str(val), str(val)

        if isinstance(field, TimeField):
            if isinstance(val, datetime.time):
                return val.strftime("%H:%M"), val.hour * 60 + val.minute
            return str(val), str(val)

        if isinstance(field, DecimalField):
            try:
                dec = val if isinstance(val, Decimal) else Decimal(str(val))
                text = f"{dec:,.{field.scale}f}"
                if field.currency:
                    text = f"{text} {field.currency}"
                return text, float(dec)
            except Exception:
                return str(val), str(val)

        if isinstance(field, IntegerField):
            try:
                return str(val), int(val)
            except Exception:
                return str(val), str(val)

        s = str(val)
        return s, s.lower()

    def _rebuild_model(self) -> None:
        visible_fields: list[tuple[str, Field]] = [
            (n, f) for n, f in self._entity_class._meta.fields.items() if f.visible
        ]
        headers = [f.display_name for _, f in visible_fields]
        is_doc = self._entity_class._meta.is_document
        ref_lookup = self._build_ref_lookup(visible_fields)

        rows: list[list[str]] = []
        sort_rows: list[list[Any]] = []
        for item in self._items:
            row: list[str] = []
            srow: list[Any] = []
            for name, field in visible_fields:
                val = item._data.get(name)
                disp, skey = self._format_cell(val, name, field, is_doc, ref_lookup)
                row.append(disp)
                srow.append(skey)
            rows.append(row)
            sort_rows.append(srow)

        source_model = _TableModel(headers, rows, self._items, sort_rows=sort_rows)
        self._proxy.setSourceModel(source_model)
        self._table.setModel(self._proxy)
        self._table.resizeColumnsToContents()

        sel_model = self._table.selectionModel()
        if sel_model is not None:
            sel_model.currentChanged.connect(lambda *_: self._update_button_states())

        self._update_count_label()
        self._update_button_states()

    def _on_search_changed(self, text: str) -> None:
        self._proxy.setFilterFixedString(text)
        self._update_count_label()
        self._update_button_states()

    def _update_count_label(self) -> None:
        total = len(self._items)
        shown = self._proxy.rowCount()
        if shown == total:
            self._count_label.setText(f"Записей: {total}")
        else:
            self._count_label.setText(f"Показано: {shown} из {total}")

    def _update_button_states(self) -> None:
        has_sel = self._selected_entity() is not None
        for act in ("Edit", "Copy", "Delete"):
            btn = self._action_buttons.get(act)
            if btn is not None:
                btn.setEnabled(has_sel)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_action(self, action: str) -> None:
        if action == "New":
            self.create_requested.emit(self._entity_class)

        elif action == "Edit":
            entity = self._selected_entity()
            if entity is not None:
                self.edit_requested.emit(entity)

        elif action == "Copy":
            entity = self._selected_entity()
            if entity is not None:
                self.copy_requested.emit(entity)

        elif action == "Delete":
            entity = self._selected_entity()
            if entity is None:
                return
            ans = QMessageBox.question(
                self,
                "Подтверждение удаления",
                f"Удалить запись «{entity}»?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            try:
                if hasattr(entity, "clear_movements"):
                    entity.clear_movements(self._db)
                self._db.delete(entity)
                self.refresh()
                self.data_changed.emit()
            except Exception as exc:
                msg = str(exc)
                if "FOREIGN KEY constraint" in msg:
                    msg = "Нельзя удалить запись, так как она используется в других документах или справочниках."
                QMessageBox.warning(self, "Не удалось удалить", msg)

        elif action == "Refresh":
            self.refresh()

    def _on_double_click(self) -> None:
        entity = self._selected_entity()
        if entity is not None:
            self.edit_requested.emit(entity)

    def _selected_entity(self) -> Entity | None:
        idx = self._table.currentIndex()
        if not idx.isValid():
            return None
        model = self._table.model()
        if isinstance(model, QSortFilterProxyModel):
            src_idx = model.mapToSource(idx)
            row = src_idx.row()
        else:
            row = idx.row()
        if 0 <= row < len(self._items):
            return self._items[row]
        return None


# -- Table model --------------------------------------------------------------

class _TableModel(QAbstractTableModel):
    def __init__(
        self,
        headers: list[str],
        rows: list[list[str]],
        items: list[Any] | None = None,
        sort_rows: list[list[Any]] | None = None,
    ) -> None:
        super().__init__()
        self._headers = headers
        self._rows = rows
        self._items = items or []
        self._sort_rows = sort_rows

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row_idx = index.row()
        col_idx = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._rows[row_idx][col_idx]

        if role == Qt.ItemDataRole.UserRole:
            if self._sort_rows and row_idx < len(self._sort_rows):
                return self._sort_rows[row_idx][col_idx]
            return self._rows[row_idx][col_idx]

        if role == Qt.ItemDataRole.FontRole:
            if 0 <= row_idx < len(self._items):
                item = self._items[row_idx]
                if self._is_item_done(item):
                    f = QFont()
                    f.setStrikeOut(True)
                    return f

        if role == Qt.ItemDataRole.ForegroundRole:
            if 0 <= row_idx < len(self._items):
                item = self._items[row_idx]
                if self._is_item_done(item):
                    return QColor(128, 128, 128)
                if self._is_item_overdue(item):
                    return QColor("#dc2626")

        return None

    @staticmethod
    def _is_item_done(item: Any) -> bool:
        data = getattr(item, "_data", {})
        for k in ("is_completed", "completed", "done", "is_done"):
            if data.get(k) is True:
                return True
        status_val = str(data.get("status", "")).lower()
        if status_val in ("завершена", "done", "completed", "выполнено"):
            return True
        return False

    @staticmethod
    def _is_item_overdue(item: Any) -> bool:
        data = getattr(item, "_data", {})
        now = datetime.datetime.now()
        for k in ("deadline", "due_date", "target_date"):
            dt = data.get(k)
            if isinstance(dt, datetime.datetime):
                naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
                if naive < now:
                    return True
            elif isinstance(dt, datetime.date):
                if dt < now.date():
                    return True
        return False

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return None
