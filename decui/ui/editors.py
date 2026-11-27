from __future__ import annotations

import datetime
import logging
import sqlite3
import sys
import traceback
from decimal import Decimal
from typing import Any, cast

from PySide6.QtCore import QDate, QDateTime, QTime, Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDateTimeEdit,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QTimeEdit,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from decui.diagnostics import report_preview_exception
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

_NULL_SENTINEL = "—"
_ORIG_CRITICAL = QMessageBox.critical
_ORIG_WARNING = QMessageBox.warning

_SQL_TEXT_MARKERS = (
    "ошибка sql",
    "[sql:",
    "(sqlite3.",
    "constraint failed",
    "sqlalchemy",
    "operationalerror",
    "integrityerror",
    "database is locked",
    "database table is locked",
    "no such table",
    "no such column",
    "foreign key constraint",
)


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    return chain


def format_ui_error(exc: BaseException | str) -> str:
    """Convert raw SQLAlchemy/SQLite errors into user-friendly Russian text."""
    if isinstance(exc, str):
        raw = exc.strip()
        lower = raw.lower()
        if not any(marker in lower for marker in _SQL_TEXT_MARKERS):
            return raw or "Произошла непредвиденная ошибка."
        combined_lower = lower
    else:
        raw = str(exc).strip()
        lower = raw.lower()
        chain = _iter_exception_chain(exc)
        is_sql = isinstance(exc, (SQLAlchemyError, sqlite3.Error)) or any(
            marker in lower for marker in _SQL_TEXT_MARKERS
        )
        if not is_sql:
            return raw or f"Произошла ошибка ({type(exc).__name__})."
        combined_lower = "\n".join(str(item).lower() for item in chain)

    if "foreign key constraint" in combined_lower or "foreignkey" in combined_lower:
        return "Нельзя выполнить операцию: запись используется в других документах или справочниках."
    if "unique constraint" in combined_lower or "unique failed" in combined_lower:
        return "Запись с такими уникальными значениями уже существует."
    if "not null constraint" in combined_lower:
        return "Не заполнено обязательное поле."
    if "check constraint" in combined_lower:
        return "Значение не соответствует ограничениям базы данных."
    if (
        "database is locked" in combined_lower
        or "database table is locked" in combined_lower
        or "sqlite_busy" in combined_lower
    ):
        return "База данных временно занята другой операцией. Повторите попытку."
    if "attempt to write a readonly database" in combined_lower or "readonly database" in combined_lower:
        return "База данных открыта только для чтения."
    if "no such table" in combined_lower or "no such column" in combined_lower:
        return "Структура базы данных устарела или не соответствует приложению."
    return "Ошибка при обращении к базе данных. Подробности доступны в деталях ошибки."


def _error_details(exc: BaseException | str) -> str | None:
    parts: list[str] = []
    if isinstance(exc, BaseException):
        raw = str(exc).strip()
        if raw and raw != format_ui_error(exc):
            parts.append(raw)
        tb = traceback.format_exc()
        if not tb or tb.strip() == "NoneType: None":
            if exc.__traceback__ is not None:
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            else:
                tb = ""
        if tb and tb.strip() != "NoneType: None":
            parts.append(tb.strip())
    elif exc.strip() and exc.strip() != format_ui_error(exc):
        parts.append(exc.strip())
    return "\n\n".join(parts) if parts else None


def show_ui_error(
    parent: QWidget | None,
    title: str,
    exc: BaseException | str,
    *,
    prefix: str = "",
    warning: bool = False,
) -> None:
    """Show a user-friendly error dialog with technical details tucked away."""
    message = f"{prefix}{format_ui_error(exc)}"
    details = _error_details(exc)
    if sys.exc_info()[0] is not None:
        report_preview_exception()
    if details:
        logging.getLogger(__name__).warning("%s: %s", title, message)
    if warning and QMessageBox.warning is not _ORIG_WARNING:
        QMessageBox.warning(parent, title, message)
        return
    if not warning and QMessageBox.critical is not _ORIG_CRITICAL:
        QMessageBox.critical(parent, title, message)
        return
    msg = QMessageBox(parent)
    msg.setIcon(QMessageBox.Icon.Warning if warning else QMessageBox.Icon.Critical)
    msg.setWindowTitle(title)
    msg.setText(message)
    if details:
        msg.setDetailedText(details)
    msg.exec()


