from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from decui.fields import BooleanField
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from decui.entity import Entity
    from decui.orm import Database


_ACTION_LABELS: dict[str, str] = {
    "New": "Новый",
    "Edit": "Изменить",
    "Delete": "Удалить",
    "Refresh": "Обновить",
}
DEFAULT_ACTIONS = ("New", "Edit", "Delete", "Refresh")


class ListView(QWidget):
    """Auto-generated list / table view for an entity type."""

    entity_selected = Signal(object)
    create_requested = Signal(type)
    edit_requested = Signal(object)

    def __init__(
        self,
        entity_class: type,
        database: Database,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._entity_class = entity_class
        self._db = database
        self._items: list = []

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
            toolbar.addWidget(btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # -- Table --
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self._table)

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)

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

    def _rebuild_model(self) -> None:
        field_names = [
            n for n, f in self._entity_class._meta.fields.items() if f.visible
        ]
        headers = [
            self._entity_class._meta.fields[n].display_name for n in field_names
        ]

        rows: list[list[str]] = []
        for item in self._items:
            row: list[str] = []
            for name in field_names:
                val = item._data.get(name, "")
                field = self._entity_class._meta.fields.get(name)
                if isinstance(field, BooleanField):
                    row.append("Проведён" if val else "Не проведён")
                else:
                    row.append(str(val) if val is not None else "")
            rows.append(row)

        model = _TableModel(headers, rows)
        self._table.setModel(model)

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

        elif action == "Delete":
            entity = self._selected_entity()
            if entity is not None:
                self._db.delete(entity)
                self.refresh()

        elif action == "Refresh":
            self.refresh()

    def _on_double_click(self) -> None:
        entity = self._selected_entity()
        if entity is not None:
            self.edit_requested.emit(entity)

    def _selected_entity(self):
        idx = self._table.currentIndex()
        if idx.isValid() and 0 <= idx.row() < len(self._items):
            return self._items[idx.row()]
        return None


# -- Simple table model (no Qt model/view framework dependency beyond basics) --

from PySide6.QtCore import QAbstractTableModel, QModelIndex  # noqa: E402


class _TableModel(QAbstractTableModel):
    def __init__(self, headers: list[str], rows: list[list[str]]) -> None:
        super().__init__()
        self._headers = headers
        self._rows = rows

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and index.isValid():
            return self._rows[index.row()][index.column()]
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return None
