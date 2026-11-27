from __future__ import annotations

import datetime
import html
import logging
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from decui.accounts import Account, AccountStore, current_account, set_current_account
from decui.orm import Database
from decui.sequence import current_role, set_role
from decui.ui.editors import show_ui_error
from decui.ui.form import AutoForm
from decui.ui.listview import ListView, ListViewState
from decui.ui.navigation import NavigationPanel
from decui.ui.report_view import ReportView
from decui.ui.theme import DESKTOP_STYLE

if TYPE_CHECKING:
    from decui.entity import Entity


def _show_error(parent: QWidget | None, title: str, exc: Exception | str) -> None:
    show_ui_error(parent, title, exc)


class ERPApplication:
    """Top-level application bootstrap."""

    def __init__(
        self,
        argv: list[str] | None = None,
        db_path: str | None = None,
        title: str = "ERP Система",
        window_size: tuple[int, int] = (1200, 800),
        *,
        app_id: str | None = None,
        db_filename: str = "data.sqlite3",
        start: type | None = None,
        first_run: Callable[[Database], None] | None = None,
        local_accounts: bool = False,
        column_renames: dict[str, dict[str, str]] | None = None,
        table_part_renames: dict[str, dict[str, str]] | None = None,
    ) -> None:
        existing = QApplication.instance()
        self._qt_app = existing if isinstance(existing, QApplication) else QApplication(argv if argv is not None else [])
        self._reminder_service: Any = None

        from decui.registry import get_all_entities, get_all_reports
        entities = [e.__name__ for e in get_all_entities()]
        reports = [r.__name__ for r in get_all_reports()]

        if db_path is None and app_id is not None:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", app_id) is None:
                raise ValueError("app_id должен содержать латинские буквы, цифры, дефис или подчёркивание")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.sqlite3", db_filename) is None:
                raise ValueError("db_filename должен быть именем файла с расширением .sqlite3")
            data_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
            directory = Path(data_dir) / "DECUI" / app_id
            directory.mkdir(parents=True, exist_ok=True)
            db_path = str(directory / db_filename)
        if db_path is None:
            db_path = ":memory:"
        self._db = Database(path=db_path, column_renames=column_renames, table_part_renames=table_part_renames)

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
        self.account: Account | None = None
        self._previous_role = current_role()
        self._previous_account = current_account()
        self._startup_ok = self._db._ready
        if self._startup_ok and local_accounts:
            self._startup_ok = self._sign_in()
        if self._startup_ok and first_run is not None:
            try:
                self._db.initialize(first_run)
            except Exception as exc:
                _show_error(self._main_window, "Ошибка начального заполнения", exc)
                self._startup_ok = False
        if self._startup_ok and (not local_accounts or (self.account is not None and self.account.role == "admin")):
            self._data_menu = QMenu("Данные", self._main_window)
            self._main_window.menuBar().addMenu(self._data_menu)
            self._data_menu.addAction("Сохранить резервную копию", self._save_backup)
        if self._startup_ok and start is not None:
            self._main_window._on_navigate(start)

    def _save_backup(self) -> None:
        if not self._main_window._confirm_leave_active_form():
            return
        suggested = "backup-" + datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S") + ".sqlite3"
        path, _ = QFileDialog.getSaveFileName(
            self._main_window, "Сохранить резервную копию", suggested, "SQLite (*.sqlite3)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return
        if not path.lower().endswith(".sqlite3"):
            path += ".sqlite3"
        try:
            destination = self._db.backup(path)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            show_ui_error(self._main_window, "Резервная копия", exc, warning=True)
            return
        self._main_window.statusBar().showMessage(f"Резервная копия сохранена: {destination}", 7000)

    def _sign_in(self) -> bool:
        from decui.ui.accounts import AccountDialog

        try:
            store = AccountStore(self._db)
            mode = "login" if store.has_accounts() else "first"
            dialog = AccountDialog(store, self._main_window, mode=mode)
            dialog.exec()
            self.account = dialog.account
            dialog.deleteLater()
        except Exception as exc:
            _show_error(self._main_window, "Ошибка входа", exc)
            return False
        if self.account is None:
            return False
        self._main_window._clear_content()
        set_current_account(self.account)
        set_role(self.account.role)
        self._main_window._nav.refresh()
        self._main_window._show_welcome()
        if self.account.role == "admin":
            self._account_menu = QMenu("Учётные записи", self._main_window)
            self._main_window.menuBar().addMenu(self._account_menu)

            def add_account() -> None:
                dialog = AccountDialog(store, self._main_window, mode="create")
                dialog.exec()
                dialog.deleteLater()

            self._account_menu.addAction("Добавить пользователя", add_account)
        self._main_window.setWindowTitle(f"{self._main_window.windowTitle()} · {self.account.login}")
        return True

    def enable_reminders(
        self,
        entity_classes: list[type],
        *,
        date_field: str = "deadline",
        done_field: str = "is_completed",
        title_field: str = "name",
        notify_before_minutes: int = 60,
        check_interval_ms: int = 30000,
        eligible: Callable[[Entity], bool] | None = None,
    ) -> Any:
        from decui.reminders import ReminderService
        if self._reminder_service is not None:
            self._reminder_service.stop()
            self._reminder_service.deleteLater()
        svc = ReminderService(
            self._db,
            entity_classes,
            date_field=date_field,
            done_field=done_field,
            title_field=title_field,
            notify_before_minutes=notify_before_minutes,
            check_interval_ms=check_interval_ms,
            parent=self._main_window,
            eligible=eligible,
        )
        svc.open_entity_requested.connect(self._main_window._on_edit)
        svc.data_changed.connect(self._main_window._refresh_active_view)
        svc.start()
        self._reminder_service = svc
        return svc

    def run(self) -> int:
        try:
            if not self._startup_ok:
                return 1
            self._main_window.show()
            return self._qt_app.exec()
        finally:
            self._main_window._remember_active_list()
            if self.account is not None:
                set_current_account(self._previous_account)
                set_role(self._previous_role)

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
        self._list_states: dict[tuple[str, type], ListViewState] = {}

        self.setWindowTitle(title)
        self.setStyleSheet(DESKTOP_STYLE)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Navigation
        self._nav = NavigationPanel(can_read=lambda entity_class: self._db.allows(entity_class, "read"))
        self._nav.setObjectName("navigation")
        self._nav.on_select(self._on_navigate)
        splitter.addWidget(self._nav)

        # Content area
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(self._content)

        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 960])
        layout.addWidget(splitter)

        self.statusBar().showMessage(f"Готово к работе · БД: {database._path}")
        self._show_welcome()

    # ------------------------------------------------------------------
    # Content management
    # ------------------------------------------------------------------

    @staticmethod
    def _list_state_key(entity: type) -> tuple[str, type]:
        account = current_account()
        return (account.login if account else "", entity)

    def _remember_active_list(self) -> None:
        if not isinstance(self._active_widget, ListView):
            return
        entity = self._active_widget._entity_class
        state = self._active_widget.list_state()
        key = self._list_state_key(entity)
        if self._list_states.get(key) == state:
            return
        self._list_states[key] = state
        try:
            self._db.save_list_settings(entity, state.to_settings())
        except (SQLAlchemyError, RuntimeError):
            logging.getLogger(__name__).warning("Не удалось сохранить настройки списка", exc_info=True)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._confirm_leave_active_form():
            event.ignore()
            return
        self._remember_active_list()
        super().closeEvent(event)

    def _clear_content(self) -> None:
        self._remember_active_list()
        self._active_widget = None
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _confirm_leave_active_form(self) -> bool:
        """Ask user before discarding unsaved edits on an open AutoForm."""
        form = self._active_widget
        return not isinstance(form, AutoForm) or form._confirm_close()

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

        entities = [item for item in get_all_entities() if self._db.allows(item, "read")]
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
            self._active_widget.refresh_related_data()
        elif isinstance(self._active_widget, ReportView):
            self._active_widget.refresh()

    # ------------------------------------------------------------------
    # Navigation handler
    # ------------------------------------------------------------------

    def _on_navigate(self, target: Any) -> None:
        if getattr(target, "_meta", None) is not None and not self._db.allows(target, "read"):
            return
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
            state = self._list_states.get(self._list_state_key(entity_class))
            if state is None:
                try:
                    settings = self._db.list_settings(entity_class)
                    if isinstance(settings, dict):
                        state = ListView.state_from_settings(entity_class, self._db, settings)
                except (SQLAlchemyError, RuntimeError):
                    logging.getLogger(__name__).warning("Не удалось загрузить настройки списка", exc_info=True)
            list_view = ListView(entity_class, self._db, state=state)
            list_view.create_requested.connect(self._on_create)
            list_view.edit_requested.connect(self._on_edit)
            list_view.copy_requested.connect(self._on_copy)
            list_view.data_changed.connect(
                lambda: self.statusBar().showMessage("Данные обновлены", 3000)
            )
            self._active_widget = list_view
            self._content_layout.addWidget(list_view)
            self.statusBar().showMessage(entity_class._meta.verbose_name)
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
        if not self._db.allows(entity_class, "create") or not self._confirm_leave_active_form():
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
        try:
            self._show_form(type(entity), full.copy_for_edit(self._db))
        except Exception as exc:
            _show_error(self, "Ошибка копирования", exc)

    # ── Report view ──

    def _show_report(self, report_class: type) -> None:
        try:
            report_view = ReportView(report_class, self._db)
            report_view.drill_down_requested.connect(self._on_drill_down)
            self._active_widget = report_view
            self._content_layout.addWidget(report_view)
            self.statusBar().showMessage(f"Отчёт: {report_class._report_meta.title}")
        except Exception as exc:
            _show_error(self, "Ошибка открытия отчёта", exc)
