"""Reusable dialogs, snippets, overview, and SQLite inspector for DECUI Studio."""

from __future__ import annotations

import csv
import datetime
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QDate, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from decui.reports import Column as ColumnMeta
from decui.reports import _parse_numeric_str, calc_aggregate, group_report_rows, sanitize_csv_cell
from decui.studio_project import (
    ALIGN_TYPES,
    AGGREGATE_TYPES,
    StudioCalculation,
    StudioDiagnostic,
    StudioEntity,
    StudioField,
    StudioProject,
    StudioReminder,
    StudioReport,
    StudioValidation,
    entity_report_fields,
    movement_compatible,
    movement_fields,
    register_report_columns,
)
from decui.ui.chart import BarChart
from decui.ui.form import FieldLayout

ENTITY_SNIPPETS: tuple[tuple[str, str], ...] = (
    (
        "on_validate — проверка перед сохранением",
        "def on_validate(self) -> list[str]:\n"
        "    errors: list[str] = []\n"
        "    # if not self.Items:\n"
        "    #     errors.append('Добавьте хотя бы одну строку.')\n"
        "    return errors",
    ),
    (
        "before_save — авторасчёт строк и итога",
        "def before_save(self, db: Database) -> None:\n"
        "    from decimal import Decimal as PyDecimal\n\n"
        "    for row in self.Items:\n"
        "        qty = PyDecimal(str(row.get('quantity') or 0))\n"
        "        prc = PyDecimal(str(row.get('price') or 0))\n"
        "        row['amount'] = qty * prc\n"
        "    self.total = self.Items.total('amount')",
    ),
    (
        "before_save — цена из регистра сведений (get_latest)",
        "def before_save(self, db: Database) -> None:\n"
        "    for row in self.Items:\n"
        "        latest = get_latest(db, PriceHistory, on_date=self.date, product=row.get('product'))\n"
        "        if latest is not None and row.get('price') in (None, 0):\n"
        "            row['price'] = latest['price']",
    ),
    (
        "after_save — действие после записи в БД",
        "def after_save(self, db: Database) -> None:\n"
        "    pass",
    ),
    (
        "before_delete — проверка перед удалением",
        "def before_delete(self, db: Database) -> None:\n"
        "    if getattr(self, 'posted', False):\n"
        "        raise ValueError('Сначала отмените проведение документа.')",
    ),
    (
        "prepare_copy — настройка при копировании записи",
        "def prepare_copy(self, db: Database) -> None:\n"
        "    pass",
    ),
    (
        "action — метод для кнопки действия (@command)",
        "def custom_action(self) -> None:\n"
        "    pass",
    ),
)

LOGIC_SNIPPETS: tuple[tuple[str, str], ...] = (
    (
        "configure_app — включить напоминания о сроках",
        "def configure_app(app: ERPApplication) -> None:\n"
        "    app.enable_reminders([Task], date_field='deadline', done_field='is_completed', notify_before_minutes=60)",
    ),
    (
        "QueryReport — пользовательский отчёт за период",
        "@report\n"
        "class CustomPeriodReport(QueryReport):\n"
        "    title = 'Сводка за период'\n"
        "    auto_run = True\n"
        "    date_from = DateParam('С')\n"
        "    date_to = DateParam('По')\n"
        "    chart = {'label': 'label', 'value': 'value', 'title': 'Сводка за период'}\n"
        "    columns: ClassVar[list[Column]] = [\n"
        "        Column('Показатель', 'label', width=240),\n"
        "        Column('Значение', 'value', width=140, align='right', aggregate='sum'),\n"
        "    ]\n\n"
        "    def execute(self, db: Database, **params: object) -> list[dict[str, object]]:\n"
        "        return []",
    ),
    (
        "define_role — регистрация ролей в контексте",
        "define_role('manager', ['edit_price', 'view_report'])",
    ),
)


def _compatible_target_fields(
    source_entity: StudioEntity, target_entity: StudioEntity,
) -> list[tuple[StudioField, list[StudioField]]]:
    """Match each target entity field with header fields of compatible type on source_entity."""
    result: list[tuple[StudioField, list[StudioField]]] = []
    for target_field in target_entity.fields:
        compatible = [
            header_field for header_field in source_entity.fields
            if header_field.kind == target_field.kind
            and (header_field.kind != "Ref" or header_field.target == target_field.target)
        ]
        if compatible:
            result.append((target_field, compatible))
    return result


