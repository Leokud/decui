"""Serializable application definitions and Python export for DECUI Studio."""

from __future__ import annotations

import ast
import datetime
import json
import keyword
import re
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from decui.fields import BasisTable

FIELD_TYPES = ("String", "Integer", "Decimal", "Boolean", "Date", "DateTime", "Time", "Choice", "Ref")
ENTITY_TYPES = {"Справочник": ("catalog", "Catalog"), "Документ": ("document", "Document")}
RESERVED_FIELDS = {"id", "date", "number", "posted", "save", "delete", "validate", "get", "list"}
AGGREGATE_TYPES = {
    "": "Нет",
    "sum": "Сумма",
    "avg": "Среднее",
    "count": "Количество",
    "min": "Минимум",
    "max": "Максимум",
}
ALIGN_TYPES = {"": "Авто", "left": "Слева", "center": "По центру", "right": "Справа"}
CURRENCY_PRESETS = ("", "RUB", "USD", "EUR", "₽", "$", "€")
NUMBERING_PATTERNS = (
    "",
    "{seq:08d}",
    "{prefix}-{seq:06d}",
    "{prefix}-{year}-{seq:06d}",
    "{prefix}-{year}-{month}-{seq:04d}",
    "{year}-{month}-{seq:04d}",
)

_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def identifier(value: str) -> bool:
    return value.isidentifier() and not keyword.iskeyword(value) and not value.startswith("_")


def suggest_identifier(label: str, *, pascal: bool = False) -> str:
    """Convert a Russian or Latin label into a valid Python identifier."""
    transliterated = "".join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in label.lower())
    words = [part for part in re.split(r"[^a-z0-9]+", transliterated) if part]
    if not words:
        return ""
    if pascal:
        candidate = "".join(word.capitalize() for word in words)
        if candidate[0].isdigit():
            candidate = "Item" + candidate
        if keyword.iskeyword(candidate):
            candidate += "Item"
        return candidate if identifier(candidate) else ""
    candidate = "_".join(words)
    if candidate[0].isdigit():
        candidate = "f_" + candidate
    if keyword.iskeyword(candidate) or candidate in RESERVED_FIELDS:
        candidate += "_value"
    return candidate if identifier(candidate) else ""


class StudioSyntaxError(SyntaxError):
    def __init__(self, error: SyntaxError, location: str, *, entity_name: str = "", source: str = ""):
        super().__init__(
            error.msg, (location, error.lineno or 1, error.offset or 1, error.text),
        )
        self.entity_name = entity_name
        self.source = source
        self.generated_line: int | None = None
        self.editor_snapshot: str | None = None


@dataclass(frozen=True)
class StudioSourceSpan:
    generated_start: int
    line_count: int
    editor_start: int
    location: str
    text: str
    entity_name: str = ""
    col_offset: int = 0


@dataclass(frozen=True)
class StudioDiagnostic:
    severity: str  # "error" | "warning"
    message: str
    entity_name: str = ""
    tab: str = ""


def parse_studio_code(code: str, location: str, *, entity_name: str = "") -> ast.Module:
    try:
        return ast.parse(code, filename=location)
    except SyntaxError as exc:
        raise StudioSyntaxError(exc, location, entity_name=entity_name) from exc


def studio_command_methods(code: ast.Module) -> list[str]:
    """Find plain instance methods that a button can call without arguments."""
    reserved = {"before_save", "after_save", "before_delete", "after_delete",
                "on_validate", "get_movements", "get_ledger_entries", "check_sequence"}
    methods = {}
    for node in code.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods[node.name] = node
    result = []
    for name, node in methods.items():
        if not isinstance(node, ast.FunctionDef) or not identifier(name) or name in reserved or node.decorator_list:
            continue
        arguments = node.args
        positional = len(arguments.posonlyargs) + len(arguments.args)
        if positional and positional - len(arguments.defaults) <= 1 and None not in arguments.kw_defaults:
            result.append(name)
    return result


@dataclass
class StudioField:
    name: str
    label: str
    kind: str = "String"
    required: bool = False
    multiline: bool = False
    target: str = ""
    related_list: str = ""
    choices: str = ""
    default: str = ""
    read_only: bool = False
    indexed: bool = False
    unique: bool = False
    cascade: bool = False
    write_roles: list[str] | None = None
    read_roles: list[str] | None = None
    form_group: str = ""
    form_tab: str = ""
    visibility_field: str = ""
    visibility_value: bool | str = True
    required_when_visible: bool = False
    create_from: str = ""
    basis_fields: dict[str, str] = field(default_factory=dict)
    basis_tables: dict[str, BasisTable] = field(default_factory=dict)
    choice_filter: dict[str, str] = field(default_factory=dict)
    max_length: int | None = None
    min_value: str = ""
    max_value: str = ""
    precision: int = 10
    scale: int = 2
    currency: str = ""
    multi: bool = False
    representation_field: str = ""
    auto_fill: str = ""
    visible: bool = True
    placeholder: str = ""


def _target_field_names(target: StudioEntity) -> set[str]:
    names = {item.name for item in target.fields}
    if target.kind == "Документ":
        names.update({"date", "number", "posted"})
    return names


def field_source(
    item: StudioField,
    owner: str,
    names: set[str],
    imports: set[str],
    *,
    entities: dict[str, StudioEntity] | None = None,
    siblings: list[StudioField] | None = None,
) -> str:
    if item.kind not in FIELD_TYPES or not item.label.strip():
        raise ValueError(f"{owner}.{item.name}: проверьте тип и подпись.")
    if item.choice_filter and (item.kind != "Ref" or not isinstance(item.choice_filter, dict)
                              or not all(isinstance(name, str) and identifier(name)
                                         for pair in item.choice_filter.items() for name in pair)):
        raise ValueError(f"{owner}.{item.name}: проверьте соответствие полей choice_filter.")
    imports.add(item.kind)
    args = [repr(item.label)]
    if item.kind == "Ref":
        if item.target not in names:
            raise ValueError(f"{owner}.{item.name}: выберите объект для ссылки.")
        args.insert(0, repr(item.target))
        target_entity = entities.get(item.target) if entities else None
        if item.representation_field.strip():
            rep = item.representation_field.strip()
            if not identifier(rep):
                raise ValueError(f"{owner}.{item.name}: проверьте поле представления «{rep}».")
            if target_entity is not None and rep not in _target_field_names(target_entity):
                raise ValueError(f"{owner}.{item.name}: поле «{rep}» отсутствует в объекте «{item.target}».")
            args.append(f"representation_field={rep!r}")
        if item.auto_fill.strip():
            af = item.auto_fill.strip()
            if not identifier(af) or af == item.name:
                raise ValueError(f"{owner}.{item.name}: проверьте поле автоподстановки «{af}».")
            if target_entity is not None and af not in _target_field_names(target_entity):
                raise ValueError(f"{owner}.{item.name}: поле автоподстановки «{af}» отсутствует в «{item.target}».")
            if siblings is not None and not any(sibling.name == af for sibling in siblings):
                raise ValueError(f"{owner}.{item.name}: добавьте поле «{af}» для автоподстановки в {owner}.")
            args.append(f"auto_fill={af!r}")
        if item.related_list:
            args.append(f"related_list={item.related_list!r}")
        if item.choice_filter:
            args.append(f"choice_filter={item.choice_filter!r}")
        if item.create_from.strip():
            args.append(f"create_from={item.create_from.strip()!r}")
            if item.basis_fields:
                args.append(f"basis_fields={item.basis_fields!r}")
            if item.basis_tables:
                args.append(f"basis_tables={item.basis_tables!r}")
        if item.cascade:
            args.append('on_delete="CASCADE"')
    if item.kind == "String":
        if item.multiline:
            args.append("multiline=True")
        if item.placeholder.strip():
            args.append(f"placeholder={item.placeholder.strip()!r}")
        if item.max_length is not None:
            if isinstance(item.max_length, bool) or not isinstance(item.max_length, int) or item.max_length < 1:
                raise ValueError(f"{owner}.{item.name}: длина строки должна быть положительным целым числом.")
            args.append(f"max_length={item.max_length}")
    if item.kind == "Integer":
        lo: int | None = None
        hi: int | None = None
        if item.min_value.strip():
            try:
                lo = int(item.min_value.strip())
            except ValueError as exc:
                raise ValueError(f"{owner}.{item.name}: укажите целое минимальное значение.") from exc
            args.append(f"min_value={lo}")
        if item.max_value.strip():
            try:
                hi = int(item.max_value.strip())
            except ValueError as exc:
                raise ValueError(f"{owner}.{item.name}: укажите целое максимальное значение.") from exc
            args.append(f"max_value={hi}")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"{owner}.{item.name}: минимум не может превышать максимум.")
    if item.kind == "Decimal":
        if (
            isinstance(item.precision, bool) or not isinstance(item.precision, int) or item.precision < 1
            or isinstance(item.scale, bool) or not isinstance(item.scale, int)
            or item.scale < 0 or item.scale > item.precision
        ):
            raise ValueError(f"{owner}.{item.name}: проверьте точность и число знаков после запятой.")
        if item.precision != 10:
            args.append(f"precision={item.precision}")
        if item.scale != 2:
            args.append(f"scale={item.scale}")
        min_dec: Decimal | None = None
        max_dec: Decimal | None = None
        if item.min_value.strip():
            raw_min = item.min_value.strip()
            try:
                min_dec = Decimal(raw_min)
                if not min_dec.is_finite():
                    raise ValueError
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"{owner}.{item.name}: некорректное минимальное десятичное число.") from exc
            imports.add("DecimalValue")
            args.append(f"min_value=DecimalValue({raw_min!r})")
        if item.max_value.strip():
            raw_max = item.max_value.strip()
            try:
                max_dec = Decimal(raw_max)
                if not max_dec.is_finite():
                    raise ValueError
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"{owner}.{item.name}: некорректное максимальное десятичное число.") from exc
            imports.add("DecimalValue")
            args.append(f"max_value=DecimalValue({raw_max!r})")
        if min_dec is not None and max_dec is not None and min_dec > max_dec:
            raise ValueError(f"{owner}.{item.name}: минимум не может превышать максимум.")
        if item.currency.strip():
            args.append(f"currency={item.currency.strip()!r}")
    if item.kind == "Choice":
        choice_pairs: list[tuple[str, str]] = []
        choice_codes: list[str] = []
        for raw_opt in item.choices.split(","):
            entry = raw_opt.strip()
            if not entry:
                continue
            if ": " in entry:
                code_part, _, label_part = entry.partition(": ")
                code_s, label_s = code_part.strip(), label_part.strip()
                if not code_s or not label_s:
                    raise ValueError(f"{owner}.{item.name}: проверьте пару «код: подпись» в «{entry}».")
                choice_pairs.append((code_s, label_s))
                choice_codes.append(code_s)
            else:
                choice_pairs.append((entry, entry))
                choice_codes.append(entry)
        if not choice_codes or len(set(choice_codes)) != len(choice_codes):
            raise ValueError(f"{owner}.{item.name}: укажите уникальные варианты через запятую.")
        if item.default:
            chosen = [p.strip() for p in item.default.split("|") if p.strip()] if item.multi else [item.default]
            if not chosen or not set(chosen).issubset(choice_codes):
                raise ValueError(f"{owner}.{item.name}: значение по умолчанию должно входить в список вариантов.")
        args.extend((f"choices={choice_pairs!r}", f"multi={item.multi!r}"))
    if not item.visible:
        args.append("visible=False")
    for option in ("required", "read_only", "indexed", "unique"):
        if getattr(item, option):
            args.append(f"{option}=True")
    if item.default:
        default = item.default
        if item.kind == "Date" and default.strip().casefold() == "today":
            imports.add("datetime")
            args.append("default=datetime.date.today")
            default = None
        elif item.kind == "DateTime" and default.strip().casefold() == "now":
            imports.add("datetime")
            args.append("default=datetime.datetime.now")
            default = None
        elif item.kind == "Time" and default.strip().casefold() == "now":
            imports.add("datetime")
            args.append("default=lambda: datetime.datetime.now().time().replace(second=0, microsecond=0)")
            default = None
        else:
            temporal_type = {"Date": datetime.date, "DateTime": datetime.datetime, "Time": datetime.time}.get(item.kind)
            if temporal_type is not None:
                try:
                    temporal_type.fromisoformat(default)
                except ValueError as exc:
                    raise ValueError(f"{owner}.{item.name}: используйте формат ISO.") from exc
                imports.add("datetime")
                args.append(f"default=datetime.{temporal_type.__name__}.fromisoformat({default!r})")
                default = None
            elif item.kind == "Decimal":
                try:
                    if not Decimal(default).is_finite():
                        raise ValueError
                except (InvalidOperation, ValueError) as exc:
                    raise ValueError(f"{owner}.{item.name}: укажите десятичное число.") from exc
                imports.add("DecimalValue")
                args.append(f"default=DecimalValue({default!r})")
                default = None
        if default is not None and item.kind not in ("String", "Choice", "Date", "DateTime", "Time", "Decimal"):
            try:
                default = ast.literal_eval(default)
            except (ValueError, SyntaxError) as exc:
                raise ValueError(f"{owner}.{item.name}: некорректное значение по умолчанию.") from exc
            expected_type = bool if item.kind == "Boolean" else int
            if type(default) is not expected_type:
                raise ValueError(f"{owner}.{item.name}: проверьте тип значения по умолчанию.")
        if default is not None:
            args.append(f"default={default!r}")
    return f"{item.name}: {item.kind} = {item.kind}({', '.join(args)})"


@dataclass
class StudioTablePart:
    name: str
    label: str
    fields: list[StudioField] = field(default_factory=list)
    form_tab: str = ""


@dataclass
class StudioMovement:
    register: str
    table_part: str = ""
    income: bool = True
    mapping: dict[str, str] = field(default_factory=dict)
    period_field: str = ""


@dataclass
class StudioCalculation:
    target: str
    kind: str = "row_formula"  # "row_formula" | "table_total" | "header_formula" | "lookup"
    source_part: str = ""
    left: str = ""
    operator: str = "*"
    right: str = ""
    register: str = ""
    resource: str = ""
    date_field: str = ""
    dimension_map: dict[str, str] = field(default_factory=dict)
    only_if_empty: bool = True


@dataclass
class StudioValidation:
    kind: str = "require_rows"  # "require_rows" | "unique_rows" | "compare"
    target: str = ""
    column: str = ""
    operator: str = ">="
    other: str = ""
    message: str = ""


@dataclass
class StudioTransition:
    label: str
    source: str
    target: str
    roles: list[str] = field(default_factory=list)
    assigned_only: bool = False


@dataclass
class StudioWorkflow:
    states: list[str] = field(default_factory=list)
    initial: str = ""
    transitions: list[StudioTransition] = field(default_factory=list)
    post_states: list[str] = field(default_factory=list)
    field_name: str = "approval_status"

    def validate(self) -> None:
        from .workflow import Transition, Workflow

        if not identifier(self.field_name) or any(not state.strip() for state in self.states):
            raise ValueError("Проверьте имя поля маршрута и названия статусов.")
        Workflow(
            self.field_name, self.initial, tuple(self.states),
            tuple(Transition(item.label, item.source, item.target, tuple(item.roles), item.assigned_only) for item in self.transitions),
            tuple(self.post_states),
        )

    def source(self) -> list[str]:
        self.validate()
        return [
            f"    {self.field_name}: Choice = Choice(",
            f"        'Статус согласования', choices={[(state, state) for state in self.states]!r},",
            f"        default={self.initial!r}, required=True, read_only=True, multi=False,",
            "    )",
            "    __workflow__ = Workflow(",
            f"        {self.field_name!r}, {self.initial!r}, {tuple(self.states)!r},",
            "        (",
            *(f"            Transition({item.label!r}, {item.source!r}, {item.target!r}, roles={tuple(item.roles)!r}, assigned_only={item.assigned_only!r}),"
              for item in self.transitions),
            "        ),",
            f"        post_states={tuple(self.post_states)!r},",
            "    )",
        ]


@dataclass
class StudioPrintForm:
    title: str
    fields: list[str] = field(default_factory=list)
    tables: dict[str, list[str]] = field(default_factory=dict)

    def source(self, entity: StudioEntity) -> list[str]:
        known = {item.name for item in entity.fields} | {"date", "number", "posted"}
        if entity.workflow is not None:
            known.add(entity.workflow.field_name)
        if entity.kind != "Документ" or not self.title.strip():
            raise ValueError("У печатной формы документа должен быть заголовок.")
        if not self.fields and not self.tables:
            raise ValueError(f"{self.title}: выберите поля или колонки.")
        if not set(self.fields) <= known:
            raise ValueError(f"{self.title}: печатная форма содержит неизвестный реквизит.")
        parts = {part.name: {item.name for item in part.fields} for part in entity.table_parts}
        for part, columns in self.tables.items():
            if not columns or part not in parts or not set(columns) <= parts[part]:
                raise ValueError(f"{self.title}: проверьте колонки табличной части «{part}».")
        return [
            "    __print_form__ = PrintForm(",
            f"        {self.title!r}, fields={tuple(self.fields)!r},",
            "        tables=(",
            *(f"            PrintTable({part!r}, {tuple(columns)!r})," for part, columns in self.tables.items()),
            "        ),",
            "    )",
        ]


FORM_NODE_KINDS = (
    "row", "column", "group", "tabs", "tab", "field", "table", "button", "label", "divider",
)
FORM_LABEL_STYLES = ("normal", "heading", "muted", "info", "warning")
FORM_BUILTIN_BUTTONS = {
    "save": "Сохранить",
    "save_and_close": "Сохранить и закрыть",
    "post": "Провести",
    "post_and_close": "Провести и закрыть",
    "unpost": "Отменить проведение",
    "print": "Печатная форма",
    "movements": "Движения",
    "close": "Закрыть",
}


@dataclass
class StudioFormNode:
    kind: str
    target: str = ""
    title: str = ""
    label: str = ""
    placeholder: str = ""
    style: str = "normal"
    stretch: int = 1
    spacing: int = 12
    min_width: int = 0
    min_height: int = 0
    read_only: bool | None = None
    collapsible: bool = False
    collapsed: bool = False
    primary: bool = False
    visibility_field: str = ""
    visibility_value: bool | str = True
    children: list[StudioFormNode] = field(default_factory=list)


@dataclass
class StudioFormConfig:
    enabled: bool = False
    title: str = ""
    include_remaining: bool = False
    show_actions: bool = True
    items: list[StudioFormNode] = field(default_factory=list)


def read_form_node(raw: dict[str, object]) -> StudioFormNode:
    children_raw = raw.get("children", [])
    return StudioFormNode(
        kind=str(raw.get("kind", "field")),
        target=str(raw.get("target", "")),
        title=str(raw.get("title", "")),
        label=str(raw.get("label", "")),
        placeholder=str(raw.get("placeholder", "")),
        style=str(raw.get("style", "normal")),
        stretch=int(raw.get("stretch", 1)),  # type: ignore[call-overload]
        spacing=int(raw.get("spacing", 12)),  # type: ignore[call-overload]
        min_width=int(raw.get("min_width", 0)),  # type: ignore[call-overload]
        min_height=int(raw.get("min_height", 0)),  # type: ignore[call-overload]
        read_only=raw.get("read_only") if isinstance(raw.get("read_only"), bool) else None,
        collapsible=bool(raw.get("collapsible", False)),
        collapsed=bool(raw.get("collapsed", False)),
        primary=bool(raw.get("primary", False)),
        visibility_field=str(raw.get("visibility_field", "")),
        visibility_value=raw.get("visibility_value", True),  # type: ignore[arg-type]
        children=[read_form_node(c) for c in children_raw if isinstance(c, dict)],  # type: ignore[attr-defined]
    )


