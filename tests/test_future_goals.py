from __future__ import annotations

import datetime as dt
import sqlite3

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from decui import Database
from decui.reminders import ReminderDialog, ReminderService
from decui.ui.form import AutoForm
from decui.ui.listview import ListView
from decui.ui.report_view import ReportView
from examples.future_goals import (
    ACTIVE, CANCELLED, CELEBRATION, COMPLETED, CREATED, FutureGoal, GoalDynamics, GoalStage, seed_demo,
)

NOW = dt.datetime(2026, 11, 25, 12)
DAY = dt.timedelta(days=1)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def test_seed_report_and_persistence(tmp_path):
    path = str(tmp_path / "goals.sqlite3")
    db = Database(path)
    seed_demo(db, NOW)
    reopened = Database(path)
    seed_demo(reopened, NOW + DAY)
    goals = reopened.list(FutureGoal)
    assert [(g.status, g.progress) for g in goals] == [
        (COMPLETED, "100%"), (ACTIVE, "17%"), (ACTIVE, "50%"), (ACTIVE, "83%"),
        (CANCELLED, "0%"),
    ]
    report = GoalDynamics()
    assert [r["progress"] for r in report.execute(reopened) if r["_style"] == "group"] == [
        "17%", "50%", "83%",
    ]
    assert [r["_style"] for r in report.execute(reopened, goal=goals[2].id)] == [
        "group", "completed", "completed", "completed", "current", "future", "future",
    ]
    for goal in goals:
        reopened.delete(goal)
    seed_demo(reopened, NOW)
    assert not reopened.list(FutureGoal)
    assert not reopened.list(GoalStage)


def test_stage_rules_and_progress(tmp_path):
    db = Database(str(tmp_path / "goals.sqlite3"))
    goal = db.save(FutureGoal(name="Цель", deadline=NOW + 4 * DAY, status=ACTIVE))
    first = db.save(GoalStage(name="Первый", goal=goal.id, deadline=NOW + DAY))
    second = GoalStage(name="Второй", goal=goal.id, deadline=first.deadline)
    with pytest.raises(ValueError, match="позже срока предыдущего"):
        db.save(second)
    assert second.id is None
    second.deadline = NOW + 5 * DAY
    with pytest.raises(ValueError, match="позже срока цели"):
        db.save(second)
    second.deadline = NOW + 2 * DAY
    second.is_completed = True
    with pytest.raises(ValueError, match="предыдущие этапы"):
        db.save(second)
    second.is_completed = False
    db.save(second)
    first.is_completed = True
    db.save(first)
    second.is_completed = True
    db.save(second)
    assert second._save_message == CELEBRATION
    assert db.get(FutureGoal, goal.id).status == COMPLETED

    first.is_completed = False
    with pytest.raises(ValueError, match="последующих этапов"):
        db.save(first)
    first.is_completed = True
    first.deadline = second.deadline
    with pytest.raises(ValueError, match="раньше срока следующего"):
        db.save(first)
    goal.deadline = NOW
    with pytest.raises(ValueError, match="последнего этапа"):
        db.save(goal)

    copy = goal.copy_for_edit(db)
    assert copy.status == CREATED and copy.progress == "0%" and copy.id is None
    clone = second.copy_for_edit(db)
    assert not clone.is_completed and clone.deadline > second.deadline
    db.save(clone)
    updated = db.get(FutureGoal, goal.id)
    assert (updated.status, updated.progress) == (ACTIVE, "67%")
    db.delete_by_id(GoalStage, clone.id)
    assert db.get(FutureGoal, goal.id).progress == "100%"


def test_rollback_restores_nested_identity_and_goal(tmp_path):
    db = Database(str(tmp_path / "goals.sqlite3"))
    goal = db.save(FutureGoal(name="Цель", status=ACTIVE, deadline=NOW + 2 * DAY))
    stage = GoalStage(name="Этап", goal=goal.id, deadline=NOW + DAY, is_completed=True)
    with pytest.raises(RuntimeError, match="откат"):
        with db.transaction():
            db.save(stage)
            assert db.get(FutureGoal, goal.id).status == COMPLETED
            raise RuntimeError("откат")
    assert stage.id is None
    assert db.list(GoalStage) == []
    assert db.get(FutureGoal, goal.id).progress == "0%"


def test_incompatible_schema_leaves_original_data(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE future_goal (id INTEGER PRIMARY KEY, name TEXT, old_data TEXT)")
        conn.execute("INSERT INTO future_goal VALUES (1, 'Цель', 'архив')")
    db = Database(str(path))
    assert not db._ready
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT old_data FROM future_goal").fetchone() == ("архив",)


def test_related_form_report_and_reminder(qt_app, monkeypatch):
    db = Database.memory()
    seed_demo(db, NOW)
    goal = db.filter(FutureGoal, status=ACTIVE)[0]
    form = AutoForm(FutureGoal, db)
    form.load_entity(goal)
    related = form.findChild(ListView)
    assert len(related._items) == 6
    assert all(item.goal == goal.id for item in related._items)

    report = ReportView(GoalDynamics, db)
    combo = report._param_widgets["goal"]
    combo.setCurrentIndex(combo.findData(goal.id))
    report._generate()
    assert report._proxy.rowCount() == 7
    assert report._proxy.index(1, 0).data(Qt.ItemDataRole.FontRole).strikeOut()
    assert report._proxy.index(2, 0).data(Qt.ItemDataRole.FontRole).bold()

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    stage = related._items[-1]
    dialog = ReminderDialog(stage, db)
    dialog._action_complete()
    assert "предыдущие этапы" in warnings[0]
    assert not db.get(GoalStage, stage.id).is_completed
    stage = db.filter(GoalStage, goal=goal.id, is_completed=False, order_by="position")[0]
    dialog = ReminderDialog(stage, db)
    dialog._action_complete()
    assert stage.is_completed
    form.refresh_related_data()
    assert form._widgets["progress"].text() == "33%"

    svc = ReminderService(db, [GoalStage], notify_before_minutes=10**6)
    upcoming = svc.check_deadlines()
    assert upcoming and all(not item.is_completed for item in upcoming)
    assert not svc.check_deadlines()
    svc.stop()


def test_application_storage_first_run_and_start_screen(qt_app, monkeypatch, tmp_path):
    from PySide6.QtCore import QStandardPaths

    from decui import ERPApplication

    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda _: str(tmp_path))
    calls = []

    def setup(db):
        calls.append(db)
        db.save(FutureGoal(name="Моя первая цель", deadline=NOW + DAY))

    first = ERPApplication(app_id="guide", first_run=setup, start=FutureGoal)
    assert isinstance(first._main_window._active_widget, ListView)
    assert first._main_window._active_widget._items[0].name == "Моя первая цель"
    assert (tmp_path / "DECUI" / "guide" / "data.sqlite3").is_file()
    first.main_window.close()

    reopened = ERPApplication(app_id="guide", first_run=setup, start=FutureGoal)
    assert len(calls) == 1
    assert reopened.database.list(FutureGoal)[0].name == "Моя первая цель"
    reopened.main_window.close()
