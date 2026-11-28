"""Visual Form Designer and live preview widget for DECUI Studio."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QTreeWidget,
    QTreeWidgetItem,
)

from decui import Boolean, Choice, Date, DateTime, Decimal as DecimalField, Integer, Ref, String, Time
from decui.fields import Field
from decui.studio_project import (
    FORM_BUILTIN_BUTTONS,
    FORM_LABEL_STYLES,
    StudioEntity,
    StudioField,
    StudioFormConfig,
    StudioFormNode,
    StudioProject,
    default_form_for_entity,
)
from decui.ui.editors import create_editor, get_editor_value
from decui.ui.form import FieldLayout
from decui.ui.python_editor import PythonEditor

FORM_SNIPPETS: tuple[tuple[str, str], ...] = (
    (
        "on_open — действия при открытии карточки",
        "def on_open(self) -> None:\n"
        "    if self.is_new:\n"
        "        pass",
    ),
    (
        "on_field_changed — реакция на изменение поля",
        "def on_field_changed(self, field_name: str, value: object) -> None:\n"
        "    # if field_name == 'need_delivery':\n"
        "    #     self.set_visible('address', bool(value))\n"
        "    pass",
    ),
    (
        "on_table_changed — пересчёт при правке строк",
        "def on_table_changed(self, part_name: str) -> None:\n"
        "    table = self.get_table(part_name)\n"
        "    if table is not None:\n"
        "        rows = table.get_rows()\n"
        "        # self.set_value('total', sum(r.get('amount') or 0 for r in rows))\n"
        "        pass",
    ),
    (
        "on_before_save — проверка или подготовка на форме",
        "def on_before_save(self) -> bool | None:\n"
        "    return True",
    ),
    (
        "build_extra — свой виджет в нижней части формы",
        "def build_extra(self, layout) -> None:\n"
        "    from PySide6.QtWidgets import QLabel\n"
        "    note = QLabel('Подсказка по заполнению карточки')\n"
        "    note.setStyleSheet('color: #566577; font-size: 12px;')\n"
        "    layout.addWidget(note)",
    ),
)

_CONTAINER_KINDS = {"row", "column", "group", "tabs", "tab"}


def _make_preview_field(item: StudioField) -> Field:
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

    if item.kind == "Ref":
        return Ref(item.target, item.label, read_only=item.read_only)
    if item.kind == "Choice":
        pairs: list[tuple[str, str]] = []
        for raw_opt in item.choices.split(","):
            entry = raw_opt.strip()
            if not entry:
                continue
            if ": " in entry:
                c, _, lbl = entry.partition(": ")
                pairs.append((c.strip(), lbl.strip()))
            else:
                pairs.append((entry, entry))
        return Choice(item.label, choices=pairs, multi=item.multi, read_only=item.read_only)
    if item.kind == "String":
        return String(
            item.label, multiline=item.multiline, max_length=item.max_length,
            placeholder=item.placeholder, read_only=item.read_only,
        )
    if item.kind == "Integer":
        return Integer(
            item.label, min_value=safe_int(item.min_value), max_value=safe_int(item.max_value),
            read_only=item.read_only,
        )
    if item.kind == "Decimal":
        return DecimalField(
            item.label, precision=max(1, item.precision),
            scale=max(0, min(item.scale, item.precision)),
            min_value=safe_dec(item.min_value), max_value=safe_dec(item.max_value),
            currency=item.currency.strip() or None, read_only=item.read_only,
        )
    constructors = {"Boolean": Boolean, "Date": Date, "DateTime": DateTime, "Time": Time}
    return constructors[item.kind](item.label, read_only=item.read_only)


def _populate_seed_combos(panel: QWidget, fields_in_order: list[StudioField], project: StudioProject | None) -> None:
    if project is None or not project.initial_data:
        return
    combos = panel.findChildren(QComboBox)
    seeds_by_entity: dict[str, list[tuple[str, str]]] = {}
    for seed in project.initial_data:
        ent = next((e for e in project.entities if e.name == seed.entity), None)
        rep_name = "name" if ent and any(f.name == "name" for f in ent.fields) else ""
        display = seed.values.get(rep_name) or next(iter(seed.values.values()), seed.key)
        seeds_by_entity.setdefault(seed.entity, []).append((seed.key, f"{display} ({seed.key})"))
    ref_fields = [f for f in fields_in_order if f.kind == "Ref"]
    ref_combos = [c for c in combos if c.count() <= 1 and c.itemData(0) is None]
    for field_def, combo in zip(ref_fields, ref_combos):
        for key, label in seeds_by_entity.get(field_def.target, []):
            combo.addItem(label, key)


def render_studio_form_preview(entity: StudioEntity, project: StudioProject | None = None) -> QWidget:
    """Build a live preview QWidget for an entity (using custom Form layout or default FieldLayout)."""
    body = QWidget()
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(16, 14, 16, 14)
    body_layout.setSpacing(12)

    all_fields: list[StudioField] = [f for f in entity.fields if f.visible]
    if entity.kind == "Документ":
        all_fields = [
            StudioField("number", "Номер", "String", read_only=bool(entity.numbering_prefix)),
            StudioField("date", "Дата", "DateTime", required=True),
            StudioField("posted", "Проведён", "Boolean", read_only=True),
            *all_fields,
        ]
    fields_map = {f.name: f for f in all_fields}
    parts_map = {p.name: p for p in entity.table_parts}

    cfg = entity.form
    title_text = (cfg.title.strip() if cfg and cfg.enabled and cfg.title.strip() else "") or f"{entity.label} ({entity.kind})"
    heading = QLabel(title_text)
    heading.setProperty("heading", True)
    body_layout.addWidget(heading)

    rendered_fields: dict[str, tuple[StudioField, Field, QWidget, QWidget, QFormLayout | None]] = {}
    placed_ref_order: list[StudioField] = []

    def make_table_box(part_name: str, title_ovr: str = "", min_h: int = 0) -> QGroupBox:
        part = parts_map[part_name]
        group = QGroupBox(title_ovr or f"Таблица: {part.label}")
        part_layout = QVBoxLayout(group)
        part_cols = [item for item in part.fields if item.visible]
        table = QTableWidget(1, max(1, len(part_cols)))
        table.setHorizontalHeaderLabels([item.label or item.name for item in part_cols] or ["Нет колонок"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setMaximumHeight(max(110, min_h) if min_h else 110)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        part_layout.addWidget(table)
        return group

    if cfg is not None and cfg.enabled:
        def create_preview_field(node: StudioFormNode) -> tuple[QLabel, QWidget, Field, StudioField] | None:
            f_def = fields_map.get(node.target)
            if f_def is None:
                return None
            preview_def = deepcopy(f_def)
            if node.placeholder.strip():
                preview_def.placeholder = node.placeholder.strip()
            if node.read_only is not None:
                preview_def.read_only = node.read_only
            field_desc = _make_preview_field(preview_def)
            lbl_txt = node.label.strip() or preview_def.label or preview_def.name
            lbl = QLabel(lbl_txt + (" *:" if preview_def.required or preview_def.required_when_visible else ":"))
            lbl.setTextFormat(Qt.TextFormat.PlainText)
            lbl.setWordWrap(True)
            control = create_editor(field_desc)
            if node.min_width > 0:
                control.setMinimumWidth(node.min_width)
            lbl.setBuddy(control)
            placed_ref_order.append(preview_def)
            return lbl, control, field_desc, preview_def

        def render_vertical(nodes: list[StudioFormNode], v_layout: QVBoxLayout) -> None:
            pending_form: QFormLayout | None = None
            for node in nodes:
                if node.kind == "field":
                    created = create_preview_field(node)
                    if created is None:
                        continue
                    lbl, control, field_desc, preview_def = created
                    if pending_form is None:
                        pending_form = FieldLayout._new_field_layout()
                        v_layout.addLayout(pending_form)
                    pending_form.addRow(lbl, control)
                    rendered_fields[node.target] = (preview_def, field_desc, control, control, pending_form)
                    continue
                pending_form = None
                render_block(node, v_layout)

        def render_block(node: StudioFormNode, parent_layout: QVBoxLayout | QHBoxLayout) -> None:
            if node.kind == "row":
                row_w = QWidget()
                hl = QHBoxLayout(row_w)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.setSpacing(node.spacing)
                for ch in node.children:
                    if ch.kind == "field":
                        created = create_preview_field(ch)
                        if created is None:
                            continue
                        lbl, control, field_desc, preview_def = created
                        cell = QWidget()
                        cl = QVBoxLayout(cell)
                        cl.setContentsMargins(0, 0, 0, 0)
                        cl.setSpacing(4)
                        cl.addWidget(lbl)
                        cl.addWidget(control)
                        hl.addWidget(cell, stretch=max(0, ch.stretch))
                        rendered_fields[ch.target] = (preview_def, field_desc, control, cell, None)
                    else:
                        render_block(ch, hl)
                parent_layout.addWidget(row_w)
            elif node.kind == "column":
                col_w = QWidget()
                vl = QVBoxLayout(col_w)
                vl.setContentsMargins(0, 0, 0, 0)
                vl.setSpacing(node.spacing)
                render_vertical(node.children, vl)
                vl.addStretch()
                if isinstance(parent_layout, QHBoxLayout):
                    parent_layout.addWidget(col_w, stretch=max(0, node.stretch))
                else:
                    parent_layout.addWidget(col_w)
            elif node.kind == "group":
                box = QGroupBox(node.title or "Группа")
                bl = QVBoxLayout(box)
                inner = QWidget()
                il = QVBoxLayout(inner)
                il.setContentsMargins(0, 0, 0, 0)
                render_vertical(node.children, il)
                bl.addWidget(inner)
                if node.collapsible:
                    box.setCheckable(True)
                    box.setChecked(not node.collapsed)
                    inner.setVisible(not node.collapsed)
                    box.toggled.connect(inner.setVisible)
                parent_layout.addWidget(box)
            elif node.kind == "tabs":
                tabs = QTabWidget()
                for tab in node.children:
                    if tab.kind != "tab":
                        continue
                    page = QWidget()
                    pl = QVBoxLayout(page)
                    pl.setAlignment(Qt.AlignmentFlag.AlignTop)
                    render_vertical(tab.children, pl)
                    tabs.addTab(page, tab.title or "Вкладка")
                parent_layout.addWidget(tabs)
            elif node.kind == "table":
                if node.target in parts_map:
                    parent_layout.addWidget(make_table_box(node.target, node.title, node.min_height))
            elif node.kind == "button":
                lbl = node.label.strip() or entity.commands.get(node.target) or FORM_BUILTIN_BUTTONS.get(node.target, node.target)
                btn = QPushButton(lbl)
                if node.primary:
                    btn.setProperty("primary", True)
                parent_layout.addWidget(btn)
            elif node.kind == "label":
                lbl = QLabel(node.title or node.label)
                lbl.setWordWrap(True)
                if node.style == "heading":
                    lbl.setProperty("heading", True)
                elif node.style == "muted":
                    lbl.setStyleSheet("color: #566577; font-size: 12px;")
                elif node.style == "info":
                    lbl.setStyleSheet("background: #eaf2fd; color: #163761; border: 1px solid #cbd6e6; border-radius: 6px; padding: 8px;")
                elif node.style == "warning":
                    lbl.setObjectName("reportNotice")
                parent_layout.addWidget(lbl)
            elif node.kind == "divider":
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                line.setFrameShadow(QFrame.Shadow.Sunken)
                parent_layout.addWidget(line)

        render_vertical(cfg.items, body_layout)
        _populate_seed_combos(body, placed_ref_order, project)

        node_vis = {
            n.target: (n.visibility_field, n.visibility_value)
            for n in cfg.items
            if n.kind == "field" and n.visibility_field
        }

        def walk_vis(items: list[StudioFormNode]) -> None:
            for n in items:
                if n.kind == "field" and n.visibility_field:
                    node_vis[n.target] = (n.visibility_field, n.visibility_value)
                walk_vis(n.children)

        walk_vis(cfg.items)

        def update_vis() -> None:
            for fname, (fdef, _desc, _ctrl, row_container, form_lay) in rendered_fields.items():
                cond = node_vis.get(fname) or ((fdef.visibility_field, fdef.visibility_value) if fdef.visibility_field else None)
                vis = True
                if cond is not None:
                    src = rendered_fields.get(cond[0])
                    vis = src is not None and get_editor_value(src[2], src[1]) == cond[1]
                if form_lay is not None:
                    form_lay.setRowVisible(row_container, vis)
                else:
                    row_container.setVisible(vis)

        for src_name in {c[0] for c in node_vis.values()} | {f.visibility_field for f in all_fields if f.visibility_field}:
            src = rendered_fields.get(src_name)
            if src is None:
                continue
            ctrl = src[2]
            if isinstance(ctrl, QCheckBox):
                ctrl.toggled.connect(update_vis)
            elif isinstance(ctrl, QComboBox):
                ctrl.currentIndexChanged.connect(update_vis)
        update_vis()
    else:
        panel = QWidget()
        fl = FieldLayout(panel)
        rows: dict[str, tuple[StudioField, Field, QWidget, QFormLayout]] = {}
        for item in all_fields:
            label = QLabel((item.label or item.name) + (" *:" if item.required or item.required_when_visible else ":"))
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            desc = _make_preview_field(item)
            control = create_editor(desc)
            label.setBuddy(control)
            sec = fl.section(item.form_tab.strip(), item.form_group.strip())
            sec.addRow(label, control)
            rows[item.name] = (item, desc, control, sec)
        fl.finish()
        body_layout.addWidget(panel)
        _populate_seed_combos(panel, all_fields, project)
        for part in entity.table_parts:
            tb = make_table_box(part.name)
            if part.form_tab.strip():
                fl.add_to_tab(part.form_tab.strip(), tb)
                fl.tabs.setVisible(True)
            else:
                body_layout.addWidget(tb)

    body_layout.addStretch()
    return body


class FormDesignerWidget(QWidget):
    """Interactive visual Form Designer + EntityForm Python hook editor for Studio."""

    changed = Signal()
    undo_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entity: StudioEntity | None = None
        self._project: StudioProject | None = None
        self._loading = False
        self._node_items: dict[int, StudioFormNode] = {}
        self._parent_map: dict[int, list[StudioFormNode]] = {}
        self._selected_node: StudioFormNode | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        container = QWidget()
        scroll.setWidget(container)
        root = QVBoxLayout(container)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Top toolbar
        top = QHBoxLayout()
        self.enabled_check = QCheckBox("Использовать настраиваемую форму (__form__)")
        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        top.addWidget(self.enabled_check)

        self.preset_btn = QPushButton("Заполнить по шаблону ▾")
        preset_menu = QMenu(self.preset_btn)
        preset_menu.addAction("Стандартная форма (поля и вкладки)", lambda: self._apply_preset("standard"))
        preset_menu.addAction("Двухколоночная шапка + вкладки таблиц", lambda: self._apply_preset("two_column"))
        preset_menu.addAction("Компактный документ", lambda: self._apply_preset("compact"))
        self.preset_btn.setMenu(preset_menu)
        top.addWidget(self.preset_btn)

        self.sync_missing_btn = QPushButton("Добавить недостающие поля")
        self.sync_missing_btn.clicked.connect(self._add_missing_fields)
        top.addWidget(self.sync_missing_btn)

        top.addStretch()
        self.preview_btn = QPushButton("Предпросмотр в окне")
        self.preview_btn.clicked.connect(self._open_window_preview)
        top.addWidget(self.preview_btn)
        root.addLayout(top)

        self.empty_hint = QLabel("Выберите справочник или документ слева, чтобы настроить его форму.")
        self.empty_hint.setStyleSheet("color: #566577; padding: 16px;")
        root.addWidget(self.empty_hint)

        self.workspace = QSplitter()
        root.addWidget(self.workspace, 1)

        # Left pane: Palette
        palette_panel = QWidget()
        p_layout = QVBoxLayout(palette_panel)
        p_layout.setContentsMargins(0, 0, 0, 0)
        p_layout.addWidget(QLabel("<b>Элементы формы</b>"))
        containers_box = QGroupBox("Контейнеры и оформление")
        cb_layout = QVBoxLayout(containers_box)
        cb_layout.setSpacing(4)
        for text, factory in (
            ("+ Строка (горизонтально)", lambda: StudioFormNode("row")),
            ("+ Колонка (внутри строки)", lambda: StudioFormNode("column")),
            ("+ Группа (рамка)", lambda: StudioFormNode("group", title="Группа")),
            ("+ Вкладки (Tabs)", lambda: StudioFormNode("tabs", children=[StudioFormNode("tab", title="Основное")])),
            ("+ Вкладка (внутри Tabs)", lambda: StudioFormNode("tab", title="Вкладка")),
            ("+ Текст / Заголовок", lambda: StudioFormNode("label", title="Пояснение", style="muted")),
            ("+ Разделитель", lambda: StudioFormNode("divider")),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(lambda _c=False, mk=factory: self._insert_node(mk()))
            cb_layout.addWidget(btn)
        p_layout.addWidget(containers_box)

        p_layout.addWidget(QLabel("Поля и таблицы (двойной щелчок)"))
        self.palette_list = QListWidget()
        self.palette_list.itemDoubleClicked.connect(self._insert_from_palette)
        p_layout.addWidget(self.palette_list, 1)
        add_pal = QPushButton("Добавить выбранное на форму")
        add_pal.clicked.connect(self._insert_from_palette)
        p_layout.addWidget(add_pal)
        self.workspace.addWidget(palette_panel)

        # Center pane: Form Tree + Properties
        center_split = QSplitter(Qt.Orientation.Vertical)
        tree_box = QWidget()
        t_layout = QVBoxLayout(tree_box)
        t_layout.setContentsMargins(0, 0, 0, 0)
        t_layout.addWidget(QLabel("<b>Структура формы</b>"))
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._on_tree_selected)
        t_layout.addWidget(self.tree, 1)

        tree_btns = QHBoxLayout()
        for lbl, slot in (
            ("↑", lambda: self._move_selected(-1)),
            ("↓", lambda: self._move_selected(1)),
            ("Внутрь →", self._nest_into_previous),
            ("← Вынести", self._unnest_selected),
            ("Копия", self._duplicate_selected),
            ("Удалить", self._delete_selected),
        ):
            b = QPushButton(lbl)
            b.clicked.connect(slot)
            tree_btns.addWidget(b)
        t_layout.addLayout(tree_btns)
        center_split.addWidget(tree_box)

        props_scroll = QScrollArea()
        props_scroll.setWidgetResizable(True)
        self.props_panel = QWidget()
        self.props_form = QFormLayout(self.props_panel)
        self.props_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        props_scroll.setWidget(self.props_panel)
        center_split.addWidget(props_scroll)
        center_split.setSizes([280, 220])
        self.workspace.addWidget(center_split)

        # Right pane: Preview + Form Code Editor
        self.right_tabs = QTabWidget()
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(True)
        self.right_tabs.addTab(self.preview_scroll, "Живой предпросмотр")

        code_page = QWidget()
        c_layout = QVBoxLayout(code_page)
        c_layout.setContentsMargins(6, 6, 6, 6)
        c_hdr = QHBoxLayout()
        c_hdr.addWidget(QLabel("Обработчики EntityForm (on_open, on_field_changed…)"))
        c_hdr.addStretch()
        snippet_btn = QPushButton("Шаблон обработчика")
        snippet_menu = QMenu(snippet_btn)
        for label, snippet in FORM_SNIPPETS:
            snippet_menu.addAction(label, lambda _c=False, body=snippet: self._insert_form_snippet(body))
        snippet_btn.setMenu(snippet_menu)
        c_hdr.addWidget(snippet_btn)
        c_layout.addLayout(c_hdr)
        self.form_code_editor = PythonEditor()
        self.form_code_editor.setPlaceholderText(
            "def on_field_changed(self, field_name: str, value: object) -> None:\n    pass"
        )
        self.form_code_editor.textChanged.connect(self._on_form_code_changed)
        c_layout.addWidget(self.form_code_editor, 1)
        self.right_tabs.addTab(code_page, "Код формы (EntityForm)")

        self.workspace.addWidget(self.right_tabs)
        self.workspace.setSizes([230, 340, 400])
        self.load_entity(None, None)

    def _insert_form_snippet(self, snippet: str) -> None:
        current = self.form_code_editor.toPlainText().rstrip()
        prefix = "\n\n" if current else ""
        cursor = self.form_code_editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(prefix + snippet + "\n")
        self.form_code_editor.setTextCursor(cursor)
        self.form_code_editor.setFocus()

    def _on_form_code_changed(self) -> None:
        if self._loading or self._entity is None:
            return
        self._entity.form_code = self.form_code_editor.toPlainText()
        self.changed.emit()

    def load_entity(self, entity: StudioEntity | None, project: StudioProject | None) -> None:
        self._loading = True
        try:
            self._entity = entity
            self._project = project
            self._selected_node = None
            has_ent = entity is not None
            self.empty_hint.setVisible(not has_ent)
            self.workspace.setVisible(has_ent)
            self.enabled_check.setEnabled(has_ent)
            self.preset_btn.setEnabled(has_ent)
            self.sync_missing_btn.setEnabled(has_ent)
            self.preview_btn.setEnabled(has_ent)
            if entity is None:
                self.enabled_check.setChecked(False)
                self.form_code_editor.setPlainText("")
                self.tree.clear()
                self.palette_list.clear()
                return
            enabled = bool(entity.form and entity.form.enabled)
            with QSignalBlocker(self.enabled_check):
                self.enabled_check.setChecked(enabled)
            with QSignalBlocker(self.form_code_editor):
                self.form_code_editor.setPlainText(entity.form_code)
            self._refresh_palette()
            self._refresh_tree()
            self._refresh_preview()
            self._rebuild_props(None)
        finally:
            self._loading = False

    def _on_enabled_toggled(self, checked: bool) -> None:
        if self._loading or self._entity is None:
            return
        self.undo_requested.emit()
        if checked:
            if self._entity.form is None or not self._entity.form.items:
                self._entity.form = default_form_for_entity(self._entity, "standard")
            else:
                self._entity.form.enabled = True
        elif self._entity.form is not None:
            self._entity.form.enabled = False
        self._refresh_palette()
        self._refresh_tree()
        self._refresh_preview()
        self.changed.emit()

    def _apply_preset(self, preset: str) -> None:
        if self._entity is None:
            return
        self.undo_requested.emit()
        self._entity.form = default_form_for_entity(self._entity, preset)
        with QSignalBlocker(self.enabled_check):
            self.enabled_check.setChecked(True)
        self._refresh_palette()
        self._refresh_tree()
        self._refresh_preview()
        self.changed.emit()

    def _add_missing_fields(self) -> None:
        if self._entity is None:
            return
        self.undo_requested.emit()
        if self._entity.form is None:
            self._entity.form = default_form_for_entity(self._entity, "standard")
        cfg = self._entity.form
        cfg.enabled = True
        with QSignalBlocker(self.enabled_check):
            self.enabled_check.setChecked(True)
        placed_fields, placed_tables = self._placed_targets(cfg.items)
        for f in self._entity.fields:
            if f.visible and f.name not in placed_fields:
                cfg.items.append(StudioFormNode("field", target=f.name))
        for p in self._entity.table_parts:
            if p.name not in placed_tables:
                cfg.items.append(StudioFormNode("table", target=p.name))
        self._refresh_palette()
        self._refresh_tree()
        self._refresh_preview()
        self.changed.emit()

    @staticmethod
    def _placed_targets(nodes: list[StudioFormNode]) -> tuple[set[str], set[str]]:
        f_set: set[str] = set()
        t_set: set[str] = set()

        def walk(items: list[StudioFormNode]) -> None:
            for n in items:
                if n.kind == "field" and n.target:
                    f_set.add(n.target)
                elif n.kind == "table" and n.target:
                    t_set.add(n.target)
                walk(n.children)

        walk(nodes)
        return f_set, t_set

    def _refresh_palette(self) -> None:
        self.palette_list.clear()
        if self._entity is None:
            return
        cfg = self._entity.form
        placed_f, placed_t = self._placed_targets(cfg.items) if cfg and cfg.enabled else (set(), set())
        if self._entity.kind == "Документ":
            for name, lbl in (("number", "Номер"), ("date", "Дата"), ("posted", "Проведён")):
                mark = "✓ " if name in placed_f else ""
                item = QListWidgetItem(f"{mark}[Поле] {lbl} ({name})")
                item.setData(Qt.ItemDataRole.UserRole, ("field", name))
                self.palette_list.addItem(item)
        for f in self._entity.fields:
            mark = "✓ " if f.name in placed_f else ""
            item = QListWidgetItem(f"{mark}[Поле] {f.label} ({f.name})")
            item.setData(Qt.ItemDataRole.UserRole, ("field", f.name))
            self.palette_list.addItem(item)
        for p in self._entity.table_parts:
            mark = "✓ " if p.name in placed_t else ""
            item = QListWidgetItem(f"{mark}[Таблица] {p.label} ({p.name})")
            item.setData(Qt.ItemDataRole.UserRole, ("table", p.name))
            self.palette_list.addItem(item)
        for cmd_name, cmd_lbl in self._entity.commands.items():
            item = QListWidgetItem(f"[Кнопка] {cmd_lbl} ({cmd_name})")
            item.setData(Qt.ItemDataRole.UserRole, ("button", cmd_name))
            self.palette_list.addItem(item)
        for b_key, b_lbl in FORM_BUILTIN_BUTTONS.items():
            if b_key in ("post", "post_and_close", "unpost", "print", "movements") and self._entity.kind != "Документ":
                continue
            item = QListWidgetItem(f"[Кнопка] {b_lbl} ({b_key})")
            item.setData(Qt.ItemDataRole.UserRole, ("button", b_key))
            self.palette_list.addItem(item)

    def _node_label(self, node: StudioFormNode) -> str:
        assert self._entity is not None
        if node.kind == "row":
            return f"[Строка] ({len(node.children)})"
        if node.kind == "column":
            return f"[Колонка] stretch={node.stretch}"
        if node.kind == "group":
            return f"[Группа] {node.title or 'Группа'}"
        if node.kind == "tabs":
            return f"[Вкладки] ({len(node.children)})"
        if node.kind == "tab":
            return f"[Вкладка] {node.title or 'Вкладка'}"
        if node.kind == "field":
            lbl = node.label or next((f.label for f in self._entity.fields if f.name == node.target), node.target)
            return f"[Поле] {lbl} ({node.target})"
        if node.kind == "table":
            lbl = node.title or next((p.label for p in self._entity.table_parts if p.name == node.target), node.target)
            return f"[Таблица] {lbl} ({node.target})"
        if node.kind == "button":
            lbl = node.label or self._entity.commands.get(node.target) or FORM_BUILTIN_BUTTONS.get(node.target, node.target)
            return f"[Кнопка] {lbl}"
        if node.kind == "label":
            return f"[Текст] {(node.title or node.label)[:28]}"
        if node.kind == "divider":
            return "[Разделитель]"
        return node.kind

    def _refresh_tree(self) -> None:
        selected = self._selected_node
        self.tree.blockSignals(True)
        self.tree.clear()
        self._node_items.clear()
        self._parent_map.clear()
        if self._entity is None:
            self.tree.blockSignals(False)
            return
        cfg = self._entity.form
        root_title = f"Форма ({self._entity.label})" if cfg and cfg.enabled else f"Форма (авторежим · {self._entity.label})"
        root_item = QTreeWidgetItem(self.tree, [root_title])
        root_item.setData(0, Qt.ItemDataRole.UserRole, None)
        target_tree_item: QTreeWidgetItem | None = None

        def add_nodes(nodes: list[StudioFormNode], parent_tree: QTreeWidgetItem) -> None:
            nonlocal target_tree_item
            for node in nodes:
                self._node_items[id(node)] = node
                self._parent_map[id(node)] = nodes
                item = QTreeWidgetItem(parent_tree, [self._node_label(node)])
                item.setData(0, Qt.ItemDataRole.UserRole, id(node))
                if node is selected:
                    target_tree_item = item
                add_nodes(node.children, item)

        if cfg is not None and cfg.enabled:
            add_nodes(cfg.items, root_item)
        self.tree.expandAll()
        self.tree.setCurrentItem(target_tree_item or root_item)
        self.tree.blockSignals(False)

    def _ensure_enabled_config(self) -> StudioFormConfig | None:
        if self._entity is None:
            return None
        if self._entity.form is None:
            self._entity.form = StudioFormConfig(enabled=True, items=[])
        elif not self._entity.form.enabled:
            self._entity.form.enabled = True
        with QSignalBlocker(self.enabled_check):
            self.enabled_check.setChecked(True)
        return self._entity.form

    def _current_container_list(self) -> tuple[list[StudioFormNode], int] | None:
        cfg = self._ensure_enabled_config()
        if cfg is None:
            return None
        cur = self.tree.currentItem()
        node_id = cur.data(0, Qt.ItemDataRole.UserRole) if cur is not None else None
        node = self._node_items.get(node_id) if isinstance(node_id, int) else None
        if node is None:
            return cfg.items, len(cfg.items)
        if node.kind in _CONTAINER_KINDS:
            return node.children, len(node.children)
        parent_list = self._parent_map.get(id(node), cfg.items)
        idx = parent_list.index(node) + 1 if node in parent_list else len(parent_list)
        return parent_list, idx

    def _insert_node(self, node: StudioFormNode) -> None:
        if self._entity is None:
            return
        self.undo_requested.emit()
        target = self._current_container_list()
        if target is None:
            return
        container, idx = target
        cfg = self._entity.form
        assert cfg is not None
        if node.kind == "tab":
            tabs_node = next((n for n in cfg.items if n.kind == "tabs"), None)
            if self._selected_node is not None and self._selected_node.kind == "tabs":
                container, idx = self._selected_node.children, len(self._selected_node.children)
            elif self._selected_node is not None and self._selected_node.kind == "tab":
                parent_list = self._parent_map.get(id(self._selected_node), cfg.items)
                container, idx = parent_list, parent_list.index(self._selected_node) + 1
            elif tabs_node is not None:
                container, idx = tabs_node.children, len(tabs_node.children)
            else:
                wrapper = StudioFormNode("tabs", children=[node])
                cfg.items.append(wrapper)
                self._selected_node = node
                self._sync_after_structure_edit()
                return
        container.insert(idx, node)
        self._selected_node = node
        self._sync_after_structure_edit()

    def _insert_from_palette(self, _item: QListWidgetItem | None = None) -> None:
        cur = self.palette_list.currentItem()
        if cur is None:
            return
        kind, target = cur.data(Qt.ItemDataRole.UserRole)
        self._insert_node(StudioFormNode(kind, target=target))

    def _move_selected(self, offset: int) -> None:
        if self._selected_node is None:
            return
        parent_list = self._parent_map.get(id(self._selected_node))
        if parent_list is None or self._selected_node not in parent_list:
            return
        idx = parent_list.index(self._selected_node)
        new_idx = idx + offset
        if 0 <= new_idx < len(parent_list):
            self.undo_requested.emit()
            parent_list.insert(new_idx, parent_list.pop(idx))
            self._sync_after_structure_edit()

    def _nest_into_previous(self) -> None:
        if self._selected_node is None:
            return
        parent_list = self._parent_map.get(id(self._selected_node))
        if parent_list is None or self._selected_node not in parent_list:
            return
        idx = parent_list.index(self._selected_node)
        if idx <= 0:
            return
        prev = parent_list[idx - 1]
        if prev.kind not in _CONTAINER_KINDS:
            return
        if prev.kind == "tabs" and self._selected_node.kind != "tab":
            if not prev.children:
                prev.children.append(StudioFormNode("tab", title="Основное"))
            dest = prev.children[-1].children
        else:
            dest = prev.children
        self.undo_requested.emit()
        node = parent_list.pop(idx)
        dest.append(node)
        self._sync_after_structure_edit()

    def _unnest_selected(self) -> None:
        if self._selected_node is None or self._entity is None or self._entity.form is None:
            return
        parent_list = self._parent_map.get(id(self._selected_node))
        if parent_list is None or parent_list is self._entity.form.items:
            return
        grandparent = next(
            (n for n in self._node_items.values() if n.children is parent_list),
            None,
        )
        if grandparent is None:
            return
        outer_list = self._parent_map.get(id(grandparent), self._entity.form.items)
        self.undo_requested.emit()
        node = parent_list.pop(parent_list.index(self._selected_node))
        outer_idx = outer_list.index(grandparent) + 1 if grandparent in outer_list else len(outer_list)
        outer_list.insert(outer_idx, node)
        self._sync_after_structure_edit()

    def _duplicate_selected(self) -> None:
        if self._selected_node is None:
            return
        parent_list = self._parent_map.get(id(self._selected_node))
        if parent_list is None or self._selected_node not in parent_list:
            return
        self.undo_requested.emit()
        cloned = deepcopy(self._selected_node)
        idx = parent_list.index(self._selected_node)
        parent_list.insert(idx + 1, cloned)
        self._selected_node = cloned
        self._sync_after_structure_edit()

    def _delete_selected(self) -> None:
        if self._selected_node is None:
            return
        parent_list = self._parent_map.get(id(self._selected_node))
        if parent_list is None or self._selected_node not in parent_list:
            return
        self.undo_requested.emit()
        idx = parent_list.index(self._selected_node)
        parent_list.pop(idx)
        self._selected_node = parent_list[min(idx, len(parent_list) - 1)] if parent_list else None
        self._sync_after_structure_edit()

    def _sync_after_structure_edit(self) -> None:
        self._refresh_palette()
        self._refresh_tree()
        self._rebuild_props(self._selected_node)
        self._refresh_preview()
        self.changed.emit()

    def _on_tree_selected(self, current: QTreeWidgetItem | None, _prev: QTreeWidgetItem | None) -> None:
        node_id = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        self._selected_node = self._node_items.get(node_id) if isinstance(node_id, int) else None
        self._rebuild_props(self._selected_node)

    def _rebuild_props(self, node: StudioFormNode | None) -> None:
        while self.props_form.rowCount():
            self.props_form.removeRow(0)
        if self._entity is None:
            return

        def on_prop_change() -> None:
            cur = self.tree.currentItem()
            if cur is not None and node is not None:
                cur.setText(0, self._node_label(node))
            self._refresh_preview()
            self.changed.emit()

        if node is None:
            cfg = self._entity.form
            if cfg is None or not cfg.enabled:
                self.props_form.addRow(QLabel("Включите конструктор формы, чтобы настраивать расположение полей и вкладок."))
                return
            title_edit = QLineEdit(cfg.title)
            title_edit.setPlaceholderText("По умолчанию: название объекта")
            inc_chk = QCheckBox("Добавлять неразмещённые поля автоматически")
            inc_chk.setChecked(cfg.include_remaining)
            act_chk = QCheckBox("Показывать нижнюю панель стандартных кнопок")
            act_chk.setChecked(cfg.show_actions)
            title_edit.textChanged.connect(lambda t: (setattr(cfg, "title", t), on_prop_change()))
            inc_chk.toggled.connect(lambda v: (setattr(cfg, "include_remaining", v), on_prop_change()))
            act_chk.toggled.connect(lambda v: (setattr(cfg, "show_actions", v), on_prop_change()))
            self.props_form.addRow("Заголовок формы", title_edit)
            self.props_form.addRow("", inc_chk)
            self.props_form.addRow("", act_chk)
            return

        if node.kind == "field":
            lbl_e = QLineEdit(node.label)
            lbl_e.setPlaceholderText("Из поля объекта")
            ph_e = QLineEdit(node.placeholder)
            stretch_s = QSpinBox()
            stretch_s.setRange(0, 12)
            stretch_s.setValue(node.stretch)
            minw_s = QSpinBox()
            minw_s.setRange(0, 1200)
            minw_s.setValue(node.min_width)
            ro_box = QComboBox()
            ro_box.addItem("Как у поля", None)
            ro_box.addItem("Только чтение", True)
            ro_box.addItem("Редактируемое", False)
            ro_box.setCurrentIndex(max(0, ro_box.findData(node.read_only)))

            vis_field = QComboBox()
            vis_field.addItem("Всегда", "")
            controllers = {
                f.name: f for f in self._entity.fields
                if f.name != node.target and ((f.kind == "Boolean") or (f.kind == "Choice" and not f.multi))
            }
            for f in controllers.values():
                vis_field.addItem(f"{f.label} ({f.name})", f.name)
            vis_val = QComboBox()

            def sync_vis_vals() -> None:
                ctrl = controllers.get(vis_field.currentData())
                vis_val.clear()
                if ctrl is not None and ctrl.kind == "Boolean":
                    vis_val.addItem("Да", True)
                    vis_val.addItem("Нет", False)
                elif ctrl is not None:
                    for opt in ctrl.choices.split(","):
                        entry = opt.strip()
                        if entry:
                            code = entry.partition(": ")[0].strip() if ": " in entry else entry
                            vis_val.addItem(entry, code)
                vis_val.setEnabled(ctrl is not None)

            vis_field.setCurrentIndex(max(0, vis_field.findData(node.visibility_field)))
            sync_vis_vals()
            vis_val.setCurrentIndex(max(0, vis_val.findData(node.visibility_value)))

            lbl_e.textChanged.connect(lambda t: (setattr(node, "label", t), on_prop_change()))
            ph_e.textChanged.connect(lambda t: (setattr(node, "placeholder", t), on_prop_change()))
            stretch_s.valueChanged.connect(lambda v: (setattr(node, "stretch", v), on_prop_change()))
            minw_s.valueChanged.connect(lambda v: (setattr(node, "min_width", v), on_prop_change()))
            ro_box.currentIndexChanged.connect(lambda _i: (setattr(node, "read_only", ro_box.currentData()), on_prop_change()))
            vis_field.currentIndexChanged.connect(lambda _i: (
                setattr(node, "visibility_field", vis_field.currentData() or ""),
                sync_vis_vals(),
                setattr(node, "visibility_value", vis_val.currentData() if vis_val.count() else True),
                on_prop_change(),
            ))
            vis_val.currentIndexChanged.connect(lambda _i: (
                setattr(node, "visibility_value", vis_val.currentData() if vis_val.count() else True),
                on_prop_change(),
            ))
            self.props_form.addRow("Подпись на форме", lbl_e)
            self.props_form.addRow("Подсказка ввода", ph_e)
            self.props_form.addRow("Пропорция в строке (stretch)", stretch_s)
            self.props_form.addRow("Мин. ширина (px)", minw_s)
            self.props_form.addRow("Режим чтения", ro_box)
            self.props_form.addRow("Показывать если", vis_field)
            self.props_form.addRow("Равно значению", vis_val)

        elif node.kind == "table":
            t_e = QLineEdit(node.title)
            t_e.setPlaceholderText("Из табличной части")
            mh_s = QSpinBox()
            mh_s.setRange(0, 1000)
            mh_s.setValue(node.min_height)
            ro_c = QCheckBox("Только чтение")
            ro_c.setChecked(node.read_only)
            t_e.textChanged.connect(lambda t: (setattr(node, "title", t), on_prop_change()))
            mh_s.valueChanged.connect(lambda v: (setattr(node, "min_height", v), on_prop_change()))
            ro_c.toggled.connect(lambda v: (setattr(node, "read_only", v), on_prop_change()))
            self.props_form.addRow("Заголовок", t_e)
            self.props_form.addRow("Мин. высота (px)", mh_s)
            self.props_form.addRow("", ro_c)

        elif node.kind == "button":
            cmd_box = QComboBox()
            for k, v in self._entity.commands.items():
                cmd_box.addItem(f"Команда: {v} ({k})", k)
            for k, v in FORM_BUILTIN_BUTTONS.items():
                cmd_box.addItem(f"Действие: {v} ({k})", k)
            cmd_box.setCurrentIndex(max(0, cmd_box.findData(node.target)))
            lbl_e = QLineEdit(node.label)
            prim_c = QCheckBox("Акцентная кнопка (primary)")
            prim_c.setChecked(node.primary)
            cmd_box.currentIndexChanged.connect(lambda _i: (setattr(node, "target", cmd_box.currentData() or ""), on_prop_change()))
            lbl_e.textChanged.connect(lambda t: (setattr(node, "label", t), on_prop_change()))
            prim_c.toggled.connect(lambda v: (setattr(node, "primary", v), on_prop_change()))
            self.props_form.addRow("Команда / действие", cmd_box)
            self.props_form.addRow("Подпись кнопки", lbl_e)
            self.props_form.addRow("", prim_c)

        elif node.kind == "label":
            txt_e = QLineEdit(node.title or node.label)
            st_box = QComboBox()
            st_box.addItems(FORM_LABEL_STYLES)
            st_box.setCurrentText(node.style if node.style in FORM_LABEL_STYLES else "normal")
            txt_e.textChanged.connect(lambda t: (setattr(node, "title", t), on_prop_change()))
            st_box.currentTextChanged.connect(lambda t: (setattr(node, "style", t), on_prop_change()))
            self.props_form.addRow("Текст", txt_e)
            self.props_form.addRow("Стиль", st_box)

        elif node.kind == "group":
            t_e = QLineEdit(node.title)
            coll_c = QCheckBox("Сворачиваемая группа")
            coll_c.setChecked(node.collapsible)
            colp_c = QCheckBox("Свёрнута по умолчанию")
            colp_c.setChecked(node.collapsed)
            colp_c.setEnabled(node.collapsible)
            t_e.textChanged.connect(lambda t: (setattr(node, "title", t), on_prop_change()))
            coll_c.toggled.connect(lambda v: (setattr(node, "collapsible", v), colp_c.setEnabled(v), on_prop_change()))
            colp_c.toggled.connect(lambda v: (setattr(node, "collapsed", v), on_prop_change()))
            self.props_form.addRow("Заголовок группы", t_e)
            self.props_form.addRow("", coll_c)
            self.props_form.addRow("", colp_c)

        elif node.kind == "tab":
            t_e = QLineEdit(node.title)
            t_e.textChanged.connect(lambda t: (setattr(node, "title", t), on_prop_change()))
            self.props_form.addRow("Название вкладки", t_e)

        elif node.kind in ("row", "column"):
            sp_s = QSpinBox()
            sp_s.setRange(0, 48)
            sp_s.setValue(node.spacing)
            sp_s.valueChanged.connect(lambda v: (setattr(node, "spacing", v), on_prop_change()))
            self.props_form.addRow("Отступ между элементами", sp_s)
            if node.kind == "column":
                st_s = QSpinBox()
                st_s.setRange(0, 12)
                st_s.setValue(node.stretch)
                st_s.valueChanged.connect(lambda v: (setattr(node, "stretch", v), on_prop_change()))
                self.props_form.addRow("Пропорция (stretch)", st_s)

    def _refresh_preview(self) -> None:
        if self._entity is None:
            self.preview_scroll.setWidget(QWidget())
            return
        self.preview_scroll.setWidget(render_studio_form_preview(self._entity, self._project))

    def _open_window_preview(self) -> None:
        if self._entity is None:
            return
        from decui.studio_tools import FormPreviewDialog

        dialog = FormPreviewDialog(self, self._entity, None, project=self._project)
        dialog.exec()
        dialog.deleteLater()
