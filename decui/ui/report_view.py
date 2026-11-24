"""Report parameter form + results table widget."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from decui.reports import (
    BooleanParam,
    Column as ColumnMeta,
    DateParam,
    DecimalParam,
    IntegerParam,
    RefParam,
    Report,
    ReportParam,
    StringParam,
    calc_aggregate,
)
from decui.ui.editors import populate_ref_editor

if TYPE_CHECKING:
    from decui.orm import Database


class ReportView(QScrollArea):
    """Auto-generated report UI: parameter form + execute button + results table."""

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

        self.setWidgetResizable(True)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 16, 16, 16)

        # Title
        title = QLabel(f"<b>Отчёт: {self._report_class._report_meta.title}</b>")
        title.setStyleSheet("font-size: 16px;")  # only size, color inherited from theme
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

        # Button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        generate_btn = QPushButton("Сформировать")
        generate_btn.clicked.connect(self._generate)
        btn_layout.addWidget(generate_btn)
        layout.addLayout(btn_layout)

        # Summary panel (hidden until data loaded)
        self._summary_label = QLabel()
        self._summary_label.setVisible(False)
        self._summary_label.setStyleSheet(
            "padding: 8px; font-size: 13px;"
        )
        layout.addWidget(self._summary_label)

        # Results table
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        layout.addWidget(self._table)

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)

        self._no_data_label = QLabel("Нажмите «Сформировать» для вывода данных")
        self._no_data_label.setAlignment(Qt.AlignCenter)
        self._no_data_label.setStyleSheet("font-style: italic; padding: 40px;")
        layout.addWidget(self._no_data_label)

    # ------------------------------------------------------------------
    # Param editor factory
    # ------------------------------------------------------------------

    def _create_param_editor(self, param: ReportParam) -> QWidget:
        if isinstance(param, StringParam):
            w = QLineEdit()
            w.setPlaceholderText(param.display_name)
            return w

        if isinstance(param, IntegerParam):
            w = QSpinBox()
            w.setRange(param.min_value, param.max_value)
            w.setValue(param.default)
            return w

        if isinstance(param, DecimalParam):
            w = QDoubleSpinBox()
            w.setDecimals(param.scale)
            w.setRange(-999999999.0, 999999999.0)
            w.setValue(param.default)
            return w

        if isinstance(param, BooleanParam):
            w = QCheckBox(param.display_name)
            w.setChecked(param.default)
            return w

        if isinstance(param, DateParam):
            w = QDateEdit()
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd")
            default_date = None
            if param.default is not None:
                d = param.default() if callable(param.default) else param.default
                default_date = d
            elif param.required:
                default_date = datetime.date.today()
            if default_date is not None:
                w.setDate(QDate(default_date.year, default_date.month, default_date.day))
            else:
                w.setDate(QDate())
            return w

        if isinstance(param, RefParam):
            w = QComboBox()
            w.addItem("", None)
            target = param.resolve_target()
            items = self._db.list(target)
            populate_ref_editor(w, items, param.representation_field)
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
            w = cast(QDateEdit, widget)
            qd = w.date()
            if qd.isNull():
                return None
            return datetime.date(qd.year(), qd.month(), qd.day())

        if isinstance(param, RefParam):
            w = cast(QComboBox, widget)
            return w.currentData(Qt.ItemDataRole.UserRole)

        return None


    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def _generate(self) -> None:
        from PySide6.QtWidgets import QMessageBox

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
        from PySide6.QtWidgets import QMessageBox

        if not rows:
            self._summary_label.setVisible(False)
            self._no_data_label.setVisible(True)
            self._no_data_label.setText("Нет данных за выбранный период")
            QMessageBox.information(self, "Отчёт", "Нет данных за выбранный период")
            return

        columns: list[ColumnMeta] = getattr(
            self._report_class, "columns", []
        )
        if not columns:
            columns = [
                ColumnMeta(k, k) for k in rows[0].keys()
            ]

        self._no_data_label.setVisible(False)

        # Summary
        summary_parts: list[str] = [f"Всего строк: {len(rows)}"]
        for c in columns:
            if c.aggregate:
                vals = [str(r.get(c.field, "")) for r in rows]
                raw = calc_aggregate(vals, c.aggregate)
                if raw and c.format:
                    try:
                        raw = c.format.format(float(raw.replace(",", ".")))
                    except Exception:
                        pass
                if raw and c.suffix:
                    raw = raw + c.suffix
                agg_name = {"sum": "Сумма", "avg": "Среднее", "count": "Кол-во",
                            "min": "Мин.", "max": "Макс."}.get(c.aggregate, c.aggregate)
                summary_parts.append(f"{c.title}: {raw}")
        self._summary_label.setText(" | ".join(summary_parts))
        self._summary_label.setVisible(True)

        headers = [c.title for c in columns]
        data: list[list[str]] = []
        for row in rows:
            formatted_row: list[str] = []
            for c in columns:
                val = row.get(c.field, "")
                s = str(val) if val is not None else ""
                if s and c.format:
                    try:
                        s = c.format.format(float(s.replace(",", ".")))
                    except Exception:
                        pass
                if s and c.prefix:
                    s = c.prefix + s
                if s and c.suffix:
                    s = s + c.suffix
                formatted_row.append(s)
            data.append(formatted_row)

        # Подвал с агрегатами
        total_row: list[str] = []
        any_agg = False
        for c in columns:
            if c.aggregate:
                any_agg = True
                vals = [str(r.get(c.field, "")) for r in rows]
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
        if any_agg:
            data.append(total_row)

        model = _TableModel(headers, data, raw_rows=rows, has_totals=any_agg)
        self._table.setModel(model)


# ── Simple table model ─────────────────────────────────────────────

from PySide6.QtCore import QAbstractTableModel, QModelIndex  # noqa: E402


class _TableModel(QAbstractTableModel):
    def __init__(
        self,
        headers: list[str],
        rows: list[list[str]],
        raw_rows: list[dict[str, Any]] | None = None,
        has_totals: bool = False,
    ) -> None:
        super().__init__()
        self._headers = headers
        self._rows = rows
        self._raw_rows = raw_rows or []
        self._has_totals = has_totals

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

        if role == Qt.ItemDataRole.FontRole:
            from PySide6.QtGui import QFont
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
            if 0 <= row_idx < len(self._raw_rows):
                from PySide6.QtGui import QColor
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
        if role == Qt.DisplayRole and orientation == Qt.Orientation.Vertical:
            if self._has_totals and section == len(self._rows) - 1:
                return "Итого"
            return str(section + 1)
        return None
