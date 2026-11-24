from __future__ import annotations

import html
import traceback
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
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


def _show_error(parent: QWidget | None, title: str, exc: Exception | str) -> None:
    msg = QMessageBox(parent)
    msg.setIcon(QMessageBox.Icon.Critical)
    msg.setWindowTitle(title)
    msg.setText(str(exc))
    tb = traceback.format_exc()
    if tb and tb.strip() != "NoneType: None":
        msg.setDetailedText(tb)
    msg.exec()


class ERPApplication:
    """Top-level application bootstrap."""

    def __init__(
        self,
        argv: list[str] | None = None,
        db_path: str = ":memory:",
        title: str = "ERP Система",
        window_size: tuple[int, int] = (1200, 800),
    ) -> None:
        existing = QApplication.instance()
        self._qt_app = existing if isinstance(existing, QApplication) else QApplication(argv if argv is not None else [])
        self._reminder_service: Any = None

        from decui.registry import get_all_entities, get_all_reports
        entities = [e.__name__ for e in get_all_entities()]
        reports = [r.__name__ for r in get_all_reports()]

        self._db = Database(path=db_path)

        self._main_window = _MainWindow(self._db, title=title)
        self._main_window.resize(*window_size)

        if not self._db._ready:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Icon.Critical)
            msg.setWindowTitle("Ошибка инициализации БД")
            msg.setText(
                f"Путь: {db_path}\n"
                f"Сущности: {entities}\n"
                f"Отчёты: {reports}"
            )
            if self._db._last_error:
                msg.setDetailedText(self._db._last_error)
            msg.exec()

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
        svc.data_changed.connect(self._main_window._refresh_active_view)
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
    """Main application window with navigation sidebar, dashboard, content views, and status bar."""

    def __init__(self, database: Database, title: str = "ERP Система") -> None:
        super().__init__()
        self._db = database
        self._title = title
        self._current_entity_class: type | None = None
        self._active_widget: QWidget | None = None

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

        # Content area
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(self._content)

        splitter.setSizes([250, 950])
        layout.addWidget(splitter)

        self.statusBar().showMessage(f"Готово к работе · БД: {database._path}")
        self._show_welcome()

    # ------------------------------------------------------------------
    # Content management
    # ------------------------------------------------------------------

    def _clear_content(self) -> None:
        self._active_widget = None
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _confirm_leave_active_form(self) -> bool:
        """Ask user before discarding unsaved edits on an open AutoForm."""
        if isinstance(self._active_widget, AutoForm) and self._active_widget.has_unsaved_changes():
            ans = QMessageBox.question(
                self,
                "Несохранённые изменения",
                "В открытой форме есть несохранённые изменения. Сохранить их?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if ans == QMessageBox.StandardButton.Cancel:
                return False
            if ans == QMessageBox.StandardButton.Yes:
                return self._active_widget._save()
        return True

    def _show_welcome(self) -> None:
        from decui.registry import get_all_entities, get_all_reports

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(32, 32, 32, 32)
        v.setSpacing(16)

        hdr = QLabel(f"<h1>{html.escape(self._title)}</h1>")
        v.addWidget(hdr)
        sub = QLabel("Выберите справочник, документ или отчёт в левой панели навигации.")
        sub.setStyleSheet("opacity: 0.75; font-size: 14px;")
        v.addWidget(sub)

        entities = get_all_entities()
        if entities:
            v.addWidget(QLabel("<h3>Разделы и данные</h3>"))
            row = QHBoxLayout()
            for cls in entities[:6]:
                btn = QPushButton(cls._meta.verbose_name)
                btn.setMinimumHeight(44)
                btn.clicked.connect(lambda checked=False, c=cls: self._on_navigate(c))
                row.addWidget(btn)
            row.addStretch()
            v.addLayout(row)

        reports = get_all_reports()
        if reports:
            v.addWidget(QLabel("<h3>Аналитика и отчёты</h3>"))
            rrow = QHBoxLayout()
            for rep in reports[:6]:
                meta = getattr(rep, "_report_meta", None)
                lbl = (meta.title if meta and meta.title else None) or rep.__name__
                btn = QPushButton(lbl)
                btn.setMinimumHeight(40)
                btn.clicked.connect(lambda checked=False, r=rep: self._on_navigate(r))
                rrow.addWidget(btn)
            rrow.addStretch()
            v.addLayout(rrow)

        v.addStretch()
        self._active_widget = scroll
        self._content_layout.addWidget(scroll)

    def _refresh_active_view(self) -> None:
        if isinstance(self._active_widget, ListView):
            self._active_widget.refresh()
        elif isinstance(self._active_widget, AutoForm):
            self._active_widget._refresh_ref_editors()

    # ------------------------------------------------------------------
    # Navigation handler
    # ------------------------------------------------------------------

    def _on_navigate(self, target: Any) -> None:
        if not self._confirm_leave_active_form():
            return
        try:
            self._clear_content()
            if bool(getattr(target, "_report_meta", None)):
                self._show_report(target)
            else:
                self._show_list(target)
        except Exception as exc:
            _show_error(self, "Ошибка навигации", exc)

    # ── Entity views ──

    def _show_list(self, entity_class: type) -> None:
        self._current_entity_class = entity_class
        try:
            list_view = ListView(entity_class, self._db)
            list_view.create_requested.connect(self._on_create)
            list_view.edit_requested.connect(self._on_edit)
            list_view.copy_requested.connect(self._on_copy)
            list_view.data_changed.connect(
                lambda: self.statusBar().showMessage("Данные обновлены", 3000)
            )
            self._active_widget = list_view
            self._content_layout.addWidget(list_view)
            self.statusBar().showMessage(
                f"{entity_class._meta.verbose_name} · записей: {len(list_view._items)}"
            )
        except Exception as exc:
            _show_error(self, "Ошибка открытия", exc)

    def _show_form(self, entity_class: type, entity: Entity | None) -> None:
        try:
            form = AutoForm(entity_class, self._db)
            form.closed.connect(lambda: self._go_back_to_list(entity_class))
            form.data_changed.connect(
                lambda msg: self.statusBar().showMessage(msg, 3500)
            )
            if entity is not None:
                form.load_entity(entity)
            else:
                form.load_new()
            self._clear_content()
            self._active_widget = form
            self._content_layout.addWidget(form)
        except Exception as exc:
            _show_error(self, "Ошибка открытия формы", exc)

    def _go_back_to_list(self, entity_class: type) -> None:
        self._clear_content()
        self._show_list(entity_class)

    def _on_create(self, entity_class: type) -> None:
        if not self._confirm_leave_active_form():
            return
        self._show_form(entity_class, None)

    def _on_edit(self, entity: Entity) -> None:
        if not self._confirm_leave_active_form():
            return
        if entity._id is None:
            self._show_form(type(entity), entity)
            return
        full = self._db.get(type(entity), entity._id)
        if full is None:
            QMessageBox.warning(
                self, "Запись не найдена", f"Запись «{entity}» была удалена."
            )
            self._refresh_active_view()
            return
        self._show_form(type(entity), full)

    def _on_drill_down(self, entity_class: type, entity_id: int) -> None:
        if not self._confirm_leave_active_form():
            return
        full = self._db.get(entity_class, entity_id)
        if full is None:
            QMessageBox.warning(self, "Запись не найдена", "Запись удалена.")
            return
        self._show_form(entity_class, full)

    def _on_copy(self, entity: Entity) -> None:
        if not self._confirm_leave_active_form():
            return
        full = (
            self._db.get(type(entity), entity._id)
            if entity._id is not None
            else entity
        )
        if full is None:
            QMessageBox.warning(
                self, "Запись не найдена", f"Запись «{entity}» была удалена."
            )
            self._refresh_active_view()
            return
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
            report_view.drill_down_requested.connect(self._on_drill_down)
            self._active_widget = report_view
            self._content_layout.addWidget(report_view)
            self.statusBar().showMessage(f"Отчёт: {report_class._report_meta.title}")
        except Exception as exc:
            _show_error(self, "Ошибка открытия отчёта", exc)