class ChoiceFilterDialog(QDialog):
    """Visual picker for Ref choice_filter pairs (target_field=header_field)."""

    def __init__(
        self,
        parent: QWidget | None,
        entity: StudioEntity,
        target: StudioEntity | None,
        current_filter: dict[str, str],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Подбор фильтра вариантов ссылки")
        self.resize(500, 340)
        self._pickers: dict[str, QComboBox] = {}
        layout = QVBoxLayout(self)
        if target is None:
            layout.addWidget(QLabel("Сначала выберите объект ссылки."))
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
            return

        note = QLabel(
            f"Выберите, каким полям карточки «{entity.label}» должны соответствовать "
            f"поля записи «{target.label}»:"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        scroll.setWidget(panel)
        form = QFormLayout(panel)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for target_field, candidates in _compatible_target_fields(entity, target):
            box = QComboBox()
            box.addItem("Не фильтровать", "")
            for candidate in candidates:
                box.addItem(f"{candidate.label} ({candidate.name})", candidate.name)
            box.setCurrentIndex(max(0, box.findData(current_filter.get(target_field.name, ""))))
            form.addRow(f"{target_field.label} ({target_field.name})", box)
            self._pickers[target_field.name] = box
        if not self._pickers:
            form.addRow(QLabel("В объектах нет полей совместимых типов."))
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_filter(self) -> dict[str, str]:
        return {
            target_name: box.currentData()
            for target_name, box in self._pickers.items()
            if box.currentData()
        }


class FormPreviewDialog(QDialog):
    """Full live preview of an entity's tabs, groups, conditional visibility, table parts, and workflow."""

    def __init__(
        self,
        parent: QWidget | None,
        entity: StudioEntity,
        preview_builder: Callable[[list[StudioField]], tuple[QWidget, FieldLayout]],
        project: StudioProject | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Предпросмотр карточки · {entity.label}")
        self.resize(780, 610)
        layout = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        scroll.setWidget(body)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 14, 16, 14)

        heading = QLabel(f"{entity.label} ({entity.kind})")
        heading.setProperty("heading", True)
        body_layout.addWidget(heading)

        visible_fields = [f for f in entity.fields if f.visible]
        if entity.kind == "Документ":
            visible_fields = [
                StudioField("number", "Номер", "String", read_only=bool(entity.numbering_prefix)),
                StudioField("date", "Дата", "DateTime", required=True),
                StudioField("posted", "Проведён", "Boolean", read_only=True),
                *visible_fields,
            ]
        panel, field_layout = preview_builder(visible_fields)
        body_layout.addWidget(panel)

        if project is not None and project.initial_data:
            self._populate_seed_refs(panel, visible_fields, project)

        for part in entity.table_parts:
            group = QGroupBox(f"Таблица: {part.label}")
            part_layout = QVBoxLayout(group)
            part_cols = [item for item in part.fields if item.visible]
            table = QTableWidget(1, max(1, len(part_cols)))
            table.setHorizontalHeaderLabels([item.label or item.name for item in part_cols] or ["Нет колонок"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.setMaximumHeight(110)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            part_layout.addWidget(table)
            if part.form_tab.strip():
                field_layout.add_to_tab(part.form_tab.strip(), group)
                field_layout.tabs.setVisible(True)
            else:
                body_layout.addWidget(group)

        meta_notes: list[str] = []
        if entity.calculations:
            meta_notes.append(f"Автовычислений: {len(entity.calculations)}")
        if entity.validations:
            meta_notes.append(f"Проверок: {len(entity.validations)}")
        if entity.print_form is not None:
            meta_notes.append(f"Печать: {entity.print_form.title}")
        if meta_notes:
            info = QLabel(" · ".join(meta_notes))
            info.setStyleSheet("color: #566577; font-size: 12px;")
            body_layout.addWidget(info)

        if entity.workflow is not None:
            wf_box = QGroupBox("Маршрут согласования")
            wf_layout = QVBoxLayout(wf_box)
            wf_layout.addWidget(QLabel(f"Начальный статус: {entity.workflow.initial}"))
            actions = ", ".join(step.label for step in entity.workflow.transitions) or "нет переходов"
            wf_layout.addWidget(QLabel(f"Переходы: {actions}"))
            body_layout.addWidget(wf_box)

        body_layout.addStretch()
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _populate_seed_refs(panel: QWidget, fields: list[StudioField], project: StudioProject) -> None:
        """Populate Ref combo boxes in FormPreviewDialog using initial_data records."""
        combos = panel.findChildren(QComboBox)
        seeds_by_entity: dict[str, list[tuple[str, str]]] = {}
        for seed in project.initial_data:
            ent = next((e for e in project.entities if e.name == seed.entity), None)
            rep_name = "name" if ent and any(f.name == "name" for f in ent.fields) else ""
            display = seed.values.get(rep_name) or next(iter(seed.values.values()), seed.key)
            seeds_by_entity.setdefault(seed.entity, []).append((seed.key, f"{display} ({seed.key})"))
        ref_fields = [f for f in fields if f.kind == "Ref"]
        # Match combos that currently only contain the empty placeholder item
        ref_combos = [c for c in combos if c.count() <= 1 and c.itemData(0) is None]
        for field_def, combo in zip(ref_fields, ref_combos):
            for key, label in seeds_by_entity.get(field_def.target, []):
                combo.addItem(label, key)


class ReportAdvancedDialog(QDialog):
    """Configure section, period filter, extra params, chart, row styling, and column formatting."""

    def __init__(
        self,
        parent: QWidget | None,
        config: StudioReport,
        project: StudioProject,
        source_kind: str,
        model_name: str,
        selected_columns: list[str],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Дополнительные настройки отчёта")
        self.resize(840, 560)
        self._source_kind = source_kind
        self._project = project
        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        # Tab 0: Main & Column formatting (preserves attributes self.section, auto_run, from_record, table, etc.)
        main_page = QWidget()
        main_layout = QVBoxLayout(main_page)
        form = QFormLayout()

        self.section = QComboBox()
        self.section.setEditable(True)
        self.section.addItems(["", *project.all_sections()])
        self.section.setCurrentText(config.section)
        self.section.setPlaceholderText("Пусто — раздел «Отчёты»")
        form.addRow("Раздел меню", self.section)

        self.auto_run = QCheckBox("Формировать автоматически при открытии")
        self.auto_run.setChecked(config.auto_run)
        form.addRow("", self.auto_run)

        self.sortable = QCheckBox("Разрешить сортировку таблицы по клику (sortable)")
        self.sortable.setChecked(config.sortable)
        form.addRow("", self.sortable)

        self.table_part = QComboBox()
        self.date_filter = QComboBox()
        self.posted_only = QCheckBox("Только проведённые документы")
        self.posted_only.setChecked(config.posted_only)
        self.from_record = QCheckBox("Открывать из карточки записи отбора (from_record)")
        self.from_record.setChecked(config.ref_filter_from_record)
        self.latest_only = QCheckBox("Только актуальные значения (срез последних)")
        self.latest_only.setChecked(config.latest_only)

        entity = next((e for e in project.entities if e.name == model_name), None) if source_kind == "entity" else None
        register = next((r for r in project.registers if r.name == model_name), None) if source_kind == "register" else None
        if entity is not None:
            self.table_part.addItem("Только шапка объекта", "")
            for part in entity.table_parts:
                self.table_part.addItem(f"{part.label} ({part.name})", part.name)
            self.table_part.setCurrentIndex(max(0, self.table_part.findData(config.table_part)))
            self.date_filter.addItem("Без фильтра по периоду", "")
            if entity.kind == "Документ":
                self.date_filter.addItem("Дата документа (date)", "date")
            for item in entity.fields:
                if item.kind in ("Date", "DateTime"):
                    self.date_filter.addItem(f"{item.label} ({item.name})", item.name)
            self.date_filter.setCurrentIndex(max(0, self.date_filter.findData(config.date_filter)))
            form.addRow("Табличная часть", self.table_part)
            form.addRow("Фильтр по периоду", self.date_filter)
            form.addRow("", self.posted_only)
            form.setRowVisible(self.table_part, bool(entity.table_parts))
            form.setRowVisible(self.posted_only, entity.kind == "Документ")
            self.latest_only.hide()
        else:
            self.table_part.hide()
            self.date_filter.hide()
            self.posted_only.hide()
            if register is not None and register.kind == "Сведения":
                form.addRow("", self.latest_only)
            else:
                self.latest_only.hide()

        form.addRow("", self.from_record)
        main_layout.addLayout(form)

        main_layout.addWidget(QLabel("Оформление и итоги колонок"))
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Колонка", "Агрегация", "Формат", "Префикс", "Суффикс", "Выравнивание", "Ширина"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        main_layout.addWidget(self.table, 1)

        labels = self._column_labels(entity, model_name)
        self._column_names = list(selected_columns)
        for col_name in self._column_names:
            row = self.table.rowCount()
            self.table.insertRow(row)
            label_item = QTableWidgetItem(f"{labels.get(col_name, col_name)} ({col_name})")
            label_item.setFlags(label_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, label_item)
            opts = config.column_options.get(col_name, {})

            agg = QComboBox()
            agg.addItem("По умолчанию", "__default__")
            for key, title in AGGREGATE_TYPES.items():
                agg.addItem(title, key)
            if "aggregate" in opts:
                agg.setCurrentIndex(max(0, agg.findData(opts["aggregate"])))
            else:
                agg.setCurrentIndex(0)
            self.table.setCellWidget(row, 1, agg)

            self.table.setItem(row, 2, QTableWidgetItem(opts.get("format", "")))
            self.table.setItem(row, 3, QTableWidgetItem(opts.get("prefix", "")))
            self.table.setItem(row, 4, QTableWidgetItem(opts.get("suffix", "")))

            align = QComboBox()
            for key, title in ALIGN_TYPES.items():
                align.addItem(title, key)
            align.setCurrentIndex(max(0, align.findData(opts.get("align", ""))))
            self.table.setCellWidget(row, 5, align)

            self.table.setItem(row, 6, QTableWidgetItem(opts.get("width", "")))

        tabs.addTab(main_page, "Основные и колонки")

        # Tab 1: Extra filters + BarChart + Row styles
        extras_page = QWidget()
        extras_layout = QVBoxLayout(extras_page)
        extra_form = QFormLayout()

        self.choice_filter_field = QComboBox()
        self.text_filter_field = QComboBox()
        self.choice_filter_field.addItem("Не фильтровать", "")
        self.text_filter_field.addItem("Не искать по строке", "")
        all_ent_fields: dict[str, StudioField] = {}
        if entity is not None:
            all_ent_fields, _ = entity_report_fields(entity, config.table_part)
            for fname, fdef in all_ent_fields.items():
                if fdef.kind == "Boolean" or (fdef.kind == "Choice" and not fdef.multi):
                    self.choice_filter_field.addItem(f"{fdef.label} ({fname})", fname)
                if fdef.kind == "String":
                    self.text_filter_field.addItem(f"{fdef.label} ({fname})", fname)
            self.choice_filter_field.setCurrentIndex(max(0, self.choice_filter_field.findData(config.choice_filter_field)))
            self.text_filter_field.setCurrentIndex(max(0, self.text_filter_field.findData(config.text_filter_field)))
            extra_form.addRow("Отбор по Choice / Boolean", self.choice_filter_field)
            extra_form.addRow("Текстовый поиск (подстрока)", self.text_filter_field)
        else:
            self.choice_filter_field.hide()
            self.text_filter_field.hide()

        self.chart_label = QComboBox()
        self.chart_value = QComboBox()
        self.chart_title = QLineEdit(config.chart_title)
        self.chart_title.setPlaceholderText("Необязательный заголовок диаграммы")
        self.chart_color = QComboBox()
        self.chart_color.setEditable(True)
        self.chart_color.addItems(("", "#4A7BFF", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6"))
        self.chart_color.setCurrentText(config.chart_color)

        self.chart_label.addItem("Отключить диаграмму", "")
        self.chart_value.addItem("Отключить диаграмму", "")
        numeric_cols = self._numeric_columns(entity, register, model_name)
        for col_name in self._column_names:
            lbl = labels.get(col_name, col_name)
            self.chart_label.addItem(f"{lbl} ({col_name})", col_name)
            if col_name in numeric_cols:
                self.chart_value.addItem(f"{lbl} ({col_name})", col_name)
        self.chart_label.setCurrentIndex(max(0, self.chart_label.findData(config.chart_label)))
        self.chart_value.setCurrentIndex(max(0, self.chart_value.findData(config.chart_value)))
        extra_form.addRow("Диаграмма: категории (ось X)", self.chart_label)
        extra_form.addRow("Диаграмма: число (ось Y)", self.chart_value)
        extra_form.addRow("Заголовок диаграммы", self.chart_title)
        extra_form.addRow("Цвет столбцов (#RRGGBB)", self.chart_color)
        extras_layout.addLayout(extra_form)

        self.style_table = QTableWidget(0, 5)
        self.style_table.setHorizontalHeaderLabels(["Поле", "Значение", "Стиль", "Цвет (#RRGGBB)", "Жирный"])
        self.style_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.style_table.verticalHeader().setVisible(False)
        self.style_table.verticalHeader().setDefaultSectionSize(34)
        self._style_fields = list(all_ent_fields.keys())
        self._style_labels = {k: v.label for k, v in all_ent_fields.items()}
        if entity is not None:
            extras_layout.addWidget(QLabel("Условное оформление строк отчёта"))
            extras_layout.addWidget(self.style_table, 1)
            for srule in config.row_styles:
                self._add_style_row(srule)
            st_btns = QHBoxLayout()
            add_st = QPushButton("Добавить правило цвета")
            add_st.clicked.connect(lambda: self._add_style_row({}))
            del_st = QPushButton("Удалить правило")
            del_st.clicked.connect(lambda: self.style_table.removeRow(self.style_table.currentRow()))
            st_btns.addWidget(add_st)
            st_btns.addWidget(del_st)
            st_btns.addStretch()
            extras_layout.addLayout(st_btns)
        else:
            self.style_table.hide()
            extras_layout.addStretch()

        tabs.addTab(extras_page, "Фильтры, диаграмма и стили")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_style_row(self, rule: dict[str, str]) -> None:
        row = self.style_table.rowCount()
        self.style_table.insertRow(row)
        field_box = QComboBox()
        for fname in self._style_fields:
            field_box.addItem(f"{self._style_labels.get(fname, fname)} ({fname})", fname)
        field_box.setCurrentIndex(max(0, field_box.findData(rule.get("field", ""))))
        self.style_table.setCellWidget(row, 0, field_box)
        self.style_table.setItem(row, 1, QTableWidgetItem(rule.get("value", "")))
        style_box = QComboBox()
        for code, title in (("", "Обычный"), ("completed", "Выполнено (серый зачёркнутый)"), ("current", "Активный (синий жирный)")):
            style_box.addItem(title, code)
        style_box.setCurrentIndex(max(0, style_box.findData(rule.get("style", ""))))
        self.style_table.setCellWidget(row, 2, style_box)
        self.style_table.setItem(row, 3, QTableWidgetItem(rule.get("color", "")))
        bold_item = QTableWidgetItem()
        bold_item.setFlags(bold_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        bold_item.setCheckState(Qt.CheckState.Checked if rule.get("bold") in ("1", "True", "true") else Qt.CheckState.Unchecked)
        self.style_table.setItem(row, 4, bold_item)

    def _numeric_columns(self, entity: StudioEntity | None, register: object | None, model_name: str) -> set[str]:
        if entity is not None:
            fields, _ = entity_report_fields(entity, self.table_part.currentData() or "")
            return {k for k, v in fields.items() if v.kind in ("Integer", "Decimal")}
        reg = next((r for r in self._project.registers if r.name == model_name), None)
        if reg is not None:
            return {m.name for m in reg.members if m.kind == "Ресурс" and m.value_type in ("Целое", "Десятичное")}
        return set()

    def _column_labels(self, entity: StudioEntity | None, model_name: str) -> dict[str, str]:
        if entity is not None:
            fields, _ = entity_report_fields(entity, self.table_part.currentData() or "")
            return {k: v.label for k, v in fields.items()}
        register = next((r for r in self._project.registers if r.name == model_name), None)
        if register is not None:
            return {m.name: m.label for m in register_report_columns(register)}
        return {}

    def apply_to(self) -> dict[str, object]:
        column_options: dict[str, dict[str, str]] = {}
        for row, col_name in enumerate(self._column_names):
            opts: dict[str, str] = {}
            agg_box = self.table.cellWidget(row, 1)
            if isinstance(agg_box, QComboBox) and agg_box.currentData() != "__default__":
                opts["aggregate"] = agg_box.currentData()
            for col_idx, key in ((2, "format"), (3, "prefix"), (4, "suffix"), (6, "width")):
                item = self.table.item(row, col_idx)
                val = item.text() if item is not None else ""
                if key == "width":
                    val = val.strip()
                if val:
                    opts[key] = val
            align_box = self.table.cellWidget(row, 5)
            if isinstance(align_box, QComboBox) and align_box.currentData():
                opts["align"] = align_box.currentData()
            if opts:
                column_options[col_name] = opts
        row_styles: list[dict[str, str]] = []
        if self._source_kind == "entity":
            for r in range(self.style_table.rowCount()):
                f_box = self.style_table.cellWidget(r, 0)
                s_box = self.style_table.cellWidget(r, 2)
                if isinstance(f_box, QComboBox) and isinstance(s_box, QComboBox) and f_box.currentData():
                    val_item = self.style_table.item(r, 1)
                    col_item = self.style_table.item(r, 3)
                    bold_item = self.style_table.item(r, 4)
                    row_styles.append({
                        "field": f_box.currentData(),
                        "value": val_item.text() if val_item else "",
                        "style": s_box.currentData() or "",
                        "color": col_item.text().strip() if col_item else "",
                        "bold": "1" if (bold_item and bold_item.checkState() == Qt.CheckState.Checked) else "",
                    })
        return {
            "section": self.section.currentText().strip(),
            "auto_run": self.auto_run.isChecked(),
            "sortable": self.sortable.isChecked(),
            "latest_only": self.latest_only.isChecked() if self._source_kind == "register" else False,
            "table_part": (self.table_part.currentData() or "") if self._source_kind == "entity" else "",
            "date_filter": (self.date_filter.currentData() or "") if self._source_kind == "entity" else "",
            "posted_only": self.posted_only.isChecked() if self._source_kind == "entity" else False,
            "ref_filter_from_record": self.from_record.isChecked(),
            "choice_filter_field": (self.choice_filter_field.currentData() or "") if self._source_kind == "entity" else "",
            "text_filter_field": (self.text_filter_field.currentData() or "") if self._source_kind == "entity" else "",
            "chart_label": self.chart_label.currentData() or "",
            "chart_value": self.chart_value.currentData() or "",
            "chart_title": self.chart_title.text().strip(),
            "chart_color": self.chart_color.currentText().strip(),
            "row_styles": row_styles,
            "column_options": column_options,
        }


class RemindersDialog(QDialog):
    """Configure deadline reminder rules for entities (app.enable_reminders)."""

    def __init__(self, parent: QWidget | None, project: StudioProject) -> None:
        super().__init__(parent)
        self.setWindowTitle("Напоминания о сроках")
        self.resize(760, 390)
        self._project = project
        layout = QVBoxLayout(self)
        note = QLabel("Настроенные правила проверяют приближение сроков и показывают окно напоминания в приложении.")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Объект", "Поле даты/времени", "Флаг выполнения", "Заголовок", "За сколько минут"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        layout.addWidget(self.table, 1)

        for rule in project.reminders:
            self._add_row(rule)

        controls = QHBoxLayout()
        add_btn = QPushButton("Добавить правило")
        add_btn.setEnabled(bool(project.entities))
        add_btn.clicked.connect(lambda: self._add_row(StudioReminder(project.entities[0].name)))
        del_btn = QPushButton("Удалить")
        del_btn.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        controls.addWidget(add_btn)
        controls.addWidget(del_btn)
        controls.addStretch()
        layout.addLayout(controls)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_row(self, rule: StudioReminder) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        entity_box = QComboBox()
        for entity in self._project.entities:
            entity_box.addItem(f"{entity.label} ({entity.name})", entity.name)
        entity_box.setCurrentIndex(max(0, entity_box.findData(rule.entity)))

        date_box = QComboBox()
        done_box = QComboBox()
        title_box = QComboBox()
        minutes = QSpinBox()
        minutes.setRange(0, 60 * 24 * 30)
        minutes.setValue(rule.notify_before_minutes)

        def populate_fields() -> None:
            ent = next((e for e in self._project.entities if e.name == entity_box.currentData()), None)
            date_cur = date_box.currentData() or rule.date_field
            done_cur = done_box.currentData() or rule.done_field
            title_cur = title_box.currentData() or rule.title_field
            date_box.clear()
            done_box.clear()
            title_box.clear()
            done_box.addItem("(не проверять)", "")
            if ent is None:
                return
            if ent.kind == "Документ":
                date_box.addItem("Дата документа (date)", "date")
                done_box.addItem("Проведён (posted)", "posted")
                title_box.addItem("Номер (number)", "number")
            for f in ent.fields:
                if f.kind in ("Date", "DateTime"):
                    date_box.addItem(f"{f.label} ({f.name})", f.name)
                if f.kind == "Boolean":
                    done_box.addItem(f"{f.label} ({f.name})", f.name)
                title_box.addItem(f"{f.label} ({f.name})", f.name)
            date_box.setCurrentIndex(max(0, date_box.findData(date_cur)))
            done_box.setCurrentIndex(max(0, done_box.findData(done_cur)))
            title_box.setCurrentIndex(max(0, title_box.findData(title_cur)))

        entity_box.currentIndexChanged.connect(populate_fields)
        populate_fields()
        for col, widget in enumerate((entity_box, date_box, done_box, title_box, minutes)):
            self.table.setCellWidget(row, col, widget)

    def _save(self) -> None:
        collected: list[StudioReminder] = []
        for row in range(self.table.rowCount()):
            entity_box = self.table.cellWidget(row, 0)
            date_box = self.table.cellWidget(row, 1)
            done_box = self.table.cellWidget(row, 2)
            title_box = self.table.cellWidget(row, 3)
            minutes_box = self.table.cellWidget(row, 4)
            assert (
                isinstance(entity_box, QComboBox) and isinstance(date_box, QComboBox)
                and isinstance(done_box, QComboBox) and isinstance(title_box, QComboBox)
                and isinstance(minutes_box, QSpinBox)
            )
            if not date_box.currentData():
                QMessageBox.warning(self, "Напоминания", "Выберите поле даты/времени для каждой строки.")
                return
            collected.append(StudioReminder(
                entity=entity_box.currentData(),
                date_field=date_box.currentData(),
                done_field=done_box.currentData() or "",
                title_field=title_box.currentData() or "",
                notify_before_minutes=minutes_box.value(),
            ))
        self.result_reminders = collected
        self.accept()


class RolesOverviewDialog(QDialog):
    """Summary of all role assignments across entities, fields, commands, and workflows."""

    def __init__(self, parent: QWidget | None, project: StudioProject) -> None:
        super().__init__(parent)
        self.setWindowTitle("Обзор ролей и прав доступа")
        self.resize(780, 440)
        layout = QVBoxLayout(self)

        roles = project.all_roles()
        header = QLabel(
            f"Роли в проекте: {', '.join(roles) if roles else 'не заданы'} · "
            f"Вход по учётным записям: {'включён' if project.local_accounts else 'отключён'}"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Объект", "Область", "Ограничение", "Роли"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(table, 1)

        def add_row(entity_label: str, scope: str, target: str, role_list: list[str] | tuple[str, ...]) -> None:
            row = table.rowCount()
            table.insertRow(row)
            for col, text in enumerate((entity_label, scope, target, ", ".join(role_list) or "только admin")):
                table.setItem(row, col, QTableWidgetItem(text))

        for entity in project.entities:
            if entity.owner_only:
                add_row(entity.label, "Записи", "Доступ по владельцу", ["владелец", "admin"])
            if entity.permissions is not None:
                for op, allowed in entity.permissions.items():
                    add_row(entity.label, "Операция", op, allowed)
            for item in entity.fields:
                if item.read_roles is not None:
                    add_row(entity.label, "Чтение поля", item.label, item.read_roles)
                if item.write_roles is not None:
                    add_row(entity.label, "Изменение поля", item.label, item.write_roles)
            for part in entity.table_parts:
                for item in part.fields:
                    if item.read_roles is not None:
                        add_row(entity.label, f"Чтение ({part.label})", item.label, item.read_roles)
                    if item.write_roles is not None:
                        add_row(entity.label, f"Изменение ({part.label})", item.label, item.write_roles)
            for cmd, allowed in entity.command_roles.items():
                add_row(entity.label, "Команда", entity.commands.get(cmd, cmd), allowed)
            if entity.workflow is not None:
                for step in entity.workflow.transitions:
                    if step.roles or step.assigned_only:
                        suffix = " (по назначению)" if step.assigned_only else ""
                        add_row(entity.label, "Согласование", step.label + suffix, step.roles or ["любая"])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class DatabaseInspectorDialog(QDialog):
    """Read-only SQLite database inspector for Studio working DBs and backups."""

    def __init__(self, parent: QWidget | None, initial_path: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Инспектор базы данных SQLite")
        self.resize(940, 580)
        self._db_path = initial_path
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.path_label = QLineEdit(str(initial_path) if initial_path else "")
        self.path_label.setReadOnly(True)
        self.path_label.setPlaceholderText("Выберите файл базы данных SQLite…")
        browse = QPushButton("Выбрать файл…")
        browse.clicked.connect(self._choose_file)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self._reload_tables)
        top.addWidget(self.path_label, 1)
        top.addWidget(browse)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.summary = QLabel("База не открыта.")
        layout.addWidget(self.summary)

        splitter = QSplitter()
        layout.addWidget(splitter, 1)

        self.tables = QListWidget()
        self.tables.currentRowChanged.connect(lambda _row: self._load_table_rows())
        splitter.addWidget(self.tables)

        right = QTabWidget()
        data_page = QWidget()
        right_layout = QVBoxLayout(data_page)
        right_layout.setContentsMargins(6, 6, 6, 6)
        filter_row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Быстрый фильтр по загруженным строкам…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.schema_label = QLabel("")
        self.schema_label.setWordWrap(True)
        export_csv_btn = QPushButton("Экспорт CSV…")
        export_csv_btn.clicked.connect(self._export_table_csv)
        filter_row.addWidget(QLabel("Фильтр:"))
        filter_row.addWidget(self.filter_edit, 1)
        filter_row.addWidget(export_csv_btn)
        right_layout.addLayout(filter_row)
        right_layout.addWidget(self.schema_label)
        self.grid = QTableWidget()
        self.grid.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.grid.setAlternatingRowColors(True)
        right_layout.addWidget(self.grid, 1)
        right.addTab(data_page, "Данные таблицы")

        sql_page = QWidget()
        sql_layout = QVBoxLayout(sql_page)
        sql_layout.setContentsMargins(6, 6, 6, 6)
        self.sql_edit = QPlainTextEdit()
        self.sql_edit.setMaximumHeight(95)
        self.sql_edit.setPlaceholderText("SELECT * FROM ... (только чтение)")
        sql_bar = QHBoxLayout()
        run_sql_btn = QPushButton("Выполнить запрос")
        run_sql_btn.setProperty("primary", True)
        run_sql_btn.clicked.connect(self._run_readonly_sql)
        self.sql_status = QLabel("Готово")
        sql_bar.addWidget(run_sql_btn)
        sql_bar.addWidget(self.sql_status, 1)
        self.sql_grid = QTableWidget()
        self.sql_grid.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sql_grid.setAlternatingRowColors(True)
        sql_layout.addWidget(self.sql_edit)
        sql_layout.addLayout(sql_bar)
        sql_layout.addWidget(self.sql_grid, 1)
        right.addTab(sql_page, "SQL-запрос (SELECT)")

        splitter.addWidget(right)
        splitter.setSizes([240, 680])

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if initial_path is not None and initial_path.is_file():
            self._reload_tables()

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть SQLite", "", "SQLite (*.sqlite3 *.sqlite *.db);;Все файлы (*)",
        )
        if path:
            self._db_path = Path(path)
            self.path_label.setText(path)
            self._reload_tables()

    @staticmethod
    def _quote_ident(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _connect(self) -> sqlite3.Connection:
        assert self._db_path is not None
        uri = self._db_path.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def _reload_tables(self) -> None:
        self.tables.clear()
        self.grid.setRowCount(0)
        self.grid.setColumnCount(0)
        self.schema_label.setText("")
        if self._db_path is None or not self._db_path.is_file():
            self.summary.setText("Файл базы данных не найден.")
            return
        try:
            with closing(self._connect()) as conn:
                names = [
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                ]
                schema_hash = ""
                if "_erp_config" in names:
                    row = conn.execute("SELECT value FROM _erp_config WHERE key='schema_hash'").fetchone()
                    schema_hash = f" · schema_hash: {row[0][:12]}" if row and row[0] else ""
                total_rows = 0
                for name in names:
                    count = conn.execute(f"SELECT COUNT(*) FROM {self._quote_ident(name)}").fetchone()[0]
                    total_rows += count
                    item = QListWidgetItem(f"{name} ({count})")
                    item.setData(Qt.ItemDataRole.UserRole, name)
                    self.tables.addItem(item)
                self.summary.setText(f"Таблиц: {len(names)} · Всего записей: {total_rows}{schema_hash}")
        except (OSError, sqlite3.Error) as exc:
            self.summary.setText(f"Ошибка чтения базы: {exc}")
            return
        if self.tables.count() > 0:
            self.tables.setCurrentRow(0)

    def _load_table_rows(self) -> None:
        item = self.tables.currentItem()
        if item is None or self._db_path is None or not self._db_path.is_file():
            return
        table_name = item.data(Qt.ItemDataRole.UserRole)
        quoted = self._quote_ident(table_name)
        try:
            with closing(self._connect()) as conn:
                info = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                columns = [col[1] for col in info]
                col_types = ", ".join(f"{col[1]}: {col[2] or 'ANY'}" for col in info)
                rows = conn.execute(f"SELECT * FROM {quoted} LIMIT 500").fetchall()
        except (OSError, sqlite3.Error) as exc:
            self.schema_label.setText(f"Ошибка: {exc}")
            return
        self.schema_label.setText(f"Колонки: {col_types}" + (" (показаны первые 500)" if len(rows) == 500 else ""))
        self.grid.clear()
        self.grid.setColumnCount(len(columns))
        self.grid.setHorizontalHeaderLabels(columns)
        self.grid.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            for c_idx, val in enumerate(row):
                text = "" if val is None else str(val)
                self.grid.setItem(r_idx, c_idx, QTableWidgetItem(text))
        self.grid.resizeColumnsToContents()
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.filter_edit.text().strip().casefold()
        for row in range(self.grid.rowCount()):
            if not needle:
                self.grid.setRowHidden(row, False)
                continue
            matched = any(
                (item := self.grid.item(row, col)) is not None and needle in item.text().casefold()
                for col in range(self.grid.columnCount())
            )
            self.grid.setRowHidden(row, not matched)

    def _export_table_csv(self) -> None:
        item = self.tables.currentItem()
        if item is None or self.grid.columnCount() == 0:
            return
        table_name = item.data(Qt.ItemDataRole.UserRole) or "table"
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт таблицы в CSV", f"{table_name}.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow([
                sanitize_csv_cell(self.grid.horizontalHeaderItem(c).text() if self.grid.horizontalHeaderItem(c) else "")
                for c in range(self.grid.columnCount())
            ])
            for row in range(self.grid.rowCount()):
                if self.grid.isRowHidden(row):
                    continue
                writer.writerow([
                    sanitize_csv_cell(self.grid.item(row, c).text() if self.grid.item(row, c) else "")
                    for c in range(self.grid.columnCount())
                ])

    def _run_readonly_sql(self) -> None:
        if self._db_path is None or not self._db_path.is_file():
            self.sql_status.setText("База не открыта.")
            return
        sql = self.sql_edit.toPlainText().strip().rstrip(";")
        if not sql:
            return
        prefix = sql.lstrip().split(None, 1)[0].upper()
        if prefix not in ("SELECT", "WITH", "PRAGMA", "EXPLAIN"):
            self.sql_status.setText("Разрешены только запросы чтения (SELECT, WITH, PRAGMA, EXPLAIN).")
            return
        try:
            with closing(self._connect()) as conn:
                cursor = conn.execute(sql)
                cols = [d[0] for d in (cursor.description or ())]
                rows = cursor.fetchmany(500)
        except (OSError, sqlite3.Error) as exc:
            self.sql_status.setText(f"Ошибка SQL: {exc}")
            return
        self.sql_grid.clear()
        self.sql_grid.setColumnCount(len(cols))
        self.sql_grid.setHorizontalHeaderLabels(cols)
        self.sql_grid.setRowCount(len(rows))
        for r_idx, row in enumerate(rows):
            for c_idx, val in enumerate(row):
                self.sql_grid.setItem(r_idx, c_idx, QTableWidgetItem("" if val is None else str(val)))
        self.sql_grid.resizeColumnsToContents()
        self.sql_status.setText(f"Строк: {len(rows)}" + (" (показаны первые 500)" if len(rows) == 500 else ""))


class ProjectDiagramWidget(QWidget):
    """Interactive ER/architecture diagram showing entities, registers, reports, and links."""

    def __init__(self, on_navigate: Callable[[str, str], None]) -> None:
        super().__init__()
        self._on_navigate = on_navigate
        self._boxes: list[tuple[QRectF, str, str, str, str, list[str]]] = []
        self._edges: list[tuple[str, str, str, QColor, Qt.PenStyle]] = []
        self._card_rects: dict[str, QRectF] = {}
        self.setMinimumSize(780, 460)

    def refresh(self, project: StudioProject) -> None:
        self._boxes.clear()
        self._edges.clear()
        self._card_rects.clear()

        catalogs = [e for e in project.entities if e.kind == "Справочник"]
        docs = [e for e in project.entities if e.kind == "Документ"]
        col_items: list[list[tuple[str, str, str, str, str, list[str]]]] = [[], [], [], []]

        def entity_lines(ent: StudioEntity) -> list[str]:
            lines = [f"{f.name}: {f.target if f.kind == 'Ref' else f.kind}" for f in ent.fields[:5]]
            for p in ent.table_parts[:2]:
                lines.append(f"[{p.name}] ({len(p.fields)} кол.)")
            return lines[:6]

        for ent in catalogs:
            col_items[0].append((ent.name, ent.label, "Справочник", "#225da6", "Объект", entity_lines(ent)))
        for ent in docs:
            col_items[1].append((ent.name, ent.label, "Документ", "#1e7a6d", "Объект", entity_lines(ent)))
        for reg in project.registers:
            lines = [f"{m.name}: {m.target or m.value_type}" for m in reg.members[:5]]
            col_items[2].append((reg.name, reg.name, f"Регистр · {reg.kind}", "#9c5712", "Регистры", lines))
        for rep in project.reports:
            col_items[3].append((rep.name, rep.title, "Отчёт", "#2d6a3e", "Отчёты", [f"← {rep.entity}"]))

        active_cols = [c for c in col_items if c]
        margin_x, margin_y = 26, 26
        col_w, col_gap, row_gap = 220, 74, 24
        max_h = 420
        for col_idx, cards in enumerate(active_cols):
            x = margin_x + col_idx * (col_w + col_gap)
            y = margin_y
            for key, title, subtitle, color, tab_name, lines in cards:
                h = 54 + max(1, len(lines)) * 18 + 10
                rect = QRectF(x, y, col_w, h)
                self._boxes.append((rect, key, title, subtitle, color, lines))
                self._card_rects[key] = rect
                y += h + row_gap
            max_h = max(max_h, int(y + 20))

        total_w = max(780, margin_x * 2 + len(active_cols) * (col_w + col_gap))
        self.setMinimumSize(total_w, max_h)

        for ent in project.entities:
            for f in ent.fields + [pf for p in ent.table_parts for pf in p.fields]:
                if f.kind == "Ref" and f.target in self._card_rects:
                    if f.create_from:
                        self._edges.append((f.target, ent.name, f.create_from, QColor("#2d6a3e"), Qt.PenStyle.DashLine))
                    else:
                        self._edges.append((ent.name, f.target, f.name, QColor("#2563ae"), Qt.PenStyle.SolidLine))
            for mv in ent.movements:
                if mv.register in self._card_rects:
                    self._edges.append((ent.name, mv.register, "+" if mv.income else "−", QColor("#b45309"), Qt.PenStyle.SolidLine))
            for calc in ent.calculations:
                if calc.kind == "lookup" and calc.register in self._card_rects:
                    self._edges.append((calc.register, ent.name, "lookup", QColor("#7c3aed"), Qt.PenStyle.DotLine))
        for rep in project.reports:
            if rep.entity in self._card_rects:
                self._edges.append((rep.entity, rep.name, "", QColor("#2d6a3e"), Qt.PenStyle.DotLine))
        self.update()

    def mousePressEvent(self, event) -> None:
        pos = event.position()
        for rect, key, _title, _sub, _color, tab_name in self._boxes:
            if rect.contains(pos):
                entity_name = key if tab_name == "Объект" else ""
                self._on_navigate(entity_name, tab_name)
                return
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f8fafd"))
        if not self._boxes:
            painter.setPen(QColor("#65748a"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Добавьте объекты для построения схемы связей")
            painter.end()
            return

        # Draw connections first
        for src_key, dst_key, label, color, style in self._edges:
            src_r = self._card_rects.get(src_key)
            dst_r = self._card_rects.get(dst_key)
            if src_r is None or dst_r is None:
                continue
            if src_r.center().x() <= dst_r.center().x():
                p1 = QPointF(src_r.right(), src_r.center().y())
                p2 = QPointF(dst_r.left(), dst_r.center().y())
            else:
                p1 = QPointF(src_r.left(), src_r.center().y())
                p2 = QPointF(dst_r.right(), dst_r.center().y())
            if src_key == dst_key:
                continue
            pen = QPen(color, 1.6, style)
            painter.setPen(pen)
            path = QPainterPath(p1)
            dx = (p2.x() - p1.x()) * 0.45
            path.cubicTo(p1.x() + dx, p1.y(), p2.x() - dx, p2.y(), p2.x(), p2.y())
            painter.drawPath(path)
            if label:
                mid = path.pointAtPercent(0.5)
                painter.setFont(QFont("", 8))
                painter.drawText(int(mid.x() - 35), int(mid.y() - 12), 70, 16, Qt.AlignmentFlag.AlignCenter, label)

        # Draw cards
        title_font = QFont("", 10, QFont.Weight.Bold)
        sub_font = QFont("", 8)
        body_font = QFont("", 9)
        fm = QFontMetrics(body_font)
        for rect, key, title, subtitle, color, lines in self._boxes:
            painter.setPen(QPen(QColor("#cbd6e6"), 1.2))
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(rect, 8, 8)
            hdr = QRectF(rect.x(), rect.y(), rect.width(), 42)
            painter.setBrush(QColor(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(hdr, 8, 8)
            painter.fillRect(QRectF(rect.x(), rect.y() + 30, rect.width(), 12), QColor(color))
            painter.setPen(QColor("#ffffff"))
            painter.setFont(title_font)
            painter.drawText(QRectF(rect.x() + 10, rect.y() + 5, rect.width() - 20, 18), Qt.AlignmentFlag.AlignLeft, title)
            painter.setFont(sub_font)
            painter.drawText(QRectF(rect.x() + 10, rect.y() + 23, rect.width() - 20, 15), Qt.AlignmentFlag.AlignLeft, f"{subtitle} · {key}")
            painter.setPen(QColor("#243c5d"))
            painter.setFont(body_font)
            for idx, line in enumerate(lines):
                elided = fm.elidedText(line, Qt.TextElideMode.ElideRight, int(rect.width() - 20))
                painter.drawText(int(rect.x() + 10), int(rect.y() + 58 + idx * 18), elided)
        painter.end()


class CalculationsValidationsDialog(QDialog):
    """Visual designer for StudioCalculation (before_save) and StudioValidation (on_validate) rules."""

    def __init__(self, parent: QWidget | None, entity: StudioEntity, project: StudioProject) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Расчёты и проверки · {entity.label}")
        self.resize(780, 480)
        self._entity = entity
        self._project = project
        self._calculations = [StudioCalculation(**vars(c)) for c in entity.calculations]
        self._validations = [StudioValidation(**vars(v)) for v in entity.validations]

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        # Tab 1: Calculations
        calc_page = QWidget()
        calc_layout = QVBoxLayout(calc_page)
        calc_layout.addWidget(QLabel("Правила автоматически генерируют before_save() для пересчёта строк, итогов и подстановки цен."))
        self.calc_list = QListWidget()
        calc_layout.addWidget(self.calc_list, 1)
        calc_btns = QHBoxLayout()
        for label, kind in (
            ("+ Формула строки", "row_formula"),
            ("+ Итог по таблице", "table_total"),
            ("+ Цена из регистра", "lookup"),
            ("+ Формула шапки", "header_formula"),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _c=False, k=kind: self._add_calc(k))
            calc_btns.addWidget(btn)
        del_calc = QPushButton("Удалить")
        del_calc.clicked.connect(self._remove_calc)
        calc_btns.addWidget(del_calc)
        calc_btns.addStretch()
        calc_layout.addLayout(calc_btns)
        tabs.addTab(calc_page, "Вычисления")

        # Tab 2: Validations
        val_page = QWidget()
        val_layout = QVBoxLayout(val_page)
        val_layout.addWidget(QLabel("Проверки выполняются перед сохранением и показывают понятное сообщение об ошибке."))
        self.val_list = QListWidget()
        val_layout.addWidget(self.val_list, 1)
        val_btns = QHBoxLayout()
        for label, kind in (
            ("+ Таблица не пуста", "require_rows"),
            ("+ Уникальность в строках", "unique_rows"),
            ("+ Сравнение полей", "compare"),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _c=False, k=kind: self._add_val(k))
            val_btns.addWidget(btn)
        del_val = QPushButton("Удалить")
        del_val.clicked.connect(self._remove_val)
        val_btns.addWidget(del_val)
        val_btns.addStretch()
        val_layout.addLayout(val_btns)
        tabs.addTab(val_page, "Проверки")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_lists()

    @property
    def result_calculations(self) -> list[StudioCalculation]:
        return self._calculations

    @property
    def result_validations(self) -> list[StudioValidation]:
        return self._validations

    def _refresh_lists(self) -> None:
        self.calc_list.clear()
        for c in self._calculations:
            if c.kind == "row_formula":
                self.calc_list.addItem(f"[Строка] {c.target} = {c.left} {c.operator} {c.right}")
            elif c.kind == "table_total":
                self.calc_list.addItem(f"[Итог] {c.target} = {c.operator}({c.source_part}.{c.left or '*'})")
            elif c.kind == "header_formula":
                self.calc_list.addItem(f"[Шапка] {c.target} = {c.left} {c.operator} {c.right}")
            else:
                self.calc_list.addItem(f"[Регистр] {c.target} ← {c.register}.{c.resource}")
        self.val_list.clear()
        for v in self._validations:
            if v.kind == "require_rows":
                self.val_list.addItem(f"[Не пусто] {v.target}: {v.message or 'обязательны строки'}")
            elif v.kind == "unique_rows":
                self.val_list.addItem(f"[Уникально] {v.target}.{v.column}: {v.message or 'без повторов'}")
            else:
                self.val_list.addItem(f"[Сравнение] {v.target} {v.operator} {v.other}: {v.message}")

    def _remove_calc(self) -> None:
        row = self.calc_list.currentRow()
        if row >= 0:
            self._calculations.pop(row)
            self._refresh_lists()

    def _remove_val(self) -> None:
        row = self.val_list.currentRow()
        if row >= 0:
            self._validations.pop(row)
            self._refresh_lists()

    def _add_calc(self, kind: str) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Новое вычисление")
        dlg.resize(480, 320)
        form = QFormLayout(dlg)
        if kind == "row_formula":
            if not self._entity.table_parts:
                QMessageBox.information(self, "Вычисления", "Сначала добавьте табличную часть.")
                return
            part_box = QComboBox()
            for p in self._entity.table_parts:
                part_box.addItem(p.label, p.name)
            target_box, left_box, op_box, right_edit = QComboBox(), QComboBox(), QComboBox(), QComboBox()
            right_edit.setEditable(True)
            op_box.addItems(["*", "+", "-", "/"])

            def load_cols() -> None:
                part = next(p for p in self._entity.table_parts if p.name == part_box.currentData())
                nums = [f for f in part.fields if f.kind in ("Integer", "Decimal")]
                for box in (target_box, left_box, right_edit):
                    box.clear()
                for f in nums:
                    target_box.addItem(f.label, f.name)
                    left_box.addItem(f.label, f.name)
                    right_edit.addItem(f.name)

            part_box.currentIndexChanged.connect(load_cols)
            load_cols()
            form.addRow("Таблица", part_box)
            form.addRow("Целевая колонка", target_box)
            form.addRow("Левый операнд", left_box)
            form.addRow("Операция", op_box)
            form.addRow("Правый операнд / число", right_edit)
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            form.addRow(btns)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            if dlg.exec() and target_box.currentData() and left_box.currentData():
                p_name = part_box.currentData()
                self._calculations.append(StudioCalculation(
                    target=f"{p_name}.{target_box.currentData()}",
                    kind="row_formula",
                    source_part=p_name,
                    left=left_box.currentData(),
                    operator=op_box.currentText(),
                    right=right_edit.currentText().strip(),
                ))
        elif kind == "table_total":
            if not self._entity.table_parts:
                QMessageBox.information(self, "Вычисления", "Сначала добавьте табличную часть.")
                return
            nums_hdr = [f for f in self._entity.fields if f.kind in ("Integer", "Decimal")]
            if not nums_hdr:
                QMessageBox.information(self, "Вычисления", "Добавьте числовое поле в шапку объекта (например, total).")
                return
            target_box, part_box, op_box, col_box = QComboBox(), QComboBox(), QComboBox(), QComboBox()
            for f in nums_hdr:
                target_box.addItem(f"{f.label} ({f.name})", f.name)
            for p in self._entity.table_parts:
                part_box.addItem(p.label, p.name)
            op_box.addItems(["sum", "avg", "count", "min", "max"])

            def load_part_cols() -> None:
                part = next(p for p in self._entity.table_parts if p.name == part_box.currentData())
                col_box.clear()
                for f in part.fields:
                    if f.kind in ("Integer", "Decimal"):
                        col_box.addItem(f"{f.label} ({f.name})", f.name)

            part_box.currentIndexChanged.connect(load_part_cols)
            load_part_cols()
            form.addRow("Поле итога в шапке", target_box)
            form.addRow("Таблица", part_box)
            form.addRow("Функция", op_box)
            form.addRow("Колонка таблицы", col_box)
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            form.addRow(btns)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            if dlg.exec() and target_box.currentData():
                self._calculations.append(StudioCalculation(
                    target=target_box.currentData(),
                    kind="table_total",
                    source_part=part_box.currentData(),
                    left=col_box.currentData() or "",
                    operator=op_box.currentText(),
                ))
        elif kind == "header_formula":
            nums_hdr = [f for f in self._entity.fields if f.kind in ("Integer", "Decimal")]
            if not nums_hdr:
                QMessageBox.information(self, "Вычисления", "Добавьте числовые поля в шапку объекта.")
                return
            target_box, left_box, op_box, right_edit = QComboBox(), QComboBox(), QComboBox(), QComboBox()
            right_edit.setEditable(True)
            op_box.addItems(["*", "+", "-", "/"])
            for f in nums_hdr:
                target_box.addItem(f.label, f.name)
                left_box.addItem(f.label, f.name)
                right_edit.addItem(f.name)
            form.addRow("Целевое поле", target_box)
            form.addRow("Левый операнд", left_box)
            form.addRow("Операция", op_box)
            form.addRow("Правый операнд / число", right_edit)
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            form.addRow(btns)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            if dlg.exec() and target_box.currentData():
                self._calculations.append(StudioCalculation(
                    target=target_box.currentData(),
                    kind="header_formula",
                    left=left_box.currentData(),
                    operator=op_box.currentText(),
                    right=right_edit.currentText().strip(),
                ))
        elif kind == "lookup":
            info_regs = [r for r in self._project.registers if r.kind == "Сведения"]
            if not info_regs:
                QMessageBox.information(self, "Подстановка из регистра", "Сначала создайте регистр сведений.")
                return
            reg_box, res_box, part_box, target_box, date_box = QComboBox(), QComboBox(), QComboBox(), QComboBox(), QComboBox()
            only_empty = QCheckBox("Заполнять только если поле пустое / 0")
            only_empty.setChecked(True)
            for r in info_regs:
                reg_box.addItem(r.name, r.name)
            part_box.addItem("Шапка объекта", "")
            for p in self._entity.table_parts:
                part_box.addItem(f"Таблица · {p.label}", p.name)
            date_box.addItem("Без даты (самая последняя)", "")
            if self._entity.kind == "Документ":
                date_box.addItem("Дата документа (date)", "date")
            for f in self._entity.fields:
                if f.kind in ("Date", "DateTime"):
                    date_box.addItem(f.label, f.name)

            dim_table = QTableWidget(0, 2)
            dim_table.setHorizontalHeaderLabels(["Измерение регистра", "Поле источника"])
            dim_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            dim_table.verticalHeader().setVisible(False)
            dim_combos: dict[str, QComboBox] = {}

            def refresh_lookup() -> None:
                reg = next(r for r in info_regs if r.name == reg_box.currentData())
                res_box.clear()
                for m in reg.members:
                    if m.kind == "Ресурс":
                        res_box.addItem(f"{m.label} ({m.name})", m.name)
                p_name = part_box.currentData() or ""
                target_box.clear()
                if p_name:
                    part = next(p for p in self._entity.table_parts if p.name == p_name)
                    for f in part.fields:
                        target_box.addItem(f.label, f"{p_name}.{f.name}")
                else:
                    for f in self._entity.fields:
                        target_box.addItem(f.label, f.name)
                avail = movement_fields(self._entity, p_name)
                dims = [m for m in reg.members if m.kind == "Измерение"]
                dim_table.setRowCount(len(dims))
                dim_combos.clear()
                for idx, m in enumerate(dims):
                    dim_table.setItem(idx, 0, QTableWidgetItem(m.label))
                    box = QComboBox()
                    box.addItem("Не задавать", "")
                    for fk, fdef in avail.items():
                        if movement_compatible(m, fdef):
                            origin = "Строка" if fk.startswith("row:") else "Шапка"
                            box.addItem(f"{origin} · {fdef.label}", fk)
                    dim_table.setCellWidget(idx, 1, box)
                    dim_combos[m.name] = box

            reg_box.currentIndexChanged.connect(refresh_lookup)
            part_box.currentIndexChanged.connect(refresh_lookup)
            refresh_lookup()
            form.addRow("Регистр сведений", reg_box)
            form.addRow("Ресурс (значение)", res_box)
            form.addRow("Куда подставлять", part_box)
            form.addRow("Целевое поле", target_box)
            form.addRow("На дату", date_box)
            form.addRow(only_empty)
            form.addRow(dim_table)
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            form.addRow(btns)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            if dlg.exec() and target_box.currentData() and res_box.currentData():
                self._calculations.append(StudioCalculation(
                    target=target_box.currentData(),
                    kind="lookup",
                    source_part=part_box.currentData() or "",
                    register=reg_box.currentData(),
                    resource=res_box.currentData(),
                    date_field=date_box.currentData() or "",
                    dimension_map={k: b.currentData() for k, b in dim_combos.items() if b.currentData()},
                    only_if_empty=only_empty.isChecked(),
                ))
        dlg.deleteLater()
        self._refresh_lists()

    def _add_val(self, kind: str) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Новая проверка")
        dlg.resize(460, 260)
        form = QFormLayout(dlg)
        msg_edit = QLineEdit()
        msg_edit.setPlaceholderText("Текст сообщения при нарушении проверки")
        if kind in ("require_rows", "unique_rows"):
            if not self._entity.table_parts:
                QMessageBox.information(self, "Проверки", "Сначала добавьте табличную часть.")
                return
            part_box = QComboBox()
            for p in self._entity.table_parts:
                part_box.addItem(p.label, p.name)
            form.addRow("Таблица", part_box)
            col_box = QComboBox()
            if kind == "unique_rows":
                def load_cols() -> None:
                    part = next(p for p in self._entity.table_parts if p.name == part_box.currentData())
                    col_box.clear()
                    for f in part.fields:
                        col_box.addItem(f.label, f.name)

                part_box.currentIndexChanged.connect(load_cols)
                load_cols()
                form.addRow("Колонка без повторов", col_box)
            form.addRow("Сообщение об ошибке", msg_edit)
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            form.addRow(btns)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            if dlg.exec():
                self._validations.append(StudioValidation(
                    kind=kind,
                    target=part_box.currentData(),
                    column=col_box.currentData() if kind == "unique_rows" else "",
                    message=msg_edit.text().strip(),
                ))
        else:
            field_box = QComboBox()
            if self._entity.kind == "Документ":
                field_box.addItem("Дата документа (date)", "date")
            for f in self._entity.fields:
                field_box.addItem(f"{f.label} ({f.name})", f.name)
            op_box = QComboBox()
            op_box.addItems([">=", "<=", ">", "<", "==", "!="])
            other_box = QComboBox()
            other_box.setEditable(True)
            for f in self._entity.fields:
                other_box.addItem(f.name)
                if f.kind == "Ref":
                    tgt = next((e for e in self._project.entities if e.name == f.target), None)
                    if tgt is not None:
                        for rf in tgt.fields:
                            other_box.addItem(f"{f.name}.{rf.name}")
            form.addRow("Поле", field_box)
            form.addRow("Условие", op_box)
            form.addRow("Другое поле / ref.поле / значение", other_box)
            form.addRow("Сообщение об ошибке", msg_edit)
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            form.addRow(btns)
            btns.accepted.connect(dlg.accept)
            btns.rejected.connect(dlg.reject)
            if dlg.exec() and field_box.currentData() and other_box.currentText().strip():
                self._validations.append(StudioValidation(
                    kind="compare",
                    target=field_box.currentData(),
                    operator=op_box.currentText(),
                    other=other_box.currentText().strip(),
                    message=msg_edit.text().strip(),
                ))
        dlg.deleteLater()
        self._refresh_lists()


class ReportPreviewDialog(QDialog):
    """Run a StudioReport inside an isolated subprocess and display table + BarChart preview."""

    def __init__(
        self,
        parent: QWidget | None,
        project: StudioProject,
        report_name: str,
        working_db: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Предпросмотр отчёта · {report_name}")
        self.resize(920, 620)
        self._project = project
        self._report_name = report_name
        self._working_db = working_db
        self._param_widgets: dict[str, tuple[str, QWidget]] = {}
        self._last_payload: dict[str, object] = {}

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Источник данных:"))
        self.source_mode = QComboBox()
        self.source_mode.addItem("Начальные данные проекта (в памяти)", "memory")
        if working_db is not None and working_db.is_file():
            self.source_mode.addItem(f"Рабочая база ({working_db.name})", "db")
            self.source_mode.setCurrentIndex(1)
        top.addWidget(self.source_mode, 1)
        self.run_btn = QPushButton("Сформировать")
        self.run_btn.setProperty("primary", True)
        self.run_btn.clicked.connect(self._run)
        top.addWidget(self.run_btn)
        layout.addLayout(top)

        self.params_form = QFormLayout()
        layout.addLayout(self.params_form)
        self.summary = QLabel("")
        layout.addWidget(self.summary)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)
        self.chart = BarChart()
        self.chart.setMinimumHeight(190)
        self.chart.setMaximumHeight(240)
        self.chart.hide()
        layout.addWidget(self.chart)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._run(initial=True)

    def _collect_params(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, (ptype, widget) in self._param_widgets.items():
            if isinstance(widget, QComboBox):
                result[name] = widget.currentData()
            elif isinstance(widget, QLineEdit):
                result[name] = widget.text() or None
            elif isinstance(widget, QDateEdit):
                qd = widget.date()
                result[name] = datetime.date(qd.year(), qd.month(), qd.day()).isoformat()
            elif isinstance(widget, QCheckBox):
                result[name] = widget.isChecked()
        return result

    def _run(self, *, initial: bool = False) -> None:
        try:
            with TemporaryDirectory(prefix="decui-rep-prev-") as tmp:
                tmp_dir = Path(tmp)
                self._project.export(tmp_dir)
                payload = {
                    "report": self._report_name,
                    "mode": self.source_mode.currentData(),
                    "db_path": str(self._working_db) if self._working_db else None,
                    "params": {} if initial else self._collect_params(),
                }
                repo_root = str(Path(__file__).resolve().parent.parent)
                res = subprocess.run(
                    [sys.executable, "-m", "decui.studio_runner", "report-preview"],
                    cwd=tmp_dir,
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env={**os.environ, "PYTHONPATH": repo_root, "QT_QPA_PLATFORM": "offscreen"},
                    check=False,
                )
                if res.returncode != 0:
                    raise RuntimeError(res.stderr.strip() or res.stdout.strip() or "Ошибка выполнения отчёта.")
                data = json.loads(res.stdout)
        except Exception as exc:
            self.summary.setText(f"Ошибка предпросмотра: {exc}")
            return

        if initial:
            self._build_param_editors(data.get("params_meta", []))
        self._render_results(data)

    def _build_param_editors(self, params_meta: list[dict]) -> None:
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        self._param_widgets.clear()
        for meta in params_meta:
            name = meta["name"]
            ptype = meta["type"]
            widget: QWidget
            if ptype in ("RefParam", "ChoiceParam"):
                box = QComboBox()
                box.addItem("Все", None)
                for val, lbl in meta.get("choices", []):
                    box.addItem(str(lbl), val)
                widget = box
            elif ptype == "DateParam":
                de = QDateEdit(QDate.currentDate())
                de.setCalendarPopup(True)
                de.setDisplayFormat("dd.MM.yyyy")
                widget = de
            elif ptype == "BooleanParam":
                widget = QCheckBox()
            else:
                widget = QLineEdit()
            self._param_widgets[name] = (ptype, widget)
            self.params_form.addRow(meta["label"] + ":", widget)

    def _render_results(self, data: dict) -> None:
        cols = [ColumnMeta(**c) for c in data.get("columns", [])]
        rows = data.get("rows", [])
        groups = tuple(data.get("group_by", []))
        grouped = group_report_rows(rows, cols, groups)
        headers = [c.title for c in cols]
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        extra_total = 1 if rows and any(c.aggregate for c in cols) else 0
        self.table.setRowCount(len(grouped) + extra_total)
        for r_idx, row in enumerate(grouped):
            for c_idx, c in enumerate(cols):
                if c.field == row.get("_group_field"):
                    text = str(row.get("_group_label", ""))
                else:
                    val = row.get(c.field)
                    text = "" if val is None else str(val)
                    if text and c.format:
                        num = _parse_numeric_str(val)
                        if num is not None:
                            try:
                                text = c.format.format(float(num))
                            except (ValueError, TypeError):
                                pass
                    if text:
                        text = f"{c.prefix}{text}{c.suffix}"
                item = QTableWidgetItem(text)
                if row.get("_style") == "group":
                    item.setBackground(QColor("#e3ecfa"))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                elif row.get("_style") == "completed":
                    font = item.font()
                    font.setStrikeOut(True)
                    item.setFont(font)
                    item.setForeground(QColor("#808080"))
                elif row.get("_bold"):
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if row.get("_color"):
                    item.setForeground(QColor(row["_color"]))
                self.table.setItem(r_idx, c_idx, item)
        summary_parts = [f"Строк: {len(rows)}"]
        if extra_total:
            total_row = len(grouped)
            for c_idx, c in enumerate(cols):
                val_text = ""
                if c.aggregate:
                    agg = calc_aggregate([r.get(c.field) for r in rows], c.aggregate)
                    if agg and c.format:
                        try:
                            agg = c.format.format(float(agg))
                        except (ValueError, TypeError):
                            pass
                    val_text = f"{c.prefix}{agg}{c.suffix}" if agg else ""
                    summary_parts.append(f"{c.title}: {val_text}")
                elif c_idx == 0:
                    val_text = "Итого"
                item = QTableWidgetItem(val_text)
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                self.table.setItem(total_row, c_idx, item)
        self.table.resizeColumnsToContents()
        self.summary.setText(" | ".join(summary_parts))

        chart_cfg = data.get("chart")
        if isinstance(chart_cfg, dict) and rows and chart_cfg.get("label") and chart_cfg.get("value"):
            l_col, v_col = chart_cfg["label"], chart_cfg["value"]
            buckets: dict[str, list] = {}
            for r in rows:
                lbl = str(r.get(l_col) or "—")
                buckets.setdefault(lbl, []).append(r.get(v_col))
            labels = list(buckets)
            values = [float(calc_aggregate(vals, "sum") or 0) for vals in buckets.values()]
            self.chart._title = chart_cfg.get("title", "")
            if chart_cfg.get("color"):
                self.chart._bar_color = QColor(chart_cfg["color"])
            self.chart.set_data(labels, values)
            self.chart.show()
        else:
            self.chart.hide()


class ProjectOverviewWidget(QWidget):
    """Project summary, navigation structure, ER diagram, and clickable diagnostics."""

    def __init__(self, on_navigate: Callable[[str, str], None]) -> None:
        super().__init__()
        self._on_navigate = on_navigate
        self._diagnostics: list[StudioDiagnostic] = []

        layout = QVBoxLayout(self)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        self.stats.setStyleSheet("font-weight: 600; padding: 6px 2px;")
        layout.addWidget(self.stats)

        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        struct_page = QWidget()
        struct_layout = QVBoxLayout(struct_page)
        struct_layout.setContentsMargins(0, 4, 0, 0)
        splitter = QSplitter()
        struct_layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_label = QLabel("Структура и связи проекта")
        left_label.setWordWrap(True)
        left_layout.addWidget(left_label)
        self.structure = QTreeWidget()
        self.structure.setHeaderHidden(True)
        left_layout.addWidget(self.structure, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_label = QLabel("Диагностика проекта (двойной щелчок для перехода)")
        right_label.setWordWrap(True)
        right_layout.addWidget(right_label)
        self.diag_list = QListWidget()
        self.diag_list.itemDoubleClicked.connect(self._jump)
        right_layout.addWidget(self.diag_list, 1)
        splitter.addWidget(right)
        splitter.setSizes([450, 380])
        tabs.addTab(struct_page, "Структура и диагностика")

        diagram_page = QWidget()
        diag_layout = QVBoxLayout(diagram_page)
        diag_bar = QHBoxLayout()
        diag_hint = QLabel("Нажмите на карточку, чтобы перейти к объекту. Стрелки: Ref, Движения, Отчёты.")
        diag_hint.setWordWrap(True)
        diag_bar.addWidget(diag_hint, 1)
        save_png = QPushButton("Сохранить схему PNG…")
        save_png.clicked.connect(self._save_diagram_png)
        diag_bar.addWidget(save_png)
        diag_layout.addLayout(diag_bar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.diagram = ProjectDiagramWidget(on_navigate)
        scroll.setWidget(self.diagram)
        diag_layout.addWidget(scroll, 1)
        tabs.addTab(diagram_page, "Схема связей")

    def _save_diagram_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить схему проекта", "schema.png", "PNG (*.png)")
        if path:
            target = path if path.lower().endswith(".png") else path + ".png"
            self.diagram.grab().save(target, "PNG")

    def refresh(self, project: StudioProject) -> None:
        self.diagram.refresh(project)
        field_count = sum(len(e.fields) + sum(len(p.fields) for p in e.table_parts) for e in project.entities)
        table_count = sum(len(e.table_parts) for e in project.entities)
        catalog_count = sum(1 for e in project.entities if e.kind == "Справочник")
        doc_count = sum(1 for e in project.entities if e.kind == "Документ")
        self.stats.setText(
            f"Справочников: {catalog_count} · Документов: {doc_count} · Полей: {field_count} · "
            f"Табличных частей: {table_count} · Регистров: {len(project.registers)} · "
            f"Отчётов: {len(project.reports)} · Начальных записей: {len(project.initial_data)} · "
            f"Ролей: {len(project.all_roles())}"
        )

        self.structure.clear()
        nav_root = QTreeWidgetItem(self.structure, ["Навигация приложения"])
        sections: dict[str, QTreeWidgetItem] = {}

        def section_node(name: str) -> QTreeWidgetItem:
            key = name.strip() or "(без раздела)"
            if key not in sections:
                sections[key] = QTreeWidgetItem(nav_root, [key])
            return sections[key]

        for entity in project.entities:
            parent = section_node(entity.section)
            node = QTreeWidgetItem(parent, [f"[{entity.kind}] {entity.label} ({entity.name})"])
            for f in entity.fields:
                if f.kind == "Ref":
                    extras = []
                    if f.choice_filter:
                        extras.append("choice_filter")
                    if f.auto_fill:
                        extras.append(f"auto_fill={f.auto_fill}")
                    if f.create_from:
                        extras.append(f"на основании: {f.create_from}")
                    if f.related_list:
                        extras.append(f"связанный список: {f.related_list}")
                    suffix = f" [{', '.join(extras)}]" if extras else ""
                    QTreeWidgetItem(node, [f"→ {f.target} ({f.label}){suffix}"])
            for mv in entity.movements:
                direction = "Приход" if mv.income else "Расход"
                QTreeWidgetItem(node, [f"⇒ Регистр {mv.register} ({direction})"])
            for calc in entity.calculations:
                QTreeWidgetItem(node, [f"ƒ {calc.target} ({calc.kind})"])
        for rep in project.reports:
            parent = section_node(rep.section or "Отчёты")
            from_rec = " [из карточки]" if rep.ref_filter_from_record else ""
            QTreeWidgetItem(parent, [f"[Отчёт] {rep.title} ← {rep.entity}{from_rec}"])
        self.structure.expandAll()

        self.diag_list.clear()
        self._diagnostics = project.diagnostics()
        if not self._diagnostics:
            self.diag_list.addItem("✓ Проблем и предупреждений не обнаружено.")
            return
        for item in self._diagnostics:
            prefix = "✕ Ошибка:" if item.severity == "error" else "⚠ Совет:"
            self.diag_list.addItem(f"{prefix} {item.message}")

    def _jump(self, _item: QListWidgetItem) -> None:
        row = self.diag_list.currentRow()
        if 0 <= row < len(self._diagnostics):
            diag = self._diagnostics[row]
            self._on_navigate(diag.entity_name, diag.tab)
