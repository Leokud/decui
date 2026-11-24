"""Report parameter form + results table widget + CSV export."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from decui.reports import (
    BooleanParam,
    ChoiceParam,
    Column as ColumnMeta,
    DateParam,
    DecimalParam,
    IntegerParam,
    RefParam,
    Report,
    ReportParam,
    StringParam,
    _parse_numeric_str,
    calc_aggregate,
)
from decui.ui.editors import populate_ref_editor

if TYPE_CHECKING:
    from decui.orm import Database


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
    ) -> None:
        super().__init__(parent)
        self._report_class = report_class
        self._db = database
        self._param_widgets: dict[str, QWidget] = {}
        self._report_instance = report_class()
        self._last_rows: list[dict[str, Any]] = []
        self._proxy = _ReportSortProxy(self)
        self._proxy.setSortRole(Qt.ItemDataRole.UserRole)

        self._build_ui()
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
        title.setStyleSheet("font-size: 16px;")
        layout.addWidget(title)

        # Param form
        params = self._report_class._report_meta.params
        if params:
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            for name, param in params.items():
                editor = self._create_param_editor(param)
                self._param_widgets[name] = editor
                label = param.display_name
                if param.required:
                    label += " *"
                form.addRow(QLabel(label + ":"), editor)
            layout.addLayout(form)

        # Action bar
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._export_btn = QPushButton("Экспорт CSV")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_csv)
        btn_layout.addWidget(self._export_btn)

        generate_btn = QPushButton("Сформировать")
        generate_btn.clicked.connect(self._generate)
        btn_layout.addWidget(generate_btn)
        layout.addLayout(btn_layout)

        # Summary panel
        self._summary_label = QLabel()
        self._summary_label.setVisible(False)
        self._summary_label.setStyleSheet("padding: 8px; font-size: 13px;")
        layout.addWidget(self._summary_label)

        # Results table (takes all remaining vertical height cleanly)
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.setVisible(False)
        layout.addWidget(self._table, stretch=1)

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        self._no_data_label = QLabel("Нажмите «Сформировать» для вывода данных")
        self._no_data_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._no_data_label.setStyleSheet("font-style: italic; padding: 40px;")
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
            w.addItem("", None)
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

    # ------------------------------------------------------------------
    # Execute & Export
    # ------------------------------------------------------------------

    def _generate(self) -> None:
        params: dict[str, Any] = {}
        errors: list[str] = []
        for name, param in self._report_class._report_meta.params.items():
            widget = self._param_widgets[name]
            value = self._read_param(param, widget)
            if param.required and (value is None or value == ""):
                errors.append(f"Не заполнен параметр «{param.display_name}»")
            params[name] = value

        if errors:
            QMessageBox.warning(self, "Ошибка", "\n".join(errors))
            return

        try:
            rows = self._report_instance.execute(self._db, **params)
            self._show_results(rows)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Ошибка формирования отчёта: {exc}")

    def _show_results(self, rows: list[dict[str, Any]]) -> None:
        self._last_rows = list(rows)
        if not rows:
            self._table.setModel(None)
            self._table.setVisible(False)
            self._summary_label.setVisible(False)
            self._export_btn.setEnabled(False)
            self._no_data_label.setVisible(True)
            self._no_data_label.setText("Нет данных за выбранный период")
            return

        columns: list[ColumnMeta] = list(getattr(self._report_class, "columns", []))
        if not columns:
            columns = [
                ColumnMeta(k, k) for k in rows[0].keys() if not str(k).startswith("_")
            ]

        self._no_data_label.setVisible(False)
        self._table.setVisible(True)
        self._export_btn.setEnabled(True)

        # Summary
        summary_parts: list[str] = [f"Всего строк: {len(rows)}"]
        for c in columns:
            if c.aggregate:
                vals = [r.get(c.field, "") for r in rows]
                raw = calc_aggregate(vals, c.aggregate)
                if raw and c.format:
                    try:
                        raw = c.format.format(float(raw.replace(",", ".")))
                    except Exception:
                        pass
                if raw and c.prefix:
                    raw = c.prefix + raw
                if raw and c.suffix:
                    raw = raw + c.suffix
                agg_name = {
                    "sum": "Сумма",
                    "avg": "Среднее",
                    "count": "Кол-во",
                    "min": "Мин.",
                    "max": "Макс.",
                }.get(c.aggregate, c.aggregate)
                summary_parts.append(f"{c.title} ({agg_name}): {raw}")
        self._summary_label.setText(" | ".join(summary_parts))
        self._summary_label.setVisible(True)

        headers = [c.title for c in columns]
        alignments = [c.align for c in columns]
        data: list[list[str]] = []
        sort_rows: list[list[Any]] = []
        for row in rows:
            formatted_row: list[str] = []
            srow: list[Any] = []
            for c in columns:
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
                if s and c.format:
                    try:
                        s = c.format.format(float(parsed_num) if parsed_num is not None else float(s.replace(",", ".")))
                    except Exception:
                        pass
                if s and c.prefix:
                    s = c.prefix + s
                if s and c.suffix:
                    s = s + c.suffix
                formatted_row.append(s)
            data.append(formatted_row)
            sort_rows.append(srow)

        # Подвал с агрегатами
        total_row: list[str] = []
        total_sort: list[Any] = []
        any_agg = False
        for c in columns:
            if c.aggregate:
                any_agg = True
                vals = [r.get(c.field, "") for r in rows]
                raw = calc_aggregate(vals, c.aggregate)
                if raw and c.format:
                    try:
                        raw = c.format.format(float(raw.replace(",", ".")))
                    except Exception:
                        pass
                if raw and c.prefix:
                    raw = c.prefix + raw
                if raw and c.suffix:
                    raw = raw + c.suffix
                total_row.append(raw if raw else "")
            else:
                total_row.append("")
            total_sort.append(None)
        if any_agg:
            data.append(total_row)
            sort_rows.append(total_sort)

        src_model = _TableModel(
            headers,
            data,
            raw_rows=rows,
            has_totals=any_agg,
            alignments=alignments,
            sort_rows=sort_rows,
        )
        self._proxy.setSourceModel(src_model)
        self._table.setModel(self._proxy)
        for i, c in enumerate(columns):
            if c.width > 0:
                self._table.setColumnWidth(i, c.width)

    def _export_csv(self) -> None:
        if not self._last_rows:
            return
        default_name = f"{self._report_class.__name__}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт в CSV", default_name, "CSV Files (*.csv);;All Files (*)"
        )
        if path:
            try:
                self._report_class.to_csv(self._last_rows, path)
                QMessageBox.information(self, "Экспорт выполнен", f"Отчёт сохранён в {path}")
            except Exception as exc:
                QMessageBox.critical(self, "Ошибка экспорта", str(exc))

    def _on_double_click(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        src_idx = self._proxy.mapToSource(index)
        row_idx = src_idx.row()
        if 0 <= row_idx < len(self._last_rows):
            raw = self._last_rows[row_idx]
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

        if role == Qt.ItemDataRole.DisplayRole:
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
                bold = bool(r.get("_bold")) or style in ("current", "active", "bold")
                strike = bool(r.get("_strikeout")) or style in ("completed", "done")
                if bold or strike:
                    f = QFont()
                    if bold:
                        f.setBold(True)
                    if strike:
                        f.setStrikeOut(True)
                    return f

        if role == Qt.ItemDataRole.ForegroundRole:
            if not self._is_total_row(index) and 0 <= row_idx < len(self._raw_rows):
                r = self._raw_rows[row_idx]
                color = r.get("_color")
                if color:
                    return QColor(color)
                style = str(r.get("_style", "")).lower()
                if style in ("completed", "done"):
                    return QColor(128, 128, 128)
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
