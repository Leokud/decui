"""DECUI desktop application authoring environment."""

from __future__ import annotations

import ast
import codecs
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QProcess, QProcessEnvironment, QSettings, QSignalBlocker, Qt, QTimer, QUrl
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
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from decui import Boolean, Choice, Date, DateTime, Decimal as DecimalField, Integer, Ref, String, Time
from decui.cli import build_arguments
from decui.entity import Document
from decui.fields import BasisTable, Field
from decui.files import copy_sqlite, publish_new_file, stage_decui_backup
from decui.studio_form_designer import FormDesignerWidget
from decui.studio_lsp import LspLocation, LspSymbol, MappedDiagnostic, StudioLspBridge
from decui.studio_project import (
    CURRENCY_PRESETS,
    DIMENSION_TYPES,
    ENTITY_TYPES,
    FIELD_TYPES,
    INFORMATION_RESOURCE_TYPES,
    NUMBERING_PATTERNS,
    REGISTER_TYPES,
    RESERVED_FIELDS,
    RESOURCE_TYPES,
    StudioCalculation,
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
    crm_delivery_template,
    entity_report_fields,
    goal_template,
    identifier,
    invoice_template,
    movement_compatible,
    movement_fields,
    movement_source,
    order_invoice_template,
    parse_studio_code,
    purchase_prices_template,
    register_report_columns,
    studio_command_methods,
    suggest_identifier,
)
from decui.studio_import import import_python_project
from decui.studio_tools import (
    ENTITY_SNIPPETS,
    LOGIC_SNIPPETS,
    CalculationsValidationsDialog,
    ChoiceFilterDialog,
    DatabaseInspectorDialog,
    FormPreviewDialog,
    ProjectOverviewWidget,
    RemindersDialog,
    ReportAdvancedDialog,
    ReportPreviewDialog,
    RolesOverviewDialog,
)
from decui.ui.editors import create_editor, get_editor_value
from decui.ui.form import FieldLayout
from decui.ui.python_editor import EditorDiagnostic, PythonEditor
from decui.ui.theme import DESKTOP_STYLE

_MAX_RECENT_PROJECTS = 8


def _inspect_db_accounts(path: Path) -> bool | None:
    """Return account presence for an existing DECUI DB, or None for a brand-new/empty DB."""
    if not path.is_file() or path.stat().st_size == 0:
        return None
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "_erp_config" not in names or conn.execute(
            "SELECT 1 FROM _erp_config WHERE key = 'schema_hash'"
        ).fetchone() is None:
            return None
        return "_erp_accounts" in names


