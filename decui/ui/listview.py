from __future__ import annotations

import csv
import datetime
import io
import logging
from copy import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QIODevice,
    QModelIndex,
    QSaveFile,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from decui.csv_import import (
    catalog_csv_keys,
    catalog_csv_template,
    read_catalog_csv,
    save_catalog_csv,
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
    StringField,
    TimeField,
)
from decui.reports import sanitize_csv_cell
from decui.ui.editors import (
    create_editor,
    get_editor_value,
    populate_ref_editor,
    set_editor_value,
    show_ui_error,
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


@dataclass
class ListViewState:
    search: str
    filters: dict[str, tuple[str, Any]]
    sort_field: str | None
    sort_order: Qt.SortOrder
    selected_id: int | None

    def to_settings(self) -> dict[str, Any]:
        def encode(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, (datetime.date, datetime.time)):
                return value.isoformat()
            if isinstance(value, tuple):
                return [encode(part) for part in value]
            return value

        return {
            "search": self.search,
            "filters": {name: [operator, encode(value)] for name, (operator, value) in self.filters.items()},
            "sort_field": self.sort_field,
            "descending": self.sort_order == Qt.SortOrder.DescendingOrder,
        }


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
        *,
        filters: dict[str, Any] | None = None,
        state: ListViewState | None = None,
    ) -> None:
        super().__init__(parent)
        self._entity_class = entity_class
        self._db = database
        self._filters = filters
        self._field_filters: dict[str, tuple[str, Any]] = state.filters.copy() if state is not None else {}
        self._list_field_names: list[str] = []
        self._items: list[Any] = []
        self._action_buttons: dict[str, QPushButton] = {}
        self._proxy = _ListSortProxy(self)
        self._proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(-1)

        self._build_ui()
        try:
            self.refresh()
            if state is not None:
                self._restore_list_state(state)
        except Exception as exc:
            show_ui_error(self, "Ошибка", exc)

    @classmethod
    def state_from_settings(
        cls, entity: type, db: Database, settings: dict[str, Any],
    ) -> ListViewState:
        filters: dict[str, tuple[str, Any]] = {}
        fields = entity._meta.fields
        columns = set(entity.__list_columns__ or fields) - set(getattr(entity, "__list_exclude__", ()))
        serialized = settings.get("filters")
        if isinstance(serialized, dict):
            for name, condition in serialized.items():
                field = fields.get(name)
                if field is None or name not in columns or not field.visible or not db.can_read_field(entity, name):
                    continue
                if not isinstance(condition, list) or len(condition) != 2:
                    continue
                operator, raw = condition
                if operator not in [code for _, code in cls._filter_operators(field)]:
                    continue
                try:
                    if operator == "isnull" and isinstance(raw, bool):
                        value = raw
                    elif operator == "between" and isinstance(raw, list) and len(raw) == 2:
                        lower, upper = (field.to_python(part) for part in raw)
                        if lower is None or upper is None or lower > upper:
                            continue
                        value = (lower, upper)
                    elif operator not in {"isnull", "notnull", "between"}:
                        value = field.to_python(raw)
                        if value is None:
                            continue
                    else:
                        continue
                    filters[name] = (operator, value)
                except (ValueError, TypeError, InvalidOperation, IndexError):
                    continue
        sort = settings.get("sort_field")
        search = settings.get("search")
        return ListViewState(
            search if isinstance(search, str) else "", filters,
            sort if isinstance(sort, str) else None,
            Qt.SortOrder.DescendingOrder if settings.get("descending") is True else Qt.SortOrder.AscendingOrder,
            None,
        )

    def list_state(self) -> ListViewState:
        header = self._table.horizontalHeader()
        column = header.sortIndicatorSection()
        selected = self._selected_entity()
        return ListViewState(
            self._search_edit.text(),
            self._field_filters.copy(),
            self._list_field_names[column] if 0 <= column < len(self._list_field_names) else None,
            header.sortIndicatorOrder(),
            selected.id if selected is not None else None,
        )

    def _restore_list_state(self, state: ListViewState) -> None:
        self._search_edit.setText(state.search)
        if state.sort_field in self._list_field_names:
            self._table.sortByColumn(self._list_field_names.index(state.sort_field), state.sort_order)
        if state.selected_id is not None:
            self._select_entity_by_id(state.selected_id)

    def _select_entity_by_id(self, entity_id: int) -> None:
        source = self._proxy.sourceModel()
        if source is not None:
            for row, entity in enumerate(self._items):
                if entity.id == entity_id:
                    index = self._proxy.mapFromSource(source.index(row, 0))
                    if index.isValid():
                        self._table.setCurrentIndex(index)
                    break

    def _choose_list_variant(self, title: str, variants: dict[str, dict[str, Any]]) -> str | None:
        if not variants:
            QMessageBox.information(self, title, "Сохранённых вариантов пока нет.")
            return None
        name, accepted = QInputDialog.getItem(self, title, "Вариант", list(variants), editable=False)
        return name if accepted else None

    def _load_list_variant(self) -> None:
        try:
            variants = self._db.list_variants(self._entity_class)
            name = self._choose_list_variant("Открыть вариант списка", variants)
            if name is None:
                return
            state = self.state_from_settings(self._entity_class, self._db, variants[name])
            self._field_filters = state.filters
            self.refresh()
            if state.sort_field not in self._list_field_names:
                self._table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
            self._restore_list_state(state)
        except Exception as exc:
            show_ui_error(self, "Варианты списка", exc, warning=True)

    def _save_list_variant(self) -> None:
        name, accepted = QInputDialog.getText(self, "Сохранить вариант списка", "Название")
        name = name.strip()
        if not accepted or not name:
            return
        try:
            variants = self._db.list_variants(self._entity_class)
            if name in variants and QMessageBox.question(
                self, "Заменить вариант", f"Заменить сохранённый вариант «{name}»?",
            ) != QMessageBox.StandardButton.Yes:
                return
            self._db.save_list_variant(self._entity_class, name, self.list_state().to_settings())
        except Exception as exc:
            show_ui_error(self, "Варианты списка", exc, warning=True)

    def _delete_list_variant(self) -> None:
        try:
            variants = self._db.list_variants(self._entity_class)
            name = self._choose_list_variant("Удалить вариант списка", variants)
            if name is not None and QMessageBox.question(
                self, "Удалить вариант", f"Удалить сохранённый вариант «{name}»?",
            ) == QMessageBox.StandardButton.Yes:
                self._db.delete_list_variant(self._entity_class, name)
        except Exception as exc:
            show_ui_error(self, "Варианты списка", exc, warning=True)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        if not self._filters:
            title = QLabel(self._entity_class._meta.verbose_name)
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setProperty("heading", True)
            title.setWordWrap(True)
            layout.addWidget(title)

        # -- Toolbar --
        toolbar = QHBoxLayout()
        for action in DEFAULT_ACTIONS:
            label = _ACTION_LABELS.get(action, action)
            btn = QPushButton(label)
            if action == "New":
                btn.setProperty("primary", True)
            btn.clicked.connect(lambda checked=False, a=action: self._on_action(a))
            self._action_buttons[action] = btn
            toolbar.addWidget(btn)

        toolbar.addStretch()

        layout.addLayout(toolbar)

        search_row = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setAccessibleName("Поиск по списку")
        self._search_edit.setPlaceholderText("Поиск (Ctrl+F)")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setMaximumWidth(260)
        self._search_edit.textChanged.connect(self._on_search_changed)
        self._search_edit.returnPressed.connect(self._focus_results)
        search_row.addWidget(self._search_edit)

        self._reset_button = QPushButton("Сбросить")
        self._reset_button.setAccessibleName("Сбросить поиск и отбор")
        self._reset_button.setToolTip("Сбросить поиск и отбор")
        self._reset_button.clicked.connect(self._reset_search_and_filters)
        self._reset_button.hide()
        search_row.addWidget(self._reset_button)
        search_row.addStretch()

        self._count_label = QLabel("")
        self._filters_button = QPushButton("Отбор…")
        self._filters_button.clicked.connect(self._edit_field_filters)
        if not self._filters:
            search_row.addWidget(self._filters_button)
            variants_button = QPushButton("Варианты")
            variants_menu = QMenu(variants_button)
            variants_menu.addAction("Открыть вариант…", self._load_list_variant)
            variants_menu.addAction("Сохранить вариант…", self._save_list_variant)
            variants_menu.addAction("Удалить вариант…", self._delete_list_variant)
            variants_button.setMenu(variants_menu)
            search_row.addWidget(variants_button)
        else:
            self._filters_button.setParent(self)
            self._filters_button.hide()
        if not self._filters and self._db.allows(self._entity_class, "read"):
            csv_button = QPushButton("CSV")
            menu = QMenu(csv_button)
            menu.addAction("Выгрузить список…", self._export_list_csv)
            if self._entity_class._meta.is_catalog and (
                self._db.allows(self._entity_class, "create")
                or (self._db.allows(self._entity_class, "update") and catalog_csv_keys(self._entity_class, self._db))
            ):
                menu.addSeparator()
                menu.addAction("Импорт CSV…", self._import_csv)
                menu.addAction("Сохранить шаблон CSV…", self._save_csv_template)
            csv_button.setMenu(menu)
            search_row.addWidget(csv_button)
        search_row.addWidget(self._count_label)
        layout.addLayout(search_row)

        # -- Table --
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        self._table.setSortingEnabled(True)
        if getattr(self._entity_class, "__list_order__", None):
            self._table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self._table.doubleClicked.connect(self._on_double_click)
        layout.addWidget(self._table, 1)

        self._empty_label = QLabel()
        self._empty_label.setObjectName("emptyResults")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.hide()
        layout.addWidget(self._empty_label, 1)

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(not self._filters)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        # Shortcuts
        descendants = Qt.ShortcutContext.WidgetWithChildrenShortcut
        list_context = descendants if self._filters else Qt.ShortcutContext.WindowShortcut
        for key, parent, context, callback in (
            ("Ctrl+F", self, list_context, self._search_edit.setFocus),
            ("F5", self, list_context, self.refresh),
            ("Delete", self._table, descendants, lambda: self._on_action("Delete")),
            ("Return", self._table, descendants, self._on_double_click),
            ("Enter", self._table, descendants, self._on_double_click),
        ):
            shortcut = QShortcut(QKeySequence(key), parent)
            shortcut.setContext(context)
            shortcut.activated.connect(callback)
        self._clear_search_shortcut = QShortcut(QKeySequence("Escape"), self._search_edit)
        self._clear_search_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._clear_search_shortcut.activated.connect(self._search_edit.clear)
        self._clear_search_shortcut.setEnabled(False)

        self._update_button_states()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload data from the database and rebuild the table."""
        try:
            selected = self._selected_entity()
            filters = dict(self._filters or {})
            for name, (operator, value) in self._field_filters.items():
                if self._db.can_read_field(self._entity_class, name):
                    filters[f"{name}__{operator}"] = value
            self._items = self._db.filter(self._entity_class, **filters) if filters else self._db.list(self._entity_class)
            self._rebuild_model()
            if selected is not None and selected.id is not None:
                self._select_entity_by_id(selected.id)
        except Exception as exc:
            show_ui_error(self, "Ошибка", exc)

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
            except (SQLAlchemyError, RuntimeError, LookupError, PermissionError, ValueError, TypeError):
                logging.getLogger(__name__).warning(
                    "Не удалось загрузить справочник для поля %s.%s",
                    self._entity_class.__name__,
                    name,
                    exc_info=True,
                )
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
            except (InvalidOperation, ValueError, TypeError, OverflowError):
                logging.getLogger(__name__).warning(
                    "Некорректное десятичное значение %r для поля %s", val, name, exc_info=True,
                )
                return str(val), str(val)

        if isinstance(field, IntegerField):
            try:
                return str(val), int(val)
            except (ValueError, TypeError, OverflowError):
                logging.getLogger(__name__).warning(
                    "Некорректное целочисленное значение %r для поля %s", val, name, exc_info=True,
                )
                return str(val), str(val)

        s = str(val)
        return s, s.lower()

    def _rebuild_model(self) -> None:
        visible_fields: list[tuple[str, Field]] = [
            (n, f) for n, f in self._entity_class._meta.fields.items()
            if f.visible and n not in getattr(self._entity_class, "__list_exclude__", ())
            and n not in (self._filters or {}) and self._db.can_read_field(self._entity_class, n)
        ]
        if configured := self._entity_class.__list_columns__:
            by_name = dict(visible_fields)
            visible_fields = [(name, by_name[name]) for name in configured if name in by_name]
        self._list_field_names = [name for name, _ in visible_fields]
        self._filters_button.setEnabled(bool(visible_fields))
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
        if self._table.model() is None:
            self._table.setModel(self._proxy)
            self._table.selectionModel().currentChanged.connect(self._update_button_states)
        self._table.resizeColumnsToContents()
        for column in range(len(headers)):
            width = self._table.columnWidth(column)
            title_width = self._table.horizontalHeader().fontMetrics().horizontalAdvance(headers[column])
            self._table.setColumnWidth(column, min(440, max(92, width + 10, title_width + 34)))
        if self._filters and headers:
            self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        sort_field = getattr(self._entity_class, "__list_order__", None)
        if sort_field and self._table.horizontalHeader().sortIndicatorSection() < 0:
            self._sort_by_field(sort_field, visible_fields)
        self._update_results()
        self._update_button_states()

    def _sort_by_field(self, name: str, fields: list[tuple[str, Field]]) -> None:
        for column, (field_name, _) in enumerate(fields):
            if field_name == name.removeprefix("-"):
                direction = Qt.SortOrder.DescendingOrder if name.startswith("-") else Qt.SortOrder.AscendingOrder
                self._table.sortByColumn(column, direction)
                break

    @staticmethod
    def _filter_operators(field: Field) -> list[tuple[str, str]]:
        operators = [("Равно", "eq"), ("Не равно", "ne")]
        if isinstance(field, StringField):
            operators.append(("Содержит", "literal_contains"))
        if isinstance(field, (IntegerField, DecimalField, DateField, DateTimeField, TimeField)):
            operators.extend((
                ("Больше", "gt"), ("Не меньше", "gte"), ("Меньше", "lt"), ("Не больше", "lte"),
                ("Диапазон", "between"),
            ))
        return operators + [("Пусто", "isnull"), ("Не пусто", "notnull")]

    def _edit_field_filters(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Отбор по полям")
        dialog.resize(620, 420)
        layout = QVBoxLayout(dialog)
        hint = QLabel("Отметьте поля и укажите значения. Запись должна соответствовать всем условиям.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, 1)
        panel = QWidget()
        scroll.setWidget(panel)
        form = QFormLayout(panel)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        editors: dict[str, tuple[Field, QCheckBox, QComboBox, QWidget, QWidget | None]] = {}
        for name in self._list_field_names:
            if not self._db.can_read_field(self._entity_class, name):
                continue
            field = copy(self._entity_class._meta.fields[name])
            field.read_only = False
            enabled = QCheckBox(field.display_name)
            enabled.setObjectName(f"filter_{name}")
            editor = create_editor(field)
            if isinstance(field, RefField) and isinstance(editor, QComboBox):
                editor.setEditable(False)
                populate_ref_editor(editor, self._db.list(field.resolve_target()), field.representation_field)
            operator = QComboBox()
            operator.setObjectName(f"operator_{name}")
            for title, code in self._filter_operators(field):
                operator.addItem(title, code)
            previous, value = self._field_filters.get(name, ("eq", None))
            if previous == "isnull" and value is False:
                previous = "notnull"
            operator.setCurrentIndex(max(0, operator.findData(previous)))
            upper = create_editor(field) if operator.findData("between") >= 0 else None
            if upper is not None:
                upper.setObjectName(f"upper_{name}")
            if previous == "between" and upper is not None:
                lower_value, upper_value = value
                set_editor_value(editor, field, lower_value)
                set_editor_value(upper, field, upper_value)
            else:
                set_editor_value(editor, field, None if previous in {"isnull", "notnull"} else value)
            enabled.setChecked(name in self._field_filters)
            controls = QWidget()
            row = QHBoxLayout(controls)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(operator)
            row.addWidget(editor, 1)
            if upper is not None:
                upper.setToolTip("Верхняя граница включительно")
                row.addWidget(upper, 1)
            controls.setEnabled(enabled.isChecked())
            enabled.toggled.connect(controls.setEnabled)

            def update_editor(
                _index: int = 0, value_editor: QWidget = editor,
                selector: QComboBox = operator, upper_editor: QWidget | None = upper,
            ) -> None:
                code = selector.currentData()
                value_editor.setEnabled(code not in {"isnull", "notnull"})
                if upper_editor is not None:
                    upper_editor.setVisible(code == "between")

            operator.currentIndexChanged.connect(update_editor)
            update_editor()
            form.addRow(enabled, controls)
            editors[name] = (field, enabled, operator, editor, upper)

        error = QLabel()
        error.setWordWrap(True)
        layout.addWidget(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        reset = buttons.addButton("Сбросить", QDialogButtonBox.ButtonRole.ResetRole)

        def clear_filters() -> None:
            for _, checkbox, _, _, _ in editors.values():
                checkbox.setChecked(False)

        def apply_filters() -> None:
            updated: dict[str, tuple[str, Any]] = {}
            for name, (field, enabled, selector, editor, upper) in editors.items():
                if not enabled.isChecked():
                    continue
                operator = selector.currentData()
                if operator in {"isnull", "notnull"}:
                    updated[name] = ("isnull", operator == "isnull")
                    continue
                value = get_editor_value(editor, field)
                if operator == "between" and upper is not None:
                    high = get_editor_value(upper, field)
                    if value is None or high is None or value > high:
                        error.setText(f"«{field.display_name}»: укажите обе границы от меньшей к большей.")
                        return
                    updated[name] = (operator, (value, high))
                    continue
                if value is None:
                    if operator in {"eq", "ne"}:
                        updated[name] = ("isnull", operator == "eq")
                        continue
                    error.setText(f"«{field.display_name}»: укажите значение.")
                    return
                updated[name] = (operator, value)
            self._field_filters = updated
            dialog.accept()

        reset.clicked.connect(clear_filters)
        buttons.accepted.connect(apply_filters)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec():
            self.refresh()
        dialog.deleteLater()

    def _on_search_changed(self, text: str) -> None:
        self._proxy.setFilterFixedString(text)
        self._clear_search_shortcut.setEnabled(bool(text))
        self._update_results()
        self._update_button_states()

    def _focus_results(self) -> None:
        if not self._proxy.rowCount() or not self._proxy.columnCount():
            return
        if not self._table.currentIndex().isValid():
            self._table.setCurrentIndex(self._proxy.index(0, 0))
        self._table.setFocus()

    def _reset_search_and_filters(self) -> None:
        had_filters = bool(self._field_filters)
        self._field_filters.clear()
        self._search_edit.clear()
        if had_filters:
            self.refresh()
        self._search_edit.setFocus()

    def _update_results(self) -> None:
        total = len(self._items)
        shown = self._proxy.rowCount()
        if shown == total:
            self._count_label.setText(f"Записей: {total}")
        else:
            self._count_label.setText(f"Показано: {shown} из {total}")

        filter_count = sum(name in self._list_field_names for name in self._field_filters)
        self._filters_button.setText(f"Отбор ({filter_count})…" if filter_count else "Отбор…")
        narrowed = bool(self._search_edit.text() or self._field_filters)
        self._reset_button.setVisible(narrowed)
        self._empty_label.setText(
            "По текущим условиям ничего не найдено." if narrowed else "Здесь пока нет записей."
        )
        self._empty_label.setVisible(shown == 0)
        self._table.setVisible(shown > 0)

    def _update_button_states(self) -> None:
        selected = self._selected_entity()
        editable = selected is not None and self._db.can_edit(selected)
        can_create = self._db.allows(self._entity_class, "create")
        if "New" in self._action_buttons:
            self._action_buttons["New"].setEnabled(can_create)
        for act in ("Edit", "Copy", "Delete"):
            btn = self._action_buttons.get(act)
            if btn is not None:
                allowed = {
                    "Edit": selected is not None,
                    "Copy": editable and can_create,
                    "Delete": selected is not None and self._db.can_delete(selected),
                }
                btn.setEnabled(allowed[act])
                if act == "Edit":
                    btn.setText("Изменить" if selected is None or editable else "Открыть")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _save_csv_file(self, title: str, content: bytes) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, title, f"{self._entity_class.__name__}.csv", "CSV (*.csv)",
        )
        if not path:
            return
        file = QSaveFile(path)
        if not file.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(file.errorString())
        if file.write(content) != len(content) or not file.commit():
            raise OSError(file.errorString())

    def _save_csv_template(self) -> None:
        try:
            self._save_csv_file("Сохранить шаблон CSV", catalog_csv_template(self._entity_class, self._db))
        except (OSError, ValueError) as exc:
            show_ui_error(self, "Шаблон CSV", exc, warning=True)

    @staticmethod
    def _csv_cell(value: Any) -> str:
        return sanitize_csv_cell(value)

    def _export_list_csv(self) -> None:
        if not self._db.allows(self._entity_class, "read"):
            return
        model = self._table.model()
        header = self._table.horizontalHeader()
        columns = [
            index for position in range(header.count())
            if not self._table.isColumnHidden(index := header.logicalIndex(position))
            and self._db.can_read_field(self._entity_class, self._list_field_names[index])
        ]
        if model is None or not columns:
            return
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter=";")
        writer.writerow([self._csv_cell(model.headerData(index, Qt.Orientation.Horizontal)) for index in columns])
        for row in range(model.rowCount()):
            writer.writerow([self._csv_cell(model.data(model.index(row, index))) for index in columns])
        try:
            self._save_csv_file("Выгрузить список в CSV", stream.getvalue().encode("utf-8-sig"))
        except OSError as exc:
            show_ui_error(self, "Выгрузка CSV", exc, warning=True)

    def _map_csv_columns(
        self, headers: list[str], fields: dict[str, str],
        suggested: list[str | None], key: str | None,
    ) -> list[str | None] | None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Сопоставление колонок CSV")
        dialog.resize(650, 420)
        layout = QVBoxLayout(dialog)
        hint = QLabel("Выберите поле справочника для каждой нужной колонки. Остальные можно пропустить.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = QTableWidget(len(headers), 2)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setHorizontalHeaderLabels(["Колонка CSV", "Поле справочника"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table)
        pickers = []
        for index, (header, name) in enumerate(zip(headers, suggested, strict=True)):
            table.setItem(index, 0, QTableWidgetItem(header or f"Колонка {index + 1}"))
            picker = QComboBox()
            picker.addItem("Пропустить", None)
            for field, label in fields.items():
                picker.addItem(f"{label} ({field})", field)
            picker.setCurrentIndex(max(0, picker.findData(name)))
            table.setCellWidget(index, 1, picker)
            pickers.append(picker)
        status = QLabel()
        status.setWordWrap(True)
        layout.addWidget(status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText("Предпросмотр")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def check_mapping() -> None:
            selected = [picker.currentData() for picker in pickers if picker.currentIndex() > 0]
            if len(set(selected)) != len(selected):
                message = "Для каждого поля выберите одну колонку CSV."
            elif key is not None and key not in selected:
                message = f"Выберите колонку для ключа «{fields[key]}»."
            elif not selected:
                message = "Выберите хотя бы одно поле."
            else:
                message = ""
            status.setText(message)
            confirm.setEnabled(not message)

        for picker in pickers:
            picker.currentIndexChanged.connect(check_mapping)
        check_mapping()
        result = [picker.currentData() for picker in pickers] if dialog.exec() else None
        dialog.deleteLater()
        return result

    def _import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Импорт справочника", "", "CSV (*.csv);;Все файлы (*)")
        if not path:
            return
        try:
            key = None
            if self._db.allows(self._entity_class, "update"):
                modes = {
                    f"Обновлять по полю «{label}» ({name})": name
                    for name, label in catalog_csv_keys(self._entity_class, self._db).items()
                }
                if modes:
                    if self._db.allows(self._entity_class, "create"):
                        modes = {"Только добавлять новые": None, **modes}
                    mode, accepted = QInputDialog.getItem(
                        self, "Импорт справочника", "Режим", list(modes), editable=False,
                    )
                    if not accepted:
                        return
                    key = modes[mode]
            columns, records = read_catalog_csv(
                Path(path), self._entity_class, self._db, key=key,
                map_columns=lambda headers, fields, suggested: self._map_csv_columns(headers, fields, suggested, key),
            )
            if not records:
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("Предпросмотр импорта CSV")
            dialog.resize(590, 420)
            layout = QVBoxLayout(dialog)
            sample = records[:15]
            updated = sum(row.record.id is not None for row in records)
            layout.addWidget(QLabel(
                f"Новых записей: {len(records) - updated}. Обновлений: {updated}. В предпросмотре: {len(sample)}."
            ))
            fields = [(name, self._entity_class._meta.fields[name]) for name in columns]
            references = self._build_ref_lookup(fields)
            headers = [field.display_name for _, field in fields]
            if key is not None:
                headers.append("Операция")
            table = QTableWidget(len(sample), len(headers))
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setHorizontalHeaderLabels(headers)
            for row, item in enumerate(sample):
                record = item.record
                for col, name in enumerate(columns):
                    value = record._data.get(name)
                    label = references.get(name, {}).get(value) if name in references else None
                    text = f"{label} (ID {value})" if label else "" if value is None else str(value)
                    table.setItem(row, col, QTableWidgetItem(text))
                if key is not None:
                    table.setItem(row, len(columns), QTableWidgetItem("Добавить" if record.id is None else "Обновить"))
            layout.addWidget(table)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Импортировать")
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            accepted = dialog.exec()
            dialog.deleteLater()
            if accepted:
                save_catalog_csv(self._db, records)
                self.refresh()
                self.data_changed.emit()
        except Exception as exc:
            show_ui_error(self, "Импорт CSV", exc, warning=True)

    def _on_action(self, action: str) -> None:
        if action == "New" and self._db.allows(self._entity_class, "create"):
            self.create_requested.emit(self._entity_class)

        elif action == "Edit":
            entity = self._selected_entity()
            if entity is not None:
                self.edit_requested.emit(entity)

        elif action == "Copy":
            entity = self._selected_entity()
            if entity is not None and self._db.can_edit(entity) and self._db.allows(self._entity_class, "create"):
                self.copy_requested.emit(entity)

        elif action == "Delete":
            entity = self._selected_entity()
            if entity is None or not self._db.can_delete(entity):
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
                with self._db.transaction():
                    if hasattr(entity, "clear_movements"):
                        entity.clear_movements(self._db)
                    self._db.delete(entity)
                self.refresh()
                self.data_changed.emit()
            except Exception as exc:
                show_ui_error(self, "Не удалось удалить", exc, warning=True)

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

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self._rows[row_idx][col_idx]

        if role == Qt.ItemDataRole.UserRole:
            if self._sort_rows and row_idx < len(self._sort_rows):
                return self._sort_rows[row_idx][col_idx]
            return self._rows[row_idx][col_idx]

        if role == Qt.ItemDataRole.FontRole:
            if 0 <= row_idx < len(self._items):
                item = self._items[row_idx]
                if self._is_item_done(item) and getattr(type(item), "__strike_completed__", True):
                    f = QFont()
                    f.setStrikeOut(True)
                    return f

        if role == Qt.ItemDataRole.ForegroundRole:
            if 0 <= row_idx < len(self._items):
                item = self._items[row_idx]
                if self._is_item_done(item):
                    if getattr(type(item), "__strike_completed__", True):
                        return QColor(128, 128, 128)
                    return None
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
