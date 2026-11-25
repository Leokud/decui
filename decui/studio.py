"""DECUI desktop application authoring environment."""

from __future__ import annotations

import ast
import codecs
import os
import re
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from decui import Boolean, Choice, Date, DateTime, Decimal, Integer, Ref, String, Time
from decui.cli import build_arguments
from decui.entity import Document
from decui.fields import BasisTable, Field
from decui.files import publish_new_file, stage_decui_backup
from decui.studio_project import (
    DIMENSION_TYPES,
    ENTITY_TYPES,
    FIELD_TYPES,
    INFORMATION_RESOURCE_TYPES,
    REGISTER_TYPES,
    RESERVED_FIELDS,
    RESOURCE_TYPES,
    StudioEntity,
    StudioField,
    StudioMovement,
    StudioPrintForm,
    StudioProject,
    StudioRegister,
    StudioRegisterMember,
    StudioReport,
    StudioSeed,
    StudioSourceSpan,
    StudioSyntaxError,
    StudioTablePart,
    StudioTransition,
    StudioWorkflow,
    approval_template,
    basis_column_options,
    basis_field_options,
    goal_template,
    identifier,
    invoice_template,
    movement_compatible,
    movement_fields,
    movement_source,
    order_invoice_template,
    parse_studio_code,
    register_report_columns,
    studio_command_methods,
)
from decui.ui.editors import create_editor, get_editor_value
from decui.ui.form import FieldLayout
from decui.ui.python_editor import PythonEditor
from decui.ui.theme import DESKTOP_STYLE


class StudioWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.project = StudioProject()
        self.path: Path | None = None
        self.saved = repr(self.project)
        self.current: StudioEntity | None = None
        self.preview_dir: TemporaryDirectory[str] | None = None
        self._build_output: tuple[Path, Path] | None = None
        self._entry_snapshot: tuple[Path, str] | None = None
        self._source_spans: list[StudioSourceSpan] = []
        self._error_location: StudioSyntaxError | None = None
        self._output_line_tail = ""
        self._output_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.setWindowTitle("DECUI Studio")
        self.setStyleSheet(DESKTOP_STYLE)
        self.resize(1120, 810)
        self._build()
        self._load_project()

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 14)
        bar = QHBoxLayout()
        for title, slot in (("Новый", self.new_project), ("Шаблоны", self.choose_template),
                            ("Открыть", self.open_project), ("Сохранить", self.save_project),
                            ("Проверить", self.check), ("Запустить", self.preview)):
            button = QPushButton(title)
            button.setProperty("primary", title == "Запустить")
            button.clicked.connect(slot)
            bar.addWidget(button)
        more = QPushButton("Действия")
        actions_menu = QMenu(more)
        for title, callback in (
            ("Экспорт Python", self.export),
            ("Запустить с копией", self.preview_backup),
            ("Проверить миграцию на копии…", self.check_backup_migration),
            ("Собрать", self.build),
            ("Остановить", self.process.terminate),
        ):
            actions_menu.addAction(title, callback)
        more.setMenu(actions_menu)
        bar.addWidget(more)
        bar.addStretch()
        guide = QPushButton("Учебник")
        guide.clicked.connect(self._open_guide)
        bar.addWidget(guide)
        layout.addLayout(bar)
        self._shortcut("Новый проект", QKeySequence.StandardKey.New, self.new_project)
        self._shortcut("Открыть проект", QKeySequence.StandardKey.Open, self.open_project)
        self._shortcut("Сохранить проект", QKeySequence.StandardKey.Save, self.save_project)

        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        sidebar = QWidget()
        left = QVBoxLayout(sidebar)
        header = QLabel("Объекты")
        header.setProperty("heading", True)
        left.addWidget(header)
        self.entities = QListWidget()
        self.entities.currentRowChanged.connect(self._select)
        left.addWidget(self.entities)
        controls = QHBoxLayout()
        for label, slot in (("Добавить", self._add_entity), ("Удалить", self._remove_entity)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            controls.addWidget(button)
        left.addLayout(controls)
        splitter.addWidget(sidebar)

        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)
        splitter.setSizes([255, 825])
        page = QScrollArea()
        page.setWidgetResizable(True)
        self.tabs.addTab(page, "Объект")
        content = QWidget()
        page.setWidget(content)
        page_layout = QVBoxLayout(content)
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.welcome = self._welcome_panel()
        page_layout.addWidget(self.welcome)
        self.editor = QWidget()
        page_layout.addWidget(self.editor)
        editor = QVBoxLayout(self.editor)
        form = QFormLayout()
        self.name, self.label, self.section = QLineEdit(), QLineEdit(), QLineEdit()
        self.prefix, self.number_field = QLineEdit(), QLineEdit()
        self.number_field.setPlaceholderText("number для документа")
        self.owner_only = QCheckBox("Только владелец и администратор")
        self.kind = QComboBox()
        self.kind.addItems(ENTITY_TYPES)
        for title, value in (("Имя Python", self.name), ("Подпись", self.label),
                             ("Тип", self.kind), ("Раздел меню", self.section),
                             ("Префикс номера", self.prefix), ("Поле номера", self.number_field),
                             ("Доступ к записям", self.owner_only)):
            form.addRow(title, value)
        self.label.textChanged.connect(self._rename_item)
        self.permissions_button = QPushButton("Настроить права по ролям")
        self.permissions_button.clicked.connect(self._edit_permissions)
        form.addRow("Права", self.permissions_button)
        self.print_button = QPushButton("Настроить печатную форму")
        self.print_button.clicked.connect(self._edit_print_form)
        self.kind.currentTextChanged.connect(lambda value: self.print_button.setEnabled(value == "Документ"))
        form.addRow("Печать", self.print_button)
        list_columns = QPushButton("Настроить колонки списка")
        list_columns.clicked.connect(self._edit_list_columns)
        form.addRow("Список", list_columns)
        editor.addLayout(form)
        tables = QHBoxLayout()
        self.table_part = QComboBox()
        self.table_part.currentIndexChanged.connect(self._show_fields)
        tables.addWidget(self.table_part, 1)
        for text, callback in (
            ("Добавить таблицу", lambda: self._edit_table_part(None)),
            ("Переименовать", self._rename_table_part),
            ("Удалить таблицу", self._remove_table_part),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            tables.addWidget(button)
        editor.addLayout(tables)
        self.fields = QTableWidget(0, 4)
        self.fields.setHorizontalHeaderLabels(["Имя", "Подпись", "Тип", "Обязательное"])
        self.fields.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        field_order = self.fields.verticalHeader()
        field_order.setSectionsMovable(True)
        field_order.setToolTip("Перетащите номер строки, чтобы изменить порядок полей")
        field_order.sectionMoved.connect(self._field_order_changed)
        self.fields.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.fields.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.fields.cellDoubleClicked.connect(lambda *_: self._edit_field())
        editor.addWidget(self.fields, 3)
        row = QHBoxLayout()
        for label, slot in (("Добавить поле", self._add_field), ("Изменить", self._edit_field),
                            ("Удалить", self._remove_field)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            row.addWidget(button)
        for label, offset in (("Выше", -1), ("Ниже", 1)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, step=offset: self._move_selected_field(step))
            row.addWidget(button)
        row.addStretch()
        editor.addLayout(row)
        self.code = PythonEditor()
        self.code.setPlaceholderText("Методы класса, команды, табличные части. Вводите код без внешнего отступа.")
        code_header = QHBoxLayout()
        code_header.addWidget(QLabel("Код внутри класса"))
        code_header.addStretch()
        commands = QPushButton("Кнопки действий…")
        commands.clicked.connect(self._edit_commands)
        code_header.addWidget(commands)
        editor.addLayout(code_header)
        editor.addWidget(self.code, 2)

        settings = QWidget()
        self.tabs.addTab(settings, "Приложение")
        config = QFormLayout(settings)
        self.title = QLineEdit()
        self.app_id = QLineEdit()
        self.local_accounts = QCheckBox("Требовать вход в приложение")
        self.preview_role = QLineEdit()
        self.preview_role.setPlaceholderText("Пусто — роль из настроек приложения")
        self.imports = PythonEditor()
        self.imports.setPlaceholderText("Импорты и константы для объявлений объектов.")
        self.logic = PythonEditor()
        self.logic.setPlaceholderText("Отчёты, регистры и функции после объектов. configure_app(app) вызывается перед запуском.")
        config.addRow("Название окна", self.title)
        config.addRow("Идентификатор", self.app_id)
        config.addRow("Учётные записи", self.local_accounts)
        config.addRow("Роль предпросмотра", self.preview_role)
        migrations = QPushButton("Переименовать колонки…")
        migrations.clicked.connect(self._edit_column_renames)
        config.addRow("Миграция базы", migrations)
        table_migrations = QPushButton("Переименовать табличные части…")
        table_migrations.clicked.connect(lambda: self._edit_column_renames(table_parts=True))
        config.addRow("", table_migrations)
        config.addRow("Код перед объектами", self.imports)
        config.addRow("Код после объектов", self.logic)

        reports = QWidget()
        self.tabs.addTab(reports, "Отчёты")
        report_layout = QVBoxLayout(reports)
        self.report_list = QListWidget()
        self.report_list.itemDoubleClicked.connect(lambda _: self._edit_report(self.report_list.currentRow()))
        report_layout.addWidget(self.report_list)
        report_actions = QHBoxLayout()
        for title, callback in (
            ("Добавить отчёт", lambda: self._edit_report(-1)),
            ("Изменить", lambda: self._edit_report(self.report_list.currentRow()) if self.report_list.currentRow() >= 0 else None),
            ("Удалить", self._remove_report),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            report_actions.addWidget(button)
        report_actions.addStretch()
        report_layout.addLayout(report_actions)

        registers = QWidget()
        self.tabs.addTab(registers, "Регистры")
        register_layout = QVBoxLayout(registers)
        self.register_list = QListWidget()
        self.register_list.itemDoubleClicked.connect(lambda _: self._edit_register(self.register_list.currentRow()))
        register_layout.addWidget(self.register_list)
        register_actions = QHBoxLayout()
        for title, callback in (
            ("Добавить регистр", lambda: self._edit_register(-1)),
            ("Изменить", self._edit_selected_register),
            ("Удалить", self._remove_register),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            register_actions.addWidget(button)
        register_actions.addStretch()
        register_layout.addLayout(register_actions)

        movements = QWidget()
        self.tabs.addTab(movements, "Движения")
        movement_layout = QVBoxLayout(movements)
        self.movement_hint = QLabel()
        self.movement_hint.setWordWrap(True)
        movement_layout.addWidget(self.movement_hint)
        self.movement_editor = QWidget()
        movement_layout.addWidget(self.movement_editor, 1)
        rules = QVBoxLayout(self.movement_editor)
        rules.setContentsMargins(0, 0, 0, 0)
        self.movement_list = QListWidget()
        self.movement_list.itemDoubleClicked.connect(lambda _: self._edit_movement(self.movement_list.currentRow()))
        rules.addWidget(self.movement_list)
        actions = QHBoxLayout()
        for title, callback in (
            ("Добавить движение", lambda: self._edit_movement(-1)),
            ("Изменить", self._edit_selected_movement),
            ("Удалить", self._remove_movement),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        rules.addLayout(actions)

        workflow = QWidget()
        self.tabs.addTab(workflow, "Согласование")
        route_layout = QVBoxLayout(workflow)
        self.workflow_hint = QLabel()
        self.workflow_hint.setWordWrap(True)
        route_layout.addWidget(self.workflow_hint)
        self.workflow_list = QListWidget()
        route_layout.addWidget(self.workflow_list, 1)
        route_actions = QHBoxLayout()
        self.workflow_edit = QPushButton("Настроить маршрут")
        self.workflow_edit.clicked.connect(self._edit_workflow)
        self.workflow_remove = QPushButton("Удалить маршрут")
        self.workflow_remove.clicked.connect(self._remove_workflow)
        route_actions.addWidget(self.workflow_edit)
        route_actions.addWidget(self.workflow_remove)
        route_actions.addStretch()
        route_layout.addLayout(route_actions)

        initial_data = QWidget()
        self.tabs.addTab(initial_data, "Начальные данные")
        initial_layout = QVBoxLayout(initial_data)
        note = QLabel("Записи загружаются один раз при первом запуске новой базы.")
        note.setWordWrap(True)
        initial_layout.addWidget(note)
        self.seed_list = QListWidget()
        self.seed_list.itemDoubleClicked.connect(lambda _: self._edit_seed(self.seed_list.currentRow()))
        initial_layout.addWidget(self.seed_list, 1)
        seed_actions = QHBoxLayout()
        for title, callback in (
            ("Добавить запись", lambda: self._edit_seed(-1)),
            ("Изменить", self._edit_selected_seed),
            ("Удалить", self._remove_seed),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            seed_actions.addWidget(button)
        seed_actions.addStretch()
        initial_layout.addLayout(seed_actions)

        self.source = PythonEditor()
        self.source.setReadOnly(True)
        self.tabs.addTab(self.source, "Код проекта")
        self.tabs.currentChanged.connect(self._tab_changed)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumHeight(95)
        self.console.setPlaceholderText("Результаты проверки и запуска")
        output_bar = QHBoxLayout()
        output_bar.addWidget(QLabel("Журнал проекта"))
        output_bar.addStretch()
        self.diagnostic_button = QPushButton("К строке ошибки")
        self.diagnostic_button.setEnabled(False)
        self.diagnostic_button.clicked.connect(self._go_to_error)
        output_bar.addWidget(self.diagnostic_button)
        layout.addLayout(output_bar)
        layout.addWidget(self.console)
        self.statusBar().showMessage("Добавьте объект и поля, чтобы запустить приложение.")

    def _welcome_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Первое приложение")
        title.setProperty("heading", True)
        layout.addWidget(title)
        description = QLabel(
            "Откройте пример с готовыми заказами и товарами. Нажмите «Запустить», "
            "откройте заказ и создайте накладную на его основании."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        template = QPushButton("Открыть пример «Заказы и накладные»")
        template.setProperty("primary", True)
        template.clicked.connect(lambda: self.load_template(order_invoice_template()))
        layout.addWidget(template, alignment=Qt.AlignmentFlag.AlignLeft)
        new = QPushButton("Добавить свой объект")
        new.clicked.connect(self._add_entity)
        layout.addWidget(new, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return panel

    def _open_guide(self) -> None:
        local = Path(__file__).resolve().parent.parent / "docs" / "index.html"
        url = QUrl.fromLocalFile(str(local)) if local.is_file() else QUrl("https://leokud.github.io/decui/")
        if not QDesktopServices.openUrl(url):
            self.statusBar().showMessage(f"Откройте учебник в браузере: {url.toDisplayString()}")

    def _shortcut(self, title, keys, slot) -> None:
        action = QAction(title, self)
        action.setShortcut(keys)
        action.triggered.connect(slot)
        self.addAction(action)

    def _edit_column_renames(self, *, table_parts: bool = False) -> None:
        self._store()
        if table_parts:
            targets = self.project.table_part_rename_targets()
            existing = self.project.table_part_renames
            validate = self.project.validate_table_part_renames
            title = "Переименование табличных частей в существующей базе"
        else:
            targets = self.project.column_rename_targets()
            existing = self.project.column_renames
            validate = self.project.validate_column_renames
            title = "Переименование колонок в существующей базе"
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(620, 390)
        layout = QVBoxLayout(dialog)
        hint = QLabel("Укажите прежнее имя и выберите текущее. Миграцию можно проверить на копии базы.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Объект или таблица", "Прежнее имя", "Текущее имя"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(table)

        def add_row(name: str = "", old: str = "", new: str = "") -> None:
            row = table.rowCount()
            table.insertRow(row)
            owner = QComboBox()
            owner.addItems(list(targets))
            if name and name not in targets:
                owner.addItem(name)
            if name:
                owner.setCurrentText(name)
            destination = QComboBox()

            def refresh(selected: str) -> None:
                destination.clear()
                destination.addItems(targets.get(selected, []))

            owner.currentTextChanged.connect(refresh)
            refresh(owner.currentText())
            if new and destination.findText(new) < 0:
                destination.addItem(new)
            if new:
                destination.setCurrentText(new)
            table.setCellWidget(row, 0, owner)
            table.setItem(row, 1, QTableWidgetItem(old))
            table.setCellWidget(row, 2, destination)
            table.selectRow(row)

        for owner, changes in existing.items():
            for old, new in changes.items():
                add_row(owner, old, new)

        controls = QHBoxLayout()
        add = QPushButton("Добавить строку")
        add.setEnabled(any(targets.values()))
        add.clicked.connect(lambda: add_row())
        controls.addWidget(add)
        remove = QPushButton("Удалить строку")
        remove.clicked.connect(lambda: table.removeRow(table.currentRow()))
        controls.addWidget(remove)
        controls.addStretch()
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)

        def save() -> None:
            changes: dict[str, dict[str, str]] = {}
            try:
                for row in range(table.rowCount()):
                    owner = table.cellWidget(row, 0)
                    destination = table.cellWidget(row, 2)
                    assert isinstance(owner, QComboBox) and isinstance(destination, QComboBox)
                    old = table.item(row, 1).text().strip()
                    mapping = changes.setdefault(owner.currentText(), {})
                    if old in mapping:
                        raise ValueError(f"Прежнее имя «{old}» указано несколько раз.")
                    mapping[old] = destination.currentText()
                validate(changes)
            except ValueError as exc:
                QMessageBox.warning(dialog, "Проверьте миграцию", str(exc))
                return
            if table_parts:
                self.project.table_part_renames = changes
            else:
                self.project.column_renames = changes
            dialog.accept()

        buttons.accepted.connect(save)
        dialog.exec()
        dialog.deleteLater()

    def _load_project(self) -> None:
        self._set_error_location(None)
        self.current = None
        self.title.setText(self.project.title)
        self.app_id.setText(self.project.app_id)
        self.preview_role.setText(self.project.preview_role)
        self.local_accounts.setChecked(self.project.local_accounts)
        self.imports.setPlainText(self.project.imports)
        self.logic.setPlainText(self.project.logic)
        self._show_reports()
        self._show_registers()
        self._show_seeds()
        self.entities.clear()
        for entity in self.project.entities:
            self.entities.addItem(entity.label)
        if self.project.entities:
            self.entities.setCurrentRow(0)
        else:
            self._select(-1)

    def _store(self) -> None:
        self.project.title = self.title.text()
        self.project.app_id = self.app_id.text()
        self.project.preview_role = self.preview_role.text()
        self.project.local_accounts = self.local_accounts.isChecked()
        self.project.imports = self.imports.toPlainText()
        self.project.logic = self.logic.toPlainText()
        if self.current is not None:
            previous = self.current.name
            name = self.name.text()
            if previous != name and name not in {entity.name for entity in self.project.entities}:
                if previous in self.project.table_part_renames:
                    self.project.table_part_renames[name] = self.project.table_part_renames.pop(previous)
                for key in list(self.project.column_renames):
                    if key.split(".", 1)[0] == previous:
                        self.project.column_renames[name + key[len(previous):]] = self.project.column_renames.pop(key)
                for seed in self.project.initial_data:
                    if seed.entity == previous:
                        seed.entity = name
                self._show_seeds()
                for register in self.project.registers:
                    for member in register.members:
                        if member.target == previous:
                            member.target = name
                for report in self.project.reports:
                    if report.source_kind == "entity" and report.entity == previous:
                        report.entity = name
                for entity in self.project.entities:
                    fields = entity.fields + [item for part in entity.table_parts for item in part.fields]
                    for item in fields:
                        if item.target == previous:
                            item.target = name
            self.current.name = name
            self.current.label = self.label.text()
            self.current.kind = self.kind.currentText()
            self.current.owner_only = self.owner_only.isChecked()
            self.current.section = self.section.text()
            self.current.code = self.code.toPlainText()
            self.current.numbering_prefix = self.prefix.text()
            self.current.numbering_field = self.number_field.text()

    def _select(self, row: int) -> None:
        self._store()
        self.current = self.project.entities[row] if 0 <= row < len(self.project.entities) else None
        self.welcome.setVisible(self.current is None)
        self.editor.setVisible(self.current is not None)
        entity = self.current or StudioEntity("", "")
        self.name.setText(entity.name)
        self.label.setText(entity.label)
        self.kind.setCurrentText(entity.kind)
        self.owner_only.setChecked(entity.owner_only)
        self.section.setText(entity.section)
        self.code.setPlainText(entity.code)
        self.prefix.setText(entity.numbering_prefix)
        self.number_field.setText(entity.numbering_field)
        self._show_table_parts()
        self._show_movements()
        self._show_workflow()

    def _rename_item(self, label: str) -> None:
        item = self.entities.currentItem()
        if item is not None:
            item.setText(label or "Без подписи")

    def _add_entity(self) -> None:
        names = {item.name for item in self.project.entities} | {self.name.text()}
        number = 1
        while f"Entity{number}" in names:
            number += 1
        entity = StudioEntity(f"Entity{number}", "Новый объект")
        self.project.entities.append(entity)
        self.entities.addItem(entity.label)
        self.entities.setCurrentRow(self.entities.count() - 1)
        self.tabs.setCurrentIndex(0)
        self.name.selectAll()
        self.name.setFocus()

    def _remove_entity(self) -> None:
        row = self.entities.currentRow()
        if row >= 0 and QMessageBox.question(self, "Удалить объект", "Удалить объект и его поля?") == QMessageBox.StandardButton.Yes:
            self.current = None
            self.project.entities.pop(row)
            self.entities.takeItem(row)
            self._select(self.entities.currentRow())

    def _show_table_parts(self, index: int = 0) -> None:
        self.table_part.blockSignals(True)
        self.table_part.clear()
        self.table_part.addItem("Поля объекта")
        if self.current is not None:
            self.table_part.addItems([part.label for part in self.current.table_parts])
        self.table_part.setCurrentIndex(min(index, self.table_part.count() - 1))
        self.table_part.blockSignals(False)
        self._show_fields()

    def _field_list(self) -> list[StudioField]:
        if self.current is None:
            return []
        index = self.table_part.currentIndex() - 1
        return self.current.table_parts[index].fields if index >= 0 else self.current.fields

    def _edit_table_part(self, index: int | None) -> None:
        if self.current is None:
            return
        parts = self.current.table_parts
        part = parts[index] if index is not None else StudioTablePart("", "")
        dialog = QDialog(self)
        dialog.setWindowTitle("Табличная часть")
        layout = QFormLayout(dialog)
        name, label = QLineEdit(part.name), QLineEdit(part.label)
        layout.addRow("Имя Python", name)
        layout.addRow("Подпись", label)
        tab = QComboBox()
        tab.setEditable(True)
        tab_names = dict.fromkeys(item.form_tab for item in [*self.current.fields, *parts] if item.form_tab)
        tab.addItems(["", *tab_names])
        tab.setCurrentText(part.form_tab)
        tab.setPlaceholderText("Пусто — под полями формы")
        layout.addRow("Вкладка на форме", tab)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addRow(buttons)
        def accept_edit() -> None:
            new = name.text()
            if index is not None and new != part.name and (not identifier(new) or any(other.name == new for other in parts)):
                QMessageBox.warning(dialog, "Имя занято или недопустимо", "Укажите уникальное имя Python.")
                return
            dialog.accept()

        buttons.accepted.connect(accept_edit)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec():
            old_name = part.name
            part.name, part.label = name.text(), label.text()
            part.form_tab = tab.currentText().strip()
            if index is not None:
                self.project.rename_table_references(self.current, old_name, part.name)
                self._show_seeds()
                self._show_movements()
            if index is None:
                index = len(parts)
                parts.append(part)
            self._show_table_parts(index + 1)
        dialog.deleteLater()

    def _rename_table_part(self) -> None:
        if self.table_part.currentIndex() > 0:
            self._edit_table_part(self.table_part.currentIndex() - 1)

    def _remove_table_part(self) -> None:
        index = self.table_part.currentIndex() - 1
        if self.current is not None and index >= 0 and QMessageBox.question(
            self, "Удалить табличную часть", "Удалить таблицу и её колонки?",
        ) == QMessageBox.StandardButton.Yes:
            self.current.table_parts.pop(index)
            self._show_table_parts()

    def _show_fields(self) -> None:
        header = self.fields.verticalHeader()
        blocked = header.blockSignals(True)
        self.fields.setRowCount(0)
        header.blockSignals(blocked)
        if self.current is None:
            return
        for row, item in enumerate(self._field_list()):
            self.fields.insertRow(row)
            for col, value in enumerate((item.name, item.label, item.kind, "Да" if item.required else "Нет")):
                self.fields.setItem(row, col, QTableWidgetItem(value))

    def _field_order_changed(self, _logical: int, old: int, new: int) -> None:
        fields = self._field_list()
        if 0 <= old < len(fields) and 0 <= new < len(fields):
            fields.insert(new, fields.pop(old))
            self._show_fields()
            self.fields.selectRow(new)

    def _move_selected_field(self, offset: int) -> None:
        source = self.fields.currentRow()
        destination = source + offset
        if source >= 0 and 0 <= destination < self.fields.rowCount():
            self.fields.verticalHeader().moveSection(source, destination)

    def _add_field(self) -> None:
        self._field_dialog(None)

    def _edit_field(self) -> None:
        row = self.fields.currentRow()
        if row >= 0:
            self._field_dialog(row)

    def _remove_field(self) -> None:
        row = self.fields.currentRow()
        if self.current is not None and row >= 0:
            self._field_list().pop(row)
            self._show_fields()

    @staticmethod
    def _form_preview(fields: list[StudioField]) -> tuple[QWidget, FieldLayout]:
        panel = QWidget()
        layout = FieldLayout(panel)
        constructors = {
            "Boolean": Boolean, "Date": Date, "DateTime": DateTime,
            "Decimal": Decimal, "Integer": Integer, "Time": Time,
        }
        rows: dict[str, tuple[StudioField, Field, QWidget, QFormLayout]] = {}
        for item in fields:
            label = QLabel((item.label or item.name or "Новое поле") + (" *:" if item.required or item.required_when_visible else ":"))
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            if item.kind == "Ref":
                field = Ref(item.target, item.label, read_only=item.read_only)
            elif item.kind == "Choice":
                options = [part.strip() for part in item.choices.split(",") if part.strip()]
                field = Choice(
                    item.label, choices=[(option, option) for option in options],
                    multi=False, read_only=item.read_only,
                )
            elif item.kind == "String":
                field = String(item.label, multiline=item.multiline, read_only=item.read_only)
            else:
                field = constructors[item.kind](item.label, read_only=item.read_only)
            control = create_editor(field)
            label.setBuddy(control)
            section = layout.section(item.form_tab.strip(), item.form_group.strip())
            section.addRow(label, control)
            rows[item.name] = (item, field, control, section)
        layout.finish()

        def update_visibility() -> None:
            sections: set[tuple[str, str]] = set()
            for item, _, control, section in rows.values():
                source = rows.get(item.visibility_field)
                visible = not item.visibility_field or (
                    source is not None and get_editor_value(source[2], source[1]) == item.visibility_value
                )
                section.setRowVisible(control, visible)
                if visible:
                    sections.add((item.form_tab.strip(), item.form_group.strip()))
            layout.set_visible_sections(sections)

        for source_name in {item.visibility_field for item in fields} - {""}:
            source = rows.get(source_name)
            if source is None:
                continue
            control = source[2]
            if isinstance(control, QCheckBox):
                control.toggled.connect(update_visibility)
            elif isinstance(control, QComboBox):
                control.currentIndexChanged.connect(update_visibility)
        update_visibility()
        return panel, layout

    def _edit_basis_fields(
        self, parent: QWidget, source_name: str, link: str, current: dict[str, str],
    ) -> dict[str, str]:
        source = next((entity for entity in self.project.entities if entity.name == source_name), None)
        if source is None or self.current is None:
            QMessageBox.information(parent, "Перенос полей", "Сначала выберите объект ссылки.")
            return current

        dialog = QDialog(parent)
        dialog.setWindowTitle("Перенос полей из исходной записи")
        dialog.resize(480, 420)
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        scroll.setWidget(panel)
        layout.addWidget(scroll, 1)
        form = QFormLayout(panel)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        editors: dict[str, QComboBox] = {}
        for destination, choices in basis_field_options(self.current, source, link):
            if not choices:
                continue
            picker = QComboBox()
            picker.addItem("Не переносить", "")
            for candidate in choices:
                picker.addItem(f"{candidate.label} ({candidate.name})", candidate.name)
            picker.setCurrentIndex(max(0, picker.findData(current.get(destination.name, ""))))
            form.addRow(f"{destination.label} ({destination.name})", picker)
            editors[destination.name] = picker
        if not editors:
            note = QLabel("Добавьте в оба объекта поля совместимых типов.")
            note.setWordWrap(True)
            form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        result = current
        if dialog.exec():
            result = {name: picker.currentData() for name, picker in editors.items() if picker.currentIndex() > 0}
        dialog.deleteLater()
        return result

    def _edit_basis_tables(
        self, parent: QWidget, source_name: str, current: dict[str, BasisTable],
    ) -> dict[str, BasisTable]:
        source = next((entity for entity in self.project.entities if entity.name == source_name), None)
        if source is None or self.current is None or not source.table_parts or not self.current.table_parts:
            QMessageBox.information(parent, "Перенос строк", "Выберите объект ссылки и добавьте таблицы в оба объекта.")
            return current

        dialog = QDialog(parent)
        dialog.setWindowTitle("Перенос строк из исходной записи")
        dialog.resize(540, 420)
        layout = QVBoxLayout(dialog)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        table_pickers: dict[str, QComboBox] = {}
        column_pickers: dict[tuple[str, str], dict[str, QComboBox]] = {}
        for destination in self.current.table_parts:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            source_picker = QComboBox()
            source_picker.addItem("Не переносить строки", "")
            page_layout.addWidget(QLabel("Исходная таблица"))
            page_layout.addWidget(source_picker)
            columns = QStackedWidget()
            columns.addWidget(QWidget())
            page_layout.addWidget(columns, 1)
            table_pickers[destination.name] = source_picker
            saved = current.get(destination.name)
            for part in source.table_parts:
                source_picker.addItem(part.label, part.name)
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                panel = QWidget()
                scroll.setWidget(panel)
                form = QFormLayout(panel)
                form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
                columns.addWidget(scroll)
                pickers: dict[str, QComboBox] = {}
                for column, options in basis_column_options(destination.fields, part.fields):
                    if not options:
                        continue
                    picker = QComboBox()
                    picker.addItem("Не переносить", "")
                    for original in options:
                        picker.addItem(original.label, original.name)
                    if saved is not None and saved["source"] == part.name:
                        picker.setCurrentIndex(max(0, picker.findData(saved["columns"].get(column.name, ""))))
                    form.addRow(column.label, picker)
                    pickers[column.name] = picker
                if not pickers:
                    form.addRow(QLabel("Нет колонок совместимых типов."))
                column_pickers[destination.name, part.name] = pickers
            source_picker.currentIndexChanged.connect(columns.setCurrentIndex)
            if saved is not None:
                source_picker.setCurrentIndex(max(0, source_picker.findData(saved["source"])))
            tabs.addTab(page, destination.label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        result = current
        if dialog.exec():
            result = {}
            for name, picker in table_pickers.items():
                table = picker.currentData()
                selected = {
                    col: editor.currentData() for col, editor in column_pickers.get((name, table), {}).items()
                    if editor.currentIndex() > 0
                }
                if selected:
                    result[name] = {"source": table, "columns": selected}
        dialog.deleteLater()
        return result

    def _field_dialog(self, row: int | None) -> None:
        self._store()
        if self.current is None:
            return
        fields = self._field_list()
        item = fields[row] if row is not None else StudioField("", "")
        dialog = QDialog(self)
        dialog.setWindowTitle("Поле")
        has_preview = self.table_part.currentIndex() == 0
        dialog.resize(900 if has_preview else 480, 620)
        outer = QVBoxLayout(dialog)
        body = QSplitter()
        outer.addWidget(body, 1)
        settings = QScrollArea()
        settings.setWidgetResizable(True)
        body.addWidget(settings)
        inputs = QWidget()
        settings.setWidget(inputs)
        form = QFormLayout(inputs)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        name, label = QLineEdit(item.name), QLineEdit(item.label)
        kind = QComboBox()
        kind.addItems(FIELD_TYPES)
        kind.setCurrentText(item.kind)
        required, multiline = QCheckBox(), QCheckBox()
        required.setChecked(item.required)
        multiline.setChecked(item.multiline)
        target = QComboBox()
        target.addItems(["", *(e.name for e in self.project.entities)])
        target.setCurrentText(item.target)
        related, choices = QLineEdit(item.related_list), QLineEdit(item.choices)
        create_from = QLineEdit(item.create_from)
        create_from.setPlaceholderText("Например, Создать накладную")
        form.addRow("Команда на исходной записи", create_from)
        mapping = QPushButton("Выбрать поля…")
        form.addRow("Перенос из исходной записи", mapping)
        basis_fields = item.basis_fields.copy()
        table_mapping = QPushButton("Выбрать таблицы и колонки…")
        form.addRow("Перенос строк", table_mapping)
        basis_tables = item.basis_tables.copy()

        def edit_mapping() -> None:
            nonlocal basis_fields
            basis_fields = self._edit_basis_fields(dialog, target.currentText(), item.name or name.text(), basis_fields)

        def edit_tables() -> None:
            nonlocal basis_tables
            basis_tables = self._edit_basis_tables(dialog, target.currentText(), basis_tables)

        mapping.clicked.connect(edit_mapping)
        table_mapping.clicked.connect(edit_tables)
        target.currentTextChanged.connect(lambda _text: basis_fields.clear())
        create_from.textChanged.connect(lambda text: mapping.setEnabled(bool(text.strip())))
        target.currentTextChanged.connect(lambda _text: basis_tables.clear())
        create_from.textChanged.connect(lambda text: table_mapping.setEnabled(bool(text.strip())))
        table_mapping.setEnabled(bool(create_from.text().strip()))
        mapping.setEnabled(bool(create_from.text().strip()))
        default = QLineEdit(item.default)
        default.setPlaceholderText("Оставьте пустым, чтобы пропустить")
        limit_read = QCheckBox("Ограничить чтение по ролям")
        limit_read.setChecked(item.read_roles is not None)
        read_roles = QLineEdit(", ".join(item.read_roles or []))
        read_roles.setPlaceholderText("Пусто — читает только администратор")
        read_roles.setEnabled(limit_read.isChecked())
        limit_read.toggled.connect(read_roles.setEnabled)
        limit_write = QCheckBox("Ограничить изменение по ролям")
        limit_write.setChecked(item.write_roles is not None)
        write_roles = QLineEdit(", ".join(item.write_roles or []))
        write_roles.setPlaceholderText("Пусто — только администратор")
        write_roles.setEnabled(limit_write.isChecked())
        limit_write.toggled.connect(write_roles.setEnabled)
        read_only, indexed, unique, cascade = QCheckBox(), QCheckBox(), QCheckBox(), QCheckBox()
        for option, value in ((read_only, item.read_only), (indexed, item.indexed),
                              (unique, item.unique), (cascade, item.cascade)):
            option.setChecked(value)
        for title, widget in (("Имя Python", name), ("Подпись", label), ("Тип", kind),
                              ("Обязательное", required), ("Многострочное", multiline),
                              ("Объект ссылки", target), ("Связанный список", related),
                              ("Варианты через запятую", choices), ("По умолчанию", default),
                              ("Только чтение", read_only), ("Индекс", indexed), ("Уникальное", unique),
                              ("Удалять вместе с целью ссылки", cascade)):
            form.addRow(title, widget)
        form_tab = QLineEdit(item.form_tab)
        form_tab.setObjectName("field_form_tab")
        form_tab.setPlaceholderText("Пусто — поле вне вкладок")
        form_group = QLineEdit(item.form_group)
        form_group.setPlaceholderText("Пусто — основная часть формы")
        if self.table_part.currentIndex() == 0:
            form.addRow("Вкладка на форме", form_tab)
            form.addRow("Группа на форме", form_group)
        else:
            form_group.hide()
            form_tab.hide()
        visibility_field = QComboBox()
        visibility_field.addItem("Всегда", "")
        controllers = {
            candidate.name: candidate for candidate in fields
            if candidate is not item and candidate.kind in {"Boolean", "Choice"} and not candidate.visibility_field
        }
        for candidate in controllers.values():
            visibility_field.addItem(f"{candidate.label} ({candidate.name})", candidate.name)
        visibility_value = QComboBox()
        required_when_visible = QCheckBox("Требовать значение, когда поле показано")
        required_when_visible.setChecked(item.required_when_visible)

        def refresh_visibility_values() -> None:
            controller = controllers.get(visibility_field.currentData())
            visibility_value.clear()
            if controller is not None and controller.kind == "Boolean":
                visibility_value.addItem("Да", True)
                visibility_value.addItem("Нет", False)
            elif controller is not None:
                for option in controller.choices.split(","):
                    if value := option.strip():
                        visibility_value.addItem(value, value)
            configurable = controller is not None and not required.isChecked()
            visibility_value.setEnabled(configurable)
            required_when_visible.setEnabled(configurable)

        visibility_field.currentIndexChanged.connect(refresh_visibility_values)
        visibility_field.setCurrentIndex(max(0, visibility_field.findData(item.visibility_field)))
        refresh_visibility_values()
        visibility_value.setCurrentIndex(max(0, visibility_value.findData(item.visibility_value)))
        if has_preview:
            form.addRow("Показывать по условию", visibility_field)
            form.addRow("Когда значение равно", visibility_value)
            form.addRow(required_when_visible)
            visibility_field.setEnabled(not required.isChecked())
            required.toggled.connect(lambda checked: visibility_field.setEnabled(not checked))
            required.toggled.connect(refresh_visibility_values)
        else:
            visibility_field.hide()
            visibility_value.hide()
            required_when_visible.hide()
        form.addRow(limit_read)
        form.addRow("Роли для чтения", read_roles)
        form.addRow(limit_write)
        form.addRow("Роли через запятую", write_roles)

        def update_options(field_kind: str) -> None:
            form.setRowVisible(multiline, field_kind == "String")
            form.setRowVisible(target, field_kind == "Ref")
            form.setRowVisible(related, field_kind == "Ref" and self.table_part.currentIndex() == 0)
            form.setRowVisible(create_from, field_kind == "Ref" and has_preview)
            form.setRowVisible(mapping, field_kind == "Ref" and has_preview)
            form.setRowVisible(table_mapping, field_kind == "Ref" and has_preview)
            form.setRowVisible(cascade, field_kind == "Ref")
            form.setRowVisible(choices, field_kind == "Choice")

        kind.currentTextChanged.connect(update_options)
        update_options(kind.currentText())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        outer.addWidget(buttons)
        def accept_edit() -> None:
            new = name.text()
            if row is not None and new != item.name and (not identifier(new) or any(other.name == new for other in fields)):
                QMessageBox.warning(dialog, "Имя занято или недопустимо", "Укажите уникальное имя Python.")
                return
            dialog.accept()

        buttons.accepted.connect(accept_edit)
        buttons.rejected.connect(dialog.reject)

        def edited_field() -> StudioField:
            return StudioField(name.text(), label.text(), kind.currentText(), required.isChecked(),
                                multiline.isChecked(), target.currentText(), related.text(), choices.text(),
                                default.text(), read_only.isChecked(), indexed.isChecked(), unique.isChecked(),
                                cascade.isChecked(), (
                                    list(dict.fromkeys(role.strip() for role in write_roles.text().split(",") if role.strip()))
                                    if limit_write.isChecked() else None
                                ), (
                                    list(dict.fromkeys(role.strip() for role in read_roles.text().split(",") if role.strip()))
                                    if limit_read.isChecked() else None
                                ), form_group=form_group.text().strip(), form_tab=form_tab.text().strip(),
                                visibility_field=visibility_field.currentData() if has_preview and not required.isChecked() else "",
                                visibility_value=visibility_value.currentData() if visibility_value.count() else True,
                                required_when_visible=bool(
                                    has_preview and not required.isChecked() and visibility_field.currentData()
                                    and required_when_visible.isChecked()
                                ),
                                create_from=create_from.text().strip() if has_preview and kind.currentText() == "Ref" else "",
                                basis_fields=basis_fields.copy()
                                if has_preview and kind.currentText() == "Ref" and create_from.text().strip() else {},
                                basis_tables=basis_tables.copy()
                                if has_preview and kind.currentText() == "Ref" and create_from.text().strip() else {})

        if has_preview:
            preview = QWidget()
            preview_layout = QVBoxLayout(preview)
            preview_layout.addWidget(QLabel("Предпросмотр полей"))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            preview_layout.addWidget(scroll, 1)
            body.addWidget(preview)
            body.setSizes([450, 450])

            def refresh_preview() -> None:
                draft = edited_field()
                preview_fields = fields.copy()
                if row is None:
                    preview_fields.append(draft)
                else:
                    preview_fields[row] = draft
                panel, layout = self._form_preview(preview_fields)
                scroll.setWidget(panel)
                page = layout.pages.get(draft.form_tab)
                if page is not None:
                    layout.tabs.setCurrentWidget(page)

            for edit in (name, label, choices, form_tab, form_group):
                edit.textChanged.connect(refresh_preview)
            for checkbox in (required, multiline, read_only):
                checkbox.toggled.connect(refresh_preview)
            kind.currentTextChanged.connect(refresh_preview)
            visibility_field.currentIndexChanged.connect(refresh_preview)
            visibility_value.currentIndexChanged.connect(refresh_preview)
            required_when_visible.toggled.connect(refresh_preview)
            refresh_preview()

        if dialog.exec():
            value = edited_field()
            if row is None:
                fields.append(value)
            else:
                old_name = fields[row].name
                fields[row] = value
                part_index = self.table_part.currentIndex() - 1
                part = self.current.table_parts[part_index].name if part_index >= 0 else ""
                self.project.rename_field_references(self.current, part, old_name, value.name)
                self.number_field.setText(self.current.numbering_field)
                self._show_seeds()
                self._show_movements()
            self._show_fields()
        dialog.deleteLater()

    def _show_reports(self) -> None:
        self.report_list.clear()
        self.report_list.addItems([report.title for report in self.project.reports])

    def _remove_report(self) -> None:
        row = self.report_list.currentRow()
        if row >= 0:
            self.project.reports.pop(row)
            self._show_reports()

    def _edit_report(self, row: int) -> None:
        self._store()
        if not self.project.entities:
            self._error(ValueError("Добавьте объект, чтобы выбрать источник отчёта."))
            return
        config = self.project.reports[row] if row >= 0 else StudioReport("", "", "")
        dialog = QDialog(self)
        dialog.setWindowTitle("Отчёт")
        form = QFormLayout(dialog)
        name = QLineEdit(config.name)
        title = QLineEdit(config.title)
        source = QComboBox()
        for item in self.project.entities:
            source.addItem(f"Объект · {item.label}", f"entity:{item.name}")
        for item in self.project.registers:
            source.addItem(f"Регистр · {item.name} · {item.kind}", f"register:{item.name}")
        source.setCurrentIndex(max(0, source.findData(f"{config.source_kind}:{config.entity}")))
        columns, groups = QListWidget(), QListWidget()
        columns.setMaximumHeight(150)
        groups.setMaximumHeight(150)
        groups.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        choices = QWidget()
        choice_layout = QHBoxLayout(choices)
        choice_layout.setContentsMargins(0, 0, 0, 0)
        for label, widget in (("Колонки", columns), ("Порядок группировок", groups)):
            panel = QVBoxLayout()
            panel.addWidget(QLabel(label))
            panel.addWidget(widget)
            choice_layout.addLayout(panel, 1)
        groups.setToolTip("Отметьте поля группировки и перетащите их в нужном порядке.")
        sort = QComboBox()
        ref = QComboBox()
        through, totals = QCheckBox(), QCheckBox()
        through.setChecked(config.as_of)
        totals.setChecked(config.totals)
        for label, widget in (
            ("Имя Python", name), ("Заголовок", title), ("Источник", source),
            ("", choices), ("Сортировка", sort), ("Отбор по ссылке", ref),
            ("Дата отчёта", through), ("Итоги по числовым колонкам", totals),
        ):
            form.addRow(label, widget)

        def load_fields() -> None:
            kind, _, model_name = source.currentData().partition(":")
            is_register = kind == "register"
            original_source = (kind, model_name) == (config.source_kind, config.entity)
            form.setRowVisible(through, is_register)
            if is_register:
                register = next(item for item in self.project.registers if item.name == model_name)
                fields = [
                    (item.name, item.label, item.value_type == "Ссылка")
                    for item in register_report_columns(register)
                ]
            else:
                entity = next(item for item in self.project.entities if item.name == model_name)
                fields = [(item.name, item.label, item.kind == "Ref") for item in entity.fields]
            columns.clear()
            groups.clear()
            sort.clear()
            ref.clear()
            sort.addItem("По умолчанию", "")
            ref.addItem("Без отбора", "")
            for field_name, label, is_ref in fields:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, field_name)
                selected = not original_source or not config.columns or field_name in config.columns
                item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
                columns.addItem(item)
                sort.addItem(label, field_name)
                sort.addItem(f"{label} · по убыванию", "-" + field_name)
                if is_ref:
                    ref.addItem(label, field_name)
            labels = {field_name: label for field_name, label, _ in fields}
            checked_order = config.group_by if original_source else []
            order = [key for key in checked_order if key in labels]
            order.extend(key for key in labels if key not in order)
            for key in order:
                item = QListWidgetItem(labels[key])
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
                item.setCheckState(Qt.CheckState.Checked if key in checked_order else Qt.CheckState.Unchecked)
                groups.addItem(item)
            if original_source:
                sort.setCurrentIndex(max(0, sort.findData(config.order_by)))
                ref.setCurrentIndex(max(0, ref.findData(config.ref_filter)))

        source.currentIndexChanged.connect(load_fields)
        load_fields()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)
        def checked(widget: QListWidget) -> list[str]:
            return [widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(widget.count())
                    if widget.item(i).checkState() == Qt.CheckState.Checked]

        def accept_report() -> None:
            chosen = checked(columns)
            grouping = checked(groups)
            if not chosen or not set(grouping) <= set(chosen):
                QMessageBox.warning(dialog, "Колонки отчёта", "Выберите колонки, включая каждое поле группировки.")
                return
            source_kind, _, model_name = source.currentData().partition(":")
            value = StudioReport(
                name.text(), title.text(), model_name, chosen, sort.currentData(), ref.currentData(),
                source_kind, source_kind == "register" and through.isChecked(), totals.isChecked(), grouping,
            )
            if row >= 0:
                self.project.reports[row] = value
            else:
                self.project.reports.append(value)
            self._show_reports()
            dialog.accept()

        buttons.accepted.connect(accept_report)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()
        dialog.deleteLater()

    def _show_registers(self) -> None:
        self.register_list.clear()
        self.register_list.addItems([f"{item.name} · {item.kind}" for item in self.project.registers])

    def _edit_selected_register(self) -> None:
        row = self.register_list.currentRow()
        if row >= 0:
            self._edit_register(row)

    def _remove_register(self) -> None:
        row = self.register_list.currentRow()
        if row >= 0:
            self.project.registers.pop(row)
            self._show_registers()

    def _edit_register(self, row: int) -> None:
        self._store()
        config = self.project.registers[row] if row >= 0 else StudioRegister("")
        dialog = QDialog(self)
        dialog.setWindowTitle("Регистр")
        dialog.resize(840, 450)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        name = QLineEdit(config.name)
        kind = QComboBox()
        kind.addItems(REGISTER_TYPES)
        kind.setCurrentText(config.kind)
        form.addRow("Имя Python", name)
        form.addRow("Вид регистра", kind)
        layout.addLayout(form)
        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["Имя", "Подпись", "Назначение", "Тип", "Объект ссылки", "Обязательное"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(42)
        layout.addWidget(table)

        def add_member(member: StudioRegisterMember) -> None:
            index = table.rowCount()
            table.insertRow(index)
            table.setItem(index, 0, QTableWidgetItem(member.name))
            table.setItem(index, 1, QTableWidgetItem(member.label))
            role, value_type, target = QComboBox(), QComboBox(), QComboBox()
            role.addItems(["Измерение", "Ресурс"])
            role.setCurrentText(member.kind)
            target.addItems(["", *(item.name for item in self.project.entities)])
            target.setCurrentText(member.target)
            required = QTableWidgetItem()
            required.setCheckState(Qt.CheckState.Checked if member.required else Qt.CheckState.Unchecked)
            table.setItem(index, 5, required)
            for column, widget in enumerate((role, value_type, target), 2):
                table.setCellWidget(index, column, widget)

            def load_types() -> None:
                current = value_type.currentText()
                resource_types = INFORMATION_RESOURCE_TYPES if kind.currentText() == "Сведения" else RESOURCE_TYPES
                value_type.clear()
                value_type.addItems(DIMENSION_TYPES if role.currentText() == "Измерение" else resource_types)
                value_type.setCurrentText(current)

            role.currentTextChanged.connect(load_types)
            kind.currentTextChanged.connect(load_types)
            value_type.currentTextChanged.connect(lambda text: target.setEnabled(text == "Ссылка"))
            load_types()
            value_type.setCurrentText(member.value_type)

        for member in config.members:
            add_member(member)
        actions = QHBoxLayout()
        for text, callback in (
            ("Добавить измерение", lambda: add_member(StudioRegisterMember("", "", "Измерение", "Строка"))),
            ("Добавить ресурс", lambda: add_member(StudioRegisterMember("", "", "Ресурс", "Десятичное"))),
            ("Удалить поле", lambda: table.removeRow(table.currentRow())),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec():
            members = [
                StudioRegisterMember(
                    table.item(i, 0).text(), table.item(i, 1).text(),
                    table.cellWidget(i, 2).currentText(), table.cellWidget(i, 3).currentText(),
                    table.cellWidget(i, 4).currentText(), table.item(i, 5).checkState() == Qt.CheckState.Checked,
                )
                for i in range(table.rowCount())
            ]
            result = StudioRegister(name.text(), kind.currentText(), members)
            if row < 0:
                self.project.registers.append(result)
            else:
                self.project.registers[row] = result
                if config.name != result.name:
                    for report in self.project.reports:
                        if report.source_kind == "register" and report.entity == config.name:
                            report.entity = result.name
                    for entity in self.project.entities:
                        for rule in entity.movements:
                            if rule.register == config.name:
                                rule.register = result.name
                self._show_movements()
            self._show_registers()
        dialog.deleteLater()

    def _show_movements(self) -> None:
        self.movement_list.clear()
        document = self.current is not None and self.current.kind == "Документ"
        self.movement_editor.setEnabled(document)
        if not document or self.current is None:
            self.movement_hint.setText("Выберите документ в списке объектов.")
            return
        self.movement_hint.setText(f"Движения при проведении: {self.current.label}")
        registers = {item.name: item.kind for item in self.project.registers}
        self.movement_list.addItems([
            f"{rule.register} · {rule.table_part or 'Поля документа'} · "
            + ("Запись сведений" if registers.get(rule.register) == "Сведения" else ("Приход" if rule.income else "Расход"))
            for rule in self.current.movements
        ])

    def _edit_selected_movement(self) -> None:
        row = self.movement_list.currentRow()
        if row >= 0:
            self._edit_movement(row)

    def _remove_movement(self) -> None:
        row = self.movement_list.currentRow()
        if self.current is not None and row >= 0:
            self.current.movements.pop(row)
            self._show_movements()

    def _edit_movement(self, row: int) -> None:
        self._store()
        entity = self.current
        if entity is None or entity.kind != "Документ":
            return
        if not self.project.registers:
            self._error(ValueError("Сначала добавьте регистр на вкладке «Регистры»."))
            return
        rule = entity.movements[row] if row >= 0 else StudioMovement("")
        dialog = QDialog(self)
        dialog.setWindowTitle("Движение при проведении")
        dialog.resize(610, 390)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        register_box, part_box, direction = QComboBox(), QComboBox(), QComboBox()
        register_box.addItems([item.name for item in self.project.registers])
        register_box.setCurrentText(rule.register)
        part_box.addItem("Одно движение на документ", "")
        for part in entity.table_parts:
            part_box.addItem(f"Движение на каждую строку: {part.label}", part.name)
        part_box.setCurrentIndex(max(0, part_box.findData(rule.table_part)))
        direction.addItems(("Приход", "Расход"))
        direction.setCurrentIndex(0 if rule.income else 1)
        form.addRow("Регистр", register_box)
        form.addRow("Периодичность записи", part_box)
        form.addRow("Движение", direction)
        layout.addLayout(form)
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(["Поле регистра", "Источник"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setDefaultSectionSize(42)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(table)
        sources: dict[str, QComboBox] = {}

        def load_mapping() -> None:
            selected = {key: box.currentData() for key, box in sources.items()}
            fields = movement_fields(entity, part_box.currentData())
            register = self.project.registers[register_box.currentIndex()]
            has_direction = register.kind == "Накопление"
            form.setRowVisible(direction, has_direction)
            table.setRowCount(0)
            sources.clear()
            for index, member in enumerate(register.members):
                box = QComboBox()
                box.addItem("Выберите поле" if member.required else "Оставить пустым", "")
                for key, item in fields.items():
                    if movement_compatible(member, item):
                        origin = "Строка" if key.startswith("row:") else "Документ"
                        box.addItem(f"{origin} · {item.label}", key)
                default = rule.mapping.get(member.name, "") if register.name == rule.register else ""
                box.setCurrentIndex(max(0, box.findData(selected.get(member.name, default))))
                table.insertRow(index)
                table.setItem(index, 0, QTableWidgetItem(f"{member.label}{' *' if member.required else ''}"))
                table.setCellWidget(index, 1, box)
                sources[member.name] = box

        register_box.currentIndexChanged.connect(load_mapping)
        part_box.currentIndexChanged.connect(load_mapping)
        load_mapping()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        def accept_rule() -> None:
            value = StudioMovement(
                register_box.currentText(), part_box.currentData(),
                direction.isHidden() or direction.currentIndex() == 0,
                {name: box.currentData() for name, box in sources.items() if box.currentData()},
            )
            try:
                movement_source(replace(entity, movements=[value]), {
                    item.name: item for item in self.project.registers
                })
            except (ValueError, KeyError) as exc:
                QMessageBox.warning(dialog, "Проверьте движение", str(exc))
                return
            if row < 0:
                entity.movements.append(value)
            else:
                entity.movements[row] = value
            self._show_movements()
            dialog.accept()

        buttons.accepted.connect(accept_rule)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    def _edit_commands(self) -> None:
        self._store()
        entity = self.current
        if entity is None:
            return
        try:
            syntax = parse_studio_code(entity.code, "Код объекта", entity_name=entity.name)
            methods = studio_command_methods(syntax)
        except StudioSyntaxError as exc:
            self._error(exc)
            self._go_to_error()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Кнопки действий")
        dialog.resize(640, 420)
        layout = QVBoxLayout(dialog)
        hint = QLabel("Выберите метод из кода объекта и подпись кнопки. Роли можно указать через запятую. Пустая ячейка оставляет кнопку доступной при праве изменения.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Метод Python", "Подпись кнопки", "Роли"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(table)

        def add_row(name: str = "", label: str = "") -> None:
            row = table.rowCount()
            table.insertRow(row)
            method = QComboBox()
            method.addItems(methods)
            if name and name not in methods:
                method.addItem(name)
            if name:
                method.setCurrentText(name)
            table.setCellWidget(row, 0, method)
            table.setItem(row, 1, QTableWidgetItem(label))
            table.setItem(row, 2, QTableWidgetItem(", ".join(entity.command_roles.get(name, []))))
            table.selectRow(row)

        reserved = RESERVED_FIELDS | set(dir(Document)) | {item.name for item in [*entity.fields, *entity.table_parts]} | {
            node.name for node in syntax.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        } | {node.id for node in ast.walk(syntax) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
        if entity.owner_only:
            reserved.add("record_owner")
        if entity.workflow is not None:
            reserved.update((entity.workflow.field_name, "approval_assignee"))
        new_methods: set[str] = set()

        def create_method() -> None:
            index = 1
            while f"action_{index}" in reserved or f"action_{index}" in new_methods:
                index += 1
            name, accepted = QInputDialog.getText(
                dialog, "Новый метод", "Имя Python", text=f"action_{index}",
            )
            name = name.strip()
            if not accepted:
                return
            if not identifier(name) or name in reserved or name in new_methods:
                QMessageBox.warning(dialog, "Имя занято или недопустимо", "Укажите свободное имя Python.")
                return
            new_methods.add(name)
            methods.append(name)
            for row in range(table.rowCount()):
                picker = table.cellWidget(row, 0)
                if isinstance(picker, QComboBox):
                    picker.addItem(name)
            add.setEnabled(True)
            add_row(name, "Новое действие")

        for name, label in entity.commands.items():
            add_row(name, label)
        controls = QHBoxLayout()
        add = QPushButton("Добавить действие")
        add.setEnabled(bool(methods))
        add.clicked.connect(lambda: add_row())
        controls.addWidget(add)
        create = QPushButton("Создать метод…")
        create.clicked.connect(create_method)
        controls.addWidget(create)
        remove = QPushButton("Удалить")
        remove.clicked.connect(lambda: table.removeRow(table.currentRow()))
        controls.addWidget(remove)
        controls.addStretch()
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)

        first_body_line: int | None = None

        def save() -> None:
            nonlocal first_body_line
            configured: dict[str, str] = {}
            restrictions: dict[str, list[str]] = {}
            for row in range(table.rowCount()):
                method = table.cellWidget(row, 0)
                assert isinstance(method, QComboBox)
                name = method.currentText()
                label = table.item(row, 1).text().strip()
                if name not in methods or name in configured or not label:
                    QMessageBox.warning(dialog, "Проверьте действие", "Выберите уникальный метод и заполните подпись.")
                    return
                configured[name] = label
                roles = list(dict.fromkeys(role.strip() for role in table.item(row, 2).text().split(",") if role.strip()))
                if roles:
                    restrictions[name] = roles
            if restrictions and not self.project.local_accounts:
                QMessageBox.warning(dialog, "Учётные записи", "Включите учётные записи на вкладке «Приложение».")
                return
            entity.commands = configured
            entity.command_roles = restrictions
            for name in configured:
                if name not in new_methods:
                    continue
                code = entity.code.rstrip()
                prefix = code + "\n\n" if code else ""
                if first_body_line is None:
                    first_body_line = prefix.count("\n") + 1
                entity.code = prefix + f"def {name}(self) -> None:\n    pass\n"
            dialog.accept()

        buttons.accepted.connect(save)
        dialog.exec()
        dialog.deleteLater()
        if first_body_line is not None:
            self.code.setPlainText(entity.code)
            cursor = QTextCursor(self.code.document().findBlockByNumber(first_body_line))
            cursor.movePosition(QTextCursor.MoveOperation.Right, n=4)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            self.code.setTextCursor(cursor)
            page = self.tabs.widget(0)
            if isinstance(page, QScrollArea):
                page.ensureWidgetVisible(self.code)
            self.code.ensureCursorVisible()
            self.code.setFocus()

    def _edit_list_columns(self) -> None:
        self._store()
        entity = self.current
        if entity is None:
            return
        fields = entity.list_field_options()
        visible = entity.list_columns or list(fields)
        order = [name for name in visible if name in fields]
        order.extend(name for name in fields if name not in order)
        dialog = QDialog(self)
        dialog.setWindowTitle("Колонки списка")
        dialog.resize(500, 430)
        layout = QVBoxLayout(dialog)
        note = QLabel("Отметьте видимые колонки и перетащите их в нужном порядке.")
        note.setWordWrap(True)
        layout.addWidget(note)
        columns = QListWidget()
        columns.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        layout.addWidget(columns, 1)
        for name in order:
            item = QListWidgetItem(f"{fields[name]} ({name})", columns)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            item.setCheckState(Qt.CheckState.Checked if name in visible else Qt.CheckState.Unchecked)
        form = QFormLayout()
        sort = QComboBox()
        direction = QComboBox()
        direction.addItems(["По возрастанию", "По убыванию"])
        direction.setCurrentIndex(int(entity.list_order.startswith("-")))
        form.addRow("Начальная сортировка", sort)
        form.addRow("Направление", direction)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        confirm = buttons.button(QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        def selected() -> list[str]:
            return [
                item.data(Qt.ItemDataRole.UserRole) for index in range(columns.count())
                if (item := columns.item(index)).checkState() == Qt.CheckState.Checked
            ]

        def refresh_sort() -> None:
            previous = sort.currentData()
            names = selected()
            sort.clear()
            sort.addItem("По умолчанию", "")
            for name in names:
                sort.addItem(fields[name], name)
            sort.setCurrentIndex(max(0, sort.findData(previous)))
            confirm.setEnabled(bool(names) or not fields)

        columns.itemChanged.connect(refresh_sort)
        refresh_sort()
        sort.setCurrentIndex(max(0, sort.findData(entity.list_order.removeprefix("-"))))
        if dialog.exec():
            names = selected()
            entity.list_columns = names if names != list(fields) else []
            key = sort.currentData()
            entity.list_order = ("-" if direction.currentIndex() else "") + key if key else ""
        dialog.deleteLater()

    def _edit_print_form(self) -> None:
        self._store()
        entity = self.current
        if entity is None or entity.kind != "Документ":
            return
        config = entity.print_form or StudioPrintForm(entity.label, ["number", "date"])
        dialog = QDialog(self)
        dialog.setWindowTitle("Печатная форма документа")
        dialog.resize(620, 460)
        layout = QVBoxLayout(dialog)
        enabled = QCheckBox("Использовать печатную форму")
        enabled.setChecked(True)
        layout.addWidget(enabled)
        form = QFormLayout()
        title = QLineEdit(config.title)
        form.addRow("Заголовок", title)
        layout.addLayout(form)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        def field_list(label: str, items: list[tuple[str, str]], selected: list[str]) -> QListWidget:
            widget = QListWidget()
            widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            labels = dict(items)
            ordered = [name for name in selected if name in labels]
            ordered.extend(name for name in labels if name not in ordered)
            for name in ordered:
                item = QListWidgetItem(labels[name])
                item.setData(Qt.ItemDataRole.UserRole, name)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
                item.setCheckState(Qt.CheckState.Checked if name in selected else Qt.CheckState.Unchecked)
                widget.addItem(item)
            tabs.addTab(widget, label)
            return widget

        fields = [("number", "Номер"), ("date", "Дата"), ("posted", "Проведён")]
        fields.extend((item.name, item.label) for item in entity.fields)
        if entity.workflow is not None:
            fields.append((entity.workflow.field_name, "Статус согласования"))
        headers = field_list("Реквизиты", fields, config.fields)
        tables = {
            part.name: field_list(part.label, [(item.name, item.label) for item in part.fields],
                                  config.tables.get(part.name, []))
            for part in entity.table_parts
        }
        notes = QLabel("Отметьте нужные поля и перетащите их в порядке печати.")
        layout.addWidget(notes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        layout.addWidget(buttons)

        def checked(widget: QListWidget) -> list[str]:
            return [widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(widget.count())
                    if widget.item(i).checkState() == Qt.CheckState.Checked]

        def accept() -> None:
            if not enabled.isChecked():
                entity.print_form = None
                dialog.accept()
                return
            value = StudioPrintForm(title.text(), checked(headers), {
                name: selected for name, widget in tables.items() if (selected := checked(widget))
            })
            try:
                value.source(entity)
            except ValueError as exc:
                QMessageBox.warning(dialog, "Печатная форма", str(exc))
                return
            entity.print_form = value
            dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()
        dialog.deleteLater()

    def _edit_permissions(self) -> None:
        self._store()
        entity = self.current
        if entity is None:
            return
        actions = [
            ("read", "Чтение"), ("create", "Создание"), ("update", "Изменение"), ("delete", "Удаление"),
        ]
        if entity.kind == "Документ":
            actions.append(("post", "Проведение"))
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Права · {entity.label}")
        dialog.resize(720, 400)
        layout = QVBoxLayout(dialog)
        enabled = QCheckBox("Ограничить доступ по ролям")
        enabled.setChecked(entity.permissions is not None)
        layout.addWidget(enabled)
        note = QLabel("Администратор имеет доступ. Для остальных ролей отметьте чтение и нужные операции.")
        note.setWordWrap(True)
        layout.addWidget(note)
        table = QTableWidget(0, len(actions) + 1)
        table.setHorizontalHeaderLabels(["Роль", *(label for _, label in actions)])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setDefaultSectionSize(34)
        table.verticalHeader().setVisible(False)
        layout.addWidget(table)

        def add_role(role: str = "") -> None:
            index = table.rowCount()
            table.insertRow(index)
            table.setItem(index, 0, QTableWidgetItem(role))
            for column, (key, _) in enumerate(actions, 1):
                item = QTableWidgetItem()
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setCheckState(
                    Qt.CheckState.Checked if role in (entity.permissions or {}).get(key, ()) else Qt.CheckState.Unchecked
                )
                table.setItem(index, column, item)

        for role in sorted({role for roles in (entity.permissions or {}).values() for role in roles}):
            add_role(role)
        controls = QHBoxLayout()
        add = QPushButton("Добавить роль")
        add.clicked.connect(lambda: add_role())
        remove = QPushButton("Удалить роль")
        remove.clicked.connect(lambda: table.removeRow(table.currentRow()))
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addStretch()
        layout.addLayout(controls)
        for widget in (add, remove, table):
            enabled.toggled.connect(widget.setEnabled)
            widget.setEnabled(enabled.isChecked())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        layout.addWidget(buttons)

        def accept() -> None:
            if not enabled.isChecked():
                entity.permissions = None
                dialog.accept()
                return
            names = [table.item(row, 0).text().strip() for row in range(table.rowCount())]
            if any(not name for name in names) or len(set(names)) != len(names):
                QMessageBox.warning(dialog, "Права", "Укажите уникальные непустые имена ролей.")
                return
            entity.permissions = {
                key: [name for row, name in enumerate(names)
                      if table.item(row, column).checkState() == Qt.CheckState.Checked]
                for column, (key, _) in enumerate(actions, 1)
            }
            dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()
        dialog.deleteLater()

    def _show_workflow(self) -> None:
        self.workflow_list.clear()
        self.workflow_edit.setEnabled(self.current is not None)
        route = self.current.workflow if self.current is not None else None
        self.workflow_remove.setEnabled(route is not None)
        self.workflow_hint.setText(
            f"Начальный статус: {route.initial}" if route else
            "Выберите объект и настройте статусы и переходы."
        )
        if route is not None:
            self.workflow_list.addItems([
                f"{step.source} → {step.target} · {step.label}"
                + (f" · {', '.join(step.roles)}" if step.roles else "")
                for step in route.transitions
            ])

    def _remove_workflow(self) -> None:
        if self.current is not None and self.current.workflow is not None and QMessageBox.question(
            self, "Удалить маршрут", "Удалить статусы и переходы из проекта?",
        ) == QMessageBox.StandardButton.Yes:
            self.current.workflow = None
            self._show_workflow()

    def _edit_workflow(self) -> None:
        self._store()
        entity = self.current
        if entity is None:
            return
        route = entity.workflow or StudioWorkflow()
        dialog = QDialog(self)
        dialog.setWindowTitle("Маршрут согласования")
        dialog.resize(860, 500)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        states = QPlainTextEdit("\n".join(route.states))
        states.setPlaceholderText("Черновик\nНа согласовании\nСогласовано")
        states.setMaximumHeight(90)
        initial = QComboBox()
        post_states = QListWidget()
        post_states.setMaximumHeight(75)
        form.addRow("Статусы, по одному в строке", states)
        form.addRow("Начальный статус", initial)
        form.addRow("Проведение разрешено в статусах", post_states)
        form.setRowVisible(post_states, entity.kind == "Документ")
        post_states.setToolTip("Если ничего не отмечено, проведение доступно в любом статусе.")
        layout.addLayout(form)
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["Действие", "Из статуса", "В статус", "Роли через запятую", "По назначению"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(40)
        layout.addWidget(table)

        def state_names() -> list[str]:
            return [line.strip() for line in states.toPlainText().splitlines() if line.strip()]

        def populate(box: QComboBox, current: str) -> None:
            box.clear()
            box.addItems(["", *state_names()])
            box.setCurrentText(current)

        def sync_states() -> None:
            populate(initial, initial.currentText())
            marked = {
                post_states.item(i).text() for i in range(post_states.count())
                if post_states.item(i).checkState() == Qt.CheckState.Checked
            }
            post_states.clear()
            for name in state_names():
                item = QListWidgetItem(name)
                item.setCheckState(Qt.CheckState.Checked if name in marked else Qt.CheckState.Unchecked)
                post_states.addItem(item)
            for row in range(table.rowCount()):
                for column in (1, 2):
                    box = table.cellWidget(row, column)
                    populate(box, box.currentText())

        def add_step(step: StudioTransition) -> None:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(step.label))
            roles = QTableWidgetItem(", ".join(step.roles))
            roles.setToolTip("Пустое поле доступно любой роли.")
            table.setItem(row, 3, roles)
            assigned = QTableWidgetItem()
            assigned.setCheckState(Qt.CheckState.Checked if step.assigned_only else Qt.CheckState.Unchecked)
            table.setItem(row, 4, assigned)
            for column, current in ((1, step.source), (2, step.target)):
                box = QComboBox()
                populate(box, current)
                table.setCellWidget(row, column, box)

        states.textChanged.connect(sync_states)
        sync_states()
        initial.setCurrentText(route.initial)
        for i in range(post_states.count()):
            item = post_states.item(i)
            if item.text() in route.post_states:
                item.setCheckState(Qt.CheckState.Checked)
        for step in route.transitions:
            add_step(step)

        controls = QHBoxLayout()
        add = QPushButton("Добавить переход")
        add.clicked.connect(lambda: add_step(StudioTransition("", "", "")))
        remove = QPushButton("Удалить переход")
        remove.clicked.connect(lambda: table.removeRow(table.currentRow()))
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addStretch()
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        layout.addWidget(buttons)

        def accept() -> None:
            approved = [post_states.item(i).text() for i in range(post_states.count())
                        if post_states.item(i).checkState() == Qt.CheckState.Checked]
            value = StudioWorkflow(
                state_names(), initial.currentText(),
                [StudioTransition(table.item(i, 0).text(), table.cellWidget(i, 1).currentText(),
                                  table.cellWidget(i, 2).currentText(),
                                  [role.strip() for role in table.item(i, 3).text().split(",") if role.strip()],
                                  table.item(i, 4).checkState() == Qt.CheckState.Checked)
                 for i in range(table.rowCount())],
                approved if entity.kind == "Документ" else [], route.field_name,
            )
            try:
                value.validate()
                if any(item.name == value.field_name for item in entity.fields + entity.table_parts):
                    raise ValueError(f"Имя поля «{value.field_name}» уже используется.")
            except ValueError as exc:
                QMessageBox.warning(dialog, "Проверьте маршрут", str(exc))
                return
            entity.workflow = value
            self._show_workflow()
            dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()
        dialog.deleteLater()

    def _show_seeds(self) -> None:
        self.seed_list.clear()
        self.seed_list.addItems([f"{item.key} · {item.entity}" for item in self.project.initial_data])

    def _edit_selected_seed(self) -> None:
        row = self.seed_list.currentRow()
        if row >= 0:
            self._edit_seed(row)

    def _remove_seed(self) -> None:
        row = self.seed_list.currentRow()
        if row >= 0:
            self.project.initial_data.pop(row)
            self._show_seeds()

    def _edit_seed(self, row: int) -> None:
        self._store()
        if not self.project.entities:
            self._error(ValueError("Сначала добавьте объект проекта."))
            return
        record = self.project.initial_data[row] if row >= 0 else StudioSeed("", "")
        dialog = QDialog(self)
        dialog.setWindowTitle("Начальная запись")
        dialog.resize(760, 490)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        key = QLineEdit(record.key)
        entity_box = QComboBox()
        for entity in self.project.entities:
            entity_box.addItem(entity.label, entity.name)
        entity_box.setCurrentIndex(max(0, entity_box.findData(record.entity)))
        form.addRow("Ключ записи", key)
        form.addRow("Объект", entity_box)
        layout.addLayout(form)
        note = QLabel("Пустое поле использует значение по умолчанию. Для ссылки укажите ключ другой начальной записи.")
        note.setWordWrap(True)
        layout.addWidget(note)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        columns: list[str] = []
        header = QTableWidget()
        tables: dict[str, tuple[QTableWidget, list[str]]] = {}

        def load_fields() -> None:
            while tabs.count():
                page = tabs.widget(0)
                tabs.removeTab(0)
                if page is not header:
                    page.deleteLater()
            tables.clear()
            entity = self.project.entities[entity_box.currentIndex()]
            fields = list(entity.fields)
            if entity.kind == "Документ":
                fields.append(StudioField("date", "Дата", "DateTime"))
            header.clear()
            header.setRowCount(len(fields))
            header.setColumnCount(2)
            header.setHorizontalHeaderLabels(["Поле", "Значение"])
            header.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            values = record.values if entity.name == record.entity else {}
            columns[:] = [item.name for item in fields]
            for i, item in enumerate(fields):
                label = QTableWidgetItem(f"{item.label} · {item.kind}")
                label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsEditable)
                header.setItem(i, 0, label)
                header.setItem(i, 1, QTableWidgetItem(values.get(item.name, "")))
            tabs.addTab(header, "Реквизиты")

            for part in entity.table_parts:
                page = QWidget()
                part_layout = QVBoxLayout(page)
                table = QTableWidget(0, len(part.fields))
                table.setHorizontalHeaderLabels([item.label for item in part.fields])
                table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
                part_layout.addWidget(table)
                controls = QHBoxLayout()
                add = QPushButton("Добавить строку")
                add.clicked.connect(lambda _checked=False, widget=table: widget.insertRow(widget.rowCount()))
                remove = QPushButton("Удалить строку")
                remove.clicked.connect(lambda _checked=False, widget=table: widget.removeRow(widget.currentRow()))
                controls.addWidget(add)
                controls.addWidget(remove)
                controls.addStretch()
                part_layout.addLayout(controls)
                names = [item.name for item in part.fields]
                tables[part.name] = (table, names)
                for values in record.tables.get(part.name, []) if entity.name == record.entity else []:
                    i = table.rowCount()
                    table.insertRow(i)
                    for column, name in enumerate(names):
                        table.setItem(i, column, QTableWidgetItem(values.get(name, "")))
                tabs.addTab(page, part.label)

        def text_at(table: QTableWidget, row: int, column: int) -> str:
            item = table.item(row, column)
            return item.text() if item is not None else ""

        def accept() -> None:
            name = key.text().strip()
            if not name or any(item is not record and item.key == name for item in self.project.initial_data):
                QMessageBox.warning(dialog, "Начальные данные", "Укажите уникальный ключ записи.")
                return
            values = {name: val for i, name in enumerate(columns) if (val := text_at(header, i, 1))}
            parts = {
                part: [
                    {name: val for j, name in enumerate(names) if (val := text_at(table, i, j))}
                    for i in range(table.rowCount())
                ]
                for part, (table, names) in tables.items() if table.rowCount()
            }
            edited = StudioSeed(name, entity_box.currentData(), values, parts)
            if row >= 0:
                self.project.initial_data[row] = edited
            else:
                self.project.initial_data.append(edited)
            self._show_seeds()
            dialog.accept()

        entity_box.currentIndexChanged.connect(load_fields)
        load_fields()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    def _tab_changed(self, index: int) -> None:
        self._store()
        self._show_movements()
        if self.tabs.widget(index) is self.source:
            try:
                self.source.setPlainText(self.project.source())
            except (ValueError, SyntaxError, KeyError) as exc:
                self.source.setPlainText(str(exc))
                if isinstance(exc, StudioSyntaxError):
                    self._set_error_location(exc)

    def save_project(self) -> bool:
        self._store()
        if self.path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект", "", "DECUI Studio (*.decui.json)")
            if not path:
                return False
            self.path = Path(path if path.endswith(".decui.json") else path + ".decui.json")
        try:
            self.project.write(self.path)
        except OSError as exc:
            self._error(exc)
            return False
        self.saved = repr(self.project)
        self.statusBar().showMessage(f"Сохранено: {self.path.name}")
        return True

    def _confirm_changes(self) -> bool:
        self._store()
        if repr(self.project) == self.saved:
            return True
        result = QMessageBox.question(
            self, "Изменения проекта", "Сохранить изменения?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        return result == QMessageBox.StandardButton.Discard or (
            result == QMessageBox.StandardButton.Save and self.save_project()
        )

    def new_project(self) -> None:
        if self._confirm_changes():
            self.project, self.path = StudioProject(), None
            self.saved = repr(self.project)
            self._load_project()
            self.tabs.setCurrentIndex(1)

    def choose_template(self) -> None:
        menu = QMenu(self)
        menu.addAction("Цели и задачи", lambda: self.load_template())
        menu.addAction("Товары и накладные", lambda: self.load_template(invoice_template()))
        menu.addAction("Заказы и накладные", lambda: self.load_template(order_invoice_template()))
        menu.addAction("Согласование накладных", lambda: self.load_template(approval_template()))
        button = self.sender()
        if isinstance(button, QPushButton):
            menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        menu.deleteLater()

    def load_template(self, project: StudioProject | None = None) -> None:
        if self._confirm_changes():
            self.project, self.path = project or goal_template(), None
            self.saved = repr(StudioProject())
            self._load_project()
            self.tabs.setCurrentIndex(0)

    def open_project(self) -> None:
        if not self._confirm_changes():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Открыть проект", "", "DECUI Studio (*.decui.json)")
        if path:
            try:
                project = StudioProject.read(Path(path))
            except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
                self._error(exc)
                return
            self.project, self.path = project, Path(path)
            self.saved = repr(project)
            self._load_project()
            self.statusBar().showMessage(f"Открыт проект: {self.path.name}")

    def export(self) -> None:
        self._store()
        directory = QFileDialog.getExistingDirectory(self, "Выберите папку для app.py")
        if directory:
            try:
                entry = self.project.export(Path(directory))
            except (OSError, ValueError, SyntaxError, KeyError) as exc:
                self._error(exc)
                return
            self.statusBar().showMessage(f"Экспортировано: {entry} · сборка: decui build app.py")

    def check(self) -> None:
        self._launch(check=True)

    def preview(self) -> None:
        self._launch(check=False)

    def preview_backup(self) -> None:
        self._choose_backup("backup", "Открыть резервную копию")

    def check_backup_migration(self) -> None:
        self._choose_backup("migration", "Выбрать базу для проверки миграции")

    def _choose_backup(self, operation: str, title: str) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.statusBar().showMessage("Сначала остановите текущий запуск.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, title, "", "SQLite (*.sqlite3 *.sqlite *.db);;Все файлы (*)",
        )
        if path:
            self._start_project(operation, backup=Path(path))

    def _launch(self, *, check: bool) -> None:
        self._start_project("check" if check else "preview")

    def build(self) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.statusBar().showMessage("Сначала остановите текущий запуск.")
            return
        self._store()
        suffix = ".exe" if sys.platform == "win32" else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить исполняемый файл", self.project.app_id + suffix,
            "Исполняемый файл (*.exe)" if suffix else "Все файлы (*)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if path:
            if suffix and not path.lower().endswith(suffix):
                path += suffix
            self._start_project("build", Path(path))

    def _start_project(
        self, operation: str, destination: Path | None = None, *, backup: Path | None = None,
    ) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.statusBar().showMessage("Сначала остановите текущий запуск.")
            return
        self._store()
        self._entry_snapshot = None
        self._source_spans.clear()
        self._set_error_location(None)
        try:
            if destination is not None and os.path.lexists(destination):
                raise FileExistsError(f"Файл уже существует: {destination}")
            self.preview_dir = TemporaryDirectory(prefix="decui-studio-")
            directory = Path(self.preview_dir.name)
            preview_db = None if operation == "build" else directory / "preview.sqlite3"
            has_accounts = None
            if operation == "backup" and backup is not None and preview_db is not None:
                has_accounts = stage_decui_backup(backup, preview_db)
            entry = self.project.export(
                directory, preview_database=preview_db, backup_has_accounts=has_accounts, spans=self._source_spans,
            )
            self._entry_snapshot = (entry, entry.read_text(encoding="utf-8"))
            if operation == "build" and destination is not None:
                dist = directory / "dist"
                args = build_arguments(entry, name="application", dist_dir=dist)
                filename = "application.exe" if sys.platform == "win32" else "application"
                self._build_output = dist / filename, destination
            elif operation in {"check", "migration"}:
                args = ["-m", "decui.cli", "check", str(entry)]
                if backup is not None:
                    args.extend(("--backup", str(backup)))
            else:
                args = [str(entry)]
        except (OSError, ValueError, SyntaxError, KeyError, sqlite3.Error) as exc:
            self._cleanup_process()
            self._error(exc)
            return
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", os.pathsep.join(filter(None, (str(Path(__file__).resolve().parent.parent), os.getenv("PYTHONPATH")))))
        env.insert("PYTHONIOENCODING", "utf-8")
        env.remove("DECUI_STUDIO_PREVIEW")
        if operation in {"preview", "backup"}:
            env.insert("DECUI_STUDIO_PREVIEW", "1")
        self.process.setProcessEnvironment(env)
        self.process.setWorkingDirectory(str(directory))
        self._output_decoder.reset()
        self._output_line_tail = ""
        self.console.clear()
        self.statusBar().showMessage({
            "build": "Сборка приложения", "check": "Проверка проекта", "preview": "Предпросмотр приложения",
            "backup": "Предпросмотр резервной копии", "migration": "Проверка миграции на копии",
        }[operation])
        self.process.start(sys.executable, args)

    def _process_error(self, error: QProcess.ProcessError) -> None:
        self.console.appendPlainText(self.process.errorString())
        if error == QProcess.ProcessError.FailedToStart:
            self._finished(-1, QProcess.ExitStatus.CrashExit)

    def _read_output(self, *, final: bool = False) -> None:
        chunk = bytes(self.process.readAllStandardOutput())
        output = self._output_decoder.decode(chunk, final=final)
        if output:
            self.console.moveCursor(QTextCursor.MoveOperation.End)
            self.console.insertPlainText(output)
            self.console.ensureCursorVisible()
            complete, _, tail = (self._output_line_tail + output).rpartition("\n")
            self._output_line_tail = tail[-4096:]
            if re.search(r'File "[^"]+", line \d+', complete):
                self._capture_runtime_error()

    def _finished(self, result: int, status: QProcess.ExitStatus) -> None:
        self._read_output(final=True)
        try:
            if result != 0 or status != QProcess.ExitStatus.NormalExit:
                self.statusBar().showMessage("Процесс завершился с ошибкой.")
                self._capture_runtime_error()
            elif self._build_output is not None:
                source, destination = self._build_output
                publish_new_file(source, destination)
                self.statusBar().showMessage(f"Собрано: {destination}")
                self.console.appendPlainText(f"Исполняемый файл: {destination}")
            else:
                self.statusBar().showMessage("Готово")
        except OSError as exc:
            self._error(exc)
        finally:
            self._cleanup_process()

    def _cleanup_process(self) -> None:
        self._build_output = None
        if self.preview_dir is not None:
            self.preview_dir.cleanup()
            self.preview_dir = None

    def _capture_runtime_error(self) -> None:
        if self._entry_snapshot is None:
            return
        entry, source = self._entry_snapshot
        matches = re.findall(r'File "([^"]+)", line (\d+)', self.console.toPlainText())
        for filename, number in reversed(matches):
            path = Path(filename)
            if path == entry or path == Path(entry.name):
                generated_line = int(number)
                location = StudioSyntaxError(
                    SyntaxError("Ошибка выполнения проекта", ("app.py", generated_line, 1, None)),
                    "app.py", source=source,
                )
                for span in self._source_spans:
                    offset = generated_line - span.generated_start
                    if 0 <= offset < span.line_count:
                        line = span.editor_start + offset
                        location = StudioSyntaxError(
                            SyntaxError("Ошибка выполнения проекта", (span.location, line, 1, None)),
                            span.location, entity_name=span.entity_name, source=source,
                        )
                        location.generated_line = generated_line
                        location.editor_snapshot = span.text
                        break
                self._set_error_location(location)
                return

    def _set_error_location(self, location: StudioSyntaxError | None) -> None:
        self._error_location = location
        self.diagnostic_button.setEnabled(location is not None)

    def _go_to_error(self) -> None:
        location = self._error_location
        if location is None:
            return
        if location.editor_snapshot is not None:
            if location.entity_name:
                entity = next((item for item in self.project.entities if item.name == location.entity_name), None)
                current_text = self.code.toPlainText() if entity is self.current and entity is not None else (
                    entity.code if entity is not None else None
                )
            else:
                target = self.imports if location.filename == "Код перед объектами" else self.logic
                current_text = target.toPlainText()
            if current_text != location.editor_snapshot:
                generated_line = location.generated_line or 1
                location = StudioSyntaxError(
                    SyntaxError("Ошибка выполнения проекта", ("app.py", generated_line, 1, None)),
                    "app.py", source=location.source,
                )
        if location.entity_name:
            index = next((i for i, entity in enumerate(self.project.entities)
                          if entity.name == location.entity_name), -1)
            if index < 0:
                return
            self.entities.setCurrentRow(index)
            self.tabs.setCurrentIndex(0)
            editor = self.code
            page = self.tabs.currentWidget()
            if isinstance(page, QScrollArea):
                page.ensureWidgetVisible(editor)
        elif location.filename in ("Код перед объектами", "Код после объектов"):
            self.tabs.setCurrentIndex(1)
            editor = self.imports if location.filename == "Код перед объектами" else self.logic
        else:
            self.tabs.setCurrentWidget(self.source)
            self.source.setPlainText(location.source)
            editor = self.source
        line = max(0, (location.lineno or 1) - 1)
        block = editor.document().findBlockByNumber(line)
        if block.isValid():
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            editor.setTextCursor(cursor)
            editor.centerCursor()
        editor.setFocus()

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, StudioSyntaxError):
            self._set_error_location(exc)
        self.console.appendPlainText(str(exc))
        self.statusBar().showMessage("Проверьте сообщение в журнале.")

    def closeEvent(self, event) -> None:
        if not self._confirm_changes():
            event.ignore()
            return
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        super().closeEvent(event)


def run_studio() -> int:
    app = QApplication(sys.argv[:1])
    window = StudioWindow()
    window.show()
    return app.exec()
