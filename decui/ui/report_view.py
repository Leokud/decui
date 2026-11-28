"""Report parameter form + results table widget + CSV export."""

from __future__ import annotations

import datetime
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QSignalBlocker,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from decui.reports import (
    BooleanParam,
    ChoiceParam,
    DateParam,
    DecimalParam,
    IntegerParam,
    RefParam,
    Report,
    ReportParam,
    StringParam,
    _parse_numeric_str,
    calc_aggregate,
    group_report_rows,
)
from decui.reports import (
    Column as ColumnMeta,
)
from decui.ui.chart import BarChart
from decui.ui.editors import populate_ref_editor, show_ui_error

if TYPE_CHECKING:
    from decui.orm import Database


_AGGREGATE_LABELS = {
    "sum": "Сумма",
    "avg": "Среднее",
    "count": "Количество числовых значений",
    "min": "Минимум",
    "max": "Максимум",
}


class _OptionalDateWidget(QWidget):
    """Checkbox + QDateEdit wrapper for nullable optional DateParam."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.checkbox = QCheckBox("Задать дату")
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd.MM.yyyy")
        self.date_edit.setDate(QDate.currentDate())
        self.date_edit.setEnabled(False)
        self.checkbox.toggled.connect(self.date_edit.setEnabled)
        layout.addWidget(self.checkbox)
        layout.addWidget(self.date_edit)
        layout.addStretch()

    def value(self) -> datetime.date | None:
        if not self.checkbox.isChecked():
            return None
        qd = self.date_edit.date()
        return datetime.date(qd.year(), qd.month(), qd.day())


class _ReportSortProxy(QSortFilterProxyModel):
    """Keeps optional footer totals row pinned at bottom while sorting body rows cleanly."""

    def lessThan(self, source_left: QModelIndex, source_right: QModelIndex) -> bool:
        src = cast(_TableModel, self.sourceModel())
        l_tot = src._is_total_row(source_left)
        r_tot = src._is_total_row(source_right)
        is_desc = self.sortOrder() == Qt.SortOrder.DescendingOrder
        if l_tot and r_tot:
            return False
        if l_tot:
            return is_desc  # Keep total row at bottom in both directions
        if r_tot:
            return not is_desc

        l_val = src.data(source_left, Qt.ItemDataRole.UserRole)
        r_val = src.data(source_right, Qt.ItemDataRole.UserRole)
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


class ReportView(QWidget):
    """Auto-generated report UI: parameter form + execute/export buttons + sortable results table."""

    drill_down_requested = Signal(type, int)

    def __init__(
        self,
        report_class: type[Report],
        database: Database,
        parent: QWidget | None = None,
        *,
        initial_refs: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._report_class = report_class
        self._variant_key = report_class.__name__
        self._db = database
        self._param_widgets: dict[str, QWidget] = {}
        self._report_instance = report_class()
        self._report_instance._interactive_grouping = True
        self._last_rows: list[dict[str, Any]] = []
        self._applied_params: dict[str, Any] | None = None
        self._generation_failed = False
        self._selected_columns: set[str] | None = None
        self._column_order: list[str] = []
        self._displayed_columns: list[ColumnMeta] = []
        self._sort_field: str | None = None
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._group_order: tuple[str, ...] | None = None
        self._aggregates: dict[str, str | None] = {}
        self._proxy = _ReportSortProxy(self)
        self._proxy.setSortRole(Qt.ItemDataRole.UserRole)

        self._build_ui()
        self._watch_params()
        for name, record_id in (initial_refs or {}).items():
            param = report_class._report_meta.params.get(name)
            widget = self._param_widgets.get(name)
            if not isinstance(param, RefParam) or not isinstance(widget, QComboBox):
                raise ValueError(f"Параметр «{name}» не является ссылкой.")
            index = widget.findData(record_id, Qt.ItemDataRole.UserRole)
            if index < 0:
                raise ValueError(f"Запись для параметра «{param.display_name}» удалена или недоступна.")
            widget.setCurrentIndex(index)
        if getattr(report_class, "auto_run", False):
            self._generate()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        # Title
        import html
        safe_title = html.escape(self._report_class._report_meta.title)
        title = QLabel(f"<b>Отчёт: {safe_title}</b>")
        title.setProperty("heading", True)
        title.setWordWrap(True)
        layout.addWidget(title)

        # Param form
        params = self._report_class._report_meta.params
        if params:
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            for name, param in params.items():
                editor = self._create_param_editor(param)
                self._param_widgets[name] = editor
                label = param.display_name
                if param.required:
                    label += " *"
                caption = QLabel(label + ":")
                caption.setTextFormat(Qt.TextFormat.PlainText)
                caption.setBuddy(editor)
                form.addRow(caption, editor)
            layout.addLayout(form)

        self._variant_picker = QComboBox()
        self._variant_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._variant_picker.activated.connect(self._apply_variant)
        self._variant_menu = QPushButton("Управление")
        menu = QMenu(self._variant_menu)
        self._totals_action = menu.addAction("Настроить итоги…", self._edit_totals)
        self._totals_action.setEnabled(bool(self._report_class.columns))
        menu.addSeparator()
        menu.addAction("Сохранить текущие настройки…", self._save_variant)
        self._delete_variant_action = menu.addAction("Удалить выбранный вариант", self._delete_variant)
        self._variant_menu.setMenu(menu)
        variant_controls = QHBoxLayout()
        variant_controls.addWidget(QLabel("Вариант отчёта"))
        variant_controls.addWidget(self._variant_picker, 1)
        variant_controls.addWidget(self._variant_menu)
        layout.addLayout(variant_controls)
        self._reload_variants()

        # Action bar
        btn_layout = QHBoxLayout()
        self._columns_button = QPushButton("Колонки")
        self._columns_button.setEnabled(bool(self._report_class.columns))
        self._columns_button.clicked.connect(self._edit_columns)
        btn_layout.addWidget(self._columns_button)
        self._groups_button = QPushButton("Группировки")
        self._groups_button.setEnabled(bool(self._report_class.columns))
        self._groups_button.clicked.connect(self._edit_groups)
        btn_layout.addWidget(self._groups_button)
        chart_cfg = getattr(self._report_class, "chart", None)
        has_chart = isinstance(chart_cfg, dict) and bool(chart_cfg.get("label")) and bool(chart_cfg.get("value"))
        self._chart_toggle = QPushButton("Диаграмма")
        self._chart_toggle.setCheckable(True)
        self._chart_toggle.setChecked(has_chart)
        self._chart_toggle.setVisible(has_chart)
        self._chart_toggle.toggled.connect(self._toggle_chart_visibility)
        btn_layout.addWidget(self._chart_toggle)
        btn_layout.addStretch()

        self._export_btn = QPushButton("Экспорт CSV")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_csv)
        btn_layout.addWidget(self._export_btn)

        generate_btn = QPushButton("Сформировать")
        generate_btn.setProperty("primary", True)
        generate_btn.setToolTip("Сформировать отчёт (F5)")
        generate_btn.clicked.connect(self._generate)
        btn_layout.addWidget(generate_btn)
        layout.addLayout(btn_layout)

        generate_shortcut = QShortcut(QKeySequence("F5"), self)
        generate_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        generate_shortcut.activated.connect(self._generate)

        self._result_notice = QLabel()
        self._result_notice.setObjectName("reportNotice")
        self._result_notice.setWordWrap(True)
        self._result_notice.setVisible(False)
        layout.addWidget(self._result_notice)

        # Summary panel
        self._summary_label = QLabel()
        self._summary_label.setVisible(False)
        self._summary_label.setStyleSheet("padding: 8px; font-size: 13px;")
        layout.addWidget(self._summary_label)

        # Results table (takes all remaining vertical height cleanly)
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().sortIndicatorChanged.connect(self._remember_sort)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(34)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setVisible(False)
        layout.addWidget(self._table, stretch=1)

        chart_cfg = getattr(self._report_class, "chart", None)
        self._chart = BarChart(
            title=(chart_cfg.get("title", "") if isinstance(chart_cfg, dict) else ""),
            bar_color=(chart_cfg.get("color", "#4A7BFF") if isinstance(chart_cfg, dict) and chart_cfg.get("color") else "#4A7BFF"),
        )
        self._chart.setMinimumHeight(200)
        self._chart.setMaximumHeight(270)
        self._chart.setVisible(False)
        layout.addWidget(self._chart)

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        self._no_data_label = QLabel("Нажмите «Сформировать» для вывода данных")
        self._no_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._no_data_label.setObjectName("emptyResults")
        self._no_data_label.setWordWrap(True)
        layout.addWidget(self._no_data_label, stretch=1)

    def showEvent(self, event: Any) -> None:
        self._refresh_ref_params()
        super().showEvent(event)

    def _refresh_ref_params(self) -> None:
        for name, param in self._report_class._report_meta.params.items():
            if isinstance(param, RefParam):
                widget = self._param_widgets.get(name)
                if isinstance(widget, QComboBox):
                    target = param.resolve_target()
                    items = self._db.list(target)
                    populate_ref_editor(widget, items, param.representation_field)
        self._update_result_status()

    def _watch_params(self) -> None:
        for widget in self._param_widgets.values():
            if isinstance(widget, _OptionalDateWidget):
                widget.checkbox.toggled.connect(self._update_result_status)
                widget.date_edit.dateChanged.connect(self._update_result_status)
            elif isinstance(widget, QDateEdit):
                widget.dateChanged.connect(self._update_result_status)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._update_result_status)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._update_result_status)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._update_result_status)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.valueChanged.connect(self._update_result_status)

    # ------------------------------------------------------------------
    # Param editor factory
    # ------------------------------------------------------------------

    def _create_param_editor(self, param: ReportParam) -> QWidget:
        if isinstance(param, StringParam):
            w = QLineEdit()
            w.setPlaceholderText(param.display_name)
            if param.default:
                w.setText(str(param.default))
            return w

        if isinstance(param, IntegerParam):
            w = QSpinBox()
            w.setRange(param.min_value, param.max_value)
            w.setValue(param.default)
            return w

        if isinstance(param, DecimalParam):
            w = QDoubleSpinBox()
            w.setDecimals(param.scale)
            w.setRange(-10**12, 10**12)
            w.setValue(param.default)
            return w

        if isinstance(param, BooleanParam):
            w = QCheckBox()
            w.setChecked(param.default)
            return w

        if isinstance(param, DateParam):
            default_val = param.default() if callable(param.default) else param.default
            if not param.required and default_val is None:
                return _OptionalDateWidget()
            w = QDateEdit()
            w.setCalendarPopup(True)
            w.setDisplayFormat("dd.MM.yyyy")
            if isinstance(default_val, datetime.date):
                w.setDate(QDate(default_val.year, default_val.month, default_val.day))
            else:
                w.setDate(QDate.currentDate())
            return w

        if isinstance(param, ChoiceParam):
            w = QComboBox()
            w.addItem("", None)
            for code, label in param.choices:
                w.addItem(label, code)
            if param.default is not None:
                idx = w.findData(param.default, Qt.ItemDataRole.UserRole)
                if idx >= 0:
                    w.setCurrentIndex(idx)
            return w

        if isinstance(param, RefParam):
            w = QComboBox()
            target = param.resolve_target()
            items = self._db.list(target)
            populate_ref_editor(w, items, param.representation_field)
            if param.default is not None:
                idx = w.findData(param.default, Qt.ItemDataRole.UserRole)
                if idx >= 0:
                    w.setCurrentIndex(idx)
            return w

        raise TypeError(f"Unknown param type: {type(param).__name__}")

    def _read_param(self, param: ReportParam, widget: QWidget) -> Any:
        if isinstance(param, StringParam):
            w = cast(QLineEdit, widget)
            return w.text() or None

        if isinstance(param, IntegerParam):
            w = cast(QSpinBox, widget)
            return w.value()

        if isinstance(param, DecimalParam):
            w = cast(QDoubleSpinBox, widget)
            return w.value()

        if isinstance(param, BooleanParam):
            w = cast(QCheckBox, widget)
            return w.isChecked()

        if isinstance(param, DateParam):
            if isinstance(widget, _OptionalDateWidget):
                return widget.value()
            w = cast(QDateEdit, widget)
            qd = w.date()
            if qd.isNull():
                return None
            return datetime.date(qd.year(), qd.month(), qd.day())

        if isinstance(param, (ChoiceParam, RefParam)):
            w = cast(QComboBox, widget)
            return w.currentData(Qt.ItemDataRole.UserRole)

        return None

    def _read_params(self) -> dict[str, Any]:
        return {
            name: self._read_param(param, self._param_widgets[name])
            for name, param in self._report_class._report_meta.params.items()
        }

    # ------------------------------------------------------------------
    # Execute & Export
    # ------------------------------------------------------------------

    def _reload_variants(self, selected: str = "") -> None:
        try:
            self._variants = self._db.report_variants(self._variant_key)
        except Exception as exc:
            self._variants = {}
            show_ui_error(self, "Варианты отчёта", exc, warning=True)
        self._variant_picker.clear()
        self._variant_picker.addItem("Выберите вариант", "")
        for name in self._variants:
            self._variant_picker.addItem(name, name)
        self._variant_picker.setCurrentIndex(max(0, self._variant_picker.findData(selected)))
        self._delete_variant_action.setEnabled(selected in self._variants)

    def _apply_variant(self, index: int) -> None:
        name = self._variant_picker.itemData(index)
        self._delete_variant_action.setEnabled(name in self._variants)
        if name not in self._variants:
            return
        self._refresh_ref_params()
        variant = self._variants[name]
        self._column_order = []
        self._sort_field = None
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._group_order = None
        self._aggregates = {}
        params = variant.get("params")
        if isinstance(params, dict):
            columns = variant.get("columns")
            self._selected_columns = {key for key in columns if isinstance(key, str)} if isinstance(columns, list) else None
            groups = variant.get("groups")
            if isinstance(groups, list):
                self._group_order = tuple(dict.fromkeys(key for key in groups if isinstance(key, str)))
            aggregates = variant.get("aggregates")
            if isinstance(aggregates, dict):
                self._aggregates = {
                    key: value for key, value in aggregates.items()
                    if value is None or (isinstance(value, str) and value in _AGGREGATE_LABELS)
                }
            sort = variant.get("sort")
            if isinstance(sort, dict) and isinstance(sort.get("field"), str):
                self._sort_field = sort["field"]
                if sort.get("descending") is True:
                    self._sort_order = Qt.SortOrder.DescendingOrder
            order = variant.get("column_order")
            if isinstance(order, list):
                self._column_order = list(dict.fromkeys(key for key in order if isinstance(key, str)))
        else:
            params = variant
            self._selected_columns = None
        for key, value in params.items():
            widget = self._param_widgets.get(key)
            if isinstance(widget, QLineEdit) and (isinstance(value, str) or value is None):
                widget.setText(value or "")
            elif isinstance(widget, QCheckBox) and type(value) is bool:
                widget.setChecked(value)
            elif isinstance(widget, QSpinBox) and type(value) is int:
                widget.setValue(max(widget.minimum(), min(value, widget.maximum())))
            elif isinstance(widget, QDoubleSpinBox) and type(value) in (int, float):
                widget.setValue(value)
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(max(0, widget.findData(value)))
            elif isinstance(widget, (QDateEdit, _OptionalDateWidget)):
                date = QDate.fromString(value, Qt.DateFormat.ISODate) if isinstance(value, str) else QDate()
                if isinstance(widget, _OptionalDateWidget):
                    widget.checkbox.setChecked(date.isValid())
                    if date.isValid():
                        widget.date_edit.setDate(date)
                elif date.isValid():
                    widget.setDate(date)

        if self._last_rows:
            self._show_results(self._last_rows)

    def _available_columns(self, rows: list[dict[str, Any]]) -> list[ColumnMeta]:
        return list(self._report_class.columns) or (
            [ColumnMeta(key, key) for key in rows[0] if not str(key).startswith("_")] if rows else []
        )

    def _ordered_columns(self, available: list[ColumnMeta]) -> list[ColumnMeta]:
        order = {name: index for index, name in enumerate(self._column_order)}
        return sorted(available, key=lambda col: order.get(col.field, len(order)))

    def _visible_columns(self, available: list[ColumnMeta]) -> list[ColumnMeta]:
        available = self._ordered_columns(available)
        if self._selected_columns is not None:
            groups = self._active_groups(available)
            available = [
                col for col in available if col.field in self._selected_columns or col.field in groups
            ] or available
        return [
            replace(col, aggregate=self._aggregates[col.field]) if col.field in self._aggregates else col
            for col in available
        ]

    def _active_groups(self, available: list[ColumnMeta]) -> tuple[str, ...]:
        if self._group_order is None:
            return self._report_class.group_by
        names = {col.field for col in available}
        return tuple(name for name in self._group_order if name in names)

    def _edit_columns(self) -> None:
        available = self._available_columns(self._last_rows)
        visible = {col.field for col in self._visible_columns(available)}
        choice = self._choose_report_fields(
            "Колонки отчёта", self._ordered_columns(available), visible,
            locked=self._active_groups(available),
        )
        if choice is not None:
            order, fields = choice
            default_order = [col.field for col in available]
            self._column_order = [] if order == default_order else order
            self._selected_columns = None if fields == set(default_order) else fields
            if self._last_rows:
                self._show_results(self._last_rows)

    def _edit_groups(self) -> None:
        available = self._available_columns(self._last_rows)
        groups = self._active_groups(available)
        priorities = {name: index for index, name in enumerate(groups)}
        ordered = sorted(available, key=lambda col: priorities.get(col.field, len(groups)))
        choice = self._choose_report_fields("Группировки отчёта", ordered, set(groups), allow_empty=True)
        if choice is not None:
            order, fields = choice
            selected = tuple(name for name in order if name in fields)
            self._group_order = None if selected == self._report_class.group_by else selected
            if self._last_rows:
                self._show_results(self._last_rows)

    def _edit_totals(self) -> None:
        available = self._ordered_columns(self._available_columns(self._last_rows))
        dialog = QDialog(self)
        dialog.setWindowTitle("Итоги отчёта")
        dialog.resize(440, min(480, 110 + len(available) * 36))
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, 1)
        panel = QWidget()
        scroll.setWidget(panel)
        form = QFormLayout(panel)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        editors: dict[str, QComboBox] = {}
        for column in available:
            editor = QComboBox()
            editor.addItem("По умолчанию", "")
            editor.addItem("Без итога", None)
            for key, label in _AGGREGATE_LABELS.items():
                editor.addItem(label, key)
            editor.setCurrentIndex(max(0, editor.findData(self._aggregates.get(column.field, ""))))
            editors[column.field] = editor
            form.addRow(column.title, editor)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec():
            self._aggregates = {
                name: editor.currentData() for name, editor in editors.items() if editor.currentIndex() != 0
            }
            if self._last_rows:
                self._show_results(self._last_rows)
        dialog.deleteLater()

    def _choose_report_fields(
        self, title: str, columns: list[ColumnMeta], checked: set[str],
        *, locked: tuple[str, ...] = (), allow_empty: bool = False,
    ) -> tuple[list[str], set[str]] | None:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        options = QListWidget()
        options.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        layout.addWidget(options)
        for col in columns:
            item = QListWidgetItem(col.title, options)
            item.setData(Qt.ItemDataRole.UserRole, col.field)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            item.setCheckState(Qt.CheckState.Checked if col.field in checked else Qt.CheckState.Unchecked)
            if col.field in locked:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setToolTip("По этой колонке сгруппирован отчёт")
        def move_current(offset: int) -> None:
            row = options.currentRow()
            target = row + offset
            if row >= 0 and 0 <= target < options.count():
                options.insertItem(target, options.takeItem(row))
                options.setCurrentRow(target)

        reorder = QHBoxLayout()
        for title, offset in (("Выше", -1), ("Ниже", 1)):
            button = QPushButton(title)
            button.clicked.connect(lambda _checked=False, step=offset: move_current(step))
            reorder.addWidget(button)
        reorder.addStretch()
        layout.addLayout(reorder)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        def selected() -> set[str]:
            return {
                item.data(Qt.ItemDataRole.UserRole) for row in range(options.count())
                if (item := options.item(row)).checkState() == Qt.CheckState.Checked
            }

        if not allow_empty:
            confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
            confirm.setEnabled(bool(checked))
            options.itemChanged.connect(lambda _item: confirm.setEnabled(bool(selected())))
        result = None
        if dialog.exec():
            order = [options.item(row).data(Qt.ItemDataRole.UserRole) for row in range(options.count())]
            result = (order, selected())
        dialog.deleteLater()
        return result

    def _save_variant(self) -> None:
        name, accepted = QInputDialog.getText(
            self, "Сохранить вариант", "Название", text=self._variant_picker.currentData(),
        )
        name = name.strip()
        if not accepted or not name:
            return
        if name in self._variants and QMessageBox.question(
            self, "Заменить вариант", f"Заменить сохранённый вариант «{name}»?",
        ) != QMessageBox.StandardButton.Yes:
            return
        values = {
            key: value.isoformat() if isinstance(value, datetime.date) else value
            for key, value in self._read_params().items()
        }
        try:
            self._db.save_report_variant(self._variant_key, name, {
                "params": values,
                "columns": None if self._selected_columns is None else sorted(self._selected_columns),
                "column_order": self._column_order,
                "groups": self._group_order,
                "aggregates": self._aggregates,
                "sort": {
                    "field": self._sort_field,
                    "descending": self._sort_order == Qt.SortOrder.DescendingOrder,
                },
            })
            self._reload_variants(name)
        except Exception as exc:
            show_ui_error(self, "Варианты отчёта", exc, warning=True)

    def _delete_variant(self) -> None:
        name = self._variant_picker.currentData()
        if name and QMessageBox.question(
            self, "Удалить вариант", f"Удалить сохранённый вариант «{name}»?",
        ) == QMessageBox.StandardButton.Yes:
            try:
                self._db.delete_report_variant(self._variant_key, name)
                self._reload_variants()
            except Exception as exc:
                show_ui_error(self, "Варианты отчёта", exc, warning=True)

    def refresh(self) -> None:
        """Update reference choices and refresh the report if its parameters are unchanged."""
        self._refresh_ref_params()
        if not self._params_changed() and (self._applied_params is not None or self._report_class.auto_run):
            self._generate()

    def _generate(self) -> None:
        params = self._read_params()
        missing = [
            (name, param.display_name)
            for name, param in self._report_class._report_meta.params.items()
            if param.required and params[name] in (None, "")
        ]
        if missing:
            errors = [f"Не заполнен параметр «{label}»" for _, label in missing]
            QMessageBox.warning(self, "Ошибка", "\n".join(errors))
            self._param_widgets[missing[0][0]].setFocus()
            self._update_result_status()
            return

        try:
            rows = self._report_instance.execute(self._db, **params)
            self._show_results(rows)
        except Exception as exc:
            if params == self._applied_params:
                self._generation_failed = True
            self._update_result_status()
            show_ui_error(self, "Ошибка", exc, prefix="Ошибка формирования отчёта: ")
        else:
            self._applied_params = params
            self._generation_failed = False
            self._update_result_status()

    def _params_changed(self) -> bool:
        return self._applied_params is not None and self._read_params() != self._applied_params

    def _update_result_status(self) -> None:
        changed = self._params_changed()
        notice = ""
        if changed:
            notice = "Параметры изменены. Нажмите «Сформировать», чтобы обновить результат."
        elif self._generation_failed:
            notice = "Не удалось обновить отчёт. Показан предыдущий результат."
        self._result_notice.setText(notice)
        self._result_notice.setVisible(bool(notice))
        self._export_btn.setEnabled(bool(self._last_rows) and not changed and not self._generation_failed)

    @staticmethod
    def _format_column_value(column: ColumnMeta, text: str, numeric: Any = None) -> str:
        if not text:
            return ""
        formatted = text
        if column.format:
            try:
                num = float(numeric) if numeric is not None else float(text.replace(",", "."))
                formatted = column.format.format(num)
            except (ValueError, TypeError, KeyError, IndexError):
                logging.getLogger(__name__).warning(
                    "Не удалось отформатировать значение %r для колонки %s",
                    text,
                    column.field,
                    exc_info=True,
                )
        if column.prefix:
            formatted = column.prefix + formatted
        if column.suffix:
            formatted = formatted + column.suffix
        return formatted

    def _show_results(self, rows: list[dict[str, Any]]) -> None:
        available = self._available_columns(rows)
        self._columns_button.setEnabled(bool(available))
        self._groups_button.setEnabled(bool(available))
        self._totals_action.setEnabled(bool(available))
        if not rows:
            self._last_rows = []
            self._table.setModel(None)
            self._table.setVisible(False)
            self._chart.set_data([], [])
            self._chart.setVisible(False)
            self._summary_label.setVisible(False)
            self._no_data_label.setVisible(True)
            self._no_data_label.setText(
                "Нет данных по применённым параметрам." if self._param_widgets else "В отчёте пока нет данных."
            )
            self._update_result_status()
            return

        columns = self._visible_columns(available)

        # Summary & footer aggregates (computed once per column)
        aggregates: dict[str, str] = {}
        summary_parts: list[str] = [f"Всего строк: {len(rows)}"]
        for c in columns:
            if c.aggregate:
                vals = [r.get(c.field, "") for r in rows]
                raw = self._format_column_value(c, calc_aggregate(vals, c.aggregate))
                aggregates[c.field] = raw
                agg_name = _AGGREGATE_LABELS.get(c.aggregate, c.aggregate)
                summary_parts.append(f"{c.title} ({agg_name}): {raw}")
        headers = [c.title for c in columns]
        alignments = [c.align for c in columns]
        data: list[list[str]] = []
        sort_rows: list[list[Any]] = []
        groups = self._active_groups(available)
        display_rows = group_report_rows(rows, columns, groups)
        for row in display_rows:
            formatted_row: list[str] = []
            srow: list[Any] = []
            for c in columns:
                if c.field == row.get("_group_field"):
                    label = row["_group_label"]
                    formatted_row.append(label)
                    srow.append(label)
                    continue
                val = row.get(c.field)
                parsed_num = _parse_numeric_str(val) if val is not None else None
                if parsed_num is not None:
                    srow.append(float(parsed_num))
                elif isinstance(val, datetime.datetime):
                    srow.append(val.timestamp())
                elif isinstance(val, datetime.date):
                    srow.append(val.toordinal())
                else:
                    srow.append(str(val).lower() if val is not None else "")

                s = str(val) if val is not None else ""
                formatted_row.append(self._format_column_value(c, s, parsed_num))
            data.append(formatted_row)
            sort_rows.append(srow)

        # Подвал с агрегатами
        any_agg = bool(aggregates)
        if any_agg:
            total_row = [
                aggregates.get(c.field, "") if c.aggregate else ("Итого" if c.field in groups[:1] else "")
                for c in columns
            ]
            total_sort = [None for _ in columns]
            data.append(total_row)
            sort_rows.append(total_sort)

        src_model = _TableModel(
            headers,
            data,
            raw_rows=display_rows,
            has_totals=any_agg,
            alignments=alignments,
            sort_rows=sort_rows,
        )
        header = self._table.horizontalHeader()
        self._displayed_columns = columns
        sortable = self._report_class.sortable and not groups
        sort_index = next((i for i, col in enumerate(columns) if col.field == self._sort_field), -1)
        with QSignalBlocker(header):
            self._table.setSortingEnabled(False)
            self._proxy.setSourceModel(src_model)
            self._table.setModel(self._proxy)
            header.setSortIndicator(sort_index if sortable else -1, self._sort_order)
            self._table.setSortingEnabled(sortable)
        self._proxy.sort(sort_index if sortable else -1, self._sort_order)
        header.setStretchLastSection(all(c.width != 0 for c in columns))
        for i, c in enumerate(columns):
            if c.width == 0:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
                if c.width > 0:
                    self._table.setColumnWidth(i, c.width)

        self._last_rows = list(rows)
        self._update_chart(rows)
        self._summary_label.setText(" | ".join(summary_parts))
        self._summary_label.setVisible(True)
        self._no_data_label.setVisible(False)
        self._table.setVisible(True)
        self._update_result_status()

    def _toggle_chart_visibility(self, checked: bool) -> None:
        self._chart.setVisible(checked and bool(self._chart._values))

    def _update_chart(self, rows: list[dict[str, Any]]) -> None:
        chart_cfg = getattr(self._report_class, "chart", None)
        if not isinstance(chart_cfg, dict):
            self._chart.set_data([], [])
            self._chart.setVisible(False)
            return
        label_col = chart_cfg.get("label", "")
        value_col = chart_cfg.get("value", "")
        agg = chart_cfg.get("aggregate", "sum") or "sum"
        if not label_col or not value_col:
            self._chart.set_data([], [])
            self._chart.setVisible(False)
            return
        buckets: dict[Any, tuple[str, list[Any]]] = {}
        for row in rows:
            raw_label = row.get(label_col)
            display = "—" if raw_label in (None, "") else str(raw_label)
            key = (row.get("_group_keys", {}).get(label_col, display), display)
            buckets.setdefault(key, (display, []))[1].append(row.get(value_col))
        labels: list[str] = []
        values: list[float] = []
        for display, bucket_vals in buckets.values():
            computed = calc_aggregate(bucket_vals, agg)
            if computed:
                try:
                    values.append(float(computed))
                    labels.append(display)
                except ValueError:
                    pass
        self._chart.set_data(labels, values)
        self._chart.setVisible(bool(labels) and self._chart_toggle.isChecked())

    def _remember_sort(self, index: int, order: Qt.SortOrder) -> None:
        if self._table.isSortingEnabled():
            self._sort_field = (
                self._displayed_columns[index].field if 0 <= index < len(self._displayed_columns) else None
            )
            self._sort_order = order

    def _export_rows(self) -> list[dict[str, Any]]:
        if not self._table.isSortingEnabled() or self._proxy.sortColumn() < 0:
            return self._last_rows
        return [
            self._last_rows[self._proxy.mapToSource(self._proxy.index(row, 0)).row()]
            for row in range(len(self._last_rows))
        ]

    def _export_csv(self) -> None:
        self._update_result_status()
        if not self._export_btn.isEnabled():
            return
        default_name = f"{self._report_class.__name__}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт в CSV", default_name, "CSV Files (*.csv);;All Files (*)"
        )
        if path:
            try:
                rows = self._export_rows()
                if (
                    self._selected_columns is None and not self._column_order
                    and self._group_order is None and not self._aggregates
                ):
                    self._report_class.to_csv(rows, path)
                else:
                    available = self._available_columns(self._last_rows)
                    columns = self._visible_columns(available)
                    self._report_class._write_csv(
                        rows, path, columns, self._active_groups(available),
                        include_totals=any(v is not None for v in self._aggregates.values()),
                    )
                QMessageBox.information(self, "Экспорт выполнен", f"Отчёт сохранён в {path}")
            except Exception as exc:
                show_ui_error(self, "Ошибка экспорта", exc)

    def _on_double_click(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        src_idx = self._proxy.mapToSource(index)
        row_idx = src_idx.row()
        model = self._proxy.sourceModel()
        if isinstance(model, _TableModel) and 0 <= row_idx < len(model._raw_rows):
            raw = model._raw_rows[row_idx]
            ent_cls = raw.get("_entity_class")
            ent_id = raw.get("_entity_id")
            if isinstance(ent_cls, type) and isinstance(ent_id, int):
                self.drill_down_requested.emit(ent_cls, ent_id)


# ── Simple table model ─────────────────────────────────────────────

class _TableModel(QAbstractTableModel):
    def __init__(
        self,
        headers: list[str],
        rows: list[list[str]],
        raw_rows: list[dict[str, Any]] | None = None,
        has_totals: bool = False,
        alignments: list[str] | None = None,
        sort_rows: list[list[Any]] | None = None,
    ) -> None:
        super().__init__()
        self._headers = headers
        self._rows = rows
        self._raw_rows = raw_rows or []
        self._has_totals = has_totals
        self._alignments = alignments or []
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

        if role == Qt.ItemDataRole.TextAlignmentRole:
            align_str = self._alignments[col_idx] if col_idx < len(self._alignments) else "left"
            v_flag = Qt.AlignmentFlag.AlignVCenter
            if align_str == "right":
                return int(Qt.AlignmentFlag.AlignRight | v_flag)
            if align_str == "center":
                return int(Qt.AlignmentFlag.AlignHCenter | v_flag)
            return int(Qt.AlignmentFlag.AlignLeft | v_flag)

        if role == Qt.ItemDataRole.FontRole:
            if self._is_total_row(index):
                f = QFont()
                f.setBold(True)
                return f
            if 0 <= row_idx < len(self._raw_rows):
                r = self._raw_rows[row_idx]
                style = str(r.get("_style", "")).lower()
                bold = bool(r.get("_bold")) or style in ("current", "active", "bold", "group")
                strike = bool(r.get("_strikeout")) or style in ("completed", "done")
                if bold or strike:
                    f = QFont()
                    if bold:
                        f.setBold(True)
                    if strike:
                        f.setStrikeOut(True)
                    return f

        if role == Qt.ItemDataRole.BackgroundRole and row_idx < len(self._raw_rows):
            if self._raw_rows[row_idx].get("_style") == "group":
                return QColor("#e3ecfa")

        if role == Qt.ItemDataRole.ForegroundRole:
            if not self._is_total_row(index) and 0 <= row_idx < len(self._raw_rows):
                r = self._raw_rows[row_idx]
                color = r.get("_color")
                if color:
                    return QColor(color)
                style = str(r.get("_style", "")).lower()
                if style in ("completed", "done"):
                    return QColor(128, 128, 128)
                if style == "group":
                    return QColor("#163761")
                if style in ("current", "active"):
                    return QColor(13, 110, 253)

        return None

    def _is_total_row(self, index: QModelIndex) -> bool:
        return self._has_totals and index.row() == len(self._rows) - 1

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Vertical:
            if self._has_totals and section == len(self._rows) - 1:
                return "Итого"
            return str(section + 1)
        return None
