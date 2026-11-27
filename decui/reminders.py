"""Reminder service and notification dialog for task and goal deadlines."""

from __future__ import annotations

import datetime
import html
import logging
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDateTimeEdit,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from decui.fields import DateField
from decui.ui.editors import show_ui_error

if TYPE_CHECKING:
    from decui.entity import Entity
    from decui.orm import Database


def celebrate_goal_completed(
    parent: QWidget | None = None,
    message: str = "Цель достигнута. Отличная работа!",
) -> None:
    """Display a celebratory message dialog upon completing a goal."""
    msg = QMessageBox(parent)
    msg.setWindowTitle("Поздравляем! 🎉")
    msg.setText(f"<h3>{html.escape(message)}</h3>")
    msg.setIcon(QMessageBox.Icon.Information)
    msg.setStandardButtons(QMessageBox.StandardButton.Ok)
    msg.exec()


def _resolve_field(entity: Entity, preferred: str, aliases: tuple[str, ...]) -> str:
    fields = entity._meta.fields
    if preferred in fields:
        return preferred
    for alias in aliases:
        if alias in fields and entity._data.get(alias) is not None:
            return alias
    for alias in aliases:
        if alias in fields:
            return alias
    return preferred


def _naive_local_dt(dt: Any) -> datetime.datetime | None:
    if dt is None:
        return None
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo is not None:
            return dt.astimezone().replace(tzinfo=None)
        return dt
    if isinstance(dt, datetime.date):
        return datetime.datetime.combine(dt, datetime.time(23, 59))
    return None


