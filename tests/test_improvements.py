from __future__ import annotations

import datetime
from decimal import Decimal
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QPlainTextEdit, QApplication

from decui import (
    Database,
    MasterData,
    String,
    DateTime,
    Boolean,
    master_data,
    ReminderService,
    ReminderDialog,
    celebrate_goal_completed,
)
from decui.ui.editors import create_editor, get_editor_value, set_editor_value
from decui.ui.listview import (
    DEFAULT_ACTIONS,
    ListView,
    _ACTION_LABELS,
    _TableModel as ListTableModel,
)
from decui.ui.report_view import _TableModel as ReportTableModel


# Ensure QApplication is initialized for offscreen Qt tests
@pytest.fixture(scope="session", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@master_data
class OlympiadTask(MasterData):
    name = String(required=True)
    description = String(multiline=True)
    deadline = DateTime(required=True)
    is_completed = Boolean(default=False)


@pytest.fixture
def db():
    return Database.memory()


# ── 1. Multiline text support ──────────────────────────────────────────

def test_multiline_field_and_editor():
    """StringField with multiline=True creates QPlainTextEdit and stores multiline text."""
    desc_field = OlympiadTask._meta.fields["description"]
    assert desc_field.multiline is True

    # Test editor widget creation
    editor = create_editor(desc_field)
    assert isinstance(editor, QPlainTextEdit)

    # Test set and get editor value
    sample_text = "Линия 1\nЛиния 2\nЛиния 3"
    set_editor_value(editor, desc_field, sample_text)
    assert get_editor_value(editor, desc_field) == sample_text

    # Clear value
    set_editor_value(editor, desc_field, "")
    assert get_editor_value(editor, desc_field) is None


def test_multiline_orm_persistence(db):
    """Multiline text is stored into and retrieved from the database."""
    multiline_content = "Первая строка описания.\nВторая строка с подробностями.\nТретья строка."
    task = OlympiadTask(
        name="Написать эссе",
        description=multiline_content,
        deadline=datetime.datetime(2026, 12, 31, 23, 59),
    )
    task.save(db)
    assert task._id is not None

    loaded = OlympiadTask.get(db, task._id)
    assert loaded is not None
    assert loaded.description == multiline_content


# ── 2. Copy action in ListView ─────────────────────────────────────────

def test_listview_copy_action(db):
    """ListView includes Copy action and emits copy_requested."""
    t1 = OlympiadTask(
        name="Задача 1",
        description="Описание задачи 1",
        deadline=datetime.datetime(2026, 11, 30, 18, 0),
    )
    t1.save(db)

    lv = ListView(OlympiadTask, db)
    assert "Copy" in _ACTION_LABELS
    assert _ACTION_LABELS["Copy"] == "Копировать"
    assert "Copy" in DEFAULT_ACTIONS

    # Verify copy_requested signal
    received = []
    lv.copy_requested.connect(lambda e: received.append(e))

    # Select the first row
    lv._table.setCurrentIndex(lv._table.model().index(0, 0))
    lv._on_action("Copy")

    assert len(received) == 1
    assert received[0]._id == t1._id
    assert received[0].name == "Задача 1"


# ── 3. Completed item styling in ListView ──────────────────────────────

def test_listview_completed_styling(db):
    """Completed tasks are displayed with strikeout font and gray color in ListView."""
    t_active = OlympiadTask(
        name="Активная задача",
        deadline=datetime.datetime(2026, 12, 1, 10, 0),
        is_completed=False,
    )
    t_done = OlympiadTask(
        name="Завершённая задача",
        deadline=datetime.datetime(2026, 11, 20, 10, 0),
        is_completed=True,
    )
    db.seed([t_active, t_done])

    headers = ["Имя", "Срок", "Выполнено"]
    rows = [["Активная задача", "2026-12-01", "False"], ["Завершённая задача", "2026-11-20", "True"]]
    model = ListTableModel(headers, rows, [t_active, t_done])

    # Active task should not have strikeout
    idx_active = model.index(0, 0)
    font_active = model.data(idx_active, Qt.ItemDataRole.FontRole)
    assert font_active is None or not font_active.strikeOut()

    # Completed task must have strikeout and gray color
    idx_done = model.index(1, 0)
    font_done = model.data(idx_done, Qt.ItemDataRole.FontRole)
    assert isinstance(font_done, QFont)
    assert font_done.strikeOut() is True

    color_done = model.data(idx_done, Qt.ItemDataRole.ForegroundRole)
    assert isinstance(color_done, QColor)
    assert color_done.red() == 128 and color_done.green() == 128 and color_done.blue() == 128


# ── 4. ReportView row styling ──────────────────────────────────────────

def test_report_view_styling():
    """ReportView formats rows based on _style (_strikeout, _bold, _color)."""
    headers = ["Этап", "Срок", "Статус"]
    table_data = [
        ["Этап 1", "2026-11-01", "Выполнен"],
        ["Этап 2", "2026-11-15", "В процессе"],
        ["Этап 3", "2026-12-01", "Запланирован"],
    ]
    raw_rows = [
        {"Этап": "Этап 1", "Срок": "2026-11-01", "_style": "completed"},
        {"Этап": "Этап 2", "Срок": "2026-11-15", "_style": "current"},
        {"Этап": "Этап 3", "Срок": "2026-12-01", "_style": "future"},
    ]

    model = ReportTableModel(headers, table_data, raw_rows=raw_rows)

    # Completed row: strikethrough + gray
    f1 = model.data(model.index(0, 0), Qt.ItemDataRole.FontRole)
    assert isinstance(f1, QFont)
    assert f1.strikeOut() is True
    c1 = model.data(model.index(0, 0), Qt.ItemDataRole.ForegroundRole)
    assert isinstance(c1, QColor)
    assert c1 == QColor(128, 128, 128)

    # Current row: bold
    f2 = model.data(model.index(1, 0), Qt.ItemDataRole.FontRole)
    assert isinstance(f2, QFont)
    assert f2.bold() is True

    # Future row: standard
    f3 = model.data(model.index(2, 0), Qt.ItemDataRole.FontRole)
    assert f3 is None


# ── 5. ReminderService and ReminderDialog ──────────────────────────────

def test_reminder_service_detection(db):
    """ReminderService identifies upcoming deadlines and triggers reminders."""
    now = datetime.datetime.now()
    # Task due in 20 minutes (within 60m notice horizon)
    approaching = OlympiadTask(
        name="Срочная задача",
        description="Нужно сделать срочно!",
        deadline=now + datetime.timedelta(minutes=20),
        is_completed=False,
    )
    # Task due next month (outside horizon)
    far_away = OlympiadTask(
        name="Далёкая задача",
        deadline=now + datetime.timedelta(days=30),
        is_completed=False,
    )
    # Task already done
    done_task = OlympiadTask(
        name="Сделанная задача",
        deadline=now + datetime.timedelta(minutes=5),
        is_completed=True,
    )
    db.seed([approaching, far_away, done_task])

    svc = ReminderService(
        db,
        [OlympiadTask],
        date_field="deadline",
        done_field="is_completed",
        notify_before_minutes=60,
    )

    triggered_items = []
    svc.reminder_triggered.connect(lambda e: triggered_items.append(e))

    detected = svc.check_deadlines()
    assert len(detected) == 1
    assert detected[0].name == "Срочная задача"
    assert len(triggered_items) == 1


def test_reminder_dialog_actions(db):
    """ReminderDialog supports complete action and postpone."""
    now = datetime.datetime.now()
    task = OlympiadTask(
        name="Тестовый дедлайн",
        description="Проверка кнопок диалога",
        deadline=now + datetime.timedelta(minutes=10),
        is_completed=False,
    )
    task.save(db)

    opened = []
    dialog = ReminderDialog(
        task,
        db,
        title_field="name",
        date_field="deadline",
        done_field="is_completed",
        on_open=lambda e: opened.append(e),
    )

    # Test open action
    dialog._action_open()
    assert len(opened) == 1
    assert opened[0]._id == task._id

    # Test complete action
    dialog._action_complete()
    assert task.is_completed is True
    reloaded = OlympiadTask.get(db, task._id)
    assert reloaded.is_completed is True