def read_form_config(raw: dict[str, object] | None) -> StudioFormConfig | None:
    if raw is None:
        return None
    items_raw = raw.get("items", [])
    return StudioFormConfig(
        enabled=bool(raw.get("enabled", False)),
        title=str(raw.get("title", "")),
        include_remaining=bool(raw.get("include_remaining", False)),
        show_actions=bool(raw.get("show_actions", True)),
        items=[read_form_node(n) for n in items_raw if isinstance(n, dict)],  # type: ignore[attr-defined]
    )


def default_form_for_entity(entity: StudioEntity, preset: str = "standard") -> StudioFormConfig:
    """Build an initial StudioFormConfig from an entity's fields and table parts."""
    visible_fields = [f for f in entity.fields if f.visible]
    items: list[StudioFormNode] = []

    if preset == "compact" or (preset == "two_column" and entity.kind == "Документ"):
        if entity.kind == "Документ":
            items.append(StudioFormNode(
                "row",
                children=[
                    StudioFormNode("field", target="number", stretch=1),
                    StudioFormNode("field", target="date", stretch=1),
                    StudioFormNode("field", target="posted", stretch=0),
                ],
            ))
    elif entity.kind == "Документ":
        items.append(StudioFormNode(
            "row",
            children=[
                StudioFormNode("field", target="number", stretch=1),
                StudioFormNode("field", target="date", stretch=1),
                StudioFormNode("field", target="posted", stretch=0),
            ],
        ))

    if preset == "two_column":
        row_buf: list[StudioFormNode] = []
        for f in visible_fields:
            if f.multiline:
                if row_buf:
                    items.append(StudioFormNode("row", children=row_buf))
                    row_buf = []
                items.append(StudioFormNode("field", target=f.name))
            else:
                row_buf.append(StudioFormNode("field", target=f.name, stretch=1))
                if len(row_buf) == 2:
                    items.append(StudioFormNode("row", children=row_buf))
                    row_buf = []
        if row_buf:
            items.append(StudioFormNode("row", children=row_buf))
        if entity.table_parts:
            tabs = [
                StudioFormNode("tab", title=part.label or part.name, children=[StudioFormNode("table", target=part.name)])
                for part in entity.table_parts
            ]
            items.append(StudioFormNode("tabs", children=tabs))
        return StudioFormConfig(enabled=True, items=items)

    # "standard" or "compact": respect existing form_tab / form_group settings
    root_groups: dict[str, list[StudioFormNode]] = {}
    tab_groups: dict[str, dict[str, list[StudioFormNode]]] = {}
    for f in visible_fields:
        node = StudioFormNode("field", target=f.name)
        t = f.form_tab.strip()
        g = f.form_group.strip()
        if t:
            tab_groups.setdefault(t, {}).setdefault(g, []).append(node)
        else:
            root_groups.setdefault(g, []).append(node)

    for grp, nodes in root_groups.items():
        if grp:
            items.append(StudioFormNode("group", title=grp, children=nodes))
        else:
            items.extend(nodes)

    tab_tables: dict[str, list[StudioFormNode]] = {}
    inline_tables: list[StudioFormNode] = []
    for part in entity.table_parts:
        t_node = StudioFormNode("table", target=part.name)
        t = part.form_tab.strip()
        if t or (preset == "compact" and len(entity.table_parts) >= 1):
            tab_tables.setdefault(t or part.label or part.name, []).append(t_node)
        else:
            inline_tables.append(t_node)

    items.extend(inline_tables)
    all_tab_names = list(dict.fromkeys([*tab_groups.keys(), *tab_tables.keys()]))
    if all_tab_names:
        tabs_nodes: list[StudioFormNode] = []
        for tab_name in all_tab_names:
            children: list[StudioFormNode] = []
            for grp, nodes in tab_groups.get(tab_name, {}).items():
                if grp:
                    children.append(StudioFormNode("group", title=grp, children=nodes))
                else:
                    children.extend(nodes)
            children.extend(tab_tables.get(tab_name, []))
            tabs_nodes.append(StudioFormNode("tab", title=tab_name, children=children))
        items.append(StudioFormNode("tabs", children=tabs_nodes))

    return StudioFormConfig(enabled=True, items=items)


def _iter_form_nodes(nodes: list[StudioFormNode]) -> list[StudioFormNode]:
    out: list[StudioFormNode] = []
    for node in nodes:
        out.append(node)
        out.extend(_iter_form_nodes(node.children))
    return out


def _rename_form_field(nodes: list[StudioFormNode], old: str, new: str) -> None:
    for node in _iter_form_nodes(nodes):
        if node.kind == "field" and node.target == old:
            node.target = new
        if node.visibility_field == old:
            node.visibility_field = new


def _rename_form_table(nodes: list[StudioFormNode], old: str, new: str) -> None:
    for node in _iter_form_nodes(nodes):
        if node.kind == "table" and node.target == old:
            node.target = new


def form_source(
    entity: StudioEntity,
    imports: set[str],
    *,
    indent: str = "    ",
    attr_name: str = "__form__",
) -> list[str]:
    """Serialize entity.form into declarative Form(...) Python source lines."""
    cfg = entity.form
    if cfg is None or not cfg.enabled:
        return []
    known_fields = _target_field_names(entity)
    fields_by_name = {f.name: f for f in entity.fields}
    known_tables = {part.name for part in entity.table_parts}
    known_commands = set(entity.commands) | set(FORM_BUILTIN_BUTTONS)

    def render_node(node: StudioFormNode, pad: str) -> str:
        if node.kind not in FORM_NODE_KINDS:
            raise ValueError(f"{entity.label}: неизвестный элемент формы «{node.kind}».")
        if node.kind == "field":
            if node.target not in known_fields:
                raise ValueError(f"{entity.label}: поле «{node.target}» на форме не найдено.")
            imports.add("FormField")
            args = [repr(node.target)]
            if node.label.strip():
                args.append(f"label={node.label.strip()!r}")
            if node.placeholder.strip():
                args.append(f"placeholder={node.placeholder.strip()!r}")
            if node.read_only is not None:
                args.append(f"read_only={node.read_only!r}")
            if node.stretch != 1:
                args.append(f"stretch={node.stretch}")
            if node.min_width > 0:
                args.append(f"min_width={node.min_width}")
            if node.visibility_field:
                ctrl = fields_by_name.get(node.visibility_field)
                if ctrl is None or ctrl.name == node.target:
                    raise ValueError(f"{entity.label}: проверьте условие видимости поля формы «{node.target}».")
                if ctrl.kind == "Boolean":
                    valid = isinstance(node.visibility_value, bool)
                else:
                    valid = ctrl.kind == "Choice" and not ctrl.multi
                if not valid:
                    raise ValueError(f"{entity.label}: недопустимое условие видимости на форме для «{node.target}».")
                args.append(f"visible_when=({node.visibility_field!r}, {node.visibility_value!r})")
            return f"{pad}FormField({', '.join(args)})"

        if node.kind == "table":
            if node.target not in known_tables:
                raise ValueError(f"{entity.label}: табличная часть «{node.target}» на форме не найдена.")
            imports.add("FormTable")
            args = [repr(node.target)]
            if node.title.strip():
                args.append(f"title={node.title.strip()!r}")
            if node.min_height > 0:
                args.append(f"min_height={node.min_height}")
            if node.read_only:
                args.append("read_only=True")
            return f"{pad}FormTable({', '.join(args)})"

        if node.kind == "button":
            if node.target not in known_commands:
                raise ValueError(f"{entity.label}: кнопка формы ссылается на неизвестное действие «{node.target}».")
            imports.add("FormButton")
            args = [repr(node.target)]
            if node.label.strip():
                args.append(f"label={node.label.strip()!r}")
            if node.primary:
                args.append("primary=True")
            return f"{pad}FormButton({', '.join(args)})"

        if node.kind == "label":
            imports.add("FormLabel")
            style = node.style if node.style in FORM_LABEL_STYLES else "normal"
            args = [repr(node.title or node.label)]
            if style != "normal":
                args.append(f"style={style!r}")
            return f"{pad}FormLabel({', '.join(args)})"

        if node.kind == "divider":
            imports.add("FormDivider")
            return f"{pad}FormDivider()"

        if node.kind == "row":
            imports.add("FormRow")
            child_pad = pad + "    "
            inner = [render_node(ch, child_pad) for ch in node.children]
            kw = [f"spacing={node.spacing}"] if node.spacing != 12 else []
            if not inner and not kw:
                return f"{pad}FormRow()"
            lines = [f"{pad}FormRow("]
            lines.extend(f"{line}," for line in inner)
            lines.extend(f"{child_pad}{k}," for k in kw)
            lines.append(f"{pad})")
            return "\n".join(lines)

        if node.kind == "column":
            imports.add("FormColumn")
            child_pad = pad + "    "
            inner = [render_node(ch, child_pad) for ch in node.children]
            kw = []
            if node.stretch != 1:
                kw.append(f"stretch={node.stretch}")
            if node.spacing != 8:
                kw.append(f"spacing={node.spacing}")
            lines = [f"{pad}FormColumn("]
            lines.extend(f"{line}," for line in inner)
            lines.extend(f"{child_pad}{k}," for k in kw)
            lines.append(f"{pad})")
            return "\n".join(lines)

        if node.kind == "group":
            imports.add("FormGroup")
            child_pad = pad + "    "
            inner = [render_node(ch, child_pad) for ch in node.children]
            kw = []
            if node.collapsible:
                kw.append("collapsible=True")
            if node.collapsed:
                kw.append("collapsed=True")
            lines = [f"{pad}FormGroup({node.title!r},"]
            lines.extend(f"{line}," for line in inner)
            lines.extend(f"{child_pad}{k}," for k in kw)
            lines.append(f"{pad})")
            return "\n".join(lines)

        if node.kind == "tab":
            imports.add("FormTab")
            child_pad = pad + "    "
            inner = [render_node(ch, child_pad) for ch in node.children]
            lines = [f"{pad}FormTab({(node.title or 'Вкладка')!r},"]
            lines.extend(f"{line}," for line in inner)
            lines.append(f"{pad})")
            return "\n".join(lines)

        if node.kind == "tabs":
            if any(ch.kind != "tab" for ch in node.children):
                raise ValueError(f"{entity.label}: контейнер вкладок может содержать только вкладки (FormTab).")
            imports.add("FormTabs")
            child_pad = pad + "    "
            inner = [render_node(ch, child_pad) for ch in node.children]
            lines = [f"{pad}FormTabs("]
            lines.extend(f"{line}," for line in inner)
            lines.append(f"{pad})")
            return "\n".join(lines)

        raise ValueError(f"{entity.label}: неизвестный элемент формы «{node.kind}».")

    imports.add("Form")
    child_pad = indent + "    "
    rendered_children = [render_node(item, child_pad) for item in cfg.items]
    kw_args: list[str] = []
    if cfg.title.strip():
        kw_args.append(f"title={cfg.title.strip()!r}")
    if cfg.include_remaining:
        kw_args.append("include_remaining=True")
    if not cfg.show_actions:
        kw_args.append("show_actions=False")
    result = [f"{indent}{attr_name} = Form("]
    result.extend(f"{line}," for line in rendered_children)
    result.extend(f"{child_pad}{kw}," for kw in kw_args)
    result.append(f"{indent})")
    return result


@dataclass
class StudioEntity:
    name: str
    label: str
    kind: str = "Справочник"
    section: str = ""
    fields: list[StudioField] = field(default_factory=list)
    code: str = ""
    numbering_prefix: str = ""
    numbering_field: str = ""
    table_parts: list[StudioTablePart] = field(default_factory=list)
    movements: list[StudioMovement] = field(default_factory=list)
    workflow: StudioWorkflow | None = None
    owner_only: bool = False
    permissions: dict[str, list[str]] | None = None
    print_form: StudioPrintForm | None = None
    list_columns: list[str] = field(default_factory=list)
    list_order: str = ""
    commands: dict[str, str] = field(default_factory=dict)
    command_roles: dict[str, list[str]] = field(default_factory=dict)
    numbering_pattern: str = ""
    check_sequence: bool = False
    calculations: list[StudioCalculation] = field(default_factory=list)
    validations: list[StudioValidation] = field(default_factory=list)
    form: StudioFormConfig | None = None
    form_code: str = ""

    def list_field_options(self) -> dict[str, str]:
        options = {"date": "Дата", "number": "Номер", "posted": "Проведён"} if self.kind == "Документ" else {}
        options.update((item.name, item.label) for item in self.fields)
        if self.workflow is not None:
            options[self.workflow.field_name] = "Статус согласования"
            if any(step.assigned_only for step in self.workflow.transitions):
                options["approval_assignee"] = "Согласующий"
        return options


def basis_field_options(
    destination: StudioEntity, source: StudioEntity, link_name: str,
) -> list[tuple[StudioField, list[StudioField]]]:
    """Match editable destination fields with source fields of the same type."""
    return basis_column_options(
        [field for field in destination.fields if field.name not in {link_name, destination.numbering_field}],
        source.fields,
    )


def basis_column_options(
    destination: list[StudioField], source: list[StudioField],
) -> list[tuple[StudioField, list[StudioField]]]:
    return [
        (target, [
            field for field in source
            if field.kind == target.kind and (field.kind != "Ref" or field.target == target.target)
        ])
        for target in destination if not target.read_only
    ]


def validate_basis_tables(entity: StudioEntity, source: StudioEntity, link: StudioField) -> None:
    destination_parts = {part.name: part for part in entity.table_parts}
    source_parts = {part.name: part for part in source.table_parts}
    for destination, mapping in link.basis_tables.items():
        dest_part = destination_parts.get(destination)
        source_part = source_parts.get(mapping["source"])
        if dest_part is not None and source_part is not None:
            options = {
                target.name: {field.name for field in available}
                for target, available in basis_column_options(dest_part.fields, source_part.fields)
            }
            if mapping["columns"] and all(
                name in options and original in options[name] for name, original in mapping["columns"].items()
            ):
                continue
        raise ValueError(f"{entity.label}: проверьте перенос строк для «{destination}».")


@dataclass
class StudioReport:
    name: str
    title: str
    entity: str
    columns: list[str] = field(default_factory=list)
    order_by: str = ""
    ref_filter: str = ""
    source_kind: str = "entity"
    as_of: bool = False
    totals: bool = False
    group_by: list[str] = field(default_factory=list)
    section: str = ""
    auto_run: bool = True
    table_part: str = ""
    date_filter: str = ""
    posted_only: bool = False
    ref_filter_from_record: bool = False
    column_options: dict[str, dict[str, str]] = field(default_factory=dict)
    latest_only: bool = False
    sortable: bool = True
    choice_filter_field: str = ""
    text_filter_field: str = ""
    chart_label: str = ""
    chart_value: str = ""
    chart_title: str = ""
    chart_color: str = ""
    row_styles: list[dict[str, str]] = field(default_factory=list)


REGISTER_TYPES = {"Накопление": "accumulation", "Сведения": "information"}
DIMENSION_TYPES = {"Строка": "str", "Целое": "int", "Дата": "datetime.date", "Да / нет": "bool", "Ссылка": ""}
RESOURCE_TYPES = {"Целое": "int", "Десятичное": "DecimalValue"}
INFORMATION_RESOURCE_TYPES = {**RESOURCE_TYPES, "Строка": "str"}


@dataclass
class StudioRegisterMember:
    name: str
    label: str
    kind: str
    value_type: str
    target: str = ""
    required: bool = True
    representation_field: str = ""


@dataclass
class StudioRegister:
    name: str
    kind: str = "Накопление"
    members: list[StudioRegisterMember] = field(default_factory=list)

    def source(
        self, entity_names: set[str], imports: set[str], *, entities: dict[str, StudioEntity] | None = None,
    ) -> str:
        if not identifier(self.name) or self.kind not in REGISTER_TYPES:
            raise ValueError(f"Некорректное имя или вид регистра «{self.name}».")
        if not any(item.kind == "Ресурс" for item in self.members):
            raise ValueError(f"{self.name}: добавьте хотя бы один ресурс.")
        decorator = REGISTER_TYPES[self.kind]
        imports.update((decorator, "Register"))
        lines = [f"@{decorator}", f"class {self.name}(Register):"]
        names = {"id", "period", "doc_type", "doc_id", "metadata", "registry"}
        for item in self.members:
            if not identifier(item.name) or item.name in names or not item.label.strip():
                raise ValueError(f"{self.name}: недопустимое или повторяющееся поле «{item.name}».")
            names.add(item.name)
            if item.kind == "Измерение":
                field_type = DIMENSION_TYPES.get(item.value_type)
                constructor = "RefDimension" if item.value_type == "Ссылка" else "Dimension"
            elif item.kind == "Ресурс":
                resource_types = INFORMATION_RESOURCE_TYPES if self.kind == "Сведения" else RESOURCE_TYPES
                field_type = resource_types.get(item.value_type)
                constructor = "Resource"
            else:
                raise ValueError(f"{self.name}.{item.name}: выберите измерение или ресурс.")
            if field_type is None:
                raise ValueError(f"{self.name}.{item.name}: недопустимый тип.")
            imports.add(constructor)
            if constructor == "RefDimension":
                if item.target not in entity_names:
                    raise ValueError(f"{self.name}.{item.name}: выберите объект для ссылки.")
                arguments = f"{item.target!r}, {item.label!r}"
                rep = item.representation_field.strip()
                if rep:
                    if not identifier(rep):
                        raise ValueError(f"{self.name}.{item.name}: проверьте поле представления «{rep}».")
                    target_entity = entities.get(item.target) if entities else None
                    if target_entity is not None and rep not in _target_field_names(target_entity):
                        raise ValueError(f"{self.name}.{item.name}: поле «{rep}» отсутствует в «{item.target}».")
                    arguments += f", representation_field={rep!r}"
            else:
                arguments = f"{item.label!r}, field_type={field_type}"
                if field_type in ("datetime.date", "DecimalValue"):
                    imports.add(field_type.split(".")[0])
            lines.append(f"    {item.name} = {constructor}({arguments}, required={item.required!r})")
        return "\n".join(lines)


def movement_fields(entity: StudioEntity, table_part: str) -> dict[str, StudioField]:
    """Fields available to a posting rule, keyed by header or row origin."""
    result = {f"header:{item.name}": item for item in entity.fields}
    result["header:date"] = StudioField("date", "Дата документа", "DateTime")
    result["header:number"] = StudioField("number", "Номер документа")
    result["header:posted"] = StudioField("posted", "Проведён", "Boolean")
    if table_part:
        part = next((item for item in entity.table_parts if item.name == table_part), None)
        if part is None:
            raise ValueError(f"{entity.label}: табличная часть «{table_part}» не найдена.")
        result.update((f"row:{item.name}", item) for item in part.fields)
    return result


