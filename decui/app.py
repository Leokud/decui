from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from decui.orm import Database
from decui.ui.form import AutoForm
from decui.ui.listview import ListView
from decui.ui.navigation import NavigationPanel

if TYPE_CHECKING:
    from decui.entity import Entity


class ERPApplication:
    """Top-level application bootstrap."""

    def __init__(
        self,
        argv: list[str] | None = None,
        db_path: str = ":memory:",
        title: str = "ERP Система",
    ) -> None:
        self._qt_app = QApplication(argv or [])

        from decui.registry import get_all_entities, get_all_reports
        entities = [e.__name__ for e in get_all_entities()]
        reports = [r.__name__ for r in get_all_reports()]

        self._db = Database(path=db_path)

        if not self._db._ready:
            QMessageBox.critical(
                None, "Ошибка инициализации БД",
                f"Путь: {db_path}\n"
                f"Сущности: {entities}\n"
                f"Отчёты: {reports}\n"
                f"Ошибка:\n{self._db._last_error}",
            )
            self._main_window = _MainWindow(self._db, title=title)
            return

        self._main_window = _MainWindow(self._db, title=title)
        self._main_window.resize(1200, 800)
        self._reminder_service: Any = None

    def enable_reminders(
        self,
        entity_classes: list[type],
        *,
        date_field: str = "deadline",
        done_field: str = "is_completed",
        title_field: str = "name",
        notify_before_minutes: int = 60,
        check_interval_ms: int = 30000,
    ) -> Any:
        from decui.reminders import ReminderService
        svc = ReminderService(
            self._db,
            entity_classes,
            date_field=date_field,
            done_field=done_field,
            title_field=title_field,
            notify_before_minutes=notify_before_minutes,
            check_interval_ms=check_interval_ms,
            parent=self._main_window,
        )
        svc.open_entity_requested.connect(self._main_window._on_edit)
        svc.start()
        self._reminder_service = svc
        return svc

    def run(self) -> int:
        if hasattr(self, "_main_window") and self._main_window:
            self._main_window.show()
        return self._qt_app.exec()

    @property
    def main_window(self) -> QMainWindow:
        return self._main_window

    @property
    def database(self) -> Database:
        return self._db


class _MainWindow(QMainWindow):
    """Main application window with navigation sidebar and content area."""

    def __init__(self, database: Database, title: str = "ERP Система") -> None:
        super().__init__()
        self._db = database
        self._current_entity_class: type | None = None

        self.setWindowTitle(title)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Navigation
        self._nav = NavigationPanel()
        self._nav.on_select(self._on_navigate)
        splitter.addWidget(self._nav)

        # Content area (replaced by clear+add)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(self._content)

        splitter.setSizes([250, 950])
        layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # Content management
    # ------------------------------------------------------------------

    def _clear_content(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ------------------------------------------------------------------
    # Navigation handler
    # ------------------------------------------------------------------

    def _on_navigate(self, target: type) -> None:
        try:
            self._clear_content()
            if bool(getattr(target, "_report_meta", None)):
                self._show_report(target)
            else:
                self._show_list(target)
        except Exception as exc:
            import traceback
            QMessageBox.critical(
                self, "Ошибка навигации",
                f"{exc}\n\n{traceback.format_exc()}",
            )

    # ── Entity views ──

    def _show_list(self, entity_class: type) -> None:
        self._current_entity_class = entity_class
        try:
            list_view = ListView(entity_class, self._db)
            list_view.create_requested.connect(self._on_create)
            list_view.edit_requested.connect(self._on_edit)
            list_view.copy_requested.connect(self._on_copy)
            self._content_layout.addWidget(list_view)
        except Exception as exc:
            import traceback
            QMessageBox.critical(
                self, "Ошибка открытия",
                f"{exc}\n\n{traceback.format_exc()}",
            )

    def _show_form(self, entity_class: type, entity: Entity | None) -> None:
        try:
            form = AutoForm(entity_class, self._db)
            form.closed.connect(lambda: self._go_back_to_list(entity_class))
            if entity is not None:
                form.load_entity(entity)
            else:
                form.load_new()
            self._clear_content()
            self._content_layout.addWidget(form)
        except Exception as exc:
            import traceback
            QMessageBox.critical(
                self, "Ошибка открытия формы",
                f"{exc}\n\n{traceback.format_exc()}",
            )

    def _go_back_to_list(self, entity_class: type) -> None:
        self._clear_content()
        self._show_list(entity_class)

    def _on_create(self, entity_class: type) -> None:
        self._show_form(entity_class, None)

    def _on_edit(self, entity: Entity) -> None:
        full = self._db.get(type(entity), entity._id)
        self._show_form(type(entity), full or entity)

    def _on_copy(self, entity: Entity) -> None:
        full = self._db.get(type(entity), entity._id) or entity
        data = full.to_dict()
        data.pop("id", None)
        data.pop("_id", None)
        num_cfg = getattr(type(entity), "__numbering__", None)
        if num_cfg:
            field_name = num_cfg.get(
                "field", "number" if entity._meta.is_document else "code"
            )
            data.pop(field_name, None)
        if entity._meta.is_document:
            data["posted"] = False
        clone = type(entity).from_dict(data)
        self._show_form(type(entity), clone)

    # ── Report view ──

    def _show_report(self, report_class: type) -> None:
        try:
            from decui.ui.report_view import ReportView
            report_view = ReportView(report_class, self._db)
            self._content_layout.addWidget(report_view)
        except Exception as exc:
            import traceback
            QMessageBox.critical(
                self, "Ошибка открытия отчёта",
                f"{exc}\n\n{traceback.format_exc()}",
            )