class _RefEditor(QComboBox):
    """Keep a stored key when the current user cannot select its record."""

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.preserved_id: int | None = None
        self.currentIndexChanged.connect(self._selection_changed)

    def set_reference(self, value: int | None, preserve_unavailable: bool) -> None:
        index = self.findData(value)
        self._preserve(value if index < 0 and preserve_unavailable else None)
        self.setCurrentIndex(index if self.preserved_id is not None else max(0, index))

    def _selection_changed(self, index: int) -> None:
        if index >= 0 and self.preserved_id is not None:
            self._preserve(None)

    def _preserve(self, value: int | None) -> None:
        self.preserved_id = value
        message = "Сохранённая ссылка недоступна для выбора. Можно выбрать замену." if value is not None else ""
        self.setPlaceholderText(message)
        self.setToolTip(message)
        self.setAccessibleDescription(message)
        if self.lineEdit() is not None:
            self.lineEdit().setPlaceholderText(message)


def create_editor(field: Field, parent: QWidget | None = None) -> QWidget:
    """Factory: returns the appropriate Qt editor widget for a field descriptor."""

    if isinstance(field, StringField):
        return _string_editor(field, parent)

    if isinstance(field, IntegerField):
        return _integer_editor(field, parent)

    if isinstance(field, DecimalField):
        return _decimal_editor(field, parent)

    if isinstance(field, BooleanField):
        return _boolean_editor(field, parent)

    if isinstance(field, DateField):
        return _date_editor(field, parent)

    if isinstance(field, DateTimeField):
        return _datetime_editor(field, parent)

    if isinstance(field, TimeField):
        return _time_editor(field, parent)

    if isinstance(field, ChoiceField):
        return _choice_editor(field, parent)

    if isinstance(field, RefField):
        return _ref_editor(field, parent)

    raise TypeError(f"No editor for field type: {type(field).__name__}")


def get_editor_value(widget: Any, field: Field, *, preserve_unavailable: bool = False) -> Any:
    """Read the current value from an editor widget."""
    if isinstance(field, StringField):
        if getattr(field, "multiline", False) or isinstance(widget, QPlainTextEdit):
            w_plain = cast(QPlainTextEdit, widget)
            text = w_plain.toPlainText()
            return text if text else None
        w = cast(QLineEdit, widget)
        text = w.text()
        return text if text else None

    if isinstance(field, IntegerField):
        w = cast(QSpinBox, widget)
        if w.specialValueText() == _NULL_SENTINEL and w.value() == w.minimum():
            return None
        return w.value()

    if isinstance(field, DecimalField):
        w = cast(QDoubleSpinBox, widget)
        if w.specialValueText() == _NULL_SENTINEL and w.value() <= w.minimum() + 1e-9:
            return None
        return Decimal(f"{w.value():.{field.scale}f}")

    if isinstance(field, BooleanField):
        cb = widget if isinstance(widget, QCheckBox) else widget.findChild(QCheckBox)
        return cb.isChecked() if cb is not None else False

    if isinstance(field, DateField):
        w = cast(QDateEdit, widget)
        qdate = w.date()
        if qdate.isNull():
            return None
        return datetime.date(qdate.year(), qdate.month(), qdate.day())

    if isinstance(field, DateTimeField):
        w = cast(QDateTimeEdit, widget)
        qdt = w.dateTime()
        if qdt.isNull():
            return None
        dt = qdt.toPython()
        return dt if isinstance(dt, datetime.datetime) else None

    if isinstance(field, TimeField):
        w = cast(QTimeEdit, widget)
        qt = w.time()
        if qt.isNull():
            return None
        return datetime.time(qt.hour(), qt.minute())

    if isinstance(field, ChoiceField):
        if isinstance(widget, QComboBox):
            val = widget.currentData(Qt.ItemDataRole.UserRole)
            return val or None
        if isinstance(widget, QGroupBox):
            selected: list[str] = []
            for cb in widget.findChildren(QCheckBox):
                if cb.isChecked():
                    selected.append(cb.property("_choice_value") or "")
            return field.separator.join(selected) if selected else None
        return None

    if isinstance(field, RefField):
        w = cast(QComboBox, widget)
        if preserve_unavailable and isinstance(w, _RefEditor) and w.currentIndex() < 0 and not w.currentText():
            return w.preserved_id
        if w.isEditable():
            text = w.currentText()
            if not text.strip():
                return None
            if text != w.itemText(w.currentIndex()):
                return w.itemData(w.findText(text), Qt.ItemDataRole.UserRole)
        return w.currentData(Qt.ItemDataRole.UserRole)

    raise TypeError(f"No reader for field type: {type(field).__name__}")