def movement_compatible(member: StudioRegisterMember, source: StudioField) -> bool:
    if member.value_type == "Ссылка":
        return source.kind == "Ref" and source.target == member.target
    allowed = {
        "Строка": {"String", "Choice"},
        "Целое": {"Integer"},
        "Десятичное": {"Decimal", "Integer"},
        "Дата": {"Date", "DateTime"},
        "Да / нет": {"Boolean"},
    }
    return source.kind in allowed.get(member.value_type, set())


def movement_source(entity: StudioEntity, registers: dict[str, StudioRegister]) -> list[str]:
    if entity.kind != "Документ":
        raise ValueError(f"{entity.label}: движения добавляются к документам.")
    header_dates = {item.name for item in entity.fields if item.kind in ("Date", "DateTime")} | {"date"}
    lines = ["", "    def get_movements(self) -> list[Movement]:", "        movements: list[Movement] = []"]
    for rule in entity.movements:
        register = registers.get(rule.register)
        if register is None:
            raise ValueError(f"{entity.label}: регистр «{rule.register}» не найден.")
        if register.kind == "Сведения" and not rule.income:
            raise ValueError(f"{register.name}: у регистра сведений нет расхода.")
        if rule.period_field and rule.period_field not in header_dates:
            raise ValueError(f"{entity.label}: поле периода «{rule.period_field}» должно быть датой документа.")
        fields = movement_fields(entity, rule.table_part)
        members = {item.name: item for item in register.members}
        if set(rule.mapping) - members.keys():
            raise ValueError(f"{register.name}: обновите соответствия полей регистра.")
        arguments = {"Измерение": [], "Ресурс": []}
        for name, member in members.items():
            source = rule.mapping.get(name)
            if not source and not member.required:
                continue
            column = fields.get(source or "")
            if column is None or not movement_compatible(member, column):
                raise ValueError(f"{register.name}.{name}: выберите совместимое поле документа или строки.")
            origin, _, _ = (source or "").partition(":")
            container = "row" if origin == "row" else "self._data"
            expression = f"{container}.get({column.name!r})"
            if member.value_type == "Дата" and column.kind == "DateTime":
                expression = f"{expression}.date() if {expression} is not None else None"
            arguments[member.kind].append(f"{name!r}: {expression}")
        indent = "        "
        if rule.table_part:
            lines.append(f"        for row in self._table_data.get({rule.table_part!r}, []):")
            indent += "    "
        period_line = (
            [f"{indent}    period=self._data.get({rule.period_field!r}),"]
            if rule.period_field and rule.period_field != "date"
            else []
        )
        lines.extend((
            f"{indent}movements.append(Movement(",
            f"{indent}    {register.name},",
            *period_line,
            f"{indent}    dimensions={{{', '.join(arguments['Измерение'])}}},",
            f"{indent}    resources={{{', '.join(arguments['Ресурс'])}}},",
            f"{indent}    is_income={rule.income!r},",
            f"{indent}))",
        ))
    lines.append("        return movements")
    return lines


def register_report_columns(register: StudioRegister) -> list[StudioRegisterMember]:
    if register.kind == "Сведения":
        return [StudioRegisterMember("period", "Дата записи", "Системное поле", "Дата"), *register.members]
    return register.members


def entity_report_fields(
    entity: StudioEntity, table_part: str = "",
) -> tuple[dict[str, StudioField], set[str]]:
    """Return all selectable report fields and the subset originating from the chosen table part."""
    fields: dict[str, StudioField] = {}
    if entity.kind == "Документ":
        fields["number"] = StudioField("number", "Номер", "String")
        fields["date"] = StudioField("date", "Дата", "DateTime")
        fields["posted"] = StudioField("posted", "Проведён", "Boolean")
    for item in entity.fields:
        fields[item.name] = item
    if entity.owner_only and "record_owner" not in fields:
        fields["record_owner"] = StudioField("record_owner", "Владелец", "String")
    if entity.workflow is not None:
        fields[entity.workflow.field_name] = StudioField(
            entity.workflow.field_name, "Статус согласования", "Choice",
            choices=", ".join(entity.workflow.states), multi=False,
        )
        if any(step.assigned_only for step in entity.workflow.transitions):
            fields["approval_assignee"] = StudioField("approval_assignee", "Согласующий", "String")
    row_fields: set[str] = set()
    if table_part:
        part = next((item for item in entity.table_parts if item.name == table_part), None)
        if part is None:
            raise ValueError(f"{entity.label}: табличная часть «{table_part}» не найдена.")
        for item in part.fields:
            fields[item.name] = item
            row_fields.add(item.name)
    return fields, row_fields


def report_group_source(config: StudioReport) -> list[str]:
    if not config.group_by:
        return []
    if len(set(config.group_by)) != len(config.group_by) or not set(config.group_by) <= set(config.columns):
        raise ValueError(f"{config.title}: группировать можно по выбранным колонкам без повторений.")
    return [f"    group_by = {tuple(config.group_by)!r}"]


def _report_column_source(
    label: str,
    column: str,
    is_number: bool,
    config: StudioReport,
) -> str:
    options = config.column_options.get(column, {})
    args: list[str] = [repr(label), repr(column)]
    width_raw = options.get("width", "").strip()
    if width_raw:
        try:
            width = int(width_raw)
            if width < 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError(f"{config.title}: некорректная ширина колонки «{column}».") from exc
        if width != 150:
            args.append(f"width={width}")
    align = options.get("align", "")
    if align not in ALIGN_TYPES:
        raise ValueError(f"{config.title}: некорректное выравнивание колонки «{column}».")
    effective_align = align or ("right" if is_number and (config.totals or options.get("aggregate")) else "")
    if effective_align and effective_align != "left":
        args.append(f"align={effective_align!r}")
    agg = options.get("aggregate", "")
    if agg not in AGGREGATE_TYPES:
        raise ValueError(f"{config.title}: неизвестная агрегация «{agg}».")
    if column not in config.group_by:
        effective_agg = agg if "aggregate" in options else ("sum" if is_number and config.totals else "")
        if effective_agg:
            args.append(f"aggregate={effective_agg!r}")
    fmt = options.get("format", "")
    if fmt:
        try:
            try:
                fmt.format(1234)
            except ValueError:
                fmt.format(1234.5)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ValueError(f"{config.title}: некорректный формат колонки «{column}»: {fmt}") from exc
        args.append(f"format={fmt!r}")
    if options.get("prefix"):
        args.append(f"prefix={options['prefix']!r}")
    if options.get("suffix"):
        args.append(f"suffix={options['suffix']!r}")
    return f"        Column({', '.join(args)}),"


def _report_chart_source(config: StudioReport, numeric_columns: set[str]) -> list[str]:
    if not config.chart_label and not config.chart_value:
        return []
    if not config.chart_label or not config.chart_value:
        raise ValueError(f"{config.title}: для диаграммы выберите обе колонки — подпись и числовое значение.")
    if config.chart_label not in config.columns or config.chart_value not in config.columns:
        raise ValueError(f"{config.title}: колонки диаграммы должны входить в отчёт.")
    if config.chart_value not in numeric_columns:
        raise ValueError(f"{config.title}: значение диаграммы должно быть числовой колонкой.")
    color = config.chart_color.strip()
    if color and not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise ValueError(f"{config.title}: цвет диаграммы должен быть в формате #RRGGBB.")
    spec: dict[str, str] = {"label": config.chart_label, "value": config.chart_value}
    if config.chart_title.strip():
        spec["title"] = config.chart_title.strip()
    if color:
        spec["color"] = color
    return [f"    chart = {spec!r}"]


def register_report_source(
    config: StudioReport, register: StudioRegister, imports: set[str],
) -> str:
    fields = {item.name: item for item in register_report_columns(register)}
    if not config.columns or len(set(config.columns)) != len(config.columns) or any(
        col not in fields for col in config.columns
    ):
        raise ValueError(f"{config.title}: выберите существующие колонки регистра.")
    if config.order_by and config.order_by.removeprefix("-") not in config.columns:
        raise ValueError(f"{config.title}: для сортировки выберите одну из колонок отчёта.")
    selected = fields.get(config.ref_filter)
    if config.ref_filter and (selected is None or selected.value_type != "Ссылка"):
        raise ValueError(f"{config.title}: для отбора выберите измерение-ссылку.")
    if config.latest_only and register.kind != "Сведения":
        raise ValueError(f"{config.title}: «Срез последних» доступен только для регистров сведений.")
    numeric_cols = {
        name for name, m in fields.items()
        if m.kind == "Ресурс" and m.value_type in RESOURCE_TYPES
    }
    imports.update(("Column", "Database", "Report", "report", "register_report_rows"))
    lines = ["@report", f"class {config.name}(Report):", f"    title = {config.title!r}"]
    if config.section.strip():
        lines.append(f"    __section__ = {config.section.strip()!r}")
    lines.append(f"    auto_run = {config.auto_run!r}")
    if not config.sortable:
        lines.append("    sortable = False")
    lines.extend((
        *report_group_source(config),
        *_report_chart_source(config, numeric_cols),
        "    columns: ClassVar[list[Column]] = [",
    ))
    for column in config.columns:
        member = fields[column]
        is_number = column in numeric_cols
        lines.append(_report_column_source(member.label, column, is_number, config))
    lines.append("    ]")
    if config.as_of:
        imports.update(("DateParam", "datetime"))
        lines.append("    through = DateParam('На дату')")
    if selected is not None:
        imports.add("RefParam")
        from_rec = ", from_record=True" if config.ref_filter_from_record else ""
        lines.append(f"    selected = RefParam({selected.label!r}, {selected.target!r}{from_rec})")
    lines.extend(("", "    def execute(self, db: Database, **params: object) -> list[dict[str, object]]:"))
    extras = []
    if config.as_of:
        lines.append("        through = params.get('through')")
        extras.append("on_date=through if isinstance(through, datetime.date) else None")
    if selected is not None:
        lines.append("        selected = params.get('selected')")
        extras.extend(("selected=selected if isinstance(selected, int) else None", f"ref_filter={selected.name!r}"))
    grouped_refs = tuple(name for name in config.columns if fields[name].value_type == "Ссылка")
    if grouped_refs:
        extras.append(f"groups={grouped_refs!r} if self._interactive_grouping else self.group_by")
    if config.order_by:
        extras.append(f"order_by={config.order_by!r}")
    if config.latest_only:
        extras.append("latest_only=True")
    lines.extend((
        "        return register_report_rows(",
        f"            db, {register.name}, {config.columns!r},",
        *(f"            {argument}," for argument in extras),
        "        )",
    ))
    return "\n".join(lines)


def entity_report_source(
    config: StudioReport, entity: StudioEntity, imports: set[str],
) -> str:
    fields, row_fields = entity_report_fields(entity, config.table_part)
    if not config.columns or len(set(config.columns)) != len(config.columns) or any(
        col not in fields for col in config.columns
    ):
        raise ValueError(f"{config.title}: выберите существующие поля для колонок.")
    if config.order_by and config.order_by.removeprefix("-") not in fields:
        raise ValueError(f"{config.title}: выберите существующее поле для сортировки.")
    if config.ref_filter and (
        config.ref_filter not in fields or fields[config.ref_filter].kind != "Ref"
    ):
        raise ValueError(f"{config.title}: для отбора нужно поле-ссылка.")
    if config.date_filter:
        date_field = fields.get(config.date_filter)
        if date_field is None or config.date_filter in row_fields or date_field.kind not in ("Date", "DateTime"):
            raise ValueError(f"{config.title}: для периода выберите поле даты шапки объекта.")
    if config.posted_only and entity.kind != "Документ":
        raise ValueError(f"{config.title}: фильтр «Только проведённые» доступен только для документов.")
    choice_param_field = fields.get(config.choice_filter_field) if config.choice_filter_field else None
    if config.choice_filter_field and (
        choice_param_field is None
        or (choice_param_field.kind != "Boolean" and not (choice_param_field.kind == "Choice" and not choice_param_field.multi))
    ):
        raise ValueError(f"{config.title}: для отбора по значению выберите одиночный «Choice» или «Boolean».")
    text_param_field = fields.get(config.text_filter_field) if config.text_filter_field else None
    if config.text_filter_field and (text_param_field is None or text_param_field.kind != "String"):
        raise ValueError(f"{config.title}: для текстового поиска выберите строковое поле.")
    for style_rule in config.row_styles:
        s_field = style_rule.get("field", "")
        if s_field not in fields:
            raise ValueError(f"{config.title}: поле «{s_field}» в условном оформлении не найдено.")
        if style_rule.get("style", "") not in ("", "completed", "current"):
            raise ValueError(f"{config.title}: неизвестный стиль строки.")
        s_color = style_rule.get("color", "").strip()
        if s_color and not re.fullmatch(r"#[0-9A-Fa-f]{6}", s_color):
            raise ValueError(f"{config.title}: цвет строки должен быть в формате #RRGGBB.")

    numeric_cols = {name for name, item in fields.items() if item.kind in ("Integer", "Decimal")}
    imports.update(("Column", "Database", "Report", "report"))
    lines = ["@report", f"class {config.name}(Report):", f"    title = {config.title!r}"]
    if config.section.strip():
        lines.append(f"    __section__ = {config.section.strip()!r}")
    lines.append(f"    auto_run = {config.auto_run!r}")
    if not config.sortable:
        lines.append("    sortable = False")
    lines.extend((
        *report_group_source(config),
        *_report_chart_source(config, numeric_cols),
        "    columns: ClassVar[list[Column]] = [",
    ))
    for column in config.columns:
        item = fields[column]
        is_measure = column in numeric_cols
        lines.append(_report_column_source(item.label, column, is_measure, config))
    lines.append("    ]")
    if config.date_filter:
        imports.update(("DateParam", "datetime"))
        lines.extend(("    date_from = DateParam('С')", "    date_to = DateParam('По')"))
    if config.ref_filter:
        filter_field = fields[config.ref_filter]
        imports.add("RefParam")
        from_rec = ", from_record=True" if config.ref_filter_from_record else ""
        lines.append(f"    selected = RefParam({filter_field.label!r}, {filter_field.target!r}{from_rec})")
    if choice_param_field is not None:
        imports.add("ChoiceParam")
        if choice_param_field.kind == "Boolean":
            choices_pairs = [("True", "Да"), ("False", "Нет")]
        else:
            choices_pairs = [(v.strip(), v.strip()) for v in choice_param_field.choices.split(",") if v.strip()]
        lines.append(f"    filter_choice = ChoiceParam({choice_param_field.label!r}, {choices_pairs!r})")
    if text_param_field is not None:
        imports.add("StringParam")
        lines.append(f"    filter_text = StringParam({text_param_field.label!r})")

    lines.extend(("", "    def execute(self, db: Database, **params: object) -> list[dict[str, object]]:"))
    filter_parts: list[str] = []
    if config.posted_only:
        filter_parts.append("'posted': True")
    lines.append(f"        filters: dict[str, object] = {{{', '.join(filter_parts)}}}")
    if config.date_filter:
        lines.extend((
            "        date_from = params.get('date_from')",
            "        date_to = params.get('date_to')",
            "        start = date_from if isinstance(date_from, datetime.date) else None",
            "        end = date_to if isinstance(date_to, datetime.date) else None",
            f"        if (start is not None or end is not None) and db.can_read_field({config.entity}, {config.date_filter!r}):",
            f"            filters[{f'{config.date_filter}__date_range'!r}] = (start, end)",
        ))
    ref_in_row = bool(config.ref_filter and config.ref_filter in row_fields)
    if config.ref_filter:
        lines.append("        selected = params.get('selected')")
        if not ref_in_row:
            lines.extend((
                f"        if isinstance(selected, int) and db.can_read_field({config.entity}, {config.ref_filter!r}):",
                f"            filters[{config.ref_filter!r}] = selected",
            ))
    choice_in_row = bool(choice_param_field is not None and config.choice_filter_field in row_fields)
    if choice_param_field is not None:
        lines.append("        filter_choice = params.get('filter_choice')")
        if not choice_in_row:
            if choice_param_field.kind == "Boolean":
                lines.extend((
                    f"        if filter_choice in ('True', 'False') and db.can_read_field({config.entity}, {choice_param_field.name!r}):",
                    f"            filters[{choice_param_field.name!r}] = (filter_choice == 'True')",
                ))
            else:
                lines.extend((
                    f"        if isinstance(filter_choice, str) and filter_choice and db.can_read_field({config.entity}, {choice_param_field.name!r}):",
                    f"            filters[{choice_param_field.name!r}] = filter_choice",
                ))
    text_in_row = bool(text_param_field is not None and config.text_filter_field in row_fields)
    if text_param_field is not None:
        lines.append("        filter_text_raw = params.get('filter_text')")
        lines.append("        filter_text_q = filter_text_raw.strip().casefold() if isinstance(filter_text_raw, str) else ''")

    sort_field = config.order_by.removeprefix("-") if config.order_by else ""
    sort_in_row = bool(sort_field and sort_field in row_fields)
    ordering = ""
    if config.order_by and not sort_in_row:
        lines.append(
            f"        order = {config.order_by!r} if db.can_read_field({config.entity}, {sort_field!r}) else None"
        )
        ordering = ", order_by=order"
    include_tp = ", include_table_parts=True" if config.table_part else ""
    lines.append(f"        records = db.filter({config.entity}{ordering}{include_tp}, **filters)")

    references = {col: fields[col].target for col in config.columns if fields[col].kind == "Ref"}
    if references:
        lines.append("        labels = {")
        for col, target in references.items():
            lines.append(f"            {col!r}: {{item.id: str(item) for item in db.list({target})}},")
        lines.append("        }")
    lines.extend(("        rows: list[dict[str, object]] = []", "        for record in records:"))
    if config.table_part:
        if text_param_field is not None and not text_in_row:
            lines.extend((
                f"            if filter_text_q and db.can_read_field({config.entity}, {text_param_field.name!r}):",
                f"                if filter_text_q not in str(getattr(record, {text_param_field.name!r}) or '').casefold():",
                "                    continue",
            ))
        lines.append(f"            for line in getattr(record, {config.table_part!r}):")
        if ref_in_row:
            lines.extend((
                f"                if isinstance(selected, int) and db.can_read_field({config.entity}, {f'{config.table_part}.{config.ref_filter}'!r}) and line.get({config.ref_filter!r}) != selected:",
                "                    continue",
            ))
        if choice_param_field is not None and choice_in_row:
            path = f"{config.table_part}.{choice_param_field.name}"
            if choice_param_field.kind == "Boolean":
                lines.extend((
                    f"                if filter_choice in ('True', 'False') and db.can_read_field({config.entity}, {path!r}) and bool(line.get({choice_param_field.name!r})) != (filter_choice == 'True'):",
                    "                    continue",
                ))
            else:
                lines.extend((
                    f"                if isinstance(filter_choice, str) and filter_choice and db.can_read_field({config.entity}, {path!r}) and line.get({choice_param_field.name!r}) != filter_choice:",
                    "                    continue",
                ))
        if text_param_field is not None and text_in_row:
            path = f"{config.table_part}.{text_param_field.name}"
            lines.extend((
                f"                if filter_text_q and db.can_read_field({config.entity}, {path!r}) and filter_text_q not in str(line.get({text_param_field.name!r}) or '').casefold():",
                "                    continue",
            ))
        indent = "                "
        row_items = ", ".join(
            f"{col!r}: line.get({col!r})" if col in row_fields else f"{col!r}: getattr(record, {col!r})"
            for col in config.columns
        )
        lines.append(f"{indent}row: dict[str, object] = {{{row_items}}}")
    else:
        if text_param_field is not None:
            lines.extend((
                f"            if filter_text_q and db.can_read_field({config.entity}, {text_param_field.name!r}):",
                f"                if filter_text_q not in str(getattr(record, {text_param_field.name!r}) or '').casefold():",
                "                    continue",
            ))
        indent = "            "
        lines.append(
            f"{indent}row: dict[str, object] = {{name: getattr(record, name) for name in {config.columns!r}}}"
        )
    if references:
        default_refs = tuple(name for name in config.group_by if name in references)
        lines.extend((
            f"{indent}group_refs = {tuple(references)!r} if self._interactive_grouping else {default_refs!r}",
            f"{indent}if group_refs:",
            f"{indent}    row['_group_keys'] = {{name: row[name] for name in group_refs}}",
            f"{indent}for name, choices in labels.items():",
            f"{indent}    value = row[name]",
            f"{indent}    if isinstance(value, int):",
            f"{indent}        row[name] = choices.get(value, value)",
        ))
    for style_rule in config.row_styles:
        s_field = style_rule["field"]
        s_val = style_rule.get("value", "")
        in_row = s_field in row_fields
        field_path = f"{config.table_part}.{s_field}" if in_row else s_field
        raw_expr = f"line.get({s_field!r})" if in_row else f"getattr(record, {s_field!r})"
        f_def = fields[s_field]
        if f_def.kind == "Boolean":
            expected = s_val.strip().casefold() in ("true", "да", "1")
            cond = f"bool({raw_expr}) is {expected!r}"
        else:
            cond = f"str({raw_expr} if {raw_expr} is not None else '') == {s_val!r}"
        lines.append(f"{indent}if db.can_read_field({config.entity}, {field_path!r}) and {cond}:")
        if style_rule.get("style"):
            lines.append(f"{indent}    row['_style'] = {style_rule['style']!r}")
        if style_rule.get("color", "").strip():
            lines.append(f"{indent}    row['_color'] = {style_rule['color'].strip()!r}")
        if style_rule.get("bold", "") in ("1", "True", "true"):
            lines.append(f"{indent}    row['_bold'] = True")
    lines.extend((
        f"{indent}row.update(_entity_class={config.entity}, _entity_id=record.id)",
        f"{indent}rows.append(row)",
    ))
    if sort_in_row and config.order_by:
        reverse_flag = "True" if config.order_by.startswith("-") else "False"
        lines.extend((
            f"        if db.can_read_field({config.entity}, {f'{config.table_part}.{sort_field}'!r}):",
            f"            known = [row for row in rows if row.get({sort_field!r}) is not None]",
            f"            known.sort(key=lambda row: row[{sort_field!r}], reverse={reverse_flag})",
            f"            rows = known + [row for row in rows if row.get({sort_field!r}) is None]",
        ))
    lines.append("        return rows")
    return "\n".join(lines)


