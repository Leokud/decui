from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, cast

from PySide6.QtCore import QDate, QDateTime, QTime, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QSpinBox,
    QTimeEdit,
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
    StringField,
    TimeField,
)


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


def get_editor_value(widget: Any, field: Field) -> Any:
    """Read the current value from an editor widget.

    Type: parameter is typed ``Any`` because the concrete widget subclass
    is determined by *field* at runtime, not by the static type system.
    """
    if isinstance(field, StringField):
        w = cast(QLineEdit, widget)
        text = w.text()
        return text if text else None

    if isinstance(field, IntegerField):
        w = cast(QSpinBox, widget)
        return w.value()

    if isinstance(field, DecimalField):
        w = cast(QDoubleSpinBox, widget)
        return Decimal(str(w.value()))

    if isinstance(field, BooleanField):
        w = cast(QCheckBox, widget)
        return w.isChecked()

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
        # read checkboxes, join with separator
        if isinstance(widget, QGroupBox):
            selected: list[str] = []
            for cb in widget.findChildren(QCheckBox):
                if cb.isChecked():
                    selected.append(cb.property("_choice_value") or "")
            return field.separator.join(selected) if selected else None
        return None

    if isinstance(field, RefField):
        w = cast(QComboBox, widget)
        return w.currentData(Qt.ItemDataRole.UserRole) or None

    raise TypeError(f"No reader for field type: {type(field).__name__}")


def set_editor_value(widget: Any, field: Field, value: Any) -> None:
    """Set the value of an editor widget from a Python value.

    Same typing reasoning as ``get_editor_value``.
    """
    if isinstance(field, StringField):
        w = cast(QLineEdit, widget)
        w.setText(str(value) if value is not None else "")

    elif isinstance(field, IntegerField):
        w = cast(QSpinBox, widget)
        if value is not None:
            clamped = value
            if field.min_value is not None:
                clamped = max(clamped, field.min_value)
            if field.max_value is not None:
                clamped = min(clamped, field.max_value)
            w.setValue(clamped)
        elif field.default is not None:
            d = field.default() if callable(field.default) else field.default
            w.setValue(d)
        else:
            w.setValue(w.minimum())

    elif isinstance(field, DecimalField):
        w = cast(QDoubleSpinBox, widget)
        if value is not None:
            from decimal import Decimal as PyDec
            val = value if isinstance(value, PyDec) else PyDec(str(value))
            if field.min_value is not None:
                val = max(val, field.min_value)
            if field.max_value is not None:
                val = min(val, field.max_value)
            w.setValue(float(val))
        elif field.default is not None:
            d = field.default() if callable(field.default) else field.default
            w.setValue(float(d))
        else:
            w.setValue(0.0)

    elif isinstance(field, BooleanField):
        w = cast(QCheckBox, widget)
        w.setChecked(bool(value) if value is not None else False)

    elif isinstance(field, DateField):
        w = cast(QDateEdit, widget)
        if isinstance(value, datetime.date):
            w.setDate(QDate(value.year, value.month, value.day))
        else:
            w.setDate(QDate.currentDate())

    elif isinstance(field, DateTimeField):
        w = cast(QDateTimeEdit, widget)
        if isinstance(value, datetime.datetime):
            w.setDateTime(value)
        else:
            w.setDateTime(datetime.datetime.now())

    elif isinstance(field, TimeField):
        w = cast(QTimeEdit, widget)
        if isinstance(value, datetime.time):
            w.setTime(QTime(value.hour, value.minute))
        elif value:
            w.setTime(QTime(9, 0))
        else:
            w.setTime(QTime(9, 0))

    elif isinstance(field, ChoiceField):
        if isinstance(widget, QGroupBox):
            selected = field.to_list(value) if value else []
            for cb in widget.findChildren(QCheckBox):
                cb.setChecked(cb.property("_choice_value") in selected)

    elif isinstance(field, RefField):
        w = cast(QComboBox, widget)
        idx = w.findData(value, Qt.ItemDataRole.UserRole)
        if idx >= 0:
            w.setCurrentIndex(idx)


# -- Concrete editor factories ----------------------------------------

def _string_editor(field: StringField, parent: QWidget | None = None) -> QLineEdit:
    w = QLineEdit(parent)
    w.setMaxLength(field.max_length or 255)
    w.setReadOnly(field.read_only)
    return w


def _integer_editor(field: IntegerField, parent: QWidget | None = None) -> QSpinBox:
    w = QSpinBox(parent)
    lo = field.min_value if field.min_value is not None else -10**9
    hi = field.max_value if field.max_value is not None else 10**9
    w.setRange(lo, hi)
    w.setReadOnly(field.read_only)
    w.setSpecialValueText("")
    if field.default is not None:
        w.setValue(field.default() if callable(field.default) else field.default)
    return w


def _decimal_editor(
    field: DecimalField, parent: QWidget | None = None
) -> QDoubleSpinBox:
    w = QDoubleSpinBox(parent)
    w.setDecimals(field.scale)
    default_max = 10 ** max(1, field.precision - field.scale)
    lo = float(field.min_value) if field.min_value is not None else (
        0.0 if field.default is not None else -default_max
    )
    hi = float(field.max_value) if field.max_value is not None else default_max
    w.setRange(lo, hi)
    w.setReadOnly(field.read_only)
    w.setSpecialValueText("")
    if field.default is not None:
        w.setValue(float(field.default() if callable(field.default) else field.default))
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


def _choice_editor(field: ChoiceField, parent: QWidget | None = None) -> QGroupBox:
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
    w = QComboBox(parent)
    w.setEnabled(not field.read_only)
    return w


def populate_ref_editor(
    combo: QComboBox,
    items: list[Any],
    display_field: str | None = None,
) -> None:
    """Fill a Ref combo-box with entity instances, preserving selection."""
    current_id = combo.currentData(Qt.ItemDataRole.UserRole)
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("", None)
    for item in items:
        if display_field and hasattr(item, display_field):
            label = str(getattr(item, display_field))
        else:
            label = repr(item)
        combo.addItem(label, item._id)
    if current_id is not None:
        idx = combo.findData(current_id, Qt.ItemDataRole.UserRole)
        if idx >= 0:
            combo.setCurrentIndex(idx)
    combo.blockSignals(False)