def set_editor_value(widget: Any, field: Field, value: Any, *, preserve_unavailable: bool = False) -> None:
    """Set the value of an editor widget from a Python value."""
    if isinstance(field, StringField):
        val_str = str(value) if value is not None else ""
        if getattr(field, "multiline", False) or isinstance(widget, QPlainTextEdit):
            w_plain = cast(QPlainTextEdit, widget)
            w_plain.setPlainText(val_str)
        else:
            w = cast(QLineEdit, widget)
            w.setText(val_str)

    elif isinstance(field, IntegerField):
        w = cast(QSpinBox, widget)
        if value is not None:
            clamped = int(value)
            if field.min_value is not None:
                clamped = max(clamped, field.min_value)
            if field.max_value is not None:
                clamped = min(clamped, field.max_value)
            w.setValue(clamped)
        elif field.default is not None:
            d = field.default() if callable(field.default) else field.default
            w.setValue(int(d))
        elif w.specialValueText() == _NULL_SENTINEL:
            w.setValue(w.minimum())
        else:
            w.setValue(max(0, w.minimum()))

    elif isinstance(field, DecimalField):
        w = cast(QDoubleSpinBox, widget)
        if value is not None:
            val = value if isinstance(value, Decimal) else Decimal(str(value))
            if field.min_value is not None:
                val = max(val, field.min_value)
            if field.max_value is not None:
                val = min(val, field.max_value)
            w.setValue(float(val))
        elif field.default is not None:
            d = field.default() if callable(field.default) else field.default
            w.setValue(float(d))
        elif w.specialValueText() == _NULL_SENTINEL:
            w.setValue(w.minimum())
        else:
            w.setValue(0.0)

    elif isinstance(field, BooleanField):
        cb = widget if isinstance(widget, QCheckBox) else widget.findChild(QCheckBox)
        if cb is not None:
            cb.setChecked(bool(value) if value is not None else False)

    elif isinstance(field, DateField):
        w = cast(QDateEdit, widget)
        resolved = (
            field.default() if callable(field.default) else field.default
        ) if value is None and field.default is not None else value
        if isinstance(resolved, datetime.datetime):
            w.setDate(QDate(resolved.year, resolved.month, resolved.day))
        elif isinstance(resolved, datetime.date):
            w.setDate(QDate(resolved.year, resolved.month, resolved.day))
        else:
            w.setDate(QDate.currentDate())

    elif isinstance(field, DateTimeField):
        w = cast(QDateTimeEdit, widget)
        resolved = (
            field.default() if callable(field.default) else field.default
        ) if value is None and field.default is not None else value
        if isinstance(resolved, datetime.datetime):
            w.setDateTime(resolved.replace(second=0, microsecond=0))
        elif isinstance(resolved, datetime.date):
            w.setDateTime(datetime.datetime.combine(resolved, datetime.time(0, 0)))
        else:
            w.setDateTime(datetime.datetime.now().replace(second=0, microsecond=0))

    elif isinstance(field, TimeField):
        w = cast(QTimeEdit, widget)
        resolved = (
            field.default() if callable(field.default) else field.default
        ) if value is None and field.default is not None else value
        parsed = field.to_python(resolved) if isinstance(resolved, str) and resolved else resolved
        if isinstance(parsed, datetime.time):
            w.setTime(QTime(parsed.hour, parsed.minute))
        elif isinstance(parsed, datetime.datetime):
            w.setTime(QTime(parsed.hour, parsed.minute))
        else:
            w.setTime(QTime(9, 0))

    elif isinstance(field, ChoiceField):
        if isinstance(widget, QComboBox):
            idx = widget.findData(value, Qt.ItemDataRole.UserRole)
            widget.setCurrentIndex(idx if idx >= 0 else 0)
        elif isinstance(widget, QGroupBox):
            selected = field.to_list(value) if value else []
            for cb in widget.findChildren(QCheckBox):
                cb.setChecked(cb.property("_choice_value") in selected)

    elif isinstance(field, RefField):
        if isinstance(widget, _RefEditor):
            widget.set_reference(value, preserve_unavailable)
            return
        w = cast(QComboBox, widget)
        idx = w.findData(value, Qt.ItemDataRole.UserRole)
        if idx >= 0:
            w.setCurrentIndex(idx)
        elif value is None:
            w.setCurrentIndex(0)


# -- Concrete editor factories ----------------------------------------

def _string_editor(field: StringField, parent: QWidget | None = None) -> QWidget:
    if getattr(field, "multiline", False):
        plain_edit = QPlainTextEdit(parent)
        plain_edit.setMinimumHeight(70)
        plain_edit.setMaximumHeight(112)
        plain_edit.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        plain_edit.setReadOnly(field.read_only)
        return plain_edit
    w = QLineEdit(parent)
    w.setMaxLength(field.max_length or 255)
    w.setReadOnly(field.read_only)
    return w