_ARITHMETIC_OPS = ("+", "-", "*", "/")
_COMPARE_OPS = ("==", "!=", "<", "<=", ">", ">=")
_TOTAL_OPS = ("sum", "avg", "count", "min", "max")


def _calc_split_target(target: str) -> tuple[str, str]:
    part, sep, col = target.partition(".")
    return (part, col) if sep else ("", target)


def _quantize_expr(expr: str, target_field: StudioField) -> str:
    if target_field.kind == "Integer":
        return f"int({expr})"
    scale = max(0, target_field.scale)
    if scale == 0:
        return f"({expr}).quantize(_Dec('1'), rounding=ROUND_HALF_UP)"
    return f"({expr}).quantize(_Dec('0.' + '0' * {scale}), rounding=ROUND_HALF_UP)"


def calculations_and_validations_source(
    entity: StudioEntity,
    entities: dict[str, StudioEntity],
    registers: dict[str, StudioRegister],
    imports: set[str],
) -> tuple[list[str], list[str]]:
    """Generate on_validate and before_save method lines from visual calculation and validation rules."""
    if not entity.calculations and not entity.validations:
        return [], []
    header_fields = {f.name: f for f in entity.fields}
    if entity.kind == "Документ":
        header_fields.setdefault("date", StudioField("date", "Дата", "DateTime"))
        header_fields.setdefault("number", StudioField("number", "Номер", "String"))
        header_fields.setdefault("posted", StudioField("posted", "Проведён", "Boolean"))
    part_fields = {p.name: {f.name: f for f in p.fields} for p in entity.table_parts}
    part_labels = {p.name: p.label for p in entity.table_parts}

    validate_body: list[str] = []
    before_body: list[str] = []
    needs_decimal = False

    def numeric_operand(expr_name: str, container: str, fields_map: dict[str, StudioField], label: str) -> str:
        if expr_name in fields_map:
            f_def = fields_map[expr_name]
            if f_def.kind not in ("Integer", "Decimal"):
                raise ValueError(f"{entity.label}: поле «{f_def.label}» в {label} должно быть числовым.")
            return f"_Dec(str({container}.get({expr_name!r}) or 0))"
        try:
            num = Decimal(expr_name.strip())
            if not num.is_finite():
                raise ValueError
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{entity.label}: некорректный операнд «{expr_name}» в {label}.") from exc
        return f"_Dec({str(num)!r})"

    # 1. Validations
    for idx, rule in enumerate(entity.validations):
        msg = rule.message.strip()
        if rule.kind == "require_rows":
            if rule.target not in part_fields:
                raise ValueError(f"{entity.label}: табличная часть «{rule.target}» не найдена в проверке.")
            err = msg or f"Добавьте хотя бы одну строку в «{part_labels[rule.target]}»."
            validate_body.extend((
                f"        if not self._table_data.get({rule.target!r}):",
                f"            errors.append({err!r})",
            ))
        elif rule.kind == "unique_rows":
            if rule.target not in part_fields or rule.column not in part_fields[rule.target]:
                raise ValueError(f"{entity.label}: проверьте таблицу и колонку для проверки уникальности.")
            col_label = part_fields[rule.target][rule.column].label
            err = msg or f"В таблице «{part_labels[rule.target]}» повторяется «{col_label}»."
            seen_var = f"_seen_{idx}"
            validate_body.extend((
                f"        {seen_var}: set[object] = set()",
                f"        for _row in self._table_data.get({rule.target!r}, []):",
                f"            _val = _row.get({rule.column!r})",
                "            if _val not in (None, ''):",
                f"                if _val in {seen_var}:",
                f"                    errors.append({err!r})",
                "                    break",
                f"                {seen_var}.add(_val)",
            ))
        elif rule.kind == "compare":
            if rule.operator not in _COMPARE_OPS:
                raise ValueError(f"{entity.label}: неизвестный оператор сравнения «{rule.operator}».")
            left_field = header_fields.get(rule.target)
            if left_field is None:
                raise ValueError(f"{entity.label}: поле «{rule.target}» для проверки не найдено.")
            err = msg or f"Проверьте значение поля «{left_field.label}»."
            other = rule.other.strip()
            if "." in other:
                ref_name, _, remote_name = other.partition(".")
                ref_field = header_fields.get(ref_name)
                if ref_field is None or ref_field.kind != "Ref":
                    raise ValueError(f"{entity.label}: поле «{ref_name}» должно быть ссылкой.")
                target_ent = entities.get(ref_field.target)
                if target_ent is None or remote_name not in _target_field_names(target_ent):
                    raise ValueError(f"{entity.label}: поле «{remote_name}» не найдено в «{ref_field.target}».")
                imports.add("Database")
                before_body.extend((
                    f"        _ref_id_{idx} = self._data.get({ref_name!r})",
                    f"        if _ref_id_{idx} is not None:",
                    f"            _ref_obj_{idx} = db.get({ref_field.target}, _ref_id_{idx})",
                    f"            _lval_{idx} = self._data.get({rule.target!r})",
                    f"            _rval_{idx} = _ref_obj_{idx}._data.get({remote_name!r}) if _ref_obj_{idx} is not None else None",
                    f"            if _lval_{idx} is not None and _rval_{idx} is not None:",
                    f"                if hasattr(_lval_{idx}, 'date') != hasattr(_rval_{idx}, 'date'):",
                    f"                    _lcmp_{idx} = _lval_{idx}.date() if hasattr(_lval_{idx}, 'date') else _lval_{idx}",
                    f"                    _rcmp_{idx} = _rval_{idx}.date() if hasattr(_rval_{idx}, 'date') else _rval_{idx}",
                    "                else:",
                    f"                    _lcmp_{idx}, _rcmp_{idx} = _lval_{idx}, _rval_{idx}",
                    f"                if not (_lcmp_{idx} {rule.operator} _rcmp_{idx}):",
                    f"                    raise ValueError({err!r})",
                ))
            elif other in header_fields:
                right_field = header_fields[other]
                same_date = left_field.kind in ("Date", "DateTime") and right_field.kind in ("Date", "DateTime")
                same_num = left_field.kind in ("Integer", "Decimal") and right_field.kind in ("Integer", "Decimal")
                if left_field.kind != right_field.kind and not same_date and not same_num:
                    raise ValueError(f"{entity.label}: сравниваемые поля «{left_field.label}» и «{right_field.label}» несовместимы.")
                validate_body.extend((
                    f"        _lv_{idx} = self._data.get({rule.target!r})",
                    f"        _rv_{idx} = self._data.get({other!r})",
                    f"        if _lv_{idx} is not None and _rv_{idx} is not None:",
                    f"            if hasattr(_lv_{idx}, 'date') != hasattr(_rv_{idx}, 'date'):",
                    f"                _lc_{idx} = _lv_{idx}.date() if hasattr(_lv_{idx}, 'date') else _lv_{idx}",
                    f"                _rc_{idx} = _rv_{idx}.date() if hasattr(_rv_{idx}, 'date') else _rv_{idx}",
                    "            else:",
                    f"                _lc_{idx}, _rc_{idx} = _lv_{idx}, _rv_{idx}",
                    f"            if not (_lc_{idx} {rule.operator} _rc_{idx}):",
                    f"                errors.append({err!r})",
                ))
            else:
                if left_field.kind == "Integer":
                    try:
                        lit: object = int(other)
                    except ValueError as exc:
                        raise ValueError(f"{entity.label}: укажите целое число для сравнения с «{left_field.label}».") from exc
                    lit_expr = repr(lit)
                elif left_field.kind == "Decimal":
                    try:
                        dec = Decimal(other)
                        if not dec.is_finite():
                            raise ValueError
                    except (InvalidOperation, ValueError) as exc:
                        raise ValueError(f"{entity.label}: укажите число для сравнения с «{left_field.label}».") from exc
                    imports.add("DecimalValue")
                    lit_expr = f"DecimalValue({other!r})"
                elif left_field.kind in ("Date", "DateTime"):
                    imports.add("datetime")
                    try:
                        if left_field.kind == "Date":
                            datetime.date.fromisoformat(other)
                            lit_expr = f"datetime.date.fromisoformat({other!r})"
                        else:
                            datetime.datetime.fromisoformat(other)
                            lit_expr = f"datetime.datetime.fromisoformat({other!r})"
                    except ValueError as exc:
                        raise ValueError(f"{entity.label}: укажите дату в формате ISO (ГГГГ-ММ-ДД).") from exc
                else:
                    lit_expr = repr(other)
                validate_body.extend((
                    f"        _lv_{idx} = self._data.get({rule.target!r})",
                    f"        if _lv_{idx} is not None and not (_lv_{idx} {rule.operator} {lit_expr}):",
                    f"            errors.append({err!r})",
                ))
        else:
            raise ValueError(f"{entity.label}: неизвестный тип проверки «{rule.kind}».")

    # 2. Calculations ordered: lookup -> row_formula -> table_total -> header_formula
    order_rank = {"lookup": 0, "row_formula": 1, "table_total": 2, "header_formula": 3}
    ordered_calcs = sorted(entity.calculations, key=lambda c: order_rank.get(c.kind, 9))
    calc_lines: list[str] = []
    for idx, calc in enumerate(ordered_calcs):
        if calc.kind == "lookup":
            reg = registers.get(calc.register)
            if reg is None or reg.kind != "Сведения":
                raise ValueError(f"{entity.label}: для подстановки выберите регистр сведений.")
            res_member = next((m for m in reg.members if m.kind == "Ресурс" and m.name == calc.resource), None)
            if res_member is None:
                raise ValueError(f"{entity.label}: ресурс «{calc.resource}» не найден в регистре «{reg.name}».")
            part_name, col_name = _calc_split_target(calc.target)
            if part_name:
                if part_name not in part_fields or col_name not in part_fields[part_name]:
                    raise ValueError(f"{entity.label}: целевая колонка «{calc.target}» не найдена.")
                target_field = part_fields[part_name][col_name]
                container = "row"
                fields_scope = movement_fields(entity, part_name)
            else:
                if col_name not in header_fields:
                    raise ValueError(f"{entity.label}: целевое поле «{col_name}» не найдено.")
                target_field = header_fields[col_name]
                container = "self._data"
                fields_scope = movement_fields(entity, "")
            dim_members = {m.name: m for m in reg.members if m.kind == "Измерение"}
            if set(calc.dimension_map) - dim_members.keys():
                raise ValueError(f"{entity.label}: обновите сопоставление измерений для «{reg.name}».")
            dim_pairs: list[str] = []
            req_checks: list[str] = []
            for d_name, d_member in dim_members.items():
                src = calc.dimension_map.get(d_name, "")
                if not src and not d_member.required:
                    continue
                col_def = fields_scope.get(src)
                if col_def is None or not movement_compatible(d_member, col_def):
                    raise ValueError(f"{entity.label}: выберите совместимое поле для измерения «{d_member.label}».")
                origin, _, f_name = src.partition(":")
                src_cont = "row" if origin == "row" else "self._data"
                expr = f"{src_cont}.get({f_name!r})"
                if d_member.value_type == "Дата" and col_def.kind == "DateTime":
                    expr = f"{expr}.date() if {expr} is not None else None"
                dim_pairs.append(f"{d_name!r}: {expr}")
                req_checks.append(f"{src_cont}.get({f_name!r}) is not None")
            on_date_arg = ""
            if calc.date_field:
                d_field = header_fields.get(calc.date_field)
                if d_field is None or d_field.kind not in ("Date", "DateTime"):
                    raise ValueError(f"{entity.label}: поле даты «{calc.date_field}» не найдено.")
                on_date_arg = f"on_date=self._data.get({calc.date_field!r}), "
            imports.update(("Database", "get_latest"))
            if target_field.kind == "Decimal":
                needs_decimal = True
                val_expr = f"_Dec(str(_rec[{calc.resource!r}]))"
            elif target_field.kind == "Integer":
                val_expr = f"int(_rec[{calc.resource!r}])"
            else:
                val_expr = f"_rec[{calc.resource!r}]"
            cond_empty = f"if {container}.get({col_name!r}) in (None, '', 0):" if calc.only_if_empty else ""
            dim_guard = " and ".join(req_checks) if req_checks else "True"
            kw_args = ", ".join(
                f"{d_name}="
                + (
                    f"{'row' if calc.dimension_map[d_name].startswith('row:') else 'self._data'}"
                    f".get({calc.dimension_map[d_name].split(':', 1)[1]!r})"
                    + (
                        ".date()"
                        if dim_members[d_name].value_type == "Дата"
                        and fields_scope[calc.dimension_map[d_name]].kind == "DateTime"
                        else ""
                    )
                )
                for d_name in dim_members
                if calc.dimension_map.get(d_name)
            )
            if part_name:
                calc_lines.append(f"        for row in self._table_data.get({part_name!r}, []):")
                indent = "            "
            else:
                indent = "        "
            if cond_empty:
                calc_lines.append(f"{indent}{cond_empty}")
                indent += "    "
            calc_lines.append(f"{indent}if {dim_guard}:")
            date_kw = (", " + on_date_arg.rstrip(", ")) if on_date_arg else ""
            dim_kw = (", " + kw_args) if kw_args else ""
            calc_lines.append(f"{indent}    _rec = get_latest(db, {reg.name}{date_kw}{dim_kw})")
            calc_lines.extend((
                f"{indent}    if _rec is not None and _rec.get({calc.resource!r}) is not None:",
                f"{indent}        {container}[{col_name!r}] = {val_expr}",
            ))
        elif calc.kind == "row_formula":
            part_name, col_name = _calc_split_target(calc.target)
            part_name = part_name or calc.source_part
            if part_name not in part_fields or col_name not in part_fields[part_name]:
                raise ValueError(f"{entity.label}: целевая колонка «{calc.target}» не найдена.")
            cols_map = part_fields[part_name]
            target_field = cols_map[col_name]
            if target_field.kind not in ("Integer", "Decimal") or calc.operator not in _ARITHMETIC_OPS:
                raise ValueError(f"{entity.label}: проверьте формулу для «{calc.target}».")
            needs_decimal = True
            imports.add("Database")
            l_expr = numeric_operand(calc.left, "row", cols_map, f"«{target_field.label}»")
            r_expr = numeric_operand(calc.right, "row", cols_map, f"«{target_field.label}»")
            calc_lines.extend((
                f"        for row in self._table_data.get({part_name!r}, []):",
                f"            _lhs = {l_expr}",
                f"            _rhs = {r_expr}",
            ))
            if calc.operator == "/":
                calc_lines.extend((
                    "            if _rhs == 0:",
                    f"                raise ValueError({f'Деление на ноль при расчёте «{target_field.label}».'!r})",
                ))
            calc_lines.append(f"            row[{col_name!r}] = {_quantize_expr(f'_lhs {calc.operator} _rhs', target_field)}")
        elif calc.kind == "table_total":
            if calc.target not in header_fields or calc.source_part not in part_fields:
                raise ValueError(f"{entity.label}: проверьте цель и таблицу для итога «{calc.target}».")
            target_field = header_fields[calc.target]
            if target_field.kind not in ("Integer", "Decimal") or calc.operator not in _TOTAL_OPS:
                raise ValueError(f"{entity.label}: проверьте операцию итога для «{target_field.label}».")
            imports.add("Database")
            rows_var = f"_rows_{idx}"
            calc_lines.append(f"        {rows_var} = self._table_data.get({calc.source_part!r}, [])")
            if calc.operator == "count":
                val_expr = f"len({rows_var})" if target_field.kind == "Integer" else f"_Dec(len({rows_var}))"
                if target_field.kind == "Decimal":
                    needs_decimal = True
                calc_lines.append(f"        self._data[{calc.target!r}] = {val_expr}")
            else:
                cols_map = part_fields[calc.source_part]
                if calc.left not in cols_map or cols_map[calc.left].kind not in ("Integer", "Decimal"):
                    raise ValueError(f"{entity.label}: выберите числовую колонку для итога «{target_field.label}».")
                needs_decimal = True
                vals_var = f"_vals_{idx}"
                calc_lines.append(
                    f"        {vals_var} = [_Dec(str(r.get({calc.left!r}))) for r in {rows_var} if r.get({calc.left!r}) is not None]"
                )
                if calc.operator == "sum":
                    res_expr = f"sum({vals_var}, _Dec('0'))"
                elif calc.operator == "avg":
                    res_expr = f"(sum({vals_var}, _Dec('0')) / len({vals_var}) if {vals_var} else _Dec('0'))"
                elif calc.operator == "min":
                    res_expr = f"(min({vals_var}) if {vals_var} else _Dec('0'))"
                else:
                    res_expr = f"(max({vals_var}) if {vals_var} else _Dec('0'))"
                calc_lines.append(f"        self._data[{calc.target!r}] = {_quantize_expr(res_expr, target_field)}")
        elif calc.kind == "header_formula":
            if calc.target not in header_fields:
                raise ValueError(f"{entity.label}: целевое поле «{calc.target}» не найдено.")
            target_field = header_fields[calc.target]
            if target_field.kind not in ("Integer", "Decimal") or calc.operator not in _ARITHMETIC_OPS:
                raise ValueError(f"{entity.label}: проверьте формулу для «{target_field.label}».")
            needs_decimal = True
            imports.add("Database")
            l_expr = numeric_operand(calc.left, "self._data", header_fields, f"«{target_field.label}»")
            r_expr = numeric_operand(calc.right, "self._data", header_fields, f"«{target_field.label}»")
            calc_lines.extend((
                f"        _lhs_{idx} = {l_expr}",
                f"        _rhs_{idx} = {r_expr}",
            ))
            if calc.operator == "/":
                calc_lines.extend((
                    f"        if _rhs_{idx} == 0:",
                    f"            raise ValueError({f'Деление на ноль при расчёте «{target_field.label}».'!r})",
                ))
            calc_lines.append(
                f"        self._data[{calc.target!r}] = {_quantize_expr(f'_lhs_{idx} {calc.operator} _rhs_{idx}', target_field)}"
            )
        else:
            raise ValueError(f"{entity.label}: неизвестный тип вычисления «{calc.kind}».")

    on_validate_lines: list[str] = []
    if validate_body:
        on_validate_lines = ["", "    def on_validate(self) -> list[str]:", "        errors: list[str] = []", *validate_body, "        return errors"]

    before_save_lines: list[str] = []
    combined_before = [*calc_lines, *before_body]
    if combined_before:
        imports.add("Database")
        header = ["", "    def before_save(self, db: Database) -> None:"]
        if needs_decimal:
            header.append("        from decimal import Decimal as _Dec, ROUND_HALF_UP")
        before_save_lines = [*header, *combined_before]

    return on_validate_lines, before_save_lines