def _field_summary(item: StudioField) -> str:
    """Short human-readable summary of field options for the object field table."""
    parts: list[str] = []
    if item.kind == "Ref" and item.target:
        parts.append(f"→ {item.target}")
    if item.auto_fill:
        parts.append(f"авто:{item.auto_fill}")
    if item.choice_filter:
        parts.append("фильтр")
    if item.create_from:
        parts.append("на основании")
    if item.related_list:
        parts.append("список")
    if item.kind == "Choice" and item.multi:
        parts.append("несколько")
    if item.kind == "String" and item.multiline:
        parts.append("многостр.")
    if item.kind == "String" and item.placeholder:
        parts.append("подсказка")
    if item.kind == "Decimal" and item.currency:
        parts.append(item.currency)
    if item.default:
        parts.append(f"={item.default}")
    if item.read_only:
        parts.append("чтение")
    if item.unique:
        parts.append("уник.")
    if item.form_tab:
        parts.append(f"[{item.form_tab}]")
    elif item.form_group:
        parts.append(f"({item.form_group})")
    if item.visibility_field:
        parts.append("усл.")
    if not item.visible:
        parts.append("скрытое")
    return ", ".join(parts)


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
        self._loading = False
        self._dirty = False
        self._entity_auto_name = False
        self._source_edit_mode = False
        self._source_dirty = False
        self._mapped_diagnostics: list[MappedDiagnostic] = []
        self._undo_stack: list[str] = []
        self._redo_stack: list[str] = []
        self._lsp_bridge = StudioLspBridge(self._lsp_snapshot, parent=self)
        self._lsp_bridge.status_changed.connect(self._on_lsp_status)
        self._lsp_bridge.mapped_diagnostics.connect(self._on_lsp_diagnostics)
        self._lsp_bridge.symbols_updated.connect(self._on_lsp_symbols)
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
        auto_lsp = os.environ.get("DECUI_DISABLE_LSP") != "1" and (
            not os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("DECUI_ENABLE_LSP") == "1"
        )
        if auto_lsp:
            QTimer.singleShot(50, self._lsp_bridge.start)

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
        self._recent_menu = actions_menu.addMenu("Недавние проекты")
        self._refresh_recent_menu()
        actions_menu.addAction("Импортировать Python (app.py)…", self.import_python)
        actions_menu.addAction("Сохранить как…", self.save_project_as)
        actions_menu.addAction("Открыть папку проекта", self._open_project_folder)
        actions_menu.addSeparator()
        self._undo_action = actions_menu.addAction("Отменить изменение структуры", self.undo)
        self._redo_action = actions_menu.addAction("Повторить изменение структуры", self.redo)
        self._undo_action.setEnabled(False)
        self._redo_action.setEnabled(False)
        actions_menu.addSeparator()
        actions_menu.addAction("Запустить с рабочей базой", self.preview_persistent)
        actions_menu.addAction("Запустить с копией…", self.preview_backup)
        actions_menu.addAction("Запустить автотест проекта", self.run_smoke_tests)
        actions_menu.addSeparator()
        db_menu = actions_menu.addMenu("База данных")
        db_menu.addAction("Инспектор базы данных…", self.inspect_database)
        db_menu.addAction("Выбрать рабочую базу…", self._choose_working_db)
        db_menu.addAction("Проверить миграцию рабочей базы", self.check_working_db_migration)
        db_menu.addAction("Проверить миграцию на копии…", self.check_backup_migration)
        db_menu.addAction("Создать резервную копию рабочей базы…", self.backup_working_db)
        db_menu.addAction("Сбросить рабочую базу…", self.reset_working_db)
        actions_menu.addSeparator()
        actions_menu.addAction("Экспорт Python", self.export)
        actions_menu.addAction("Экспорт полного проекта…", self.export_full)
        actions_menu.addAction("Собрать", self.build)
        actions_menu.addAction("Остановить", self.process.terminate)
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
        self._shortcut("Сохранить проект как", QKeySequence("Ctrl+Shift+S"), self.save_project_as)

        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        sidebar = QWidget()
        left = QVBoxLayout(sidebar)
        header = QLabel("Объекты")
        header.setProperty("heading", True)
        left.addWidget(header)
        self.entity_filter = QLineEdit()
        self.entity_filter.setPlaceholderText("Поиск объектов…")
        self.entity_filter.setClearButtonEnabled(True)
        self.entity_filter.textChanged.connect(lambda _text: self._refresh_entity_list())
        left.addWidget(self.entity_filter)
        self.entities = QListWidget()
        self.entities.currentRowChanged.connect(self._select)
        left.addWidget(self.entities)
        controls = QHBoxLayout()
        for label, slot in (("Добавить", self._add_entity), ("Удалить", self._remove_entity)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            controls.addWidget(button)
        left.addLayout(controls)
        order_controls = QHBoxLayout()
        for label, slot in (
            ("Копия", self._duplicate_entity),
            ("Выше", lambda: self._move_entity(-1)),
            ("Ниже", lambda: self._move_entity(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            order_controls.addWidget(button)
        left.addLayout(order_controls)
        preset_btn = QPushButton("Добавить из заготовки")
        preset_menu = QMenu(preset_btn)
        preset_menu.addAction("Справочник «Товары»", lambda: self._add_preset_entity("product"))
        preset_menu.addAction("Справочник «Контрагенты»", lambda: self._add_preset_entity("partner"))
        preset_menu.addAction("Документ «Заказ» с позициями", lambda: self._add_preset_entity("order"))
        preset_menu.addAction("Справочник «Задачи со сроком»", lambda: self._add_preset_entity("task"))
        preset_btn.setMenu(preset_menu)
        left.addWidget(preset_btn)
        splitter.addWidget(sidebar)

        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        self._source_bar = QWidget()
        source_bar_layout = QHBoxLayout(self._source_bar)
        source_bar_layout.setContentsMargins(4, 2, 4, 2)
        self.source_edit_check = QCheckBox("Редактировать app.py напрямую (LSP IDE)")
        self.source_edit_check.toggled.connect(self._toggle_source_edit_mode)
        source_bar_layout.addWidget(self.source_edit_check)
        source_bar_layout.addStretch()
        self.source_apply_btn = QPushButton("Применить код в Studio")
        self.source_apply_btn.setProperty("primary", True)
        self.source_apply_btn.setEnabled(False)
        self.source_apply_btn.clicked.connect(self._apply_source_to_project)
        source_bar_layout.addWidget(self.source_apply_btn)
        self.source_reset_btn = QPushButton("Обновить из конструктора")
        self.source_reset_btn.clicked.connect(self._reset_source_from_project)
        source_bar_layout.addWidget(self.source_reset_btn)
        self._source_bar.setVisible(False)
        right_layout.addWidget(self._source_bar)
        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, 1)
        splitter.addWidget(right_container)
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
        self.name, self.label = QLineEdit(), QLineEdit()
        self.section = QComboBox()
        self.section.setEditable(True)
        self.section.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.section.setPlaceholderText("Например, Склад или Продажи/Заказы")
        self.prefix, self.number_field = QLineEdit(), QLineEdit()
        self.number_field.setPlaceholderText("number для документа, code для справочника")
        self.numbering_pattern = QComboBox()
        self.numbering_pattern.setEditable(True)
        self.numbering_pattern.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.numbering_pattern.addItems(NUMBERING_PATTERNS)
        self.numbering_pattern.setPlaceholderText("По умолчанию: {seq:08d}")
        self.numbering_sample = QLabel()
        self.numbering_sample.setStyleSheet("color: #566577; font-size: 12px;")
        for signal in (
            self.prefix.textChanged,
            self.number_field.textChanged,
            self.numbering_pattern.currentTextChanged,
        ):
            signal.connect(self._update_numbering_sample)
        self.check_sequence = QCheckBox("Контроль хронологии проведения")
        self.check_sequence.setToolTip("Запрещать проведение задним числом после более поздних проведённых документов (__sequence__ = True)")
        self.owner_only = QCheckBox("Только владелец и администратор")
        self.kind = QComboBox()
        self.kind.addItems(ENTITY_TYPES)
        for title, value in (("Имя Python", self.name), ("Подпись", self.label),
                             ("Тип", self.kind), ("Раздел меню", self.section),
                             ("Префикс номера", self.prefix), ("Поле номера", self.number_field),
                             ("Шаблон номера", self.numbering_pattern), ("", self.numbering_sample),
                             ("Последовательность", self.check_sequence),
                             ("Доступ к записям", self.owner_only)):
            form.addRow(title, value)
        self.label.textChanged.connect(self._on_entity_label_changed)
        self.name.textEdited.connect(self._stop_entity_auto_name)
        for text_edit in (self.name, self.label, self.prefix, self.number_field):
            text_edit.textChanged.connect(lambda _t: self._mark_dirty())
        self.section.currentTextChanged.connect(lambda _t: self._mark_dirty())
        self.numbering_pattern.currentTextChanged.connect(lambda _t: self._mark_dirty())
        self.check_sequence.toggled.connect(lambda _c: self._mark_dirty())
        self.owner_only.toggled.connect(lambda _c: self._mark_dirty())
        self.permissions_button = QPushButton("Настроить права по ролям")
        self.permissions_button.clicked.connect(self._edit_permissions)
        form.addRow("Права", self.permissions_button)
        self.print_button = QPushButton("Настроить печатную форму")
        self.print_button.clicked.connect(self._edit_print_form)
        self.kind.currentTextChanged.connect(self._on_kind_changed)
        self.kind.currentTextChanged.connect(lambda _t: self._mark_dirty())
        form.addRow("Печать", self.print_button)
        list_columns = QPushButton("Настроить колонки списка")
        list_columns.clicked.connect(self._edit_list_columns)
        form.addRow("Список", list_columns)
        preview_form = QPushButton("Предпросмотр формы")
        preview_form.clicked.connect(self._preview_entity_form)
        form.addRow("Форма", preview_form)
        self.rules_button = QPushButton("Расчёты и проверки…")
        self.rules_button.clicked.connect(self._edit_rules)
        form.addRow("Правила", self.rules_button)
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
        self.fields = QTableWidget(0, 5)
        self.fields.setHorizontalHeaderLabels(["Имя", "Подпись", "Тип", "Обязательное", "Параметры"])
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
        for label, slot in (("Добавить", self._add_field), ("Изменить", self._edit_field),
                            ("Копия", self._duplicate_field), ("Удалить", self._remove_field)):
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
        self.code.setPlaceholderText("Методы класса, команды, обработчики сохранения. Вводите код без внешнего отступа.")
        self.code.textChanged.connect(self._mark_dirty)
        code_header = QHBoxLayout()
        code_header.addWidget(QLabel("Код внутри класса"))
        code_header.addStretch()
        snippet_btn = QPushButton("Шаблон кода")
        snippet_btn.setMenu(self._snippet_menu(self.code, ENTITY_SNIPPETS))
        code_header.addWidget(snippet_btn)
        commands = QPushButton("Кнопки действий…")
        commands.clicked.connect(self._edit_commands)
        code_header.addWidget(commands)
        editor.addLayout(code_header)
        editor.addWidget(self.code, 2)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings = QWidget()
        settings_scroll.setWidget(settings)
        self.tabs.addTab(settings_scroll, "Приложение")
        config = QFormLayout(settings)
        self.title = QLineEdit()
        self.app_id = QLineEdit()
        self.start_target = QComboBox()
        self.window_width = QSpinBox()
        self.window_width.setRange(400, 3840)
        self.window_height = QSpinBox()
        self.window_height.setRange(300, 2160)
        size_row = QHBoxLayout()
        size_row.addWidget(self.window_width)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.window_height)
        size_row.addStretch()
        self.local_accounts = QCheckBox("Требовать вход в приложение")
        self.preview_role = QComboBox()
        self.preview_role.setEditable(True)
        self.preview_role.setPlaceholderText("Пусто — роль из настроек приложения")
        role_row = QHBoxLayout()
        role_row.addWidget(self.preview_role, 1)
        roles_overview_btn = QPushButton("Обзор ролей…")
        roles_overview_btn.clicked.connect(self._show_roles_overview)
        role_row.addWidget(roles_overview_btn)
        self.working_db = QLineEdit()
        self.working_db.setPlaceholderText("Авто (рядом с файлом проекта) или выберите .sqlite3")
        db_row = QHBoxLayout()
        db_row.addWidget(self.working_db, 1)
        db_pick = QPushButton("Выбрать…")
        db_pick.clicked.connect(self._choose_working_db)
        db_inspect = QPushButton("Открыть инспектор…")
        db_inspect.clicked.connect(self.inspect_database)
        db_row.addWidget(db_pick)
        db_row.addWidget(db_inspect)
        self.reminders_button = QPushButton("Напоминания о сроках…")
        self.reminders_button.clicked.connect(self._edit_reminders)
        self.imports = PythonEditor()
        self.imports.setMinimumHeight(80)
        self.imports.setPlaceholderText("Импорты и константы для объявлений объектов.")
        self.logic = PythonEditor()
        self.logic.setMinimumHeight(110)
        self.logic.setPlaceholderText("Отчёты, регистры и функции после объектов. configure_app(app) вызывается перед запуском.")
        for edit in (self.title, self.app_id, self.working_db):
            edit.textChanged.connect(lambda _t: self._mark_dirty())
        self.local_accounts.toggled.connect(lambda _c: self._mark_dirty())
        self.preview_role.currentTextChanged.connect(lambda _t: self._mark_dirty())
        self.start_target.currentIndexChanged.connect(lambda _i: self._mark_dirty())
        self.window_width.valueChanged.connect(lambda _v: self._mark_dirty())
        self.window_height.valueChanged.connect(lambda _v: self._mark_dirty())
        self.imports.textChanged.connect(self._mark_dirty)
        self.logic.textChanged.connect(self._mark_dirty)
        config.addRow("Название окна", self.title)
        config.addRow("Идентификатор", self.app_id)
        config.addRow("Стартовый экран", self.start_target)
        config.addRow("Размер окна", size_row)
        config.addRow("Учётные записи", self.local_accounts)
        config.addRow("Роль предпросмотра", role_row)
        config.addRow("Рабочая база", db_row)
        config.addRow("Напоминания", self.reminders_button)
        migrations = QPushButton("Переименовать колонки…")
        migrations.clicked.connect(self._edit_column_renames)
        config.addRow("Миграция базы", migrations)
        table_migrations = QPushButton("Переименовать табличные части…")
        table_migrations.clicked.connect(lambda: self._edit_column_renames(table_parts=True))
        config.addRow("", table_migrations)
        config.addRow("Код перед объектами", self.imports)
        logic_box = QWidget()
        logic_box_layout = QVBoxLayout(logic_box)
        logic_box_layout.setContentsMargins(0, 0, 0, 0)
        logic_header = QHBoxLayout()
        logic_header.addStretch()
        logic_snippets = QPushButton("Шаблон кода")
        logic_snippets.setMenu(self._snippet_menu(self.logic, LOGIC_SNIPPETS))
        logic_header.addWidget(logic_snippets)
        logic_box_layout.addLayout(logic_header)
        logic_box_layout.addWidget(self.logic)
        config.addRow("Код после объектов", logic_box)

        self.form_designer = FormDesignerWidget()
        self.form_designer.changed.connect(self._on_form_designer_changed)
        self.form_designer.undo_requested.connect(self._push_undo)
        self.tabs.addTab(self.form_designer, "Форма")

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
            ("Предпросмотр", self._preview_selected_report),
            ("Удалить", self._remove_report),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            report_actions.addWidget(button)
        report_actions.addStretch()
        report_layout.addLayout(report_actions)
        report_order = QHBoxLayout()
        for title, callback in (
            ("Копия", self._duplicate_report),
            ("Выше", lambda: self._move_report(-1)),
            ("Ниже", lambda: self._move_report(1)),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            report_order.addWidget(button)
        report_order.addStretch()
        report_layout.addLayout(report_order)

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
        register_order = QHBoxLayout()
        for title, callback in (
            ("Копия", self._duplicate_register),
            ("Выше", lambda: self._move_register(-1)),
            ("Ниже", lambda: self._move_register(1)),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            register_order.addWidget(button)
        register_order.addStretch()
        register_layout.addLayout(register_order)

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
            ("Копия", self._duplicate_seed),
            ("Удалить", self._remove_seed),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            seed_actions.addWidget(button)
        seed_actions.addStretch()
        initial_layout.addLayout(seed_actions)
        seed_import_row = QHBoxLayout()
        capture_btn = QPushButton("Загрузить из базы…")
        capture_btn.clicked.connect(self._capture_seeds_from_db)
        seed_import_row.addWidget(capture_btn)
        seed_import_row.addStretch()
        initial_layout.addLayout(seed_import_row)

        self.source = PythonEditor()
        self.source.setReadOnly(True)
        self.source.textChanged.connect(self._on_source_edited)
        self.tabs.addTab(self.source, "Код проекта")

        self.overview = ProjectOverviewWidget(self._jump_to_diagnostic)
        self.tabs.addTab(self.overview, "Обзор")

        self.tabs.currentChanged.connect(self._tab_changed)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setPlaceholderText("Результаты проверки и запуска")
        self.problems_list = QListWidget()
        self.problems_list.itemDoubleClicked.connect(self._jump_to_problem_item)
        self.symbols_tree = QTreeWidget()
        self.symbols_tree.setHeaderLabels(["Символ", "Подпись / тип", "Строка"])
        self.symbols_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.symbols_tree.itemDoubleClicked.connect(self._jump_to_symbol_item)

        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setMaximumHeight(135)
        self.bottom_tabs.addTab(self.console, "Журнал проекта")
        self.bottom_tabs.addTab(self.problems_list, "Проблемы (0)")
        self.bottom_tabs.addTab(self.symbols_tree, "Символы кода")

        output_bar = QHBoxLayout()
        output_bar.addWidget(QLabel("Панель IDE и журнал"))
        output_bar.addStretch()
        copy_code = QPushButton("Копировать код")
        copy_code.clicked.connect(self._copy_project_code)
        output_bar.addWidget(copy_code)
        clear_log = QPushButton("Очистить")
        clear_log.clicked.connect(self.console.clear)
        output_bar.addWidget(clear_log)
        self.diagnostic_button = QPushButton("К строке ошибки")
        self.diagnostic_button.setEnabled(False)
        self.diagnostic_button.clicked.connect(self._go_to_error)
        output_bar.addWidget(self.diagnostic_button)
        layout.addLayout(output_bar)
        layout.addWidget(self.bottom_tabs)

        self.cursor_status_label = QLabel("Стр 1, Кол 1")
        self.lsp_status_label = QLabel("LSP: отключён" if os.environ.get("DECUI_DISABLE_LSP") == "1" else "LSP: запуск…")
        self.statusBar().addPermanentWidget(self.cursor_status_label)
        self.statusBar().addPermanentWidget(self.lsp_status_label)
        self.statusBar().showMessage("Добавьте объект и поля, чтобы запустить приложение.")

        current_entity = lambda: (self.current.name if self.current is not None else "")
        self.code.bind_lsp(self._lsp_bridge, "Код объекта", current_entity, self._on_lsp_definition)
        self.form_designer.form_code_editor.bind_lsp(self._lsp_bridge, "Код формы", current_entity, self._on_lsp_definition)
        self.imports.bind_lsp(self._lsp_bridge, "Код перед объектами", on_definition=self._on_lsp_definition)
        self.logic.bind_lsp(self._lsp_bridge, "Код после объектов", on_definition=self._on_lsp_definition)
        self.source.bind_lsp(self._lsp_bridge, "Код проекта", on_definition=self._on_lsp_definition)
        for ed in (self.code, self.form_designer.form_code_editor, self.imports, self.logic, self.source):
            ed.cursor_location_changed.connect(self._update_cursor_indicator)

    def _snippet_menu(self, editor: PythonEditor, snippets: tuple[tuple[str, str], ...]) -> QMenu:
        menu = QMenu(self)
        for label, code in snippets:
            menu.addAction(label, lambda _checked=False, target=editor, body=code: self._insert_snippet(target, body))
        return menu

    def _insert_snippet(self, editor: PythonEditor, snippet: str) -> None:
        current = editor.toPlainText().rstrip()
        prefix = "\n\n" if current else ""
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(prefix + snippet + "\n")
        editor.setTextCursor(cursor)
        editor.setFocus()

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
        crm = QPushButton("Открыть пример «CRM и доставка»")
        crm.clicked.connect(lambda: self.load_template(crm_delivery_template()))
        layout.addWidget(crm, alignment=Qt.AlignmentFlag.AlignLeft)
        purchases = QPushButton("Открыть пример «Закупки и прайс-листы»")
        purchases.clicked.connect(lambda: self.load_template(purchase_prices_template()))
        layout.addWidget(purchases, alignment=Qt.AlignmentFlag.AlignLeft)
        open_btn = QPushButton("Открыть файл проекта…")
        open_btn.clicked.connect(self.open_project)
        layout.addWidget(open_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        import_btn = QPushButton("Импортировать из Python (app.py)…")
        import_btn.clicked.connect(self.import_python)
        layout.addWidget(import_btn, alignment=Qt.AlignmentFlag.AlignLeft)
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

    def _on_kind_changed(self, value: str) -> None:
        is_doc = value == "Документ"
        self.print_button.setEnabled(is_doc)
        self.check_sequence.setEnabled(is_doc)
        self._update_numbering_sample()

    def _update_numbering_sample(self) -> None:
        prefix = self.prefix.text().strip()
        if not prefix:
            self.numbering_sample.setText("Автонумерация отключена (укажите префикс).")
            return
        pattern = self.numbering_pattern.currentText().strip() or "{seq:08d}"
        try:
            sample = pattern.format(prefix=prefix, year=2026, month="11", seq=1)
        except (ValueError, KeyError, IndexError):
            self.numbering_sample.setText("Проверьте шаблон ({prefix}, {year}, {month}, {seq:06d}).")
            return
        self.numbering_sample.setText(f"Пример номера: {sample}")

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
            self._push_undo()
            if table_parts:
                self.project.table_part_renames = changes
            else:
                self.project.column_renames = changes
            self._mark_dirty()
            dialog.accept()

        buttons.accepted.connect(save)
        dialog.exec()
        dialog.deleteLater()

    def _refresh_app_pickers(self) -> None:
        selected_start = self.project.start_target
        self.start_target.blockSignals(True)
        self.start_target.clear()
        self.start_target.addItem("Первый объект", "")
        for entity in self.project.entities:
            self.start_target.addItem(f"Объект · {entity.label}", entity.name)
        for rep in self.project.reports:
            self.start_target.addItem(f"Отчёт · {rep.title}", rep.name)
        self.start_target.setCurrentIndex(max(0, self.start_target.findData(selected_start)))
        self.start_target.blockSignals(False)

        current_role = self.preview_role.currentText()
        self.preview_role.blockSignals(True)
        self.preview_role.clear()
        self.preview_role.addItems(["", *self.project.all_roles()])
        self.preview_role.setCurrentText(current_role)
        self.preview_role.blockSignals(False)

        current_section = self.section.currentText()
        self.section.blockSignals(True)
        self.section.clear()
        self.section.addItems(["", *self.project.all_sections()])
        self.section.setCurrentText(current_section)
        self.section.blockSignals(False)

    def _refresh_reminders_label(self) -> None:
        count = len(self.project.reminders)
        self.reminders_button.setText(f"Напоминания о сроках ({count})…" if count else "Напоминания о сроках…")

    def _refresh_rules_label(self) -> None:
        entity = self.current
        if entity is None:
            self.rules_button.setText("Расчёты и проверки…")
            return
        total = len(entity.calculations) + len(entity.validations)
        self.rules_button.setText(f"Расчёты и проверки ({total})…" if total else "Расчёты и проверки…")

    def _mark_dirty(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._update_window_title()
        self._lsp_bridge.schedule_sync()

    def _refresh_undo_actions(self) -> None:
        if hasattr(self, "_undo_action"):
            self._undo_action.setEnabled(bool(self._undo_stack))
        if hasattr(self, "_redo_action"):
            self._redo_action.setEnabled(bool(self._redo_stack))

    def _push_undo(self) -> None:
        if self._loading:
            return
        self._store()
        snapshot = self.project.to_json()
        if not self._undo_stack or self._undo_stack[-1] != snapshot:
            self._undo_stack.append(snapshot)
            if len(self._undo_stack) > 40:
                self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._refresh_undo_actions()

    def _restore_snapshot(self, raw_json: str) -> None:
        selected_name = self.current.name if self.current is not None else ""
        self.project = StudioProject.from_dict(json.loads(raw_json))
        self._load_project(clear_history=False, preferred_entity=selected_name)
        self._refresh_undo_actions()

    def undo(self) -> None:
        if not self._undo_stack:
            return
        self._store()
        current_json = self.project.to_json()
        target_json = self._undo_stack.pop()
        self._redo_stack.append(current_json)
        self._restore_snapshot(target_json)
        self.statusBar().showMessage("Изменение структуры отменено.")

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._store()
        current_json = self.project.to_json()
        target_json = self._redo_stack.pop()
        self._undo_stack.append(current_json)
        self._restore_snapshot(target_json)
        self.statusBar().showMessage("Изменение структуры повторено.")

    def _sync_editor_completions(self) -> None:
        words: list[str] = []
        for entity in self.project.entities:
            if entity.name:
                words.append(entity.name)
            for field in entity.fields:
                if field.name:
                    words.append(field.name)
            for part in entity.table_parts:
                if part.name:
                    words.append(part.name)
                for col in part.fields:
                    if col.name:
                        words.append(col.name)
        for register in self.project.registers:
            if register.name:
                words.append(register.name)
            for member in register.members:
                if member.name:
                    words.append(member.name)
        for rep in self.project.reports:
            if rep.name:
                words.append(rep.name)
        if self.current is not None:
            for field in self.current.fields:
                if field.name:
                    words.append(f"self.{field.name}")
            for part in self.current.table_parts:
                if part.name:
                    words.append(f"self.{part.name}")
            for method_name in self.current.commands:
                words.append(f"self.{method_name}")
        for editor in (self.code, self.imports, self.logic, self.form_designer.form_code_editor, self.source):
            editor.set_completion_words(words)
        if not self._loading:
            self._lsp_bridge.schedule_sync()

    def _edit_rules(self) -> None:
        self._store()
        entity = self.current
        if entity is None:
            return
        dialog = CalculationsValidationsDialog(self, entity, self.project)
        if dialog.exec():
            self._push_undo()
            entity.calculations = dialog.result_calculations
            entity.validations = dialog.result_validations
            self._refresh_rules_label()
            self._mark_dirty()
        dialog.deleteLater()

    def _on_entity_label_changed(self, label: str) -> None:
        self._rename_item(label)
        if not self._entity_auto_name or self._loading or self.current is None:
            return
        if suggested := suggest_identifier(label, pascal=True):
            taken = {item.name for item in self.project.entities if item is not self.current}
            candidate = suggested
            suffix = 2
            while candidate in taken:
                candidate = f"{suggested}{suffix}"
                suffix += 1
            with QSignalBlocker(self.name):
                self.name.setText(candidate)

    def _stop_entity_auto_name(self, _text: str = "") -> None:
        self._entity_auto_name = False

    def _add_preset_entity(self, preset_key: str) -> None:
        presets: dict[str, StudioEntity] = {
            "product": StudioEntity(
                "Product", "Товары", kind="Справочник", section="Справочники",
                numbering_prefix="PRD", numbering_field="code",
                fields=[
                    StudioField("name", "Наименование", required=True),
                    StudioField("sku", "Артикул", indexed=True),
                    StudioField("price", "Цена", kind="Decimal", default="0.00", min_value="0", currency="RUB"),
                ],
            ),
            "partner": StudioEntity(
                "Partner", "Контрагенты", kind="Справочник", section="Справочники",
                fields=[
                    StudioField("name", "Название", required=True),
                    StudioField("phone", "Телефон"),
                    StudioField("email", "Email"),
                    StudioField("inn", "ИНН"),
                ],
            ),
            "order": StudioEntity(
                "CustomerOrder", "Заказы покупателей", kind="Документ", section="Документы",
                numbering_prefix="ORD",
                fields=[
                    StudioField("customer", "Покупатель"),
                    StudioField("total", "Итого", kind="Decimal", read_only=True, default="0.00", currency="RUB"),
                ],
                table_parts=[StudioTablePart("Items", "Позиции", [
                    StudioField("title", "Позиция", required=True),
                    StudioField("quantity", "Количество", kind="Integer", required=True, default="1", min_value="1"),
                    StudioField("price", "Цена", kind="Decimal", default="0.00", min_value="0"),
                    StudioField("amount", "Сумма", kind="Decimal", read_only=True, default="0.00"),
                ])],
                calculations=[
                    StudioCalculation("Items.amount", "row_formula", source_part="Items", left="quantity", operator="*", right="price"),
                    StudioCalculation("total", "table_total", source_part="Items", left="amount", operator="sum"),
                ],
            ),
            "task": StudioEntity(
                "Task", "Задачи", kind="Справочник", section="Планирование",
                fields=[
                    StudioField("name", "Задача", required=True),
                    StudioField("due_date", "Срок", kind="Date", default="today"),
                    StudioField("due_time", "Время", kind="Time"),
                    StudioField("status", "Статус", kind="Choice", choices="Новая, В работе, Готово", default="Новая"),
                ],
            ),
        }
        template = presets.get(preset_key)
        if template is None:
            return
        self._push_undo()
        taken = {item.name for item in self.project.entities}
        candidate = template.name
        suffix = 2
        while candidate in taken:
            candidate = f"{template.name}{suffix}"
            suffix += 1
        entity = deepcopy(template)
        entity.name = candidate
        self.project.entities.append(entity)
        self.entity_filter.clear()
        self._refresh_entity_list(select_index=len(self.project.entities) - 1)
        self._sync_editor_completions()
        self._mark_dirty()
        self.tabs.setCurrentIndex(0)

    def _copy_project_code(self) -> None:
        self._store()
        try:
            code = self.project.source()
        except (ValueError, SyntaxError, KeyError) as exc:
            self._error(exc)
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(code)
        self.statusBar().showMessage("Код проекта скопирован в буфер обмена.")

    def import_python(self) -> None:
        if not self._confirm_changes():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Импортировать приложение Python", "", "Python (*.py)")
        if path:
            self._import_python_path(Path(path))

    def _import_python_path(self, path: Path) -> bool:
        try:
            source_text = path.read_text(encoding="utf-8")
            project, warnings = import_python_project(
                source_text,
                default_title=path.stem.replace("_", " ").title(),
                default_app_id=suggest_identifier(path.stem) or "imported_app",
            )
        except (OSError, ValueError, SyntaxError) as exc:
            self._error(exc)
            return False
        self.project = project
        self.path = None
        self.saved = repr(StudioProject())
        self._load_project()
        self.tabs.setCurrentIndex(0 if self.project.entities else 1)
        self.console.clear()
        self.console.appendPlainText(
            f"Импортировано из {path.name}: объектов — {len(project.entities)}, "
            f"регистров — {len(project.registers)}, отчётов — {len(project.reports)}."
        )
        for warning in warnings:
            self.console.appendPlainText(f"⚠ {warning}")
        self.statusBar().showMessage(f"Импортирован проект из {path.name}")
        return True

    def _load_project(self, *, clear_history: bool = True, preferred_entity: str = "") -> None:
        self._loading = True
        try:
            self._set_error_location(None)
            self._entity_auto_name = False
            self.current = None
            self.title.setText(self.project.title)
            self.app_id.setText(self.project.app_id)
            self.window_width.setValue(max(400, self.project.window_width))
            self.window_height.setValue(max(300, self.project.window_height))
            self.local_accounts.setChecked(self.project.local_accounts)
            self.working_db.setText(self.project.working_db)
            self.imports.setPlainText(self.project.imports)
            self.logic.setPlainText(self.project.logic)
            self._refresh_app_pickers()
            self.preview_role.setCurrentText(self.project.preview_role)
            self.start_target.setCurrentIndex(max(0, self.start_target.findData(self.project.start_target)))
            self._refresh_reminders_label()
            self._refresh_rules_label()
            self._show_reports()
            self._show_registers()
            self._show_seeds()
            self.entity_filter.clear()
            if clear_history:
                self._undo_stack.clear()
                self._redo_stack.clear()
            self._source_edit_mode = False
            self._source_dirty = False
            if hasattr(self, "source_edit_check"):
                with QSignalBlocker(self.source_edit_check):
                    self.source_edit_check.setChecked(False)
                self.source.setReadOnly(True)
                self.source_apply_btn.setEnabled(False)
            self._dirty = repr(self.project) != self.saved
        finally:
            self._loading = False
        target_index = next(
            (i for i, ent in enumerate(self.project.entities) if preferred_entity and ent.name == preferred_entity),
            0 if self.project.entities else -1,
        )
        self._refresh_entity_list(select_index=target_index)
        self._sync_editor_completions()
        self._refresh_undo_actions()
        self._update_window_title()
        self._lsp_bridge.schedule_sync()

    def _refresh_entity_list(self, *, select_index: int | None = None) -> None:
        if select_index is None:
            select_index = self._selected_entity_index()
        needle = self.entity_filter.text().strip().casefold()
        self.entities.blockSignals(True)
        self.entities.clear()
        selected_row = -1
        for idx, entity in enumerate(self.project.entities):
            if needle and needle not in f"{entity.label} {entity.name} {entity.section} {entity.kind}".casefold():
                continue
            item = QListWidgetItem(entity.label or "Без подписи")
            item.setData(Qt.ItemDataRole.UserRole, idx)
            self.entities.addItem(item)
            if idx == select_index:
                selected_row = self.entities.count() - 1
        self.entities.blockSignals(False)
        if selected_row >= 0:
            self.entities.setCurrentRow(selected_row)
            self._select(selected_row)
        elif self.entities.count() > 0 and not needle:
            self.entities.setCurrentRow(0)
            self._select(0)
        elif needle and selected_row < 0:
            self._select(-1)
        else:
            self._select(-1)

    def _selected_entity_index(self, row: int | None = None) -> int:
        if row is None:
            row = self.entities.currentRow()
        if row < 0:
            return -1
        item = self.entities.item(row)
        if item is not None:
            idx = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(idx, int):
                return idx
        return row if 0 <= row < len(self.project.entities) else -1

    def _store(self) -> None:
        if self._loading:
            return
        self.project.title = self.title.text()
        self.project.app_id = self.app_id.text()
        self.project.preview_role = self.preview_role.currentText()
        self.project.local_accounts = self.local_accounts.isChecked()
        self.project.start_target = self.start_target.currentData() or ""
        self.project.window_width = self.window_width.value()
        self.project.window_height = self.window_height.value()
        self.project.working_db = self.working_db.text().strip()
        self.project.imports = self.imports.toPlainText()
        self.project.logic = self.logic.toPlainText()
        if self.current is not None:
            previous = self.current.name
            name = self.name.text()
            if previous and name and previous != name and name not in {entity.name for entity in self.project.entities}:
                if previous in self.project.table_part_renames:
                    self.project.table_part_renames[name] = self.project.table_part_renames.pop(previous)
                for key in list(self.project.column_renames):
                    if key.split(".", 1)[0] == previous:
                        self.project.column_renames[name + key[len(previous):]] = self.project.column_renames.pop(key)
                if self.project.start_target == previous:
                    self.project.start_target = name
                for reminder in self.project.reminders:
                    if reminder.entity == previous:
                        reminder.entity = name
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
            self.current.check_sequence = self.check_sequence.isChecked()
            self.current.section = self.section.currentText()
            self.current.code = self.code.toPlainText()
            self.current.form_code = self.form_designer.form_code_editor.toPlainText()
            self.current.numbering_prefix = self.prefix.text()
            self.current.numbering_field = self.number_field.text()
            self.current.numbering_pattern = self.numbering_pattern.currentText().strip()

    def _select(self, row: int) -> None:
        self._store()
        self._entity_auto_name = False
        index = self._selected_entity_index(row)
        self.current = self.project.entities[index] if 0 <= index < len(self.project.entities) else None
        self.welcome.setVisible(self.current is None)
        self.editor.setVisible(self.current is not None)
        entity = self.current or StudioEntity("", "")
        self.name.setText(entity.name)
        self.label.setText(entity.label)
        self.kind.setCurrentText(entity.kind)
        self._on_kind_changed(entity.kind)
        self.check_sequence.setChecked(entity.check_sequence)
        self.owner_only.setChecked(entity.owner_only)
        self._refresh_app_pickers()
        self.section.setCurrentText(entity.section)
        self.code.setPlainText(entity.code)
        self.prefix.setText(entity.numbering_prefix)
        self.number_field.setText(entity.numbering_field)
        self.numbering_pattern.setCurrentText(entity.numbering_pattern)
        self.form_designer.load_entity(self.current, self.project)
        self._update_numbering_sample()
        self._refresh_rules_label()
        self._show_table_parts()
        self._show_movements()
        self._show_workflow()
        self._sync_editor_completions()
        self._apply_diagnostics_to_current()

    def _rename_item(self, label: str) -> None:
        item = self.entities.currentItem()
        if item is not None:
            item.setText(label or "Без подписи")

    def _add_entity(self) -> None:
        self._push_undo()
        names = {item.name for item in self.project.entities} | {self.name.text()}
        number = 1
        while f"Entity{number}" in names:
            number += 1
        entity = StudioEntity(f"Entity{number}", "Новый объект")
        self.project.entities.append(entity)
        self.entity_filter.clear()
        self._refresh_entity_list(select_index=len(self.project.entities) - 1)
        self._entity_auto_name = True
        self._sync_editor_completions()
        self._mark_dirty()
        self.tabs.setCurrentIndex(0)
        self.label.selectAll()
        self.label.setFocus()

    def _duplicate_entity(self) -> None:
        index = self._selected_entity_index()
        if index < 0:
            return
        self._push_undo()
        source = self.project.entities[index]
        names = {item.name for item in self.project.entities}
        candidate = f"{source.name}Copy"
        counter = 2
        while candidate in names:
            candidate = f"{source.name}Copy{counter}"
            counter += 1
        cloned = deepcopy(source)
        cloned.name = candidate
        cloned.label = f"{source.label} (копия)"
        self.project.entities.insert(index + 1, cloned)
        self.entity_filter.clear()
        self._refresh_entity_list(select_index=index + 1)
        self._sync_editor_completions()
        self._mark_dirty()
        self.tabs.setCurrentIndex(0)

    def _move_entity(self, offset: int) -> None:
        index = self._selected_entity_index()
        target = index + offset
        if 0 <= index < len(self.project.entities) and 0 <= target < len(self.project.entities):
            self._push_undo()
            item = self.project.entities.pop(index)
            self.project.entities.insert(target, item)
            self._refresh_entity_list(select_index=target)
            self._mark_dirty()

    def _remove_entity(self) -> None:
        index = self._selected_entity_index()
        if index < 0:
            return
        removed = self.project.entities[index]
        dependents: list[str] = []
        for other in self.project.entities:
            if other is removed:
                continue
            for item in [*other.fields, *(f for p in other.table_parts for f in p.fields)]:
                if item.kind == "Ref" and item.target == removed.name:
                    dependents.append(f"ссылка «{other.label}.{item.label}»")
        for reg in self.project.registers:
            if any(m.value_type == "Ссылка" and m.target == removed.name for m in reg.members):
                dependents.append(f"регистр «{reg.name}»")
        for rep in self.project.reports:
            if rep.source_kind == "entity" and rep.entity == removed.name:
                dependents.append(f"отчёт «{rep.title}»")
        for rem in self.project.reminders:
            if rem.entity == removed.name:
                dependents.append(f"напоминание «{rem.title or rem.entity}»")
        if any(s.entity == removed.name for s in self.project.initial_data):
            dependents.append("начальные данные")
        prompt = "Удалить объект и его поля?"
        if dependents:
            preview = ", ".join(dict.fromkeys(dependents[:5]))
            extra = f" (и ещё {len(dependents) - 5})" if len(dependents) > 5 else ""
            prompt += f"\n\nТакже будут очищены или удалены связанные ссылки: {preview}{extra}."
        if QMessageBox.question(self, "Удалить объект", prompt) != QMessageBox.StandardButton.Yes:
            return
        self._push_undo()
        self.current = None
        self.project.entities.pop(index)
        for other in self.project.entities:
            for item in [*other.fields, *(f for p in other.table_parts for f in p.fields)]:
                if item.kind == "Ref" and item.target == removed.name:
                    item.target = ""
                    item.choice_filter.clear()
                    item.create_from = ""
                    item.basis_fields.clear()
                    item.basis_tables.clear()
                    item.related_list = ""
        for reg in self.project.registers:
            for member in reg.members:
                if member.value_type == "Ссылка" and member.target == removed.name:
                    member.target = ""
                    member.representation_field = ""
        self.project.reports = [
            rep for rep in self.project.reports
            if not (rep.source_kind == "entity" and rep.entity == removed.name)
        ]
        self.project.reminders = [rem for rem in self.project.reminders if rem.entity != removed.name]
        self.project.initial_data = [seed for seed in self.project.initial_data if seed.entity != removed.name]
        if self.project.start_target == removed.name:
            self.project.start_target = ""
        self._show_reports()
        self._show_seeds()
        self._refresh_reminders_label()
        self._refresh_app_pickers()
        next_idx = min(index, len(self.project.entities) - 1)
        self._refresh_entity_list(select_index=next_idx)
        self._sync_editor_completions()
        self._mark_dirty()

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
        name_auto = index is None and not part.name

        def sync_name(text: str) -> None:
            if name_auto and (suggested := suggest_identifier(text, pascal=True)):
                with QSignalBlocker(name):
                    name.setText(suggested)

        def stop_auto(_text: str) -> None:
            nonlocal name_auto
            name_auto = False

        label.textChanged.connect(sync_name)
        name.textChanged.connect(stop_auto)
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
            self._push_undo()
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
            self._sync_editor_completions()
            self._mark_dirty()
        dialog.deleteLater()

    def _rename_table_part(self) -> None:
        if self.table_part.currentIndex() > 0:
            self._edit_table_part(self.table_part.currentIndex() - 1)

    def _remove_table_part(self) -> None:
        index = self.table_part.currentIndex() - 1
        if self.current is not None and index >= 0 and QMessageBox.question(
            self, "Удалить табличную часть", "Удалить таблицу и её колонки?",
        ) == QMessageBox.StandardButton.Yes:
            self._push_undo()
            self.current.table_parts.pop(index)
            self._show_table_parts()
            self._sync_editor_completions()
            self._mark_dirty()

    def _show_fields(self) -> None:
        header = self.fields.verticalHeader()
        blocked = header.blockSignals(True)
        self.fields.setRowCount(0)
        header.blockSignals(blocked)
        if self.current is None:
            return
        for row, item in enumerate(self._field_list()):
            self.fields.insertRow(row)
            values = (item.name, item.label, item.kind, "Да" if item.required else "Нет", _field_summary(item))
            for col, value in enumerate(values):
                self.fields.setItem(row, col, QTableWidgetItem(value))

    def _field_order_changed(self, _logical: int, old: int, new: int) -> None:
        fields = self._field_list()
        if 0 <= old < len(fields) and 0 <= new < len(fields):
            self._push_undo()
            fields.insert(new, fields.pop(old))
            self._show_fields()
            self.fields.selectRow(new)
            self._mark_dirty()

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

    def _duplicate_field(self) -> None:
        row = self.fields.currentRow()
        fields = self._field_list()
        if self.current is None or row < 0 or row >= len(fields):
            return
        self._push_undo()
        existing = {item.name for item in fields} | RESERVED_FIELDS
        source = fields[row]
        candidate = f"{source.name}_copy"
        counter = 2
        while candidate in existing:
            candidate = f"{source.name}_copy_{counter}"
            counter += 1
        cloned = deepcopy(source)
        cloned.name = candidate
        cloned.label = f"{source.label} (копия)"
        cloned.unique = False
        fields.insert(row + 1, cloned)
        self._show_fields()
        self.fields.selectRow(row + 1)
        self._sync_editor_completions()
        self._mark_dirty()

    def _remove_field(self) -> None:
        row = self.fields.currentRow()
        if self.current is not None and row >= 0:
            self._push_undo()
            self._field_list().pop(row)
            self._show_fields()
            self._sync_editor_completions()
            self._mark_dirty()

    @staticmethod
    def _form_preview(fields: list[StudioField]) -> tuple[QWidget, FieldLayout]:
        panel = QWidget()
        layout = FieldLayout(panel)
        rows: dict[str, tuple[StudioField, Field, QWidget, QFormLayout]] = {}

        def safe_int(raw: str) -> int | None:
            try:
                return int(raw.strip()) if raw.strip() else None
            except ValueError:
                return None

        def safe_dec(raw: str) -> Decimal | None:
            try:
                val = Decimal(raw.strip()) if raw.strip() else None
                return val if val is not None and val.is_finite() else None
            except (InvalidOperation, ValueError):
                return None

        for item in fields:
            label = QLabel((item.label or item.name or "Новое поле") + (" *:" if item.required or item.required_when_visible else ":"))
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            if item.kind == "Ref":
                field: Field = Ref(item.target, item.label, read_only=item.read_only)
            elif item.kind == "Choice":
                options = [part.strip() for part in item.choices.split(",") if part.strip()]
                field = Choice(
                    item.label, choices=[(option, option) for option in options],
                    multi=item.multi, read_only=item.read_only,
                )
            elif item.kind == "String":
                field = String(item.label, multiline=item.multiline, max_length=item.max_length, read_only=item.read_only)
            elif item.kind == "Integer":
                field = Integer(
                    item.label, min_value=safe_int(item.min_value), max_value=safe_int(item.max_value),
                    read_only=item.read_only,
                )
            elif item.kind == "Decimal":
                field = DecimalField(
                    item.label, precision=max(1, item.precision), scale=max(0, min(item.scale, item.precision)),
                    min_value=safe_dec(item.min_value), max_value=safe_dec(item.max_value),
                    currency=item.currency.strip() or None, read_only=item.read_only,
                )
            else:
                constructors = {"Boolean": Boolean, "Date": Date, "DateTime": DateTime, "Time": Time}
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

    def _preview_entity_form(self) -> None:
        self._store()
        if self.current is None:
            return
        dialog = FormPreviewDialog(self, self.current, self._form_preview, project=self.project)
        dialog.exec()
        dialog.deleteLater()

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
        dialog.resize(920 if has_preview else 500, 660)
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
        name_auto = row is None and not item.name

        def sync_field_name(text: str) -> None:
            if name_auto and (suggested := suggest_identifier(text)):
                with QSignalBlocker(name):
                    name.setText(suggested)

        def stop_field_auto(_text: str) -> None:
            nonlocal name_auto
            name_auto = False

        label.textChanged.connect(sync_field_name)
        name.textChanged.connect(stop_field_auto)

        kind = QComboBox()
        kind.addItems(FIELD_TYPES)
        kind.setCurrentText(item.kind)
        required, multiline = QCheckBox(), QCheckBox()
        required.setChecked(item.required)
        multiline.setChecked(item.multiline)
        placeholder = QLineEdit(item.placeholder)
        placeholder.setPlaceholderText("Подсказка внутри пустого поля")
        max_length = QSpinBox()
        max_length.setRange(0, 10000)
        max_length.setSpecialValueText("Без ограничения (255)")
        max_length.setValue(item.max_length or 0)
        precision = QSpinBox()
        precision.setRange(1, 38)
        precision.setValue(item.precision)
        scale = QSpinBox()
        scale.setRange(0, 10)
        scale.setValue(item.scale)
        currency = QComboBox()
        currency.setEditable(True)
        currency.addItems(CURRENCY_PRESETS)
        currency.setCurrentText(item.currency)
        min_value = QLineEdit(item.min_value)
        min_value.setPlaceholderText("Не задано")
        max_value = QLineEdit(item.max_value)
        max_value.setPlaceholderText("Не задано")

        target = QComboBox()
        target.addItems(["", *(e.name for e in self.project.entities)])
        target.setCurrentText(item.target)
        representation_field = QComboBox()
        representation_field.setEditable(True)
        representation_field.setPlaceholderText("По умолчанию (name / number)")
        auto_fill = QComboBox()
        auto_fill.setEditable(True)
        auto_fill.setPlaceholderText("Не заполнять")
        related, choices = QLineEdit(item.related_list), QLineEdit(item.choices)
        multi_choice = QCheckBox("Разрешить выбор нескольких вариантов")
        multi_choice.setChecked(item.multi)
        choice_filter = QLineEdit(", ".join(f"{key}={value}" for key, value in item.choice_filter.items()))
        choice_filter.setObjectName("field_choice_filter")
        choice_filter.setPlaceholderText("owner=customer, city=delivery_city")
        choice_filter.setToolTip("Поле объекта ссылки = поле текущей карточки")
        pick_filter = QPushButton("Подобрать…")
        filter_row = QWidget()
        filter_layout = QHBoxLayout(filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.addWidget(choice_filter, 1)
        filter_layout.addWidget(pick_filter)
        filters = item.choice_filter.copy()
        create_from = QLineEdit(item.create_from)
        create_from.setPlaceholderText("Например, Создать накладную")
        mapping = QPushButton("Выбрать поля…")
        basis_fields = item.basis_fields.copy()
        table_mapping = QPushButton("Выбрать таблицы и колонки…")
        basis_tables = item.basis_tables.copy()

        def refresh_ref_helpers() -> None:
            ent = next((e for e in self.project.entities if e.name == target.currentText()), None)
            cur_rep = representation_field.currentText()
            cur_af = auto_fill.currentText()
            representation_field.clear()
            auto_fill.clear()
            representation_field.addItem("")
            auto_fill.addItem("")
            if ent is not None:
                target_names = [f.name for f in ent.fields]
                if ent.kind == "Документ":
                    target_names.extend(("number", "date"))
                representation_field.addItems(target_names)
                sibling_names = {f.name for f in fields if f is not item}
                auto_fill.addItems([n for n in target_names if n in sibling_names])
            representation_field.setCurrentText(cur_rep)
            auto_fill.setCurrentText(cur_af)

        def pick_choice_filter() -> None:
            if self.current is None:
                return
            ent = next((e for e in self.project.entities if e.name == target.currentText()), None)
            current_pairs: dict[str, str] = {}
            for pair in choice_filter.text().split(","):
                k, _, v = (part.strip() for part in pair.partition("="))
                if identifier(k) and identifier(v):
                    current_pairs[k] = v
            picker = ChoiceFilterDialog(dialog, self.current, ent, current_pairs)
            if picker.exec():
                selected = picker.selected_filter()
                choice_filter.setText(", ".join(f"{k}={v}" for k, v in selected.items()))
            picker.deleteLater()

        def edit_mapping() -> None:
            nonlocal basis_fields
            basis_fields = self._edit_basis_fields(dialog, target.currentText(), item.name or name.text(), basis_fields)

        def edit_tables() -> None:
            nonlocal basis_tables
            basis_tables = self._edit_basis_tables(dialog, target.currentText(), basis_tables)

        pick_filter.clicked.connect(pick_choice_filter)
        mapping.clicked.connect(edit_mapping)
        table_mapping.clicked.connect(edit_tables)
        target.currentTextChanged.connect(lambda _text: basis_fields.clear())
        target.currentTextChanged.connect(lambda _text: basis_tables.clear())
        target.currentTextChanged.connect(lambda _text: refresh_ref_helpers())
        create_from.textChanged.connect(lambda text: mapping.setEnabled(bool(text.strip())))
        create_from.textChanged.connect(lambda text: table_mapping.setEnabled(bool(text.strip())))
        table_mapping.setEnabled(bool(create_from.text().strip()))
        mapping.setEnabled(bool(create_from.text().strip()))
        refresh_ref_helpers()

        default = QLineEdit(item.default)
        default.setPlaceholderText("Оставьте пустым, чтобы пропустить")
        default_today_btn = QPushButton("Сегодня / Сейчас")
        default_today_btn.clicked.connect(
            lambda: default.setText("now" if kind.currentText() in ("DateTime", "Time") else "today")
        )
        default_row = QWidget()
        default_layout = QHBoxLayout(default_row)
        default_layout.setContentsMargins(0, 0, 0, 0)
        default_layout.addWidget(default, 1)
        default_layout.addWidget(default_today_btn)

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
        read_only, indexed, unique, cascade, visible = (
            QCheckBox(), QCheckBox(), QCheckBox(), QCheckBox(), QCheckBox()
        )
        for option, value in ((read_only, item.read_only), (indexed, item.indexed),
                              (unique, item.unique), (cascade, item.cascade), (visible, item.visible)):
            option.setChecked(value)
        for title, widget in (("Имя Python", name), ("Подпись", label), ("Тип", kind),
                              ("Обязательное", required), ("Многострочное", multiline),
                              ("Подсказка (placeholder)", placeholder),
                              ("Макс. длина", max_length),
                              ("Точность (Decimal)", precision), ("Знаков после запятой", scale),
                              ("Валюта", currency), ("Минимум", min_value), ("Максимум", max_value),
                              ("Объект ссылки", target), ("Поле представления", representation_field),
                              ("Автоподстановка поля", auto_fill), ("Связанный список", related),
                              ("Отбор вариантов ссылки", filter_row),
                              ("Команда на исходной записи", create_from),
                              ("Перенос из исходной записи", mapping), ("Перенос строк", table_mapping),
                              ("Варианты через запятую", choices), ("", multi_choice),
                              ("По умолчанию", default_row),
                              ("Только чтение", read_only), ("Индекс", indexed), ("Уникальное", unique),
                              ("Удалять вместе с целью ссылки", cascade), ("Показывать на форме", visible)):
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
            if candidate is not item
            and ((candidate.kind == "Boolean") or (candidate.kind == "Choice" and not candidate.multi))
            and not candidate.visibility_field
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
                    raw_opt = option.strip()
                    if not raw_opt:
                        continue
                    code_opt, sep, lbl_opt = raw_opt.partition(": ")
                    if sep and code_opt.strip():
                        visibility_value.addItem(f"{lbl_opt.strip()} ({code_opt.strip()})", code_opt.strip())
                    else:
                        visibility_value.addItem(raw_opt, raw_opt)
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
            form.setRowVisible(placeholder, field_kind == "String")
            form.setRowVisible(max_length, field_kind == "String")
            form.setRowVisible(precision, field_kind == "Decimal")
            form.setRowVisible(scale, field_kind == "Decimal")
            form.setRowVisible(currency, field_kind == "Decimal")
            form.setRowVisible(min_value, field_kind in ("Integer", "Decimal"))
            form.setRowVisible(max_value, field_kind in ("Integer", "Decimal"))
            form.setRowVisible(target, field_kind == "Ref")
            form.setRowVisible(representation_field, field_kind == "Ref")
            form.setRowVisible(auto_fill, field_kind == "Ref")
            form.setRowVisible(related, field_kind == "Ref" and has_preview)
            form.setRowVisible(filter_row, field_kind == "Ref" and has_preview)
            form.setRowVisible(create_from, field_kind == "Ref" and has_preview)
            form.setRowVisible(mapping, field_kind == "Ref" and has_preview)
            form.setRowVisible(table_mapping, field_kind == "Ref" and has_preview)
            form.setRowVisible(cascade, field_kind == "Ref")
            form.setRowVisible(choices, field_kind == "Choice")
            form.setRowVisible(multi_choice, field_kind == "Choice")
            default_today_btn.setVisible(field_kind in ("Date", "DateTime", "Time"))

        kind.currentTextChanged.connect(update_options)
        update_options(kind.currentText())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        outer.addWidget(buttons)

        def edited_field() -> StudioField:
            field_kind = kind.currentText()
            return StudioField(
                name.text(), label.text(), field_kind, required.isChecked(),
                multiline.isChecked(), target.currentText(), related.text(), choices.text(),
                default.text(), read_only.isChecked(), indexed.isChecked(), unique.isChecked(),
                cascade.isChecked(),
                (
                    list(dict.fromkeys(role.strip() for role in write_roles.text().split(",") if role.strip()))
                    if limit_write.isChecked() else None
                ),
                (
                    list(dict.fromkeys(role.strip() for role in read_roles.text().split(",") if role.strip()))
                    if limit_read.isChecked() else None
                ),
                form_group=form_group.text().strip(), form_tab=form_tab.text().strip(),
                visibility_field=visibility_field.currentData() if has_preview and not required.isChecked() else "",
                visibility_value=visibility_value.currentData() if visibility_value.count() else True,
                required_when_visible=bool(
                    has_preview and not required.isChecked() and visibility_field.currentData()
                    and required_when_visible.isChecked()
                ),
                create_from=create_from.text().strip() if has_preview and field_kind == "Ref" else "",
                basis_fields=basis_fields.copy()
                if has_preview and field_kind == "Ref" and create_from.text().strip() else {},
                basis_tables=basis_tables.copy()
                if has_preview and field_kind == "Ref" and create_from.text().strip() else {},
                choice_filter=filters.copy(),
                max_length=(max_length.value() or None) if field_kind == "String" else None,
                min_value=min_value.text().strip() if field_kind in ("Integer", "Decimal") else "",
                max_value=max_value.text().strip() if field_kind in ("Integer", "Decimal") else "",
                precision=precision.value() if field_kind == "Decimal" else 10,
                scale=scale.value() if field_kind == "Decimal" else 2,
                currency=currency.currentText().strip() if field_kind == "Decimal" else "",
                multi=multi_choice.isChecked() if field_kind == "Choice" else False,
                representation_field=representation_field.currentText().strip() if field_kind == "Ref" else "",
                auto_fill=auto_fill.currentText().strip() if field_kind == "Ref" else "",
                visible=visible.isChecked(),
                placeholder=placeholder.text().strip() if field_kind == "String" else "",
            )

        def accept_edit() -> None:
            new = name.text()
            if row is not None and new != item.name and (not identifier(new) or any(other.name == new for other in fields)):
                QMessageBox.warning(dialog, "Имя занято или недопустимо", "Укажите уникальное имя Python.")
                return
            filters.clear()
            if has_preview and kind.currentText() == "Ref" and choice_filter.text().strip():
                for pair in choice_filter.text().split(","):
                    key, _, value = (part.strip() for part in pair.partition("="))
                    if not identifier(key) or not identifier(value) or key in filters:
                        QMessageBox.warning(
                            dialog, "Отбор вариантов ссылки",
                            "Укажите уникальные пары полей через запятую, например owner=customer.",
                        )
                        choice_filter.setFocus()
                        return
                    filters[key] = value
            draft = edited_field()
            if draft.kind == "Choice" and draft.default.strip():
                options = [
                    opt.strip().partition(": ")[0].strip() if ": " in opt else opt.strip()
                    for opt in draft.choices.split(",")
                    if opt.strip()
                ]
                if not draft.multi and draft.default.strip() not in options:
                    QMessageBox.warning(
                        dialog, "Значение по умолчанию",
                        "Для поля выбора значение по умолчанию должно входить в список вариантов.",
                    )
                    default.setFocus()
                    return
                if draft.multi:
                    bad = [part.strip() for part in draft.default.split(",") if part.strip() and part.strip() not in options]
                    if bad:
                        QMessageBox.warning(
                            dialog, "Значение по умолчанию",
                            f"Неизвестный вариант по умолчанию: {', '.join(bad)}.",
                        )
                        default.setFocus()
                        return
            if draft.kind in ("Integer", "Decimal"):
                try:
                    if draft.kind == "Integer":
                        lo_i = int(draft.min_value) if draft.min_value else None
                        hi_i = int(draft.max_value) if draft.max_value else None
                        if lo_i is not None and hi_i is not None and lo_i > hi_i:
                            raise ValueError("Минимум не может превышать максимум.")
                    else:
                        if draft.scale > draft.precision:
                            raise ValueError("Число знаков после запятой не может превышать точность.")
                        lo_d = Decimal(draft.min_value) if draft.min_value else None
                        hi_d = Decimal(draft.max_value) if draft.max_value else None
                        if (lo_d is not None and not lo_d.is_finite()) or (hi_d is not None and not hi_d.is_finite()):
                            raise ValueError("Укажите конечные числа.")
                        if lo_d is not None and hi_d is not None and lo_d > hi_d:
                            raise ValueError("Минимум не может превышать максимум.")
                except (ValueError, InvalidOperation) as exc:
                    QMessageBox.warning(dialog, "Параметры числа", str(exc))
                    return
            dialog.accept()

        buttons.accepted.connect(accept_edit)
        buttons.rejected.connect(dialog.reject)

        if has_preview:
            preview = QWidget()
            preview_layout = QVBoxLayout(preview)
            preview_layout.addWidget(QLabel("Предпросмотр полей"))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            preview_layout.addWidget(scroll, 1)
            body.addWidget(preview)
            body.setSizes([460, 460])

            def refresh_preview() -> None:
                draft = edited_field()
                preview_fields = fields.copy()
                if row is None:
                    preview_fields.append(draft)
                else:
                    preview_fields[row] = draft
                panel, preview_form_layout = self._form_preview(preview_fields)
                scroll.setWidget(panel)
                preview_page = preview_form_layout.pages.get(draft.form_tab)
                if preview_page is not None:
                    preview_form_layout.tabs.setCurrentWidget(preview_page)

            for edit in (name, label, choices, form_tab, form_group, min_value, max_value):
                edit.textChanged.connect(refresh_preview)
            for checkbox in (required, multiline, read_only, multi_choice):
                checkbox.toggled.connect(refresh_preview)
            for spin in (max_length, precision, scale):
                spin.valueChanged.connect(refresh_preview)
            currency.currentTextChanged.connect(refresh_preview)
            kind.currentTextChanged.connect(refresh_preview)
            visibility_field.currentIndexChanged.connect(refresh_preview)
            visibility_value.currentIndexChanged.connect(refresh_preview)
            required_when_visible.toggled.connect(refresh_preview)
            refresh_preview()

        if dialog.exec():
            self._push_undo()
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
            self._sync_editor_completions()
            self._mark_dirty()
        dialog.deleteLater()

    def _show_reports(self) -> None:
        self.report_list.clear()
        self.report_list.addItems([
            f"{report.title} ({report.section})" if report.section else report.title
            for report in self.project.reports
        ])

    def _duplicate_report(self) -> None:
        row = self.report_list.currentRow()
        if row < 0 or row >= len(self.project.reports):
            return
        self._push_undo()
        source = self.project.reports[row]
        existing = {r.name for r in self.project.reports} | {e.name for e in self.project.entities}
        candidate = f"{source.name}Copy"
        counter = 2
        while candidate in existing:
            candidate = f"{source.name}Copy{counter}"
            counter += 1
        cloned = deepcopy(source)
        cloned.name = candidate
        cloned.title = f"{source.title} (копия)"
        self.project.reports.insert(row + 1, cloned)
        self._show_reports()
        self.report_list.setCurrentRow(row + 1)
        self._refresh_app_pickers()
        self._sync_editor_completions()
        self._mark_dirty()

    def _move_report(self, offset: int) -> None:
        row = self.report_list.currentRow()
        target = row + offset
        if 0 <= row < len(self.project.reports) and 0 <= target < len(self.project.reports):
            self._push_undo()
            item = self.project.reports.pop(row)
            self.project.reports.insert(target, item)
            self._show_reports()
            self.report_list.setCurrentRow(target)
            self._mark_dirty()

    def _preview_selected_report(self) -> None:
        self._store()
        row = self.report_list.currentRow()
        if row < 0 or row >= len(self.project.reports):
            self.statusBar().showMessage("Выберите отчёт в списке.")
            return
        report = self.project.reports[row]
        try:
            self.project.source()
        except (ValueError, SyntaxError, KeyError) as exc:
            self._error(exc)
            return
        dialog = ReportPreviewDialog(self, self.project, report.name, self._resolve_working_db())
        dialog.exec()
        dialog.deleteLater()

    def _remove_report(self) -> None:
        row = self.report_list.currentRow()
        if row >= 0:
            self._push_undo()
            removed = self.project.reports.pop(row)
            if self.project.start_target == removed.name:
                self.project.start_target = ""
            self._show_reports()
            self._refresh_app_pickers()
            self._sync_editor_completions()
            self._mark_dirty()

    def _edit_report(self, row: int) -> None:
        self._store()
        if not self.project.entities:
            self._error(ValueError("Добавьте объект, чтобы выбрать источник отчёта."))
            return
        config = self.project.reports[row] if row >= 0 else StudioReport("", "", "")
        adv_state: dict[str, object] = {
            "section": config.section,
            "auto_run": config.auto_run,
            "sortable": config.sortable,
            "latest_only": config.latest_only,
            "table_part": config.table_part,
            "date_filter": config.date_filter,
            "posted_only": config.posted_only,
            "ref_filter_from_record": config.ref_filter_from_record,
            "choice_filter_field": config.choice_filter_field,
            "text_filter_field": config.text_filter_field,
            "chart_label": config.chart_label,
            "chart_value": config.chart_value,
            "chart_title": config.chart_title,
            "chart_color": config.chart_color,
            "row_styles": deepcopy(config.row_styles),
            "column_options": deepcopy(config.column_options),
        }
        dialog = QDialog(self)
        dialog.setWindowTitle("Отчёт")
        form = QFormLayout(dialog)
        name = QLineEdit(config.name)
        title = QLineEdit(config.title)
        name_auto = row < 0 and not config.name

        def sync_report_name(text: str) -> None:
            if name_auto and (suggested := suggest_identifier(text, pascal=True)):
                with QSignalBlocker(name):
                    name.setText(suggested)

        def stop_report_auto(_text: str) -> None:
            nonlocal name_auto
            name_auto = False

        title.textChanged.connect(sync_report_name)
        name.textChanged.connect(stop_report_auto)

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
        for label_text, widget in (("Колонки", columns), ("Порядок группировок", groups)):
            panel = QVBoxLayout()
            panel.addWidget(QLabel(label_text))
            panel.addWidget(widget)
            choice_layout.addLayout(panel, 1)
        groups.setToolTip("Отметьте поля группировки и перетащите их в нужном порядке.")
        sort = QComboBox()
        ref = QComboBox()
        through, totals = QCheckBox(), QCheckBox()
        through.setChecked(config.as_of)
        totals.setChecked(config.totals)
        adv_button = QPushButton("Период, табличная часть и формат колонок…")
        for label_text, widget in (
            ("Имя Python", name), ("Заголовок", title), ("Источник", source),
            ("", choices), ("Сортировка", sort), ("Отбор по ссылке", ref),
            ("Дата отчёта", through), ("Итоги по числовым колонкам", totals),
            ("Дополнительно", adv_button),
        ):
            form.addRow(label_text, widget)

        def checked(widget: QListWidget) -> list[str]:
            return [widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(widget.count())
                    if widget.item(i).checkState() == Qt.CheckState.Checked]

        def load_fields(*, reset_adv_on_source_change: bool = False) -> None:
            kind, _, model_name = source.currentData().partition(":")
            is_register = kind == "register"
            original_source = (kind, model_name) == (config.source_kind, config.entity)
            if reset_adv_on_source_change and not original_source:
                adv_state["table_part"] = ""
                adv_state["date_filter"] = ""
                adv_state["posted_only"] = False
                adv_state["latest_only"] = False
                adv_state["choice_filter_field"] = ""
                adv_state["text_filter_field"] = ""
                adv_state["chart_label"] = ""
                adv_state["chart_value"] = ""
                adv_state["row_styles"] = []
            form.setRowVisible(through, is_register)
            if is_register:
                register = next(item for item in self.project.registers if item.name == model_name)
                fields = [
                    (item.name, item.label, item.value_type == "Ссылка")
                    for item in register_report_columns(register)
                ]
            else:
                entity = next(item for item in self.project.entities if item.name == model_name)
                table_part = str(adv_state["table_part"])
                if table_part and all(p.name != table_part for p in entity.table_parts):
                    table_part = ""
                    adv_state["table_part"] = ""
                ent_fields, _ = entity_report_fields(entity, table_part)
                fields = [(item.name, item.label, item.kind == "Ref") for item in ent_fields.values()]
            columns.clear()
            groups.clear()
            sort.clear()
            ref.clear()
            sort.addItem("По умолчанию", "")
            ref.addItem("Без отбора", "")
            for field_name, field_label, is_ref in fields:
                item = QListWidgetItem(field_label)
                item.setData(Qt.ItemDataRole.UserRole, field_name)
                selected = not original_source or not config.columns or field_name in config.columns
                item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
                columns.addItem(item)
                sort.addItem(field_label, field_name)
                sort.addItem(f"{field_label} · по убыванию", "-" + field_name)
                if is_ref:
                    ref.addItem(field_label, field_name)
            labels = {field_name: field_label for field_name, field_label, _ in fields}
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

        def open_advanced() -> None:
            source_kind, _, model_name = source.currentData().partition(":")
            col_opts_raw = adv_state["column_options"]
            row_styles_raw = adv_state["row_styles"]
            draft = StudioReport(
                name.text(), title.text(), model_name, checked(columns), sort.currentData(), ref.currentData(),
                source_kind, source_kind == "register" and through.isChecked(), totals.isChecked(), checked(groups),
                section=str(adv_state["section"]),
                auto_run=bool(adv_state["auto_run"]),
                table_part=str(adv_state["table_part"]),
                date_filter=str(adv_state["date_filter"]),
                posted_only=bool(adv_state["posted_only"]),
                ref_filter_from_record=bool(adv_state["ref_filter_from_record"]),
                column_options={k: dict(v) for k, v in col_opts_raw.items()} if isinstance(col_opts_raw, dict) else {},
                latest_only=bool(adv_state["latest_only"]),
                sortable=bool(adv_state["sortable"]),
                choice_filter_field=str(adv_state["choice_filter_field"]),
                text_filter_field=str(adv_state["text_filter_field"]),
                chart_label=str(adv_state["chart_label"]),
                chart_value=str(adv_state["chart_value"]),
                chart_title=str(adv_state["chart_title"]),
                chart_color=str(adv_state["chart_color"]),
                row_styles=[dict(r) for r in row_styles_raw] if isinstance(row_styles_raw, list) else [],
            )
            adv_dialog = ReportAdvancedDialog(dialog, draft, self.project, source_kind, model_name, checked(columns))
            if adv_dialog.exec():
                prev_part = adv_state["table_part"]
                adv_state.update(adv_dialog.apply_to())
                if adv_state["table_part"] != prev_part:
                    load_fields()
            adv_dialog.deleteLater()

        adv_button.clicked.connect(open_advanced)
        source.currentIndexChanged.connect(lambda _idx: load_fields(reset_adv_on_source_change=True))
        load_fields()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        form.addRow(buttons)

        def accept_report() -> None:
            chosen = checked(columns)
            grouping = checked(groups)
            if not chosen or not set(grouping) <= set(chosen):
                QMessageBox.warning(dialog, "Колонки отчёта", "Выберите колонки, включая каждое поле группировки.")
                return
            source_kind, _, model_name = source.currentData().partition(":")
            col_opts_raw = adv_state["column_options"]
            col_opts = (
                {k: dict(v) for k, v in col_opts_raw.items() if k in chosen}
                if isinstance(col_opts_raw, dict) else {}
            )
            row_styles_raw = adv_state["row_styles"]
            value = StudioReport(
                name.text(), title.text(), model_name, chosen, sort.currentData(), ref.currentData(),
                source_kind, source_kind == "register" and through.isChecked(), totals.isChecked(), grouping,
                section=str(adv_state["section"]),
                auto_run=bool(adv_state["auto_run"]),
                table_part=str(adv_state["table_part"]) if source_kind == "entity" else "",
                date_filter=str(adv_state["date_filter"]) if source_kind == "entity" else "",
                posted_only=bool(adv_state["posted_only"]) if source_kind == "entity" else False,
                ref_filter_from_record=bool(adv_state["ref_filter_from_record"]),
                column_options=col_opts,
                latest_only=bool(adv_state["latest_only"]) if source_kind == "register" else False,
                sortable=bool(adv_state["sortable"]),
                choice_filter_field=str(adv_state["choice_filter_field"]) if source_kind == "entity" else "",
                text_filter_field=str(adv_state["text_filter_field"]) if source_kind == "entity" else "",
                chart_label=str(adv_state["chart_label"]),
                chart_value=str(adv_state["chart_value"]),
                chart_title=str(adv_state["chart_title"]),
                chart_color=str(adv_state["chart_color"]),
                row_styles=(
                    [dict(r) for r in row_styles_raw]
                    if source_kind == "entity" and isinstance(row_styles_raw, list) else []
                ),
            )
            self._push_undo()
            if row >= 0:
                self.project.reports[row] = value
            else:
                self.project.reports.append(value)
            self._show_reports()
            self._refresh_app_pickers()
            self._sync_editor_completions()
            self._mark_dirty()
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

    def _duplicate_register(self) -> None:
        row = self.register_list.currentRow()
        if row < 0 or row >= len(self.project.registers):
            return
        self._push_undo()
        source = self.project.registers[row]
        existing = {r.name for r in self.project.registers} | {e.name for e in self.project.entities}
        candidate = f"{source.name}Copy"
        counter = 2
        while candidate in existing:
            candidate = f"{source.name}Copy{counter}"
            counter += 1
        cloned = deepcopy(source)
        cloned.name = candidate
        self.project.registers.insert(row + 1, cloned)
        self._show_registers()
        self.register_list.setCurrentRow(row + 1)
        self._sync_editor_completions()
        self._mark_dirty()

    def _move_register(self, offset: int) -> None:
        row = self.register_list.currentRow()
        target = row + offset
        if 0 <= row < len(self.project.registers) and 0 <= target < len(self.project.registers):
            self._push_undo()
            item = self.project.registers.pop(row)
            self.project.registers.insert(target, item)
            self._show_registers()
            self.register_list.setCurrentRow(target)
            self._mark_dirty()

    def _remove_register(self) -> None:
        row = self.register_list.currentRow()
        if row < 0:
            return
        removed = self.project.registers[row]
        used_by: list[str] = [
            f"документ «{ent.label}»"
            for ent in self.project.entities
            if any(mv.register == removed.name for mv in ent.movements)
        ]
        used_by.extend(
            f"отчёт «{rep.title}»"
            for rep in self.project.reports
            if rep.source_kind == "register" and rep.entity == removed.name
        )
        if used_by:
            if QMessageBox.question(
                self, "Удалить регистр",
                f"Удалить регистр «{removed.name}»?\n\nСвязанные записи будут удалены из: {', '.join(used_by)}.",
            ) != QMessageBox.StandardButton.Yes:
                return
        self._push_undo()
        self.project.registers.pop(row)
        for ent in self.project.entities:
            ent.movements = [mv for mv in ent.movements if mv.register != removed.name]
        self.project.reports = [
            rep for rep in self.project.reports
            if not (rep.source_kind == "register" and rep.entity == removed.name)
        ]
        self._show_registers()
        self._show_reports()
        self._show_movements()
        self._refresh_app_pickers()
        self._sync_editor_completions()
        self._mark_dirty()

    def _edit_register(self, row: int) -> None:
        self._store()
        config = self.project.registers[row] if row >= 0 else StudioRegister("")
        dialog = QDialog(self)
        dialog.setWindowTitle("Регистр")
        dialog.resize(920, 460)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        name = QLineEdit(config.name)
        kind = QComboBox()
        kind.addItems(REGISTER_TYPES)
        kind.setCurrentText(config.kind)
        form.addRow("Имя Python", name)
        form.addRow("Вид регистра", kind)
        layout.addLayout(form)
        table = QTableWidget(0, 7)
        table.setHorizontalHeaderLabels([
            "Имя", "Подпись", "Назначение", "Тип", "Объект ссылки", "Обязательное", "Представление",
        ])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(42)
        layout.addWidget(table)

        entities_by_name = {ent.name: ent for ent in self.project.entities}

        def add_member(member: StudioRegisterMember) -> None:
            index = table.rowCount()
            table.insertRow(index)
            table.setItem(index, 0, QTableWidgetItem(member.name))
            table.setItem(index, 1, QTableWidgetItem(member.label))
            role, value_type, target = QComboBox(), QComboBox(), QComboBox()
            rep_box = QComboBox()
            rep_box.setEditable(True)
            rep_box.setPlaceholderText("По умолчанию")
            role.addItems(["Измерение", "Ресурс"])
            role.setCurrentText(member.kind)
            target.addItems(["", *(item.name for item in self.project.entities)])
            target.setCurrentText(member.target)
            required = QTableWidgetItem()
            required.setCheckState(Qt.CheckState.Checked if member.required else Qt.CheckState.Unchecked)
            table.setItem(index, 5, required)
            for column, widget in enumerate((role, value_type, target), 2):
                table.setCellWidget(index, column, widget)
            table.setCellWidget(index, 6, rep_box)

            def load_rep_fields() -> None:
                is_ref_dim = value_type.currentText() == "Ссылка" and role.currentText() == "Измерение"
                rep_box.setEnabled(is_ref_dim)
                current_rep = rep_box.currentText()
                rep_box.clear()
                rep_box.addItem("")
                if ent := entities_by_name.get(target.currentText()):
                    names = [f.name for f in ent.fields]
                    if ent.kind == "Документ":
                        names.extend(("number", "date"))
                    rep_box.addItems(names)
                rep_box.setCurrentText(current_rep)

            def load_types() -> None:
                current = value_type.currentText()
                resource_types = INFORMATION_RESOURCE_TYPES if kind.currentText() == "Сведения" else RESOURCE_TYPES
                value_type.clear()
                value_type.addItems(DIMENSION_TYPES if role.currentText() == "Измерение" else resource_types)
                value_type.setCurrentText(current)
                target.setEnabled(value_type.currentText() == "Ссылка")
                load_rep_fields()

            role.currentTextChanged.connect(load_types)
            kind.currentTextChanged.connect(load_types)
            value_type.currentTextChanged.connect(lambda text: target.setEnabled(text == "Ссылка"))
            value_type.currentTextChanged.connect(lambda _text: load_rep_fields())
            target.currentTextChanged.connect(lambda _text: load_rep_fields())
            load_types()
            value_type.setCurrentText(member.value_type)
            load_rep_fields()
            rep_box.setCurrentText(member.representation_field)

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
        saved_register: StudioRegister | None = None

        def accept_register() -> None:
            nonlocal saved_register
            members = [
                StudioRegisterMember(
                    table.item(i, 0).text().strip(),
                    table.item(i, 1).text().strip(),
                    table.cellWidget(i, 2).currentText(),
                    table.cellWidget(i, 3).currentText(),
                    table.cellWidget(i, 4).currentText(),
                    table.item(i, 5).checkState() == Qt.CheckState.Checked,
                    representation_field=(
                        table.cellWidget(i, 6).currentText().strip()
                        if table.cellWidget(i, 2).currentText() == "Измерение"
                        and table.cellWidget(i, 3).currentText() == "Ссылка"
                        else ""
                    ),
                )
                for i in range(table.rowCount())
            ]
            candidate = StudioRegister(name.text().strip(), kind.currentText(), members)
            try:
                candidate.source(set(entities_by_name), set(), entities=entities_by_name)
            except ValueError as exc:
                QMessageBox.warning(dialog, "Проверьте регистр", str(exc))
                return
            saved_register = candidate
            dialog.accept()

        buttons.accepted.connect(accept_register)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() and saved_register is not None:
            self._push_undo()
            if row < 0:
                self.project.registers.append(saved_register)
            else:
                self.project.registers[row] = saved_register
                if config.name != saved_register.name:
                    for report in self.project.reports:
                        if report.source_kind == "register" and report.entity == config.name:
                            report.entity = saved_register.name
                    for entity in self.project.entities:
                        for rule in entity.movements:
                            if rule.register == config.name:
                                rule.register = saved_register.name
                self._show_movements()
            self._show_registers()
            self._refresh_app_pickers()
            self._sync_editor_completions()
            self._mark_dirty()
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
            + (
                "Запись сведений"
                + (f" (дата: {rule.period_field})" if rule.period_field else "")
                if registers.get(rule.register) == "Сведения"
                else ("Приход" if rule.income else "Расход")
            )
            for rule in self.current.movements
        ])

    def _edit_selected_movement(self) -> None:
        row = self.movement_list.currentRow()
        if row >= 0:
            self._edit_movement(row)

    def _remove_movement(self) -> None:
        row = self.movement_list.currentRow()
        if self.current is not None and row >= 0:
            self._push_undo()
            self.current.movements.pop(row)
            self._show_movements()
            self._mark_dirty()

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
        dialog.resize(620, 420)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        register_box, part_box, direction, period_box = QComboBox(), QComboBox(), QComboBox(), QComboBox()
        register_box.addItems([item.name for item in self.project.registers])
        register_box.setCurrentText(rule.register)
        part_box.addItem("Одно движение на документ", "")
        for part in entity.table_parts:
            part_box.addItem(f"Движение на каждую строку: {part.label}", part.name)
        part_box.setCurrentIndex(max(0, part_box.findData(rule.table_part)))
        direction.addItems(("Приход", "Расход"))
        direction.setCurrentIndex(0 if rule.income else 1)
        period_box.addItem("Дата документа (date)", "")
        for field in entity.fields:
            if field.kind in ("Date", "DateTime"):
                period_box.addItem(f"{field.label} ({field.name})", field.name)
        period_box.setCurrentIndex(max(0, period_box.findData(rule.period_field)))
        form.addRow("Регистр", register_box)
        form.addRow("Периодичность записи", part_box)
        form.addRow("Движение", direction)
        form.addRow("Дата записи", period_box)
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
            form.setRowVisible(period_box, not has_direction)
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
            register = self.project.registers[register_box.currentIndex()]
            value = StudioMovement(
                register_box.currentText(), part_box.currentData(),
                direction.isHidden() or direction.currentIndex() == 0,
                {name: box.currentData() for name, box in sources.items() if box.currentData()},
                period_field=(period_box.currentData() or "") if register.kind == "Сведения" else "",
            )
            try:
                movement_source(replace(entity, movements=[value]), {
                    item.name: item for item in self.project.registers
                })
            except (ValueError, KeyError) as exc:
                QMessageBox.warning(dialog, "Проверьте движение", str(exc))
                return
            self._push_undo()
            if row < 0:
                entity.movements.append(value)
            else:
                entity.movements[row] = value
            self._show_movements()
            self._mark_dirty()
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
        visual_bodies: dict[str, str] = {}

        def _make_action_name(seed: str = "action") -> str:
            base = suggest_identifier(seed) or "action"
            if base in reserved or base in new_methods:
                index = 1
                while f"{base}_{index}" in reserved or f"{base}_{index}" in new_methods:
                    index += 1
                return f"{base}_{index}"
            return base

        def create_visual_action() -> None:
            builder = QDialog(dialog)
            builder.setWindowTitle("Создать действие без кода")
            builder.resize(460, 260)
            b_form = QFormLayout(builder)
            b_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            btn_label = QLineEdit("Отметить выполненным")
            m_name = QLineEdit(_make_action_name(btn_label.text()))
            m_auto = True

            def _sync_m_name(txt: str) -> None:
                if m_auto:
                    with QSignalBlocker(m_name):
                        m_name.setText(_make_action_name(txt))

            def _stop_m_auto() -> None:
                nonlocal m_auto
                m_auto = False

            btn_label.textChanged.connect(_sync_m_name)
            m_name.textEdited.connect(_stop_m_auto)

            action_kind = QComboBox()
            action_kind.addItem("Установить значение поля", "set_field")
            if entity.kind == "Документ":
                action_kind.addItem("Провести документ", "post")
                action_kind.addItem("Отменить проведение", "unpost")
            if entity.workflow is not None:
                action_kind.addItem("Изменить статус согласования", "workflow")
            action_kind.addItem("Обновить поле текущей датой/временем", "set_now")
            action_kind.addItem("Увеличить числовое поле на 1", "increment")

            target_field = QComboBox()
            for f in entity.fields:
                target_field.addItem(f"{f.label} ({f.name})", f.name)
            val_edit = QLineEdit()
            val_edit.setPlaceholderText("Значение (текст, число или True/False)")
            wf_state = QComboBox()
            if entity.workflow is not None:
                states = [entity.workflow.initial, *(t.target for t in entity.workflow.transitions if t.target)]
                wf_state.addItems(list(dict.fromkeys(s for s in states if s)))

            def _refresh_builder_rows() -> None:
                k = action_kind.currentData()
                b_form.setRowVisible(target_field, k in ("set_field", "set_now", "increment"))
                b_form.setRowVisible(val_edit, k == "set_field")
                b_form.setRowVisible(wf_state, k == "workflow")

            action_kind.currentIndexChanged.connect(_refresh_builder_rows)
            b_form.addRow("Подпись кнопки", btn_label)
            b_form.addRow("Имя метода Python", m_name)
            b_form.addRow("Что сделать", action_kind)
            b_form.addRow("Поле", target_field)
            b_form.addRow("Новое значение", val_edit)
            b_form.addRow("Новый статус", wf_state)
            _refresh_builder_rows()

            b_btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            b_btns.button(QDialogButtonBox.StandardButton.Ok).setText("Создать")
            b_btns.rejected.connect(builder.reject)

            def _accept_builder() -> None:
                lbl = btn_label.text().strip()
                fn = m_name.text().strip()
                if not lbl or not identifier(fn) or fn in reserved or fn in new_methods:
                    QMessageBox.warning(builder, "Проверьте данные", "Укажите уникальное имя метода и подпись кнопки.")
                    return
                k = action_kind.currentData()
                f_name = target_field.currentData() or ""
                if k == "set_field":
                    if not f_name:
                        QMessageBox.warning(builder, "Нет поля", "Добавьте хотя бы одно поле на объект.")
                        return
                    f_meta = next((item for item in entity.fields if item.name == f_name), None)
                    raw_val = val_edit.text().strip()
                    if f_meta is not None and f_meta.kind == "Boolean":
                        lit = repr(raw_val.casefold() in ("1", "true", "да", "yes"))
                    elif f_meta is not None and f_meta.kind == "Integer":
                        try:
                            lit = str(int(raw_val or "0"))
                        except ValueError:
                            QMessageBox.warning(builder, "Число", "Укажите целое число.")
                            return
                    elif f_meta is not None and f_meta.kind == "Decimal":
                        try:
                            Decimal(raw_val or "0")
                            lit = f"Decimal({raw_val or '0'!r})"
                        except (InvalidOperation, ValueError):
                            QMessageBox.warning(builder, "Число", "Укажите корректное число.")
                            return
                    else:
                        lit = repr(val_edit.text())
                    body = f"    self.{f_name} = {lit}"
                elif k == "post":
                    body = '    self._data["posted"] = True'
                elif k == "unpost":
                    body = '    self._data["posted"] = False'
                elif k == "workflow":
                    wf_field = entity.workflow.field_name if entity.workflow else "status"
                    body = f"    self.{wf_field} = {wf_state.currentText()!r}"
                elif k == "set_now":
                    if not f_name:
                        QMessageBox.warning(builder, "Нет поля", "Добавьте хотя бы одно поле даты/времени.")
                        return
                    f_meta = next((item for item in entity.fields if item.name == f_name), None)
                    if f_meta is not None and f_meta.kind == "Date":
                        body = f"    from datetime import date\n    self.{f_name} = date.today()"
                    elif f_meta is not None and f_meta.kind == "Time":
                        body = f"    from datetime import datetime\n    self.{f_name} = datetime.now().time()"
                    else:
                        body = f"    from datetime import datetime\n    self.{f_name} = datetime.now()"
                else:
                    if not f_name:
                        QMessageBox.warning(builder, "Нет поля", "Добавьте числовое поле на объект.")
                        return
                    body = f"    self.{f_name} = (self.{f_name} or 0) + 1"
                new_methods.add(fn)
                visual_bodies[fn] = body
                methods.append(fn)
                for row_i in range(table.rowCount()):
                    picker = table.cellWidget(row_i, 0)
                    if isinstance(picker, QComboBox):
                        picker.addItem(fn)
                add.setEnabled(True)
                add_row(fn, lbl)
                builder.accept()

            b_btns.accepted.connect(_accept_builder)
            b_form.addRow(b_btns)
            builder.exec()
            builder.deleteLater()

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
        create_visual = QPushButton("Создать без кода…")
        create_visual.clicked.connect(create_visual_action)
        controls.addWidget(create_visual)
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
            self._push_undo()
            entity.commands = configured
            entity.command_roles = restrictions
            for name in configured:
                if name not in new_methods:
                    continue
                code = entity.code.rstrip()
                prefix = code + "\n\n" if code else ""
                body = visual_bodies.get(name, "    pass")
                if name not in visual_bodies and first_body_line is None:
                    first_body_line = prefix.count("\n") + 1
                entity.code = prefix + f"def {name}(self) -> None:\n{body}\n"
            self._sync_editor_completions()
            self._mark_dirty()
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
            self._push_undo()
            names = selected()
            entity.list_columns = names if names != list(fields) else []
            key = sort.currentData()
            entity.list_order = ("-" if direction.currentIndex() else "") + key if key else ""
            self._mark_dirty()
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
                self._push_undo()
                entity.print_form = None
                self._mark_dirty()
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
            self._push_undo()
            entity.print_form = value
            self._mark_dirty()
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
                self._push_undo()
                entity.permissions = None
                self._mark_dirty()
                dialog.accept()
                return
            names = [table.item(row, 0).text().strip() for row in range(table.rowCount())]
            if any(not name for name in names) or len(set(names)) != len(names):
                QMessageBox.warning(dialog, "Права", "Укажите уникальные непустые имена ролей.")
                return
            self._push_undo()
            entity.permissions = {
                key: [name for row, name in enumerate(names)
                      if table.item(row, column).checkState() == Qt.CheckState.Checked]
                for column, (key, _) in enumerate(actions, 1)
            }
            self._mark_dirty()
            dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()
        dialog.deleteLater()

    def _show_roles_overview(self) -> None:
        self._store()
        dialog = RolesOverviewDialog(self, self.project)
        dialog.exec()
        dialog.deleteLater()

    def _edit_reminders(self) -> None:
        self._store()
        if not self.project.entities:
            self._error(ValueError("Сначала добавьте объект с полем даты или срока."))
            return
        dialog = RemindersDialog(self, self.project)
        if dialog.exec():
            self._push_undo()
            self.project.reminders = dialog.result_reminders
            self._refresh_reminders_label()
            self._mark_dirty()
        dialog.deleteLater()

    def _show_workflow(self) -> None:
        self.workflow_list.clear()
        self.workflow_edit.setEnabled(self.current is not None)
        route = self.current.workflow if self.current is not None else None
        self.workflow_remove.setEnabled(route is not None)
        if route is not None:
            posting = f" · проведение: {', '.join(route.post_states)}" if route.post_states else ""
            self.workflow_hint.setText(f"Начальный статус: {route.initial}{posting}")
            self.workflow_list.addItems([
                f"{step.source} → {step.target} · {step.label}"
                + (f" · {', '.join(step.roles)}" if step.roles else "")
                + (" (по назначению)" if step.assigned_only else "")
                for step in route.transitions
            ])
        else:
            self.workflow_hint.setText("Выберите объект и настройте статусы и переходы.")

    def _remove_workflow(self) -> None:
        if self.current is not None and self.current.workflow is not None and QMessageBox.question(
            self, "Удалить маршрут", "Удалить статусы и переходы из проекта?",
        ) == QMessageBox.StandardButton.Yes:
            self._push_undo()
            self.current.workflow = None
            self._show_workflow()
            self._mark_dirty()

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
            self._push_undo()
            entity.workflow = value
            self._show_workflow()
            self._mark_dirty()
            dialog.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        dialog.exec()
        dialog.deleteLater()

    def _show_seeds(self) -> None:
        self.seed_list.clear()
        self.seed_list.addItems([
            f"{item.key} · {item.entity}" + (" · провести" if item.post else "")
            for item in self.project.initial_data
        ])

    def _edit_selected_seed(self) -> None:
        row = self.seed_list.currentRow()
        if row >= 0:
            self._edit_seed(row)

    def _duplicate_seed(self) -> None:
        row = self.seed_list.currentRow()
        if row < 0 or row >= len(self.project.initial_data):
            return
        self._push_undo()
        source = self.project.initial_data[row]
        existing = {item.key for item in self.project.initial_data}
        candidate = f"{source.key}_copy"
        counter = 2
        while candidate in existing:
            candidate = f"{source.key}_copy_{counter}"
            counter += 1
        cloned = deepcopy(source)
        cloned.key = candidate
        self.project.initial_data.insert(row + 1, cloned)
        self._show_seeds()
        self.seed_list.setCurrentRow(row + 1)
        self._mark_dirty()

    def _capture_seeds_from_db(self) -> None:
        self._store()
        target = self._resolve_working_db()
        if target is None or not target.is_file():
            chosen, _ = QFileDialog.getOpenFileName(
                self, "Выбрать SQLite базу для загрузки начальных данных", "",
                "SQLite (*.sqlite3 *.sqlite *.db);;Все файлы (*)",
            )
            if not chosen:
                return
            target = Path(chosen)
        self._capture_seeds_from_path(target)

    def _capture_seeds_from_path(self, db_path: Path) -> bool:
        try:
            self.project.source()
            with TemporaryDirectory(prefix="decui-capture-") as tmp:
                tmp_dir = Path(tmp)
                self.project.export(tmp_dir)
                repo_root = str(Path(__file__).resolve().parent.parent)
                res = subprocess.run(
                    [sys.executable, "-m", "decui.studio_runner", "capture-seeds", str(db_path)],
                    cwd=tmp_dir,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env={**os.environ, "PYTHONPATH": repo_root, "QT_QPA_PLATFORM": "offscreen"},
                    check=False,
                )
                if res.returncode != 0:
                    raise RuntimeError(res.stderr.strip() or res.stdout.strip() or "Не удалось загрузить записи из базы.")
                raw_seeds = json.loads(res.stdout)
        except (OSError, ValueError, SyntaxError, RuntimeError, subprocess.SubprocessError) as exc:
            self._error(exc)
            return False
        if not raw_seeds:
            self.statusBar().showMessage("В выбранной базе нет записей для загрузки.")
            return True
        self._push_undo()
        existing_keys = {item.key for item in self.project.initial_data}
        key_remap: dict[str, str] = {}
        parsed_seeds: list[StudioSeed] = []
        for entry in raw_seeds:
            orig_key = str(entry["key"])
            new_key = orig_key
            suffix = 2
            while new_key in existing_keys:
                new_key = f"{orig_key}_{suffix}"
                suffix += 1
            existing_keys.add(new_key)
            key_remap[orig_key] = new_key
            parsed_seeds.append(StudioSeed(
                key=new_key,
                entity=str(entry["entity"]),
                values={str(k): str(v) for k, v in (entry.get("values") or {}).items()},
                tables={
                    str(part): [{str(ck): str(cv) for ck, cv in row.items()} for row in rows]
                    for part, rows in (entry.get("tables") or {}).items()
                },
                post=bool(entry.get("post", False)),
            ))
        ent_by_name = {ent.name: ent for ent in self.project.entities}
        for seed in parsed_seeds:
            ent = ent_by_name.get(seed.entity)
            if ent is None:
                continue
            for field in ent.fields:
                if field.kind == "Ref" and field.name in seed.values:
                    seed.values[field.name] = key_remap.get(seed.values[field.name], seed.values[field.name])
            for part in ent.table_parts:
                for row_vals in seed.tables.get(part.name, []):
                    for field in part.fields:
                        if field.kind == "Ref" and field.name in row_vals:
                            row_vals[field.name] = key_remap.get(row_vals[field.name], row_vals[field.name])
        self.project.initial_data.extend(parsed_seeds)
        self._show_seeds()
        self._mark_dirty()
        self.statusBar().showMessage(f"Загружено начальных записей из базы: {len(parsed_seeds)}.")
        return True

    def _remove_seed(self) -> None:
        row = self.seed_list.currentRow()
        if row >= 0:
            self._push_undo()
            self.project.initial_data.pop(row)
            self._show_seeds()
            self._mark_dirty()

    def _edit_seed(self, row: int) -> None:
        self._store()
        if not self.project.entities:
            self._error(ValueError("Сначала добавьте объект проекта."))
            return
        record = self.project.initial_data[row] if row >= 0 else StudioSeed("", "")
        dialog = QDialog(self)
        dialog.setWindowTitle("Начальная запись")
        dialog.resize(760, 510)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        key = QLineEdit(record.key)
        entity_box = QComboBox()
        for entity in self.project.entities:
            entity_box.addItem(entity.label, entity.name)
        entity_box.setCurrentIndex(max(0, entity_box.findData(record.entity)))
        post_check = QCheckBox("Провести документ при загрузке")
        post_check.setChecked(record.post)
        form.addRow("Ключ записи", key)
        form.addRow("Объект", entity_box)
        form.addRow("", post_check)
        layout.addLayout(form)
        note = QLabel("Пустое поле использует значение по умолчанию. Для ссылки укажите ключ другой начальной записи.")
        note.setWordWrap(True)
        layout.addWidget(note)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        columns: list[str] = []
        header = QTableWidget()
        tables: dict[str, tuple[QTableWidget, list[str], list[StudioField]]] = {}

        def make_cell_editor(field: StudioField, value: str) -> QWidget | None:
            if field.kind == "Boolean":
                box = QComboBox()
                box.addItem("По умолчанию", "")
                box.addItem("Да", "Да")
                box.addItem("Нет", "Нет")
                norm = value.strip().casefold()
                if norm in {"true", "да", "1"}:
                    box.setCurrentIndex(1)
                elif norm in {"false", "нет", "0"}:
                    box.setCurrentIndex(2)
                return box
            if field.kind == "Choice" and not field.multi:
                box = QComboBox()
                box.setEditable(True)
                box.addItems(["", *(v.strip() for v in field.choices.split(",") if v.strip())])
                box.setCurrentText(value)
                return box
            if field.kind == "Ref":
                box = QComboBox()
                box.setEditable(True)
                keys = [
                    seed.key for seed in self.project.initial_data
                    if seed is not record and seed.entity == field.target
                ]
                box.addItems(["", *keys])
                box.setCurrentText(value)
                return box
            return None

        def set_table_cell(widget: QTableWidget, r_idx: int, c_idx: int, field: StudioField, value: str) -> None:
            editor = make_cell_editor(field, value)
            if editor is not None:
                widget.setCellWidget(r_idx, c_idx, editor)
            else:
                widget.setItem(r_idx, c_idx, QTableWidgetItem(value))

        def load_fields() -> None:
            while tabs.count():
                tab_page = tabs.widget(0)
                tabs.removeTab(0)
                if tab_page is not header:
                    tab_page.deleteLater()
            tables.clear()
            entity = self.project.entities[entity_box.currentIndex()]
            form.setRowVisible(post_check, entity.kind == "Документ")
            fields = list(entity.fields)
            if entity.kind == "Документ":
                fields.append(StudioField("date", "Дата", "DateTime"))
            header.clear()
            header.setRowCount(len(fields))
            header.setColumnCount(2)
            header.setHorizontalHeaderLabels(["Поле", "Значение"])
            header.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            header.verticalHeader().setDefaultSectionSize(34)
            values = record.values if entity.name == record.entity else {}
            columns[:] = [item.name for item in fields]
            for i, item in enumerate(fields):
                label_item = QTableWidgetItem(f"{item.label} · {item.kind}")
                label_item.setFlags(label_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                header.setItem(i, 0, label_item)
                set_table_cell(header, i, 1, item, values.get(item.name, ""))
            tabs.addTab(header, "Реквизиты")

            for part in entity.table_parts:
                tab_page = QWidget()
                part_layout = QVBoxLayout(tab_page)
                table = QTableWidget(0, len(part.fields))
                table.setHorizontalHeaderLabels([item.label for item in part.fields])
                table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
                table.verticalHeader().setDefaultSectionSize(34)
                part_layout.addWidget(table)
                part_fields = list(part.fields)

                def append_part_row(w: QTableWidget = table, pf: list[StudioField] = part_fields, row_vals: dict[str, str] | None = None) -> None:
                    r = w.rowCount()
                    w.insertRow(r)
                    for c, f in enumerate(pf):
                        set_table_cell(w, r, c, f, (row_vals or {}).get(f.name, ""))

                controls = QHBoxLayout()
                add = QPushButton("Добавить строку")
                add.clicked.connect(lambda _checked=False, fn=append_part_row: fn())
                remove = QPushButton("Удалить строку")
                remove.clicked.connect(lambda _checked=False, widget=table: widget.removeRow(widget.currentRow()))
                controls.addWidget(add)
                controls.addWidget(remove)
                controls.addStretch()
                part_layout.addLayout(controls)
                names = [item.name for item in part.fields]
                tables[part.name] = (table, names, part_fields)
                for row_values in record.tables.get(part.name, []) if entity.name == record.entity else []:
                    append_part_row(table, part_fields, row_values)
                tabs.addTab(tab_page, part.label)

        def cell_value(table: QTableWidget, r_idx: int, c_idx: int) -> str:
            widget = table.cellWidget(r_idx, c_idx)
            if isinstance(widget, QComboBox):
                if not widget.isEditable():
                    data = widget.currentData()
                    return str(data) if data not in (None, "") else ""
                return widget.currentText().strip()
            item = table.item(r_idx, c_idx)
            return item.text() if item is not None else ""

        def accept() -> None:
            name = key.text().strip()
            if not name or any(item is not record and item.key == name for item in self.project.initial_data):
                QMessageBox.warning(dialog, "Начальные данные", "Укажите уникальный ключ записи.")
                return
            entity = self.project.entities[entity_box.currentIndex()]
            values = {name: val for i, name in enumerate(columns) if (val := cell_value(header, i, 1))}
            parts = {
                part: [
                    {name: val for j, name in enumerate(names) if (val := cell_value(table, i, j))}
                    for i in range(table.rowCount())
                ]
                for part, (table, names, _) in tables.items() if table.rowCount()
            }
            edited = StudioSeed(
                name, entity.name, values, parts,
                post=entity.kind == "Документ" and post_check.isChecked(),
            )
            self._push_undo()
            if row >= 0:
                self.project.initial_data[row] = edited
            else:
                self.project.initial_data.append(edited)
            self._show_seeds()
            self._mark_dirty()
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

    def _jump_to_diagnostic(self, entity_name: str, tab_name: str) -> None:
        if entity_name:
            index = next((i for i, e in enumerate(self.project.entities) if e.name == entity_name), -1)
            if index >= 0:
                self.entity_filter.clear()
                self.entities.setCurrentRow(index)
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == (tab_name or "Объект"):
                self.tabs.setCurrentIndex(i)
                return
        self.tabs.setCurrentIndex(0)

    def _tab_changed(self, index: int) -> None:
        self._store()
        self._show_movements()
        self._refresh_app_pickers()
        self._sync_editor_completions()
        widget = self.tabs.widget(index)
        if hasattr(self, "_source_bar"):
            self._source_bar.setVisible(widget is self.source)
        if widget is self.form_designer and self.current is not None:
            self.form_designer.load_entity(self.current, self.project)
            self._apply_diagnostics_to_current()
        elif widget is self.source and not self._source_edit_mode:
            try:
                with QSignalBlocker(self.source):
                    self.source.setPlainText(self.project.source())
            except (ValueError, SyntaxError, KeyError) as exc:
                with QSignalBlocker(self.source):
                    self.source.setPlainText(str(exc))
                if isinstance(exc, StudioSyntaxError):
                    self._set_error_location(exc)
        elif widget is self.overview:
            self.overview.refresh(self.project)

    def _on_form_designer_changed(self) -> None:
        self._mark_dirty()

    def _lsp_snapshot(self) -> tuple[StudioProject, str | None]:
        self._store()
        return self.project, (self.source.toPlainText() if self._source_edit_mode else None)

    def _on_lsp_status(self, ready: bool, message: str) -> None:
        self.lsp_status_label.setText(f"LSP: {message}" if ready else f"LSP: {message}")

    def _update_cursor_indicator(self, line: int, col: int) -> None:
        self.cursor_status_label.setText(f"Стр {line}, Кол {col}")

    def _to_editor_diag(self, diag: MappedDiagnostic) -> EditorDiagnostic:
        return EditorDiagnostic(
            line=diag.line,
            col=diag.col,
            end_line=diag.end_line,
            end_col=diag.end_col,
            severity=diag.severity,
            message=diag.message,
        )

    def _apply_diagnostics_to_current(self) -> None:
        entity_name = self.current.name if self.current is not None else ""
        self.code.set_diagnostics([
            self._to_editor_diag(d)
            for d in self._mapped_diagnostics
            if d.location == "Код объекта" and d.entity_name == entity_name
        ])
        self.form_designer.form_code_editor.set_diagnostics([
            self._to_editor_diag(d)
            for d in self._mapped_diagnostics
            if d.location == "Код формы" and d.entity_name == entity_name
        ])
        self.imports.set_diagnostics([
            self._to_editor_diag(d) for d in self._mapped_diagnostics if d.location == "Код перед объектами"
        ])
        self.logic.set_diagnostics([
            self._to_editor_diag(d) for d in self._mapped_diagnostics if d.location == "Код после объектов"
        ])
        self.source.set_diagnostics([
            self._to_editor_diag(d) for d in self._mapped_diagnostics if d.location == "Код проекта"
        ])

    def _on_lsp_diagnostics(self, diags: list[MappedDiagnostic]) -> None:
        self._mapped_diagnostics = diags
        self._apply_diagnostics_to_current()
        self.problems_list.clear()
        for d in diags:
            sev = {1: "Ошибка", 2: "Предупреждение", 3: "Инфо", 4: "Подсказка"}.get(d.severity, "Замечание")
            where = f"{d.entity_name} · {d.location}" if d.entity_name else d.location
            first_line = d.message.splitlines()[0] if d.message else ""
            item = QListWidgetItem(f"[{sev}] {where}:{d.line + 1} — {first_line}")
            item.setData(Qt.ItemDataRole.UserRole, d)
            self.problems_list.addItem(item)
        self.bottom_tabs.setTabText(1, f"Проблемы ({len(diags)})")

    def _on_lsp_symbols(self, symbols: list[LspSymbol]) -> None:
        self.symbols_tree.clear()

        def _append(parent: QTreeWidgetItem | None, sym: LspSymbol) -> None:
            kind_name = {5: "Класс", 6: "Метод", 12: "Функция", 13: "Поле", 14: "Константа"}.get(sym.kind, "")
            detail = f"{kind_name} · {sym.detail}".strip(" ·")
            node = QTreeWidgetItem([sym.name, detail, str(sym.line + 1)])
            node.setData(0, Qt.ItemDataRole.UserRole, (sym.line, sym.col))
            if parent is None:
                self.symbols_tree.addTopLevelItem(node)
            else:
                parent.addChild(node)
            for child in sym.children:
                _append(node, child)

        for root_sym in symbols:
            _append(None, root_sym)
        self.symbols_tree.expandToDepth(0)

    def _jump_to_mapped_location(self, location: str, entity_name: str, line_0: int, col_0: int) -> None:
        if entity_name:
            index = next((i for i, e in enumerate(self.project.entities) if e.name == entity_name), -1)
            if index >= 0:
                self.entity_filter.clear()
                self.entities.setCurrentRow(index)
        if location == "Код объекта":
            self.tabs.setCurrentIndex(0)
            editor = self.code
            page = self.tabs.currentWidget()
            if isinstance(page, QScrollArea):
                page.ensureWidgetVisible(editor)
        elif location == "Код формы":
            self.tabs.setCurrentWidget(self.form_designer)
            self.form_designer.right_tabs.setCurrentIndex(1)
            editor = self.form_designer.form_code_editor
        elif location == "Код перед объектами":
            self.tabs.setCurrentIndex(1)
            editor = self.imports
        elif location == "Код после объектов":
            self.tabs.setCurrentIndex(1)
            editor = self.logic
        else:
            self.tabs.setCurrentWidget(self.source)
            if not self._source_edit_mode:
                try:
                    with QSignalBlocker(self.source):
                        self.source.setPlainText(self.project.source())
                except (ValueError, SyntaxError, KeyError):
                    pass
            editor = self.source
        editor.go_to_line(line_0 + 1, col_0 + 1)

    def _jump_to_problem_item(self, item: QListWidgetItem) -> None:
        diag = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(diag, MappedDiagnostic):
            self._jump_to_mapped_location(diag.location, diag.entity_name, diag.line, diag.col)

    def _jump_to_symbol_item(self, item: QTreeWidgetItem, _col: int) -> None:
        coords = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(coords, tuple) and len(coords) == 2:
            line_0, col_0 = coords
            if not self._source_edit_mode:
                loc, ent, ed_l, ed_c = self._lsp_bridge.map_virtual_to_editor(line_0, col_0)
                if loc != "Код проекта":
                    self._jump_to_mapped_location(loc, ent, ed_l, ed_c)
                    return
            self._jump_to_mapped_location("Код проекта", "", line_0, col_0)

    def _on_lsp_definition(self, target: object) -> None:
        if isinstance(target, tuple) and len(target) == 4:
            loc, ent, line_0, col_0 = target
            self._jump_to_mapped_location(str(loc), str(ent), int(line_0), int(col_0))
        elif isinstance(target, LspLocation):
            self.statusBar().showMessage(f"Определение: {target.path.name}:{target.line + 1}")

    def _toggle_source_edit_mode(self, enabled: bool) -> None:
        if self._source_dirty and not enabled:
            answer = QMessageBox.question(
                self,
                "Применить изменения кода?",
                "Вы изменили app.py вручную. Применить изменения в визуальный проект Studio перед блокировкой редактирования?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                with QSignalBlocker(self.source_edit_check):
                    self.source_edit_check.setChecked(True)
                return
            if answer == QMessageBox.StandardButton.Yes:
                if not self._apply_source_to_project():
                    with QSignalBlocker(self.source_edit_check):
                        self.source_edit_check.setChecked(True)
                    return
                return
        self._source_edit_mode = enabled
        self.source.setReadOnly(not enabled)
        if enabled:
            try:
                with QSignalBlocker(self.source):
                    self.source.setPlainText(self.project.source())
            except (ValueError, SyntaxError, KeyError) as exc:
                self._error(exc)
            self._source_dirty = False
            self.source_apply_btn.setEnabled(False)
            self.statusBar().showMessage("Режим IDE для app.py: используйте Ctrl+Space, F12 и «Применить код в Studio».")
        else:
            self._source_dirty = False
            self.source_apply_btn.setEnabled(False)
            self._reset_source_from_project()
        self._lsp_bridge.schedule_sync()

    def _on_source_edited(self) -> None:
        if self._loading or not self._source_edit_mode:
            return
        self._source_dirty = True
        self.source_apply_btn.setEnabled(True)
        self._lsp_bridge.schedule_sync()

    def _reset_source_from_project(self) -> None:
        try:
            text = self.project.source()
        except (ValueError, SyntaxError, KeyError) as exc:
            text = str(exc)
        with QSignalBlocker(self.source):
            self.source.setPlainText(text)
        self._source_dirty = False
        self.source_apply_btn.setEnabled(False)
        self._lsp_bridge.schedule_sync()

    def _apply_source_to_project(self) -> bool:
        code_text = self.source.toPlainText()
        try:
            imported, warnings = import_python_project(
                code_text,
                default_title=self.project.title or "Моё приложение",
                default_app_id=self.project.app_id or "my_app",
            )
        except (ValueError, SyntaxError) as exc:
            self._error(exc)
            return False
        imported.window_width = self.project.window_width
        imported.window_height = self.project.window_height
        imported.working_db = self.project.working_db
        preferred = self.current.name if self.current is not None else ""
        self._push_undo()
        self.project = imported
        self._load_project(clear_history=False, preferred_entity=preferred)
        self.tabs.setCurrentWidget(self.source)
        self.console.appendPlainText(
            f"Код app.py синхронизирован со Studio (объектов: {len(imported.entities)}, отчётов: {len(imported.reports)})."
        )
        for warning in warnings:
            self.console.appendPlainText(f"⚠ {warning}")
        self.statusBar().showMessage("Код app.py применён в конструктор Studio.")
        return True

    def _update_window_title(self) -> None:
        name = self.path.name if self.path is not None else self.project.title or "Новый проект"
        dirty = "*" if (self._dirty or repr(self.project) != self.saved) else ""
        self.setWindowTitle(f"DECUI Studio — {name}{dirty}")

    def _remember_recent(self, path: Path) -> None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        settings = QSettings("DECUI", "Studio")
        recent = [str(item) for item in (settings.value("recent_projects", []) or []) if isinstance(item, str)]
        resolved = str(path.resolve())
        updated = [resolved, *[item for item in recent if item != resolved]][:_MAX_RECENT_PROJECTS]
        settings.setValue("recent_projects", updated)
        self._refresh_recent_menu()

    def _refresh_recent_menu(self) -> None:
        if not hasattr(self, "_recent_menu"):
            return
        self._recent_menu.clear()
        if os.environ.get("PYTEST_CURRENT_TEST"):
            recent: list[str] = []
        else:
            settings = QSettings("DECUI", "Studio")
            recent = [str(item) for item in (settings.value("recent_projects", []) or []) if isinstance(item, str)]
        existing = [Path(item) for item in recent if Path(item).is_file()]
        if not existing:
            empty = self._recent_menu.addAction("(нет недавних проектов)")
            empty.setEnabled(False)
            return
        for path in existing:
            self._recent_menu.addAction(
                f"{path.name} — {path.parent}",
                lambda _checked=False, target=path: self._open_project_path(target),
            )

    def save_project(self) -> bool:
        self._store()
        if self.path is None:
            return self.save_project_as()
        try:
            self.project.write(self.path)
        except OSError as exc:
            self._error(exc)
            return False
        self.saved = repr(self.project)
        self._dirty = False
        self._remember_recent(self.path)
        self._update_window_title()
        self.statusBar().showMessage(f"Сохранено: {self.path.name}")
        return True

    def save_project_as(self) -> bool:
        self._store()
        suggested = str(self.path) if self.path is not None else f"{self.project.app_id}.decui.json"
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить проект", suggested, "DECUI Studio (*.decui.json)")
        if not path:
            return False
        self.path = Path(path if path.endswith(".decui.json") else path + ".decui.json")
        return self.save_project()

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
        menu.addAction("CRM и доставка", lambda: self.load_template(crm_delivery_template()))
        menu.addAction("Закупки и прайс-листы", lambda: self.load_template(purchase_prices_template()))
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

    def _open_project_path(self, path: Path) -> None:
        if not self._confirm_changes():
            return
        try:
            project = StudioProject.read(path)
        except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
            self._error(exc)
            return
        self.project, self.path = project, path
        self.saved = repr(project)
        self._remember_recent(path)
        self._load_project()
        self.statusBar().showMessage(f"Открыт проект: {self.path.name}")

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
            self._remember_recent(self.path)
            self._load_project()
            self.statusBar().showMessage(f"Открыт проект: {self.path.name}")

    def _open_project_folder(self) -> None:
        if self.path is None:
            self.statusBar().showMessage("Сначала сохраните или откройте файл проекта.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))

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

    def export_full(self) -> None:
        self._store()
        directory = QFileDialog.getExistingDirectory(self, "Выберите папку для полного проекта")
        if directory:
            try:
                entry = self.project.export_full_project(Path(directory))
            except (OSError, ValueError, SyntaxError, KeyError) as exc:
                self._error(exc)
                return
            self.statusBar().showMessage(f"Проект экспортирован в {entry.parent} (app.py + test_app.py)")

    def _resolve_working_db(self) -> Path | None:
        raw = self.working_db.text().strip() or self.project.working_db.strip()
        if raw:
            p = Path(raw)
            if not p.is_absolute() and self.path is not None:
                return self.path.parent / p
            return p
        if self.path is not None:
            stem = self.path.name.removesuffix(".decui.json").removesuffix(".json")
            return self.path.parent / f"{stem}.sqlite3"
        return None

    def _choose_working_db(self) -> Path | None:
        suggested = str(self._resolve_working_db() or f"{self.project.app_id}.sqlite3")
        path, _ = QFileDialog.getSaveFileName(
            self, "Файл рабочей базы данных", suggested, "SQLite (*.sqlite3 *.sqlite *.db)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return None
        chosen = Path(path if path.endswith((".sqlite3", ".sqlite", ".db")) else path + ".sqlite3")
        if self.path is not None:
            try:
                stored = str(chosen.relative_to(self.path.parent))
            except ValueError:
                stored = str(chosen)
        else:
            stored = str(chosen)
        self.working_db.setText(stored)
        self.project.working_db = stored
        return chosen

    def _ensure_working_db(self) -> Path | None:
        self._store()
        target = self._resolve_working_db()
        if target is None:
            target = self._choose_working_db()
        return target

    def preview_persistent(self) -> None:
        target = self._ensure_working_db()
        if target is not None:
            self._start_project("persistent", backup=target)

    def inspect_database(self) -> None:
        dialog = DatabaseInspectorDialog(self, self._resolve_working_db())
        dialog.exec()
        dialog.deleteLater()

    def check_working_db_migration(self) -> None:
        target = self._resolve_working_db()
        if target is None or not target.is_file():
            self._error(ValueError("Рабочая база ещё не создана. Запустите приложение с рабочей базой или выберите файл."))
            return
        self._start_project("migration", backup=target)

    def backup_working_db(self) -> None:
        target = self._resolve_working_db()
        if target is None or not target.is_file():
            self._error(ValueError("Рабочая база ещё не создана."))
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "Сохранить резервную копию", f"{self.project.app_id}-backup.sqlite3", "SQLite (*.sqlite3)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not dest:
            return
        dest_path = Path(dest if dest.endswith(".sqlite3") else dest + ".sqlite3")
        try:
            if os.path.lexists(dest_path):
                raise FileExistsError(f"Файл уже существует: {dest_path}")
            uri = target.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as src, closing(sqlite3.connect(dest_path)) as dst:
                copy_sqlite(src, dst)
        except (OSError, ValueError, TimeoutError, sqlite3.Error) as exc:
            self._error(exc)
            return
        self.statusBar().showMessage(f"Резервная копия сохранена: {dest_path}")

    def reset_working_db(self) -> None:
        target = self._resolve_working_db()
        if target is None or not target.is_file():
            self.statusBar().showMessage("Файл рабочей базы отсутствует.")
            return
        if QMessageBox.question(
            self, "Сбросить рабочую базу",
            f"Удалить файл рабочей базы «{target}»?\nПри следующем запуске данные создадутся заново.",
        ) == QMessageBox.StandardButton.Yes:
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                self._error(exc)
                return
            self.statusBar().showMessage(f"Рабочая база удалена: {target.name}")

    def check(self) -> None:
        self._launch(check=True)

    def preview(self) -> None:
        self._launch(check=False)

    def run_smoke_tests(self) -> None:
        self._start_project("test")

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
            has_accounts: bool | None = None
            if operation in {"build", "test"}:
                preview_db = None
            elif operation == "persistent" and backup is not None:
                backup.parent.mkdir(parents=True, exist_ok=True)
                preview_db = backup
                has_accounts = _inspect_db_accounts(backup)
            else:
                preview_db = directory / "preview.sqlite3"
                if operation == "backup" and backup is not None:
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
            elif operation == "test":
                test_file = directory / "test_app.py"
                test_file.write_text(self.project.smoke_test_source(), encoding="utf-8")
                args = ["-m", "pytest", "-v", str(test_file)]
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
        if operation in {"preview", "backup", "persistent"}:
            env.insert("DECUI_STUDIO_PREVIEW", "1")
        if operation == "test":
            env.insert("QT_QPA_PLATFORM", "offscreen")
        self.process.setProcessEnvironment(env)
        self.process.setWorkingDirectory(str(directory))
        self._output_decoder.reset()
        self._output_line_tail = ""
        self.console.clear()
        self.statusBar().showMessage({
            "build": "Сборка приложения", "check": "Проверка проекта", "preview": "Предпросмотр приложения",
            "persistent": "Работа с постоянной базой", "test": "Автотест проекта",
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
        for editor in (self.code, self.form_designer.form_code_editor, self.imports, self.logic, self.source):
            editor.set_error_line(None)
        if location is None:
            return
        line = max(1, location.lineno or 1)
        if location.filename == "Код формы":
            if self.current is not None and self.current.name == location.entity_name:
                self.form_designer.form_code_editor.set_error_line(line)
        elif location.entity_name:
            if self.current is not None and self.current.name == location.entity_name:
                self.code.set_error_line(line)
        elif location.filename == "Код перед объектами":
            self.imports.set_error_line(line)
        elif location.filename == "Код после объектов":
            self.logic.set_error_line(line)
        else:
            self.source.set_error_line(line)

    def _go_to_error(self) -> None:
        location = self._error_location
        if location is None:
            return
        if location.editor_snapshot is not None:
            if location.filename == "Код формы":
                entity = next((item for item in self.project.entities if item.name == location.entity_name), None)
                current_text = (
                    self.form_designer.form_code_editor.toPlainText()
                    if entity is self.current and entity is not None
                    else (entity.form_code if entity is not None else None)
                )
            elif location.entity_name:
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
        if location.filename == "Код формы":
            index = next((i for i, entity in enumerate(self.project.entities)
                          if entity.name == location.entity_name), -1)
            if index < 0:
                return
            self.entity_filter.clear()
            self.entities.setCurrentRow(index)
            self.tabs.setCurrentWidget(self.form_designer)
            self.form_designer.right_tabs.setCurrentIndex(1)
            editor = self.form_designer.form_code_editor
        elif location.entity_name:
            index = next((i for i, entity in enumerate(self.project.entities)
                          if entity.name == location.entity_name), -1)
            if index < 0:
                return
            self.entity_filter.clear()
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
        editor.set_error_line(line + 1)
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
        self._lsp_bridge.stop()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        super().closeEvent(event)


def run_studio(initial_path: str | None = None) -> int:
    app = QApplication(sys.argv[:1])
    window = StudioWindow()
    if initial_path:
        target = Path(initial_path)
        if target.is_file():
            if target.suffix == ".py":
                window._import_python_path(target)
            else:
                window._open_project_path(target)
    window.show()
    return app.exec()