def _integer_editor(field: IntegerField, parent: QWidget | None = None) -> QSpinBox:
    w = QSpinBox(parent)
    lo = field.min_value if field.min_value is not None else -10**9
    hi = field.max_value if field.max_value is not None else 10**9
    nullable = not field.required and field.default is None
    if nullable:
        w.setRange(lo - 1, hi)
        w.setSpecialValueText(_NULL_SENTINEL)
        w.setValue(lo - 1)
    else:
        w.setRange(lo, hi)
        if field.default is not None:
            w.setValue(field.default() if callable(field.default) else field.default)
        else:
            w.setValue(max(0, lo))
    w.setReadOnly(field.read_only)
    return w


def _decimal_editor(
    field: DecimalField, parent: QWidget | None = None
) -> QDoubleSpinBox:
    w = QDoubleSpinBox(parent)
    w.setDecimals(field.scale)
    default_max = float(10 ** max(1, field.precision - field.scale))
    lo = float(field.min_value) if field.min_value is not None else -default_max
    hi = float(field.max_value) if field.max_value is not None else default_max
    if field.currency:
        w.setSuffix(f" {field.currency}")

    nullable = not field.required and field.default is None
    if nullable:
        step = 10 ** (-field.scale)
        w.setRange(lo - step, hi)
        w.setSpecialValueText(_NULL_SENTINEL)
        w.setValue(lo - step)
    else:
        w.setRange(lo, hi)
        if field.default is not None:
            w.setValue(float(field.default() if callable(field.default) else field.default))
        else:
            w.setValue(max(0.0, lo))
    w.setReadOnly(field.read_only)
    return w


def _boolean_editor(
    field: BooleanField, parent: QWidget | None = None
) -> QCheckBox:
    w = QCheckBox(parent)
    w.setEnabled(not field.read_only)
    return w


def _date_editor(field: DateField, parent: QWidget | None = None) -> QDateEdit:
    w = QDateEdit(parent)
    w.setCalendarPopup(True)
    w.setDisplayFormat("dd.MM.yyyy")
    w.setReadOnly(field.read_only)
    return w


def _datetime_editor(
    field: DateTimeField, parent: QWidget | None = None
) -> QDateTimeEdit:
    w = QDateTimeEdit(parent)
    w.setCalendarPopup(True)
    w.setDisplayFormat("dd.MM.yyyy HH:mm")
    w.setReadOnly(field.read_only)
    return w


def _time_editor(field: TimeField, parent: QWidget | None = None) -> QTimeEdit:
    w = QTimeEdit(parent)
    w.setDisplayFormat("HH:mm")
    w.setReadOnly(field.read_only)
    if field.default is not None:
        d = field.default() if callable(field.default) else field.default
        if isinstance(d, datetime.time):
            w.setTime(QTime(d.hour, d.minute))
    return w


def _choice_editor(field: ChoiceField, parent: QWidget | None = None) -> QWidget:
    if not getattr(field, "multi", True):
        combo = QComboBox(parent)
        combo.addItem("", None)
        for value, label in field.choices:
            combo.addItem(label, value)
        combo.setEnabled(not field.read_only)
        return combo

    gb = QGroupBox(parent)
    layout = QHBoxLayout(gb)
    layout.setContentsMargins(0, 0, 0, 0)
    for value, label in field.choices:
        cb = QCheckBox(label)
        cb.setProperty("_choice_value", value)
        cb.setEnabled(not field.read_only)
        layout.addWidget(cb)
    layout.addStretch()
    return gb


def _ref_editor(field: RefField, parent: QWidget | None = None) -> QComboBox:
    w = _RefEditor(parent)
    w.setEnabled(not field.read_only)
    if not field.read_only:
        w.setEditable(True)
        w.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        completer = QCompleter(w.model(), w)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        w.setCompleter(completer)
    return w


def populate_ref_editor(
    combo: QComboBox,
    items: list[Any],
    display_field: str | None = None,
) -> None:
    """Fill a Ref combo-box with entity instances, preserving selection."""
    current_id = combo.currentData(Qt.ItemDataRole.UserRole)
    signals_were_blocked = combo.blockSignals(True)
    combo.clear()
    combo.addItem("", None)
    for item in items:
        if display_field and hasattr(item, display_field):
            raw = getattr(item, display_field)
            label = str(raw) if raw is not None else str(item)
        else:
            label = str(item)
        combo.addItem(label, item._id)
    if combo.isEditable() and combo.completer() is not None:
        combo.completer().setModel(combo.model())
    if current_id is not None:
        idx = combo.findData(current_id, Qt.ItemDataRole.UserRole)
        if idx >= 0:
            combo.setCurrentIndex(idx)
    combo.blockSignals(signals_were_blocked)