def read_workflow(raw: dict | None) -> StudioWorkflow | None:
    if raw is None:
        return None
    return StudioWorkflow(**{
        **raw, "transitions": [StudioTransition(**item) for item in raw.get("transitions", [])],
    })


@dataclass
class StudioSeed:
    key: str
    entity: str
    values: dict[str, str] = field(default_factory=dict)
    tables: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    post: bool = False


@dataclass
class StudioReminder:
    entity: str
    date_field: str = "deadline"
    done_field: str = "is_completed"
    title_field: str = "name"
    notify_before_minutes: int = 60


def initial_data_source(
    seeds: list[StudioSeed], entities: list[StudioEntity], imports: set[str],
) -> str:
    if not seeds:
        return ""
    by_key = {record.key: record for record in seeds}
    by_entity = {item.name: item for item in entities}
    if len(by_key) != len(seeds) or any(not key.strip() for key in by_key):
        raise ValueError("Ключи начальных записей должны быть непустыми и уникальными.")
    imports.add("Database")
    order: TopologicalSorter[str] = TopologicalSorter()
    statements: dict[str, str] = {}
    needs_ids = False

    def values_source(
        values: dict[str, str], fields: list[StudioField], label: str, dependencies: list[str],
    ) -> str:
        known = {item.name: item for item in fields}
        values_code = []
        for name, raw in values.items():
            item = known.get(name)
            if item is None:
                raise ValueError(f"{label}: поле «{name}» не найдено.")
            if item.kind == "Ref":
                target = by_key.get(raw)
                if target is None or target.entity != item.target:
                    raise ValueError(f"{label}: укажите ключ записи типа «{item.target}» для «{name}».")
                dependencies.append(raw)
                expression = f"created[{raw!r}]"
            elif item.kind == "Boolean":
                options = {"true": True, "false": False, "да": True, "нет": False, "1": True, "0": False}
                if raw.strip().casefold() not in options:
                    raise ValueError(f"{label}: для «{name}» укажите Да или Нет.")
                expression = repr(options[raw.strip().casefold()])
            else:
                parsers = {
                    "Integer": int, "Decimal": Decimal, "Date": datetime.date.fromisoformat,
                    "DateTime": datetime.datetime.fromisoformat, "Time": datetime.time.fromisoformat,
                }
                parser = parsers.get(item.kind)
                try:
                    if parser is not None:
                        parsed = parser(raw)
                        if isinstance(parsed, Decimal) and not parsed.is_finite():
                            raise ValueError
                    if item.kind == "Choice":
                        allowed = {value.strip() for value in item.choices.split(",") if value.strip()}
                        selected = [part.strip() for part in raw.split("|") if part.strip()] if item.multi else [raw]
                        if not selected or not set(selected).issubset(allowed):
                            raise ValueError
                except (ValueError, InvalidOperation) as exc:
                    raise ValueError(f"{label}: проверьте значение поля «{item.label}».") from exc
                expression = repr(raw)
            values_code.append(f"{name!r}: {expression}")
        return "{" + ", ".join(values_code) + "}"

    for record in seeds:
        entity = by_entity.get(record.entity)
        if entity is None:
            raise ValueError(f"{record.key}: объект начальной записи не найден.")
        if record.post and entity.kind != "Документ":
            raise ValueError(f"Начальная запись «{record.key}»: проведение доступно только для документов.")
        dependencies: list[str] = []
        label = f"Начальная запись «{record.key}»"

        header = list(entity.fields)
        if entity.kind == "Документ":
            header.append(StudioField("date", "Дата", "DateTime"))
        arguments = [f"**{values_source(record.values, header, label, dependencies)}"] if record.values else []
        parts = {item.name: item.fields for item in entity.table_parts}
        for part, rows in record.tables.items():
            if part not in parts:
                raise ValueError(f"{label}: таблица «{part}» не найдена.")
            items = ", ".join(values_source(row, parts[part], label, dependencies) for row in rows)
            arguments.append(f"{part}=[{items}]")
        order.add(record.key, *dependencies)
        needs_ids |= bool(dependencies)
        call = "db.post" if record.post else "db.save"
        extra = "" if record.post else ", validate=True"
        statements[record.key] = f"{call}({record.entity}({', '.join(arguments)}){extra})"
    try:
        sorted_keys = tuple(order.static_order())
    except CycleError as exc:
        raise ValueError("Начальные записи не могут ссылаться друг на друга по кругу.") from exc

    lines = ["def _seed_initial_data(db: Database) -> None:"]
    if needs_ids:
        lines.append("    created: dict[str, int] = {}")
    for key in sorted_keys:
        action = statements[key]
        if needs_ids:
            lines.extend((f"    item = {action}", "    assert item.id is not None", f"    created[{key!r}] = item.id"))
        else:
            lines.append(f"    {action}")
    return "\n".join(lines)


def _rename_key[Value](mapping: dict[str, Value], old: str, new: str) -> None:
    if old in mapping:
        items = [(new if key == old else key, value) for key, value in mapping.items()]
        mapping.clear()
        mapping.update(items)