class ReminderDialog(QDialog):
    """Notification dialog displaying an approaching or overdue deadline."""

    def __init__(
        self,
        entity: Entity,
        database: Database,
        *,
        title_field: str = "name",
        date_field: str = "deadline",
        desc_field: str = "description",
        done_field: str = "is_completed",
        parent: QWidget | None = None,
        on_open: Any = None,
        on_postpone: Any = None,
        on_complete: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._entity = entity
        self._db = database
        self._title_field = _resolve_field(entity, title_field, ("name", "title", "number"))
        self._date_field = _resolve_field(entity, date_field, ("deadline", "due_date", "target_date", "date"))
        self._desc_field = _resolve_field(entity, desc_field, ("description", "note", "comment"))
        self._done_field = _resolve_field(entity, done_field, ("is_completed", "completed", "done", "is_done"))
        self._on_open = on_open
        self._on_postpone = on_postpone
        self._on_complete = on_complete

        self.setWindowTitle("Напоминание о сроке")
        self.setMinimumWidth(440)
        self._build_ui()
        self.resize(540, 330)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(22, 20, 22, 20)

        deadline = _naive_local_dt(self._entity._data.get(self._date_field))
        overdue = deadline is not None and deadline < datetime.datetime.now()
        header = QLabel("Срок истёк" if overdue else "Приближается срок выполнения")
        header.setProperty("heading", True)
        header.setWordWrap(True)
        layout.addWidget(header)

        title = str(self._entity._data.get(self._title_field) or "Задача")
        dt_val = self._entity._data.get(self._date_field)
        dt_str = dt_val.strftime("%d.%m.%Y %H:%M") if isinstance(dt_val, datetime.datetime) else (
            dt_val.strftime("%d.%m.%Y") if isinstance(dt_val, datetime.date) else "Срок не указан"
        )
        description = html.escape(str(self._entity._data.get(self._desc_field) or ""))
        details = QTextBrowser()
        details.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        details.setMinimumHeight(130)
        details.setHtml(
            f"<p><b>{html.escape(title)}</b></p>"
            f"<p>Срок: <b>{html.escape(dt_str)}</b></p>"
            f"<p>{description.replace(chr(10), '<br>')}</p>"
        )
        layout.addWidget(details, stretch=1)

        # Postpone editor section
        self._snooze_widget = QWidget()
        snooze_layout = QHBoxLayout(self._snooze_widget)
        snooze_layout.setContentsMargins(0, 0, 0, 0)
        snooze_layout.addWidget(QLabel("Новый срок:"))
        self._new_dt_edit = QDateTimeEdit()
        self._new_dt_edit.setCalendarPopup(True)
        self._new_dt_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        if isinstance(dt_val, (datetime.datetime, datetime.date)):
            curr_dt = (
                dt_val
                if isinstance(dt_val, datetime.datetime)
                else datetime.datetime.combine(dt_val, datetime.time(12, 0))
            )
            self._new_dt_edit.setDateTime(curr_dt + datetime.timedelta(days=1))
        else:
            self._new_dt_edit.setDateTime(datetime.datetime.now() + datetime.timedelta(days=1))
        snooze_layout.addWidget(self._new_dt_edit)

        btn_confirm_snooze = QPushButton("Сохранить срок")
        btn_confirm_snooze.clicked.connect(self._apply_postpone)
        snooze_layout.addWidget(btn_confirm_snooze)
        self._snooze_widget.setVisible(False)
        layout.addWidget(self._snooze_widget)

        # Action Buttons
        btn_layout = QGridLayout()
        btn_open = QPushButton("Открыть задачу")
        btn_open.clicked.connect(self._action_open)
        btn_layout.addWidget(btn_open, 0, 0)

        btn_postpone = QPushButton("Перенести срок")
        btn_postpone.clicked.connect(self._toggle_postpone)
        btn_layout.addWidget(btn_postpone, 0, 1)

        btn_complete = QPushButton("Выполнено ✓")
        btn_complete.setProperty("primary", True)
        btn_complete.clicked.connect(self._action_complete)
        btn_layout.addWidget(btn_complete, 1, 0)

        btn_close = QPushButton("Закрыть")
        btn_close.clicked.connect(self.reject)
        btn_layout.addWidget(btn_close, 1, 1)

        layout.addLayout(btn_layout)

    def _toggle_postpone(self) -> None:
        self._snooze_widget.setVisible(not self._snooze_widget.isVisible())

    def _apply_postpone(self) -> None:
        qdt = self._new_dt_edit.dateTime()
        new_dt = qdt.toPython()
        field_def = self._entity._meta.fields.get(self._date_field)
        if isinstance(field_def, DateField) and isinstance(new_dt, datetime.datetime):
            new_val: Any = new_dt.date()
        else:
            new_val = new_dt
        if not self._save_change(self._date_field, new_val):
            return
        if callable(self._on_postpone):
            self._on_postpone(self._entity)
        QMessageBox.information(self, "Срок обновлён", f"Срок перенесён на {new_val}")
        self.accept()

    def _action_open(self) -> None:
        self.accept()
        if callable(self._on_open):
            self._on_open(self._entity)

    def _action_complete(self) -> None:
        field = self._done_field
        value: Any = True
        if field not in self._entity._meta.fields:
            field, value = "status", "Завершена"
        if not self._save_change(field, value):
            return
        message = getattr(self._entity, "_save_message", None)
        if message:
            celebrate_goal_completed(self, message)
        if callable(self._on_complete):
            self._on_complete(self._entity)
        self.accept()


    def _save_change(self, field: str, value: Any) -> bool:
        try:
            with self._db.transaction():
                pk = self._entity._id
                current = self._db.get(type(self._entity), pk) if pk is not None else None
                if current is None:
                    raise ValueError("Запись была удалена.")
                fields = (field, self._date_field, self._done_field)
                if any(current._data.get(name) != self._entity._data.get(name) for name in fields):
                    raise ValueError("Запись была изменена. Откройте её, чтобы проверить новые данные.")
                current._data[field] = value
                self._db.save(current, validate=True)
            self._entity._data.update(current._data)
            self._entity._save_message = getattr(current, "_save_message", None)
            return True
        except Exception as exc:
            show_ui_error(self, "Не удалось сохранить", exc, warning=True)
            return False


class ReminderService(QObject):
    """Background service monitoring upcoming deadlines for registered entities."""

    reminder_triggered = Signal(object)
    open_entity_requested = Signal(object)
    data_changed = Signal()

    def __init__(
        self,
        database: Database,
        entity_classes: list[type],
        *,
        date_field: str = "deadline",
        done_field: str = "is_completed",
        title_field: str = "name",
        desc_field: str = "description",
        notify_before_minutes: int = 60,
        check_interval_ms: int = 30000,
        parent: QWidget | None = None,
        eligible: Callable[[Entity], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._eligible = eligible
        self._db = database
        self._entity_classes = entity_classes
        self._date_field = date_field
        self._done_field = done_field
        self._title_field = title_field
        self._desc_field = desc_field
        self._notify_before = datetime.timedelta(minutes=notify_before_minutes)
        self._parent_widget = parent
        self._dismissed_keys: dict[tuple[str, int], datetime.datetime] = {}
        self._open_keys: set[tuple[type, int]] = set()
        self._dialogs: set[ReminderDialog] = set()

        self._timer = QTimer(self)
        self._timer.setInterval(check_interval_ms)
        self._timer.timeout.connect(self.check_deadlines)

    def start(self) -> None:
        self._timer.start()
        QTimer.singleShot(1000, self, self._check_if_running)

    def stop(self) -> None:
        self._timer.stop()
        for dialog in tuple(self._dialogs):
            dialog.close()

    def _check_if_running(self) -> None:
        if self._timer.isActive():
            self.check_deadlines()

    def check_deadlines(self) -> list[Entity]:
        """Check all entities and trigger reminders for items approaching deadline."""
        approaching: list[Entity] = []
        now = datetime.datetime.now()
        horizon = now + self._notify_before

        for cls in self._entity_classes:
            try:
                items = self._db.list(cls)
            except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
                logging.getLogger(__name__).warning(
                    "Не удалось проверить сроки для %s", cls.__name__, exc_info=True,
                )
                continue

            for item in items:
                ent_id = getattr(item, "_id", None)
                if ent_id is None:
                    continue

                # Check completion
                is_done = False
                for k in (self._done_field, "completed", "done", "is_done"):
                    if item._data.get(k) is True:
                        is_done = True
                        break
                status_val = str(item._data.get("status", "")).lower()
                if status_val in ("завершена", "done", "completed", "выполнено"):
                    is_done = True
                if is_done or (self._eligible is not None and not self._eligible(item)):
                    continue

                # Check deadline
                resolved_dt_field = _resolve_field(
                    item, self._date_field, ("deadline", "due_date", "target_date", "date")
                )
                dt = _naive_local_dt(item._data.get(resolved_dt_field))
                if dt is None:
                    continue

                if dt <= horizon:
                    key = (cls.__name__, ent_id)
                    if (cls, ent_id) not in self._open_keys and self._dismissed_keys.get(key) != dt:
                        self._dismissed_keys[key] = dt
                        approaching.append(item)
                        self._trigger_reminder(item)

        return approaching

    def _on_entity_postponed(self, entity: Entity) -> None:
        if entity._id is not None:
            self._dismissed_keys.pop((type(entity).__name__, entity._id), None)
        self.data_changed.emit()

    def _on_entity_completed(self, entity: Entity) -> None:
        self.data_changed.emit()

    def _trigger_reminder(self, entity: Entity) -> None:
        self.reminder_triggered.emit(entity)
        dialog = ReminderDialog(
            entity,
            self._db,
            title_field=self._title_field,
            date_field=self._date_field,
            desc_field=self._desc_field,
            done_field=self._done_field,
            parent=self._parent_widget,
            on_open=self.open_entity_requested.emit,
            on_postpone=self._on_entity_postponed,
            on_complete=self._on_entity_completed,
        )
        if entity._id is None:
            return
        key = (type(entity), entity._id)
        self._open_keys.add(key)
        self._dialogs.add(dialog)

        def finished() -> None:
            self._open_keys.discard(key)
            self._dialogs.discard(dialog)

        dialog.finished.connect(finished)
        dialog.show()