@dataclass
class StudioProject:
    title: str = "Моё приложение"
    app_id: str = "my-app"
    entities: list[StudioEntity] = field(default_factory=list)
    logic: str = ""
    imports: str = ""
    reports: list[StudioReport] = field(default_factory=list)
    registers: list[StudioRegister] = field(default_factory=list)
    preview_role: str = ""
    local_accounts: bool = False
    initial_data: list[StudioSeed] = field(default_factory=list)
    column_renames: dict[str, dict[str, str]] = field(default_factory=dict)
    table_part_renames: dict[str, dict[str, str]] = field(default_factory=dict)
    start_target: str = ""
    window_width: int = 1200
    window_height: int = 800
    reminders: list[StudioReminder] = field(default_factory=list)
    working_db: str = ""

    def all_roles(self) -> list[str]:
        """Collect distinct non-admin role names configured across the project."""
        roles: list[str] = []
        seen: set[str] = set()

        def add(values: list[str] | None) -> None:
            for role in values or ():
                cleaned = role.strip()
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    roles.append(cleaned)

        for entity in self.entities:
            for item in entity.fields:
                add(item.read_roles)
                add(item.write_roles)
            for part in entity.table_parts:
                for item in part.fields:
                    add(item.read_roles)
                    add(item.write_roles)
            if entity.permissions:
                for allowed in entity.permissions.values():
                    add(allowed)
            for allowed in entity.command_roles.values():
                add(allowed)
            if entity.workflow:
                for step in entity.workflow.transitions:
                    add(step.roles)
        return roles

    def all_sections(self) -> list[str]:
        """Collect distinct non-empty navigation sections across entities and reports."""
        sections: list[str] = []
        seen: set[str] = set()
        for item in (*self.entities, *self.reports):
            section = item.section.strip()
            if section and section not in seen:
                seen.add(section)
                sections.append(section)
        return sections

    def column_rename_targets(self) -> dict[str, list[str]]:
        targets: dict[str, list[str]] = {}
        for entity in self.entities:
            targets[entity.name] = [item.name for item in entity.fields]
            targets.update({
                f"{entity.name}.{part.name}": [item.name for item in part.fields]
                for part in entity.table_parts
            })
        return targets

    def table_part_rename_targets(self) -> dict[str, list[str]]:
        return {entity.name: [part.name for part in entity.table_parts] for entity in self.entities}

    def validate_column_renames(self, renames: dict[str, dict[str, str]]) -> None:
        self._validate_renames(renames, self.column_rename_targets())

    def validate_table_part_renames(self, renames: dict[str, dict[str, str]]) -> None:
        self._validate_renames(renames, self.table_part_rename_targets())

    @staticmethod
    def _validate_renames(renames: dict[str, dict[str, str]], available: dict[str, list[str]]) -> None:
        for name, changes in renames.items():
            columns = available.get(name)
            targets = set(changes.values())
            if columns is None:
                raise ValueError(f"Миграция: объект «{name}» не найден.")
            if (
                len(targets) != len(changes) or not targets.issubset(columns)
                or set(changes).intersection(columns) or any(not identifier(old) for old in changes)
            ):
                raise ValueError(f"Миграция «{name}»: проверьте старые и новые имена.")

    def rename_field_references(self, entity: StudioEntity, part: str, old: str, new: str) -> None:
        if old == new:
            return
        key = f"{entity.name}.{part}" if part else entity.name
        for previous, target in self.column_renames.get(key, {}).items():
            if target == old:
                self.column_renames[key][previous] = new
        if not part:
            for item in entity.fields:
                if item.visibility_field == old:
                    item.visibility_field = new
                if item.auto_fill == old:
                    item.auto_fill = new
                for target, source in item.choice_filter.items():
                    if source == old:
                        item.choice_filter[target] = new
            entity.list_columns = [new if name == old else name for name in entity.list_columns]
            if entity.list_order.removeprefix("-") == old:
                entity.list_order = ("-" if entity.list_order.startswith("-") else "") + new
            if entity.numbering_field == old:
                entity.numbering_field = new
            for reminder in self.reminders:
                if reminder.entity == entity.name:
                    if reminder.date_field == old:
                        reminder.date_field = new
                    if reminder.done_field == old:
                        reminder.done_field = new
                    if reminder.title_field == old:
                        reminder.title_field = new
            for report in self.reports:
                if report.source_kind == "entity" and report.entity == entity.name:
                    report.columns = [new if name == old else name for name in report.columns]
                    report.group_by = [new if name == old else name for name in report.group_by]
                    if report.ref_filter == old:
                        report.ref_filter = new
                    if report.date_filter == old:
                        report.date_filter = new
                    if report.order_by.removeprefix("-") == old:
                        report.order_by = ("-" if report.order_by.startswith("-") else "") + new
                    if report.choice_filter_field == old:
                        report.choice_filter_field = new
                    if report.text_filter_field == old:
                        report.text_filter_field = new
                    if report.chart_label == old:
                        report.chart_label = new
                    if report.chart_value == old:
                        report.chart_value = new
                    for style_rule in report.row_styles:
                        if style_rule.get("field") == old:
                            style_rule["field"] = new
                    _rename_key(report.column_options, old, new)
        else:
            part_obj = next((item for item in entity.table_parts if item.name == part), None)
            if part_obj is not None:
                for item in part_obj.fields:
                    if item.auto_fill == old:
                        item.auto_fill = new
            for report in self.reports:
                if report.source_kind == "entity" and report.entity == entity.name and report.table_part == part:
                    report.columns = [new if name == old else name for name in report.columns]
                    report.group_by = [new if name == old else name for name in report.group_by]
                    if report.ref_filter == old:
                        report.ref_filter = new
                    if report.order_by.removeprefix("-") == old:
                        report.order_by = ("-" if report.order_by.startswith("-") else "") + new
                    if report.choice_filter_field == old:
                        report.choice_filter_field = new
                    if report.text_filter_field == old:
                        report.text_filter_field = new
                    if report.chart_label == old:
                        report.chart_label = new
                    if report.chart_value == old:
                        report.chart_value = new
                    for style_rule in report.row_styles:
                        if style_rule.get("field") == old:
                            style_rule["field"] = new
                    _rename_key(report.column_options, old, new)
        for calc in entity.calculations:
            p_name, c_name = _calc_split_target(calc.target)
            if p_name == part and c_name == old:
                calc.target = f"{part}.{new}" if part else new
            if not part:
                if calc.date_field == old:
                    calc.date_field = new
                if calc.kind == "header_formula":
                    if calc.left == old:
                        calc.left = new
                    if calc.right == old:
                        calc.right = new
            elif calc.source_part == part:
                if calc.left == old:
                    calc.left = new
                if calc.right == old:
                    calc.right = new
            if calc.dimension_map:
                pref = f"{'row' if part else 'header'}:{old}"
                new_pref = f"{'row' if part else 'header'}:{new}"
                for d_key, d_val in calc.dimension_map.items():
                    if d_val == pref:
                        calc.dimension_map[d_key] = new_pref
        for val in entity.validations:
            if not part:
                if val.kind == "compare":
                    if val.target == old:
                        val.target = new
                    if val.other == old:
                        val.other = new
            elif val.kind == "unique_rows" and val.target == part and val.column == old:
                val.column = new
        if not part and entity.form is not None:
            _rename_form_field(entity.form.items, old, new)
        for reg in self.registers:
            for member in reg.members:
                if not part and member.target == entity.name and member.representation_field == old:
                    member.representation_field = new
        if entity.print_form is not None:
            fields = entity.print_form.tables.get(part, []) if part else entity.print_form.fields
            fields[:] = [new if name == old else name for name in fields]
        for seed in self.initial_data:
            if seed.entity != entity.name:
                continue
            if part:
                for row in seed.tables.get(part, []):
                    _rename_key(row, old, new)
            else:
                _rename_key(seed.values, old, new)
        prefix = "row:" if part else "header:"
        for rule in entity.movements:
            if not part or rule.table_part == part:
                for member, source in rule.mapping.items():
                    if source == prefix + old:
                        rule.mapping[member] = prefix + new
        for destination in self.entities:
            fields = destination.fields + [item for tp in destination.table_parts for item in tp.fields]
            for link in fields:
                if not part:
                    if destination is entity:
                        _rename_key(link.basis_fields, old, new)
                    if link.target == entity.name:
                        if link.representation_field == old:
                            link.representation_field = new
                        if link.auto_fill == old:
                            link.auto_fill = new
                        _rename_key(link.choice_filter, old, new)
                        for target, source in link.basis_fields.items():
                            if source == old:
                                link.basis_fields[target] = new
                else:
                    for table, mapping in link.basis_tables.items():
                        if destination is entity and table == part:
                            _rename_key(mapping["columns"], old, new)
                        if link.target == entity.name and mapping["source"] == part:
                            for target, source in mapping["columns"].items():
                                if source == old:
                                    mapping["columns"][target] = new

    def rename_table_references(self, entity: StudioEntity, old: str, new: str) -> None:
        if old == new:
            return
        _rename_key(self.column_renames, f"{entity.name}.{old}", f"{entity.name}.{new}")
        for previous, target in self.table_part_renames.get(entity.name, {}).items():
            if target == old:
                self.table_part_renames[entity.name][previous] = new
        for rule in entity.movements:
            if rule.table_part == old:
                rule.table_part = new
        for calc in entity.calculations:
            p_name, c_name = _calc_split_target(calc.target)
            if p_name == old:
                calc.target = f"{new}.{c_name}"
            if calc.source_part == old:
                calc.source_part = new
        for val in entity.validations:
            if val.kind in ("require_rows", "unique_rows") and val.target == old:
                val.target = new
        if entity.form is not None:
            _rename_form_table(entity.form.items, old, new)
        for report in self.reports:
            if report.source_kind == "entity" and report.entity == entity.name and report.table_part == old:
                report.table_part = new
        if entity.print_form is not None:
            _rename_key(entity.print_form.tables, old, new)
        for seed in self.initial_data:
            if seed.entity == entity.name:
                _rename_key(seed.tables, old, new)
        for destination in self.entities:
            for link in destination.fields:
                if destination is entity:
                    _rename_key(link.basis_tables, old, new)
                if link.target == entity.name:
                    for mapping in link.basis_tables.values():
                        if mapping["source"] == old:
                            mapping["source"] = new

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> StudioProject:
        if raw.get("version", 1) != 1:
            raise ValueError("Версия проекта не поддерживается.")
        return cls(
            title=str(raw["title"]), app_id=str(raw["app_id"]), logic=str(raw.get("logic", "")),
            imports=str(raw.get("imports", "")),
            preview_role=str(raw.get("preview_role", "")),
            local_accounts=bool(raw.get("local_accounts", False)),
            column_renames=raw.get("column_renames", {}),  # type: ignore[arg-type]
            table_part_renames=raw.get("table_part_renames", {}),  # type: ignore[arg-type]
            start_target=str(raw.get("start_target", "")),
            window_width=int(raw.get("window_width", 1200)),  # type: ignore[call-overload]
            window_height=int(raw.get("window_height", 800)),  # type: ignore[call-overload]
            reminders=[StudioReminder(**item) for item in raw.get("reminders", [])],  # type: ignore[attr-defined]
            working_db=str(raw.get("working_db", "")),
            initial_data=[StudioSeed(**item) for item in raw.get("initial_data", [])],  # type: ignore[attr-defined]
            reports=[StudioReport(**item) for item in raw.get("reports", [])],  # type: ignore[attr-defined]
            registers=[
                StudioRegister(item["name"], item["kind"], [StudioRegisterMember(**m) for m in item["members"]])
                for item in raw.get("registers", [])  # type: ignore[attr-defined]
            ],
            entities=[
                StudioEntity(**{
                    **item, "fields": [StudioField(**f) for f in item["fields"]],
                    "print_form": StudioPrintForm(**item["print_form"]) if item.get("print_form") else None,
                    "workflow": read_workflow(item.get("workflow")),
                    "form": read_form_config(item.get("form")),  # type: ignore[arg-type]
                    "form_code": str(item.get("form_code", "")),
                    "movements": [StudioMovement(**m) for m in item.get("movements", [])],
                    "calculations": [StudioCalculation(**c) for c in item.get("calculations", [])],
                    "validations": [StudioValidation(**v) for v in item.get("validations", [])],
                    "table_parts": [
                        StudioTablePart(
                            part["name"], part["label"], [StudioField(**f) for f in part["fields"]],
                            form_tab=part.get("form_tab", ""),
                        )
                        for part in item.get("table_parts", [])
                    ],
                })
                for item in raw["entities"]  # type: ignore[attr-defined]
            ],
        )

    @classmethod
    def read(cls, path: Path) -> StudioProject:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    def to_json(self) -> str:
        return json.dumps({"version": 1, **asdict(self)}, ensure_ascii=False, indent=2) + "\n"

    def write(self, path: Path) -> None:
        from PySide6.QtCore import QIODevice, QSaveFile

        file = QSaveFile(str(path))
        data = self.to_json().encode()
        if not file.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(file.errorString())
        if file.write(data) != len(data) or not file.commit():
            raise OSError(file.errorString())

    def source(
        self, *, preview_database: Path | None = None, backup_has_accounts: bool | None = None,
        spans: list[StudioSourceSpan] | None = None,
    ) -> str:
        if not self.title.strip() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", self.app_id):
            raise ValueError("Укажите заголовок и идентификатор приложения латиницей.")
        if not self.entities:
            raise ValueError("Добавьте хотя бы один объект.")
        if self.window_width < 400 or self.window_height < 300:
            raise ValueError("Размер окна должен быть не меньше 400×300.")
        names = {item.name for item in self.entities}
        if len(names) != len(self.entities):
            raise ValueError("Имена объектов должны быть уникальными.")
        self.validate_column_renames(self.column_renames)
        self.validate_table_part_renames(self.table_part_renames)
        entities = {entity.name: entity for entity in self.entities}
        imports = {"ERPApplication"}
        blocks = []
        block_spans: dict[int, StudioSourceSpan] = {}
        registers = {item.name: item for item in self.registers}
        uses_any = False
        for entity in self.entities:
            if not identifier(entity.name) or not entity.label.strip():
                raise ValueError(f"Некорректное имя или заголовок объекта «{entity.name}».")
            decorator, base = ENTITY_TYPES[entity.kind]
            imports.update((base, decorator))
            lines = [f"@{decorator}", f"class {entity.name}({base}):",
                     f"    __verbose_name__ = {entity.label!r}"]
            if entity.section:
                lines.append(f"    __section__ = {entity.section!r}")
            if entity.check_sequence:
                if entity.kind != "Документ":
                    raise ValueError(f"{entity.label}: контроль последовательности доступен только для документов.")
                lines.append("    __sequence__ = True")
            form_groups = {item.name: item.form_group.strip() for item in entity.fields if item.form_group.strip()}
            if form_groups:
                lines.append(f"    __form_groups__ = {form_groups!r}")
            conditions: dict[str, tuple[str, bool | str]] = {}
            fields_by_name = {item.name: item for item in entity.fields}
            for item in entity.fields:
                if not item.visibility_field:
                    continue
                controller = fields_by_name.get(item.visibility_field)
                if controller is None or controller is item or controller.visibility_field or item.required:
                    raise ValueError(f"{entity.label}, «{item.label}»: проверьте условие видимости необязательного поля.")
                value = item.visibility_value
                if controller.kind == "Boolean":
                    valid = isinstance(value, bool)
                else:
                    valid_codes: set[str] = set()
                    for option in controller.choices.split(","):
                        entry = option.strip()
                        if entry:
                            valid_codes.add(entry.partition(": ")[0].strip() if ": " in entry else entry)
                    valid = controller.kind == "Choice" and not controller.multi and value in valid_codes
                if not valid:
                    raise ValueError(f"{entity.label}, «{item.label}»: выберите допустимое значение условия видимости.")
                conditions[item.name] = (controller.name, value)
            if conditions:
                lines.append(f"    __form_visible_when__ = {conditions!r}")
            conditional_required = tuple(item.name for item in entity.fields if item.required_when_visible)
            if conditional_required:
                if not set(conditional_required) <= conditions.keys():
                    raise ValueError(f"{entity.label}: обязательность по условию требует настройки видимости.")
                lines.append(f"    __required_when_visible__ = {conditional_required!r}")
            form_tabs = {item.name: item.form_tab.strip() for item in entity.fields if item.form_tab.strip()}
            if form_tabs:
                lines.append(f"    __form_tabs__ = {form_tabs!r}")
            table_tabs = {part.name: part.form_tab.strip() for part in entity.table_parts if part.form_tab.strip()}
            if table_tabs:
                lines.append(f"    __form_table_tabs__ = {table_tabs!r}")
            list_fields = entity.list_field_options()
            if entity.list_columns:
                if len(set(entity.list_columns)) != len(entity.list_columns) or not set(entity.list_columns) <= set(list_fields):
                    raise ValueError(f"{entity.label}: проверьте колонки списка.")
                lines.append(f"    __list_columns__ = {tuple(entity.list_columns)!r}")
            if entity.list_order:
                if entity.list_order.removeprefix("-") not in (entity.list_columns or list_fields):
                    raise ValueError(f"{entity.label}: для сортировки выберите видимую колонку.")
                lines.append(f"    __list_order__ = {entity.list_order!r}")
            if entity.numbering_prefix:
                numbering_field = entity.numbering_field or ("number" if base == "Document" else "")
                implicit_number = base == "Document" and numbering_field == "number"
                string_number = any(item.name == numbering_field and item.kind == "String" for item in entity.fields)
                if not implicit_number and not string_number:
                    raise ValueError(f"{entity.label}: для нумерации выберите строковое поле.")
                numbering: dict[str, str] = {"prefix": entity.numbering_prefix, "field": numbering_field}
                pattern = entity.numbering_pattern.strip()
                if pattern:
                    try:
                        if "{seq" not in pattern:
                            raise ValueError("отсутствует {seq}")
                        pattern.format(prefix=entity.numbering_prefix, year=2026, month="01", seq=1)
                    except (ValueError, KeyError, IndexError) as exc:
                        raise ValueError(f"{entity.label}: некорректный шаблон номера «{pattern}».") from exc
                    numbering["pattern"] = pattern
                lines.append(f"    __numbering__ = {numbering!r}")
            used: set[str] = set()
            for item in entity.fields:
                if not identifier(item.name) or item.name in RESERVED_FIELDS or item.name in used:
                    raise ValueError(f"{entity.name}: недопустимое или повторяющееся поле «{item.name}».")
                used.add(item.name)
                if item.basis_fields:
                    source = entities.get(item.target)
                    options = {
                        target.name: {field.name for field in available}
                        for target, available in basis_field_options(entity, source, item.name)
                    } if source is not None else {}
                    if item.kind != "Ref" or not item.create_from.strip() or any(
                        name not in options or source_name not in options[name]
                        for name, source_name in item.basis_fields.items()
                    ):
                        raise ValueError(f"{entity.label}: проверьте перенос полей для «{item.label}».")
                if item.basis_tables:
                    source = entities.get(item.target)
                    if item.kind != "Ref" or not item.create_from.strip() or source is None:
                        raise ValueError(f"{entity.label}: настройте команду создания для «{item.label}».")
                    validate_basis_tables(entity, source, item)
                lines.append(
                    "    " + field_source(item, entity.name, names, imports, entities=entities, siblings=entity.fields)
                )
            field_roles = {item.name: tuple(item.write_roles) for item in entity.fields if item.write_roles is not None}
            field_roles.update({
                f"{part.name}.{item.name}": tuple(item.write_roles)
                for part in entity.table_parts for item in part.fields if item.write_roles is not None
            })
            if field_roles:
                if not self.local_accounts:
                    raise ValueError(f"{entity.label}: включите учётные записи для прав на реквизиты.")
                lines.append(f"    __field_write_roles__ = {field_roles!r}")
            read_roles = {item.name: tuple(item.read_roles) for item in entity.fields if item.read_roles is not None}
            read_roles.update({
                f"{part.name}.{item.name}": tuple(item.read_roles)
                for part in entity.table_parts for item in part.fields if item.read_roles is not None
            })
            if read_roles:
                if not self.local_accounts:
                    raise ValueError(f"{entity.label}: включите учётные записи для прав на чтение реквизитов.")
                lines.append(f"    __field_read_roles__ = {read_roles!r}")
            if entity.permissions is not None:
                if not self.local_accounts:
                    raise ValueError(f"{entity.label}: включите учётные записи для настройки ролей.")
                if any(item.kind == "Ref" and item.cascade for item in entity.fields):
                    raise ValueError(f"{entity.label}: отключите каскадное удаление у ссылок защищённого объекта.")
                supported = {"read", "create", "update", "delete", "post"}
                if set(entity.permissions) - supported:
                    raise ValueError(f"{entity.label}: неизвестная операция в матрице ролей.")
                rights = {action: tuple(roles) for action, roles in entity.permissions.items()}
                lines.append(f"    __permissions__ = {rights!r}")
            if entity.owner_only:
                if not self.local_accounts:
                    raise ValueError(f"{entity.label}: включите учётные записи для доступа по владельцу.")
                if "record_owner" in used:
                    raise ValueError(f"{entity.label}: поле «record_owner» уже существует.")
                if any(item.kind == "Ref" and item.cascade for item in entity.fields):
                    raise ValueError(f"{entity.label}: отключите каскадное удаление у ссылок записи владельца.")
                used.add("record_owner")
                imports.add("String")
                lines.extend((
                    "    record_owner: String = String('Владелец', read_only=True)",
                    "    __owner_field__ = 'record_owner'",
                ))
            if entity.workflow is not None:
                workflow = entity.workflow
                if workflow.field_name in used or workflow.field_name in RESERVED_FIELDS:
                    raise ValueError(f"{entity.label}: поле статуса «{workflow.field_name}» уже существует.")
                if workflow.post_states and entity.kind != "Документ":
                    raise ValueError(f"{entity.label}: ограничение проведения доступно только для документов.")
                used.add(workflow.field_name)
                if any(step.assigned_only for step in workflow.transitions):
                    if not self.local_accounts:
                        raise ValueError(f"{entity.label}: включите учётные записи для назначения согласующих.")
                    assignee = "approval_assignee"
                    if assignee in used:
                        raise ValueError(f"{entity.label}: поле «{assignee}» уже существует.")
                    used.add(assignee)
                    imports.add("String")
                    lines.append("    approval_assignee: String = String('Согласующий', read_only=True)")
                imports.update(("Choice", "Transition", "Workflow"))
                lines.extend(workflow.source())
            for part in entity.table_parts:
                if not identifier(part.name) or part.name in used or part.name in RESERVED_FIELDS or not part.label.strip():
                    raise ValueError(f"{entity.name}: недопустимое или повторяющееся имя таблицы «{part.name}».")
                if not part.fields:
                    raise ValueError(f"{part.label}: добавьте хотя бы одну колонку.")
                used.add(part.name)
                imports.add("tab_part")
                lines.extend(("", "    @tab_part", f"    class {part.name}:", f"        __verbose_name__ = {part.label!r}"))
                columns: set[str] = set()
                for item in part.fields:
                    if not identifier(item.name) or item.name in {"id", "parent_id"} or item.name in columns:
                        raise ValueError(f"{part.label}: недопустимая или повторяющаяся колонка «{item.name}».")
                    columns.add(item.name)
                    if item.choice_filter:
                        raise ValueError(
                            f"{entity.name}.{part.name}.{item.name}: choice_filter доступен только в шапке карточки."
                        )
                    lines.append(
                        "        "
                        + field_source(item, part.name, names, imports, entities=entities, siblings=part.fields)
                    )
            if entity.print_form is not None:
                imports.add("PrintForm")
                if entity.print_form.tables:
                    imports.add("PrintTable")
                lines.extend(entity.print_form.source(entity))
            has_custom_form_code = bool(entity.form_code.strip())
            if entity.form is not None and entity.form.enabled and not has_custom_form_code:
                lines.extend(form_source(entity, imports, indent="    ", attr_name="__form__"))
            custom_methods = parse_studio_code(entity.code, "Код объекта", entity_name=entity.name)
            has_init = any(isinstance(node, ast.FunctionDef) and node.name == "__init__" for node in custom_methods.body)
            if not has_init:
                uses_any = True
                lines.extend(("", "    def __init__(self, **values: Any) -> None:", "        super().__init__(**values)"))
            if entity.movements:
                if any(isinstance(node, ast.FunctionDef) and node.name in ("get_movements", "get_ledger_entries")
                       for node in custom_methods.body):
                    raise ValueError(f"{entity.label}: выберите визуальные движения или методы движений в Python.")
                imports.add("Movement")
                lines.extend(movement_source(entity, registers))
            if entity.calculations or entity.validations:
                val_lines, save_lines = calculations_and_validations_source(entity, entities, registers, imports)
                defined_methods = {node.name for node in custom_methods.body if isinstance(node, ast.FunctionDef)}
                if val_lines and "on_validate" in defined_methods:
                    raise ValueError(f"{entity.label}: выберите визуальные проверки или метод on_validate() в Python.")
                if save_lines and "before_save" in defined_methods:
                    raise ValueError(f"{entity.label}: выберите визуальные расчёты или метод before_save() в Python.")
                lines.extend(val_lines)
                lines.extend(save_lines)
            if entity.code.strip():
                body = entity.code.strip("\n").splitlines()
                block_spans[len(blocks)] = StudioSourceSpan(
                    len(lines) + 2, len(body), len(entity.code) - len(entity.code.lstrip("\n")) + 1,
                    "Код объекта", entity.code, entity.name, col_offset=4,
                )
                lines.extend(("", *("    " + line if line else "" for line in body)))
            if entity.command_roles:
                if not self.local_accounts:
                    raise ValueError(f"{entity.label}: включите учётные записи для ограничения команд.")
                if not entity.command_roles.keys() <= entity.commands.keys():
                    raise ValueError(f"{entity.label}: права указаны для неизвестной команды.")
                roles = {name: tuple(role for role in allowed) for name, allowed in entity.command_roles.items()}
                lines.append(f"    __command_roles__ = {roles!r}")
            if entity.commands:
                methods = set(studio_command_methods(custom_methods))
                for name, label in entity.commands.items():
                    if name not in methods or not label.strip():
                        raise ValueError(f"{entity.label}: проверьте подпись и метод команды «{name}».")
                    imports.add("command")
                    lines.append(f"    {name} = command({label.strip()!r})({name})")
            blocks.append("\n".join(lines))
            if has_custom_form_code:
                parse_studio_code(entity.form_code, "Код формы", entity_name=entity.name)
                imports.update(("EntityForm", "form_for"))
                form_cls_name = f"{entity.name}Form"
                form_lines = [f"@form_for({entity.name})", f"class {form_cls_name}(EntityForm):"]
                if entity.form is not None and entity.form.enabled:
                    form_lines.extend(form_source(entity, imports, indent="    ", attr_name="layout"))
                f_body = entity.form_code.strip("\n").splitlines()
                block_spans[len(blocks)] = StudioSourceSpan(
                    len(form_lines) + 2, len(f_body), len(entity.form_code) - len(entity.form_code.lstrip("\n")) + 1,
                    "Код формы", entity.form_code, entity.name, col_offset=4,
                )
                form_lines.extend(("", *("    " + line if line else "" for line in f_body)))
                blocks.append("\n".join(form_lines))
        by_entity = {entity.name: entity for entity in self.entities}
        for config in self.reports:
            if not identifier(config.name) or config.name in names or not config.title.strip():
                raise ValueError(f"Некорректное или повторяющееся имя отчёта «{config.name}».")
            if config.source_kind == "register":
                if config.entity not in registers:
                    raise ValueError(f"{config.title}: выберите регистр.")
                blocks.append(register_report_source(config, registers[config.entity], imports))
                names.add(config.name)
                continue
            if config.source_kind != "entity" or config.entity not in by_entity:
                raise ValueError(f"{config.title}: выберите источник данных.")
            blocks.append(entity_report_source(config, by_entity[config.entity], imports))
            names.add(config.name)
        register_blocks = []
        for register in self.registers:
            if register.name in names:
                raise ValueError(f"Имя регистра «{register.name}» уже используется.")
            register_blocks.append(register.source(set(by_entity), imports, entities=by_entity))
            names.add(register.name)
        reminder_lines: list[str] = []
        for reminder in self.reminders:
            target = by_entity.get(reminder.entity)
            if target is None:
                raise ValueError(f"Напоминание: объект «{reminder.entity}» не найден.")
            target_fields = _target_field_names(target)
            date_item = next((f for f in target.fields if f.name == reminder.date_field), None)
            is_doc_date = target.kind == "Документ" and reminder.date_field == "date"
            if not is_doc_date and (date_item is None or date_item.kind not in ("Date", "DateTime")):
                raise ValueError(f"Напоминание ({target.label}): выберите поле даты/времени «{reminder.date_field}».")
            if reminder.done_field and reminder.done_field not in target_fields:
                raise ValueError(f"Напоминание ({target.label}): поле завершения «{reminder.done_field}» не найдено.")
            if reminder.title_field and reminder.title_field not in target_fields:
                raise ValueError(f"Напоминание ({target.label}): поле заголовка «{reminder.title_field}» не найдено.")
            if reminder.notify_before_minutes < 0:
                raise ValueError(f"Напоминание ({target.label}): время предупреждения не может быть отрицательным.")
            args = [f"[{target.name}]"]
            if reminder.date_field != "deadline":
                args.append(f"date_field={reminder.date_field!r}")
            if reminder.done_field != "is_completed":
                args.append(f"done_field={reminder.done_field!r}")
            if reminder.title_field != "name":
                args.append(f"title_field={reminder.title_field!r}")
            if reminder.notify_before_minutes != 60:
                args.append(f"notify_before_minutes={reminder.notify_before_minutes}")
            reminder_lines.append(f"    app.enable_reminders({', '.join(args)})")
        seed = initial_data_source(self.initial_data, self.entities, imports)
        needs_identity = any(
            entity.owner_only or entity.permissions is not None or bool(entity.command_roles)
            or any(item.write_roles is not None or item.read_roles is not None for item in entity.fields)
            or any(item.write_roles is not None or item.read_roles is not None
                   for part in entity.table_parts for item in part.fields)
            or (entity.workflow and any(step.assigned_only for step in entity.workflow.transitions))
            for entity in self.entities
        )
        simulate_role = preview_database is not None and backup_has_accounts is None and not needs_identity
        preview_role = self.preview_role.strip() if simulate_role else ""
        if preview_role:
            imports.add("set_role")
        if names & (imports | {"main", "Any", "ClassVar", "datetime", "DecimalValue"}):
            raise ValueError("Имя объекта совпадает с импортом или функцией main.")
        uses_datetime = "datetime" in imports
        uses_decimal = "DecimalValue" in imports
        imports.difference_update(("datetime", "DecimalValue"))
        typing_names = [name for name, needed in (("Any", uses_any), ("ClassVar", bool(self.reports))) if needed]
        start_candidates = {e.name for e in self.entities} | {r.name for r in self.reports}
        start = self.start_target.strip() or self.entities[0].name
        if start not in start_candidates:
            raise ValueError(f"Стартовый экран «{start}» не найден среди объектов и отчётов.")
        db_arg = f", db_path={str(preview_database)!r}" if preview_database is not None else ""
        size_arg = (
            f", window_size=({self.window_width}, {self.window_height})"
            if (self.window_width, self.window_height) != (1200, 800) else ""
        )
        rename_arg = ", column_renames=_DECUI_COLUMN_RENAMES" if self.column_renames else ""
        part_arg = ", table_part_renames=_DECUI_TABLE_PART_RENAMES" if self.table_part_renames else ""
        auth_arg = ", local_accounts=True" if (self.local_accounts or backup_has_accounts) and not preview_role else ""
        seed_arg = ", first_run=_seed_initial_data" if seed and backup_has_accounts is None else ""
        parse_studio_code(self.imports, "Код перед объектами")
        custom_code = parse_studio_code(self.logic, "Код после объектов")
        has_setup = any(isinstance(node, ast.FunctionDef) and node.name == "configure_app" for node in custom_code.body)
        setup_line = "\n    configure_app(app)" if has_setup else ""
        reminders_block = ("\n" + "\n".join(reminder_lines)) if reminder_lines else ""
        role_line = f"\n    set_role({preview_role!r})" if preview_role else ""
        preamble = (
            "from __future__ import annotations\n\n"
            + ("import datetime\n" if uses_datetime else "")
            + ("from decimal import Decimal as DecimalValue\n" if uses_decimal else "")
            + (f"from typing import {', '.join(typing_names)}\n" if typing_names else "")
            + ("\n" if uses_datetime or uses_decimal or typing_names else "")
            + "from decui import (\n"
            + "".join(f"    {name},\n" for name in sorted(imports)) + ")"
        )

        chunks: list[str] = []
        line = 1

        def append(text: str, span: StudioSourceSpan | None = None) -> None:
            nonlocal line
            if not text:
                return
            if spans is not None and span is not None:
                spans.append(replace(span, generated_start=line + span.generated_start - 1))
            chunks.append(text)
            line += text.count("\n") + 3

        def append_editor(text: str, location: str) -> None:
            body = text.strip()
            leading = text[:len(text) - len(text.lstrip())].count("\n")
            append(body, StudioSourceSpan(1, len(body.splitlines()), leading + 1, location, text))

        append(preamble)
        append_editor(self.imports, "Код перед объектами")
        for block in register_blocks:
            append(block)
        for index, block in enumerate(blocks):
            append(block, block_spans.get(index))
        append_editor(self.logic, "Код после объектов")
        for section in (
            seed,
            f"_DECUI_COLUMN_RENAMES = {self.column_renames!r}" if self.column_renames else "",
            f"_DECUI_TABLE_PART_RENAMES = {self.table_part_renames!r}" if self.table_part_renames else "",
            (
                f"if __name__ == '__main__':\n    app = ERPApplication("
                f"title={self.title!r}, app_id={self.app_id!r}, start={start}{size_arg}{db_arg}{auth_arg}{seed_arg}{rename_arg}{part_arg})"
                f"{setup_line}{reminders_block}{role_line}\n    raise SystemExit(app.run())"
            ),
        ):
            append(section)
        source = "\n\n\n".join(chunks) + "\n"
        try:
            compile(source, "app.py", "exec")
        except SyntaxError as exc:
            raise StudioSyntaxError(exc, "app.py", source=source) from exc
        return source

    def diagnostics(self) -> list[StudioDiagnostic]:
        """Collect instant static errors and non-blocking design warnings across the project."""
        items: list[StudioDiagnostic] = []
        try:
            self.source()
        except StudioSyntaxError as exc:
            if exc.filename in ("Код перед объектами", "Код после объектов"):
                tab = "Приложение"
            elif exc.filename == "Код формы":
                tab = "Форма"
            else:
                tab = "Объект"
            items.append(StudioDiagnostic("error", f"{exc.filename} (стр. {exc.lineno or 1}): {exc.msg}", exc.entity_name, tab))
        except ValueError as exc:
            items.append(StudioDiagnostic("error", str(exc)))

        roles = self.all_roles()
        if roles and not self.local_accounts:
            items.append(StudioDiagnostic(
                "warning",
                f"В проекте настроены роли ({', '.join(roles)}), но вход по учётным записям отключён.",
                tab="Приложение",
            ))
        for entity in self.entities:
            if not entity.fields and not entity.table_parts:
                items.append(StudioDiagnostic(
                    "warning",
                    f"Объект «{entity.label}» не содержит собственных реквизитов или табличных частей.",
                    entity.name, "Объект",
                ))
            if entity.form is not None and entity.form.enabled and not entity.form.include_remaining:
                placed = {n.target for n in _iter_form_nodes(entity.form.items) if n.kind == "field"}
                missing_req = [f.label for f in entity.fields if f.required and f.visible and f.name not in placed]
                if missing_req:
                    items.append(StudioDiagnostic(
                        "warning",
                        f"На форме «{entity.label}» не размещены обязательные поля: {', '.join(missing_req)}.",
                        entity.name, "Форма",
                    ))
            if entity.kind == "Документ" and self.registers and not entity.movements and "get_movements" not in entity.code:
                items.append(StudioDiagnostic(
                    "warning",
                    f"Документ «{entity.label}» пока не формирует движений по регистрам.",
                    entity.name, "Движения",
                ))
        used_registers = {
            rule.register for entity in self.entities for rule in entity.movements
        } | {
            calc.register for entity in self.entities for calc in entity.calculations if calc.kind == "lookup"
        }
        for reg in self.registers:
            in_code = any(reg.name in e.code for e in self.entities) or reg.name in self.logic
            if reg.name not in used_registers and not in_code:
                items.append(StudioDiagnostic(
                    "warning",
                    f"Регистр «{reg.name}» объявлен, но пока не заполняется ни одним документом или правилом.",
                    tab="Регистры",
                ))
        if self.entities and not self.reports:
            items.append(StudioDiagnostic(
                "warning",
                "В проекте пока нет отчётов. Добавьте отчёт на вкладке «Отчёты» для анализа данных.",
                tab="Отчёты",
            ))
        return items

    def lsp_document(self) -> tuple[str, list[StudioSourceSpan]]:
        """Build a syntax-resilient virtual app.py and exact spans for live Pyrefly LSP analysis."""
        spans: list[StudioSourceSpan] = []
        imports = {
            "ERPApplication", "Database", "Catalog", "Document", "catalog", "document",
            "String", "Integer", "Decimal", "Boolean", "Date", "DateTime", "Time", "Choice", "Ref",
            "tab_part", "command", "Movement", "Register", "accumulation", "information",
            "Dimension", "RefDimension", "Resource", "Report", "QueryReport", "Column", "report",
            "DateParam", "RefParam", "ChoiceParam", "StringParam", "IntegerParam", "DecimalParam", "BooleanParam",
            "Form", "FormRow", "FormColumn", "FormGroup", "FormTabs", "FormTab", "FormField",
            "FormTable", "FormButton", "FormLabel", "FormDivider", "EntityForm", "form_for",
            "Transition", "Workflow", "PrintForm", "PrintTable", "get_latest", "get_balance",
            "get_records", "set_record", "register_report_rows", "define_role", "set_role",
        }
        names = {e.name for e in self.entities if identifier(e.name)}
        entities = {e.name: e for e in self.entities if identifier(e.name)}
        registers = {r.name: r for r in self.registers if identifier(r.name)}

        reg_blocks: list[str] = []
        for reg in self.registers:
            try:
                reg_blocks.append(reg.source(names, imports, entities=entities))
            except ValueError:
                pass

        entity_blocks: list[tuple[str, list[tuple[int, int, str, str, str, int]]]] = []
        for entity in self.entities:
            if not identifier(entity.name):
                continue
            decorator, base = ENTITY_TYPES.get(entity.kind, ("catalog", "Catalog"))
            lines = [f"@{decorator}", f"class {entity.name}({base}):", f"    __verbose_name__ = {entity.label or entity.name!r}"]
            for item in entity.fields:
                if not identifier(item.name):
                    continue
                try:
                    lines.append("    " + field_source(item, entity.name, names, imports, entities=entities, siblings=entity.fields))
                except ValueError:
                    lines.append(f"    {item.name} = String({item.label or item.name!r})")
            for part in entity.table_parts:
                if not identifier(part.name):
                    continue
                lines.extend(("", "    @tab_part", f"    class {part.name}:", f"        __verbose_name__ = {part.label or part.name!r}"))
                for col in part.fields:
                    if not identifier(col.name):
                        continue
                    try:
                        lines.append("        " + field_source(col, part.name, names, imports, entities=entities, siblings=part.fields))
                    except ValueError:
                        lines.append(f"        {col.name} = String({col.label or col.name!r})")
            if entity.workflow is not None:
                try:
                    lines.extend(entity.workflow.source())
                except ValueError:
                    pass
            if entity.print_form is not None:
                try:
                    lines.extend(entity.print_form.source(entity))
                except ValueError:
                    pass
            if entity.form is not None and entity.form.enabled and not entity.form_code:
                try:
                    lines.extend(form_source(entity, imports, indent="    ", attr_name="__form__"))
                except ValueError:
                    pass
            lines.extend(("", "    def __init__(self, **values: Any) -> None:", "        super().__init__(**values)"))
            if entity.movements:
                try:
                    lines.extend(movement_source(entity, registers))
                except ValueError:
                    pass
            if entity.calculations or entity.validations:
                try:
                    v_lines, s_lines = calculations_and_validations_source(entity, entities, registers, imports)
                    lines.extend(v_lines)
                    lines.extend(s_lines)
                except ValueError:
                    pass

            local_spans: list[tuple[int, int, str, str, str, int]] = []
            code_lines = entity.code.split("\n")
            if entity.code:
                start_offset = len(lines) + 2
                local_spans.append((start_offset, len(code_lines), "Код объекта", entity.code, entity.name, 4))
                lines.extend(("", *("    " + ln if ln else "" for ln in code_lines)))
            for cmd_name, cmd_lbl in entity.commands.items():
                if identifier(cmd_name) and cmd_lbl.strip():
                    lines.append(f"    {cmd_name} = command({cmd_lbl!r})({cmd_name})")
            entity_blocks.append(("\n".join(lines), local_spans))

            if entity.form_code or (entity.form is not None and entity.form.enabled and entity.form_code != ""):
                f_lines_hdr = [f"@form_for({entity.name})", f"class {entity.name}Form(EntityForm):"]
                if entity.form is not None and entity.form.enabled:
                    try:
                        f_lines_hdr.extend(form_source(entity, imports, indent="    ", attr_name="layout"))
                    except ValueError:
                        pass
                f_code_lines = entity.form_code.split("\n")
                f_spans: list[tuple[int, int, str, str, str, int]] = []
                if entity.form_code:
                    f_spans.append((len(f_lines_hdr) + 2, len(f_code_lines), "Код формы", entity.form_code, entity.name, 4))
                    f_lines_hdr.extend(("", *("    " + ln if ln else "" for ln in f_code_lines)))
                else:
                    f_lines_hdr.append("    pass")
                entity_blocks.append(("\n".join(f_lines_hdr), f_spans))

        report_blocks: list[str] = []
        for rep in self.reports:
            try:
                if rep.source_kind == "register" and rep.entity in registers:
                    report_blocks.append(register_report_source(rep, registers[rep.entity], imports))
                elif rep.source_kind == "entity" and rep.entity in entities:
                    report_blocks.append(entity_report_source(rep, entities[rep.entity], imports))
            except ValueError:
                pass

        preamble = (
            "from __future__ import annotations\n\n"
            "import datetime\n"
            "from decimal import Decimal as DecimalValue\n"
            "from typing import Any, ClassVar\n\n"
            "from decui import (\n"
            + "".join(f"    {name},\n" for name in sorted(imports)) + ")"
        )

        chunks: list[str] = []
        line = 1

        def append_chunk(text: str, raw_spans: list[tuple[int, int, str, str, str, int]] | tuple[()] = ()) -> None:
            nonlocal line
            if not text:
                return
            for rel_start, count, loc, raw_txt, ent_name, col_off in raw_spans:
                spans.append(StudioSourceSpan(
                    generated_start=line + rel_start - 1,
                    line_count=count,
                    editor_start=1,
                    location=loc,
                    text=raw_txt,
                    entity_name=ent_name,
                    col_offset=col_off,
                ))
            chunks.append(text)
            line += text.count("\n") + 3

        append_chunk(preamble)
        if self.imports:
            imp_lines = self.imports.split("\n")
            append_chunk(self.imports, [(1, len(imp_lines), "Код перед объектами", self.imports, "", 0)])
        for rb in reg_blocks:
            append_chunk(rb)
        for eb, e_spans in entity_blocks:
            append_chunk(eb, e_spans)
        for rep_b in report_blocks:
            append_chunk(rep_b)
        if self.logic:
            log_lines = self.logic.split("\n")
            append_chunk(self.logic, [(1, len(log_lines), "Код после объектов", self.logic, "", 0)])
        return ("\n\n\n".join(chunks) + "\n"), spans

    def smoke_test_source(self, app_module: str = "app") -> str:
        """Generate a standalone pytest smoke suite for the exported application."""
        entities_map = {entity.name: entity for entity in self.entities}
        imports = [entity.name for entity in self.entities] + [report.name for report in self.reports]
        if self.initial_data:
            imports.append("_seed_initial_data")
        lines = [
            "from __future__ import annotations",
            "",
            "import datetime",
            "from decimal import Decimal",
            "",
            f"from {app_module} import {', '.join(imports)}",
            "from decui import Database",
        ]
        if self.local_accounts:
            lines.append("from decui.accounts import Account, set_current_account")
        lines.extend(["", ""])

        lines.extend([
            "def test_schema_and_metadata():",
            "    db = Database.memory()",
            "    assert db._ready",
            "",
        ])

        if self.initial_data:
            lines.append("def test_initial_data_and_reports():")
            lines.append("    db = Database.memory()")
            if self.local_accounts:
                lines.append("    set_current_account(Account('smoke_admin', 'admin'))")
            lines.append("    assert db.initialize(_seed_initial_data)")
            lines.append("    assert not db.initialize(_seed_initial_data)")
            counts: dict[str, int] = {}
            for seed in self.initial_data:
                counts[seed.entity] = counts.get(seed.entity, 0) + 1
            for name, count in counts.items():
                lines.append(f"    assert len(db.list({name})) >= {count}")
            for report in self.reports:
                lines.append(f"    assert isinstance({report.name}().execute(db), list)")
            if self.local_accounts:
                lines.append("    set_current_account(None)")
            lines.append("")

        def sample_expr(field: StudioField, var_map: dict[str, str], idx: int) -> str | None:
            if field.kind == "Ref":
                return f"{var_map[field.target]}.id" if field.target in var_map else None
            if field.kind == "String":
                raw = f"test_{field.name}_{idx}"
                if field.max_length is not None and field.max_length > 0:
                    raw = raw[:field.max_length]
                return repr(raw)
            if field.kind == "Integer":
                lo = int(field.min_value) if field.min_value.strip() else None
                hi = int(field.max_value) if field.max_value.strip() else None
                val = 1
                if lo is not None:
                    val = max(val, lo)
                if hi is not None:
                    val = min(val, hi)
                return repr(val)
            if field.kind == "Decimal":
                lo_d = Decimal(field.min_value) if field.min_value.strip() else None
                hi_d = Decimal(field.max_value) if field.max_value.strip() else None
                dec = Decimal("10.00")
                if lo_d is not None:
                    dec = max(dec, lo_d)
                if hi_d is not None:
                    dec = min(dec, hi_d)
                return f"Decimal({str(dec)!r})"
            if field.kind == "Boolean":
                return "False"
            if field.kind == "Date":
                return "datetime.date(2026, 6, 1)"
            if field.kind == "DateTime":
                return "datetime.datetime(2026, 6, 1, 12, 0)"
            if field.kind == "Time":
                return "datetime.time(12, 0)"
            if field.kind == "Choice":
                opts = [v.strip() for v in field.choices.split(",") if v.strip()]
                return repr(opts[0] if opts else "")
            return None

        def can_auto_build(entity: StudioEntity, visiting: set[str]) -> bool:
            if (
                entity.name in visiting
                or any(hook in entity.code for hook in ("before_save", "on_validate"))
                or any(c.kind == "lookup" for c in entity.calculations)
                or any(v.kind == "compare" for v in entity.validations)
            ):
                return False
            next_visit = visiting | {entity.name}
            for item in entity.fields:
                if item.required and item.kind == "Ref":
                    target = entities_map.get(item.target)
                    if target is None or not can_auto_build(target, next_visit):
                        return False
            for part in entity.table_parts:
                for item in part.fields:
                    if item.required and item.kind == "Ref":
                        target = entities_map.get(item.target)
                        if target is None or not can_auto_build(target, next_visit):
                            return False
            return True

        simple_entities = [e for e in self.entities if can_auto_build(e, set())]
        if simple_entities:
            lines.append("def test_entity_roundtrip_and_reports():")
            lines.append("    db = Database.memory()")
            if self.local_accounts:
                lines.append("    set_current_account(Account('smoke_admin', 'admin'))")
            created_vars: dict[str, str] = {}
            step = 0

            def emit_create(entity: StudioEntity) -> str:
                nonlocal step
                if entity.name in created_vars:
                    return created_vars[entity.name]
                for item in entity.fields:
                    if item.required and item.kind == "Ref":
                        emit_create(entities_map[item.target])
                for part in entity.table_parts:
                    for item in part.fields:
                        if item.required and item.kind == "Ref":
                            emit_create(entities_map[item.target])
                step += 1
                var_name = f"obj_{entity.name.lower()}"
                kwargs: list[str] = []
                for item in entity.fields:
                    if item.read_only or item.visibility_field:
                        continue
                    if item.required or item.unique or item.kind == "String":
                        expr = sample_expr(item, created_vars, step)
                        if expr is not None:
                            kwargs.append(f"{item.name}={expr}")
                for part in entity.table_parts:
                    row_pairs: list[str] = []
                    for item in part.fields:
                        if item.read_only:
                            continue
                        if item.required or item.unique:
                            expr = sample_expr(item, created_vars, step)
                            if expr is not None:
                                row_pairs.append(f"{item.name!r}: {expr}")
                    kwargs.append(f"{part.name}=[{{{', '.join(row_pairs)}}}]")
                lines.append(f"    {var_name} = db.save({entity.name}({', '.join(kwargs)}), validate=True)")
                lines.append(f"    assert db.get({entity.name}, {var_name}.id) is not None")
                if (
                    entity.kind == "Документ"
                    and (entity.workflow is None or not entity.workflow.post_states)
                    and "get_movements" not in entity.code
                ):
                    lines.append(f"    db.post({var_name})")
                    lines.append(f"    assert {var_name}.is_posted")
                    lines.append(f"    db.unpost({var_name})")
                    lines.append(f"    assert not {var_name}.is_posted")
                created_vars[entity.name] = var_name
                return var_name

            for entity in simple_entities:
                emit_create(entity)
            for report in self.reports:
                lines.append(f"    assert isinstance({report.name}().execute(db), list)")
            if self.local_accounts:
                lines.append("    set_current_account(None)")
            lines.append("")
        if not self.initial_data and not simple_entities and self.reports:
            lines.append("def test_reports_on_empty_db():")
            lines.append("    db = Database.memory()")
            if self.local_accounts:
                lines.append("    set_current_account(Account('smoke_admin', 'admin'))")
            for report in self.reports:
                lines.append(f"    assert isinstance({report.name}().execute(db), list)")
            if self.local_accounts:
                lines.append("    set_current_account(None)")
            lines.append("")
        return "\n".join(lines) + "\n"

    def export(
        self, directory: Path, *, preview_database: Path | None = None, backup_has_accounts: bool | None = None,
        spans: list[StudioSourceSpan] | None = None,
    ) -> Path:
        source = self.source(preview_database=preview_database, backup_has_accounts=backup_has_accounts, spans=spans)
        directory.mkdir(parents=True, exist_ok=True)
        entry = directory / "app.py"
        with entry.open("x", encoding="utf-8") as file:
            file.write(source)
        return entry

    def export_full_project(self, directory: Path) -> Path:
        """Export a complete runnable project folder with app.py, test_app.py, README.md, and project JSON."""
        from decui.cli import STARTER_GITIGNORE

        entry = self.export(directory)
        tests = directory / "test_app.py"
        readme = directory / "README.md"
        gitignore = directory / ".gitignore"
        spec = directory / f"{self.app_id}.decui.json"
        for path in (tests, readme, gitignore, spec):
            if path.exists():
                raise FileExistsError(f"Файл уже существует: {path}")
        tests.write_text(self.smoke_test_source(), encoding="utf-8")
        readme.write_text(
            f"# {self.title}\n\n"
            "Проект сгенерирован в **DECUI Studio**.\n\n"
            "## Команды\n\n"
            "```bash\n"
            "decui check\n"
            "decui test -q\n"
            "decui run\n"
            "decui build\n"
            "```\n\n"
            f"Исходный файл проекта Studio: `{spec.name}`.\n",
            encoding="utf-8",
        )
        gitignore.write_text(STARTER_GITIGNORE, encoding="utf-8")
        self.write(spec)
        return entry


def goal_template() -> StudioProject:
    """Starter with related records and a report that can be edited in Studio."""
    return StudioProject(
        title="Мои цели",
        app_id="studio-goals",
        entities=[
            StudioEntity("Goal", "Цели", section="Планирование", fields=[
                StudioField("name", "Название", required=True),
                StudioField("deadline", "Срок", "DateTime", required=True),
            ]),
            StudioEntity("Task", "Задачи", section="Планирование", fields=[
                StudioField("name", "Название", required=True),
                StudioField("goal", "Цель", "Ref", required=True, target="Goal", related_list="Задачи цели"),
                StudioField("description", "Описание", multiline=True),
                StudioField("deadline", "Срок", "DateTime", required=True),
                StudioField("is_completed", "Выполнено", "Boolean"),
            ], code=(
                "def before_save(self, db: Database) -> None:\n"
                "    goal = db.get(Goal, self.goal)\n"
                "    if goal is not None and self.deadline > goal.deadline:\n"
                '        raise ValueError("Срок задачи не может быть позже срока цели.")'
            )),
        ],
        imports="from typing import ClassVar\n\nfrom decui import Column, Database, Report, report",
        logic=(
            "@report\n"
            "class OpenTasks(Report):\n"
            '    title = "Открытые задачи"\n'
            "    auto_run = True\n"
            "    columns: ClassVar[list[Column]] = [\n"
            '        Column("Задача", "name", width=0),\n'
            '        Column("Срок", "deadline", width=170),\n'
            "    ]\n\n"
            "    def execute(self, db: Database, **params: object) -> list[dict[str, object]]:\n"
            "        return [\n"
            '            {"name": task.name, "deadline": task.deadline.strftime("%d.%m.%Y %H:%M")}\n'
            '            for task in db.filter(Task, is_completed=False, order_by="deadline")\n'
            "        ]\n\n\n"
            "def configure_app(app: ERPApplication) -> None:\n"
            "    app.enable_reminders([Task])"
        ),
    )


def invoice_template() -> StudioProject:
    """Invoice and item catalog with editable line items."""
    return StudioProject(
        title="Товары и накладные",
        app_id="studio-invoices",
        entities=[
            StudioEntity("Product", "Товары", section="Склад", fields=[
                StudioField("name", "Название", required=True),
                StudioField("price", "Цена", kind="Decimal", required=True, default="0.00"),
            ]),
            StudioEntity(
                "Invoice", "Накладные", kind="Документ", section="Склад", numbering_prefix="INV",
                print_form=StudioPrintForm("Товарная накладная", ["number", "date"], {
                    "Items": ["product", "quantity", "price"],
                }),
                movements=[StudioMovement("Stock", "Items", mapping={
                    "product": "row:product", "quantity": "row:quantity",
                })],
                table_parts=[StudioTablePart("Items", "Товары накладной", fields=[
                    StudioField("product", "Товар", kind="Ref", required=True, target="Product"),
                    StudioField("quantity", "Количество", kind="Integer", required=True, default="1"),
                    StudioField("price", "Цена", kind="Decimal", required=True, default="0.00"),
                ])],
            ),
        ],
        initial_data=[
            StudioSeed("paper", "Product", {"name": "Бумага А4", "price": "380.00"}),
            StudioSeed("pens", "Product", {"name": "Ручки синие", "price": "28.50"}),
            StudioSeed("delivery", "Invoice", tables={"Items": [
                {"product": "paper", "quantity": "12", "price": "380.00"},
                {"product": "pens", "quantity": "40", "price": "28.50"},
            ]}),
        ],
        registers=[StudioRegister("Stock", members=[
            StudioRegisterMember("product", "Товар", "Измерение", "Ссылка", "Product"),
            StudioRegisterMember("quantity", "Количество", "Ресурс", "Целое"),
        ])],
        reports=[
            StudioReport("ProductPrices", "Цены товаров", "Product", ["name", "price"], "name"),
            StudioReport(
                "WarehouseBalances", "Остатки товаров", "Stock", ["product", "quantity"],
                order_by="product", ref_filter="product", source_kind="register", as_of=True, totals=True,
            ),
        ],
    )


def order_invoice_template() -> StudioProject:
    """Order with seeded lines that can be turned into a stock invoice."""
    project = invoice_template()
    project.title = "Заказы и накладные"
    project.app_id = "studio-order-invoices"
    invoice = project.entities[1]
    invoice.fields.extend((
        StudioField(
            "order_ref", "Заказ", "Ref", required=True, target="SalesOrder",
            related_list="Накладные по заказу", create_from="Создать накладную",
            basis_fields={"comment": "instructions"},
            basis_tables={
                "Items": {"source": "Lines", "columns": {
                    "product": "product", "quantity": "ordered_qty", "price": "price",
                }},
            },
        ),
        StudioField("comment", "Комментарий", multiline=True),
    ))
    project.entities.insert(1, StudioEntity(
        "SalesOrder", "Заказы", kind="Документ", section="Склад", numbering_prefix="ORD",
        fields=[StudioField("instructions", "Указания к поставке", multiline=True)],
        table_parts=[StudioTablePart("Lines", "Позиции заказа", [
            StudioField("product", "Товар", "Ref", required=True, target="Product"),
            StudioField("ordered_qty", "Заказано", "Integer", required=True, default="1"),
            StudioField("price", "Цена", "Decimal", required=True, default="0.00"),
        ])],
    ))
    project.initial_data[-1] = StudioSeed(
        "first_order", "SalesOrder", {"instructions": "Доставить до обеда"},
        tables={"Lines": [
            {"product": "paper", "ordered_qty": "12", "price": "380.00"},
            {"product": "pens", "ordered_qty": "40", "price": "28.50"},
        ]},
    )
    return project


def approval_template() -> StudioProject:
    project = invoice_template()
    project.title = "Согласование накладных"
    project.app_id = "studio-invoice-approval"
    project.entities[1].workflow = StudioWorkflow(
        ["Черновик", "На согласовании", "Согласовано"], "Черновик",
        [
            StudioTransition("Отправить", "Черновик", "На согласовании"),
            StudioTransition("Вернуть на доработку", "На согласовании", "Черновик"),
            StudioTransition("Согласовать", "На согласовании", "Согласовано"),
        ],
        ["Согласовано"],
    )
    return project


def crm_delivery_template() -> StudioProject:
    """CRM, addresses with choice_filter, auto-filled prices, and line-item sales report."""
    return StudioProject(
        title="CRM и доставка",
        app_id="studio-crm-delivery",
        start_target="DeliveryOrder",
        entities=[
            StudioEntity("Customer", "Клиенты", section="Продажи", fields=[
                StudioField("name", "Наименование", required=True),
                StudioField("phone", "Телефон"),
                StudioField("tags", "Категория", kind="Choice", choices="VIP, Розница, Опт", multi=True),
            ]),
            StudioEntity("Address", "Адреса доставки", section="Продажи", fields=[
                StudioField("owner", "Клиент", kind="Ref", required=True, target="Customer",
                            related_list="Адреса клиента"),
                StudioField("name", "Адрес", required=True),
            ]),
            StudioEntity("Product", "Товары", section="Склад", fields=[
                StudioField("name", "Название", required=True, unique=True),
                StudioField("price", "Цена", kind="Decimal", required=True, default="0.00",
                            min_value="0", currency="RUB"),
            ]),
            StudioEntity(
                "DeliveryOrder", "Заказы на доставку", kind="Документ", section="Продажи",
                numbering_prefix="DLV", numbering_pattern="{prefix}-{year}-{seq:05d}",
                check_sequence=True,
                fields=[
                    StudioField("customer", "Клиент", kind="Ref", required=True, target="Customer",
                                auto_fill="phone"),
                    StudioField("phone", "Телефон клиента"),
                    StudioField("need_delivery", "Нужна доставка", kind="Boolean", default="True"),
                    StudioField("address", "Адрес доставки", kind="Ref", target="Address",
                                choice_filter={"owner": "customer"},
                                visibility_field="need_delivery", visibility_value=True,
                                required_when_visible=True),
                    StudioField("due_date", "Дата отгрузки", kind="Date", default="today"),
                    StudioField("total", "Итого", kind="Decimal", read_only=True, default="0.00", currency="RUB"),
                ],
                table_parts=[StudioTablePart("Items", "Позиции заказа", [
                    StudioField("product", "Товар", kind="Ref", required=True, target="Product", auto_fill="price"),
                    StudioField("quantity", "Количество", kind="Integer", required=True, default="1", min_value="1"),
                    StudioField("price", "Цена", kind="Decimal", required=True, default="0.00", min_value="0"),
                    StudioField("amount", "Сумма", kind="Decimal", read_only=True, default="0.00"),
                ])],
                code=(
                    "def before_save(self, db: Database) -> None:\n"
                    "    for row in self.Items:\n"
                    "        qty = PyDecimal(str(row.get('quantity') or 0))\n"
                    "        prc = PyDecimal(str(row.get('price') or 0))\n"
                    "        row['amount'] = qty * prc\n"
                    "    self.total = self.Items.total('amount')"
                ),
                print_form=StudioPrintForm("Заказ на доставку", ["number", "date", "customer", "phone", "total"], {
                    "Items": ["product", "quantity", "price", "amount"],
                }),
            ),
        ],
        imports="from decimal import Decimal as PyDecimal\n\nfrom decui import Database",
        initial_data=[
            StudioSeed("cust_alpha", "Customer", {"name": "ООО Альфа", "phone": "+7 900 111-22-33", "tags": "Опт"}),
            StudioSeed("addr_alpha", "Address", {"owner": "cust_alpha", "name": "ул. Ленина, д. 10"}),
            StudioSeed("prod_coffee", "Product", {"name": "Кофе в зёрнах", "price": "1250.00"}),
            StudioSeed("prod_tea", "Product", {"name": "Чай зелёный", "price": "450.00"}),
            StudioSeed(
                "order_1", "DeliveryOrder",
                {"customer": "cust_alpha", "phone": "+7 900 111-22-33", "address": "addr_alpha"},
                tables={"Items": [
                    {"product": "prod_coffee", "quantity": "2", "price": "1250.00"},
                    {"product": "prod_tea", "quantity": "3", "price": "450.00"},
                ]},
                post=True,
            ),
        ],
        reports=[
            StudioReport(
                "SalesLinesReport", "Продажи по строкам", "DeliveryOrder",
                columns=["number", "customer", "product", "quantity", "amount"],
                order_by="-number", ref_filter="customer",
                section="Отчёты/Продажи", table_part="Items",
                date_filter="date", posted_only=True, ref_filter_from_record=True,
                totals=True, group_by=["customer"],
                column_options={
                    "amount": {"aggregate": "sum", "format": "{:,.2f}", "suffix": " ₽", "align": "right"},
                },
            ),
        ],
    )


def purchase_prices_template() -> StudioProject:
    """Suppliers, periodic price register (get_latest), auto-calculated order lines, and chart report."""
    return StudioProject(
        title="Закупки и прайс-листы",
        app_id="studio-purchases",
        start_target="PurchaseOrder",
        entities=[
            StudioEntity("Supplier", "Поставщики", section="Закупки", fields=[
                StudioField("name", "Наименование", required=True, unique=True),
                StudioField("phone", "Телефон"),
            ]),
            StudioEntity("Product", "Товары", section="Склад", fields=[
                StudioField("name", "Название", required=True, unique=True),
                StudioField("price", "Базовая цена", kind="Decimal", default="0.00", min_value="0", currency="RUB"),
            ]),
            StudioEntity(
                "PriceSheet", "Установка цен", kind="Документ", section="Закупки", numbering_prefix="PRC",
                fields=[
                    StudioField("supplier", "Поставщик", kind="Ref", required=True, target="Supplier"),
                    StudioField("effective_date", "Действует с", kind="Date", required=True, default="today"),
                ],
                table_parts=[StudioTablePart("Items", "Позиции прайс-листа", [
                    StudioField("product", "Товар", kind="Ref", required=True, target="Product"),
                    StudioField("price", "Цена", kind="Decimal", required=True, default="0.00", min_value="0"),
                ])],
                movements=[StudioMovement(
                    "SupplierPrices", "Items", period_field="effective_date",
                    mapping={"product": "row:product", "supplier": "header:supplier", "price": "row:price"},
                )],
                validations=[StudioValidation("require_rows", "Items", message="Добавьте хотя бы одну цену в прайс-лист.")],
            ),
            StudioEntity(
                "PurchaseOrder", "Заказы поставщикам", kind="Документ", section="Закупки",
                numbering_prefix="PO", numbering_pattern="{prefix}-{year}-{seq:05d}",
                fields=[
                    StudioField("supplier", "Поставщик", kind="Ref", required=True, target="Supplier",
                                related_list="Заказы поставщику"),
                    StudioField("total", "Итого", kind="Decimal", read_only=True, default="0.00", currency="RUB"),
                ],
                table_parts=[StudioTablePart("Items", "Позиции заказа", [
                    StudioField("product", "Товар", kind="Ref", required=True, target="Product"),
                    StudioField("quantity", "Количество", kind="Integer", required=True, default="1", min_value="1"),
                    StudioField("price", "Цена", kind="Decimal", default="0.00", min_value="0"),
                    StudioField("amount", "Сумма", kind="Decimal", read_only=True, default="0.00"),
                ])],
                calculations=[
                    StudioCalculation(
                        target="Items.price", kind="lookup", source_part="Items",
                        register="SupplierPrices", resource="price", date_field="date",
                        dimension_map={"product": "row:product", "supplier": "header:supplier"},
                        only_if_empty=True,
                    ),
                    StudioCalculation(
                        target="Items.amount", kind="row_formula", source_part="Items",
                        left="quantity", operator="*", right="price",
                    ),
                    StudioCalculation(
                        target="total", kind="table_total", source_part="Items",
                        left="amount", operator="sum",
                    ),
                ],
                validations=[
                    StudioValidation("require_rows", "Items", message="Добавьте хотя бы одну позицию в заказ."),
                    StudioValidation("unique_rows", "Items", column="product", message="Товар в заказе указан дважды."),
                ],
                print_form=StudioPrintForm("Заказ поставщику", ["number", "date", "supplier", "total"], {
                    "Items": ["product", "quantity", "price", "amount"],
                }),
            ),
        ],
        registers=[
            StudioRegister("SupplierPrices", "Сведения", [
                StudioRegisterMember("product", "Товар", "Измерение", "Ссылка", "Product"),
                StudioRegisterMember("supplier", "Поставщик", "Измерение", "Ссылка", "Supplier"),
                StudioRegisterMember("price", "Цена", "Ресурс", "Десятичное"),
            ]),
        ],
        initial_data=[
            StudioSeed("sup_north", "Supplier", {"name": "ООО Север", "phone": "+7 901 100-20-30"}),
            StudioSeed("sup_south", "Supplier", {"name": "ООО Юг", "phone": "+7 902 400-50-60"}),
            StudioSeed("prod_paper", "Product", {"name": "Бумага А4", "price": "380.00"}),
            StudioSeed("prod_toner", "Product", {"name": "Тонер", "price": "1950.00"}),
            StudioSeed("prices_north", "PriceSheet", {"supplier": "sup_north"}, tables={"Items": [
                {"product": "prod_paper", "price": "360.00"},
                {"product": "prod_toner", "price": "1820.00"},
            ]}, post=True),
            StudioSeed("order_north", "PurchaseOrder", {"supplier": "sup_north"}, tables={"Items": [
                {"product": "prod_paper", "quantity": "10"},
                {"product": "prod_toner", "quantity": "2"},
            ]}, post=True),
        ],
        reports=[
            StudioReport(
                "CurrentSupplierPrices", "Актуальные цены поставщиков", "SupplierPrices",
                columns=["period", "supplier", "product", "price"],
                source_kind="register", as_of=True, latest_only=True, ref_filter="supplier",
                section="Отчёты/Закупки", ref_filter_from_record=True,
            ),
            StudioReport(
                "PurchasesBySupplier", "Закупки по поставщикам", "PurchaseOrder",
                columns=["number", "supplier", "product", "quantity", "amount"],
                section="Отчёты/Закупки", table_part="Items",
                date_filter="date", ref_filter="supplier", posted_only=True, ref_filter_from_record=True,
                totals=True, group_by=["supplier"],
                chart_label="supplier", chart_value="amount", chart_title="Закупки по поставщикам",
                column_options={
                    "amount": {"aggregate": "sum", "format": "{:,.2f}", "suffix": " ₽", "align": "right"},
                },
            ),
        ],
    )
