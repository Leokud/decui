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


def identifier(value: str) -> bool:
    return value.isidentifier() and not keyword.iskeyword(value) and not value.startswith("_")


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


def field_source(item: StudioField, owner: str, names: set[str], imports: set[str]) -> str:
    if item.kind not in FIELD_TYPES or not item.label.strip():
        raise ValueError(f"{owner}.{item.name}: проверьте тип и подпись.")
    imports.add(item.kind)
    args = [repr(item.label)]
    if item.kind == "Ref":
        if item.target not in names:
            raise ValueError(f"{owner}.{item.name}: выберите объект для ссылки.")
        args.insert(0, repr(item.target))
        if item.related_list:
            args.append(f"related_list={item.related_list!r}")
        if item.create_from.strip():
            args.append(f"create_from={item.create_from.strip()!r}")
            if item.basis_fields:
                args.append(f"basis_fields={item.basis_fields!r}")
            if item.basis_tables:
                args.append(f"basis_tables={item.basis_tables!r}")
        if item.cascade:
            args.append('on_delete="CASCADE"')
    if item.kind == "String" and item.multiline:
        args.append("multiline=True")
    if item.kind == "Choice":
        values = [value.strip() for value in item.choices.split(",") if value.strip()]
        if not values or len(set(values)) != len(values):
            raise ValueError(f"{owner}.{item.name}: укажите уникальные варианты через запятую.")
        args.extend((f"choices={[(v, v) for v in values]!r}", "multi=False"))
    for option in ("required", "read_only", "indexed", "unique"):
        if getattr(item, option):
            args.append(f"{option}=True")
    if item.default:
        default = item.default
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
        if item.kind not in ("String", "Choice", "Date", "DateTime", "Time", "Decimal"):
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


@dataclass
class StudioRegister:
    name: str
    kind: str = "Накопление"
    members: list[StudioRegisterMember] = field(default_factory=list)

    def source(self, entity_names: set[str], imports: set[str]) -> str:
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
    lines = ["", "    def get_movements(self) -> list[Movement]:", "        movements: list[Movement] = []"]
    for rule in entity.movements:
        register = registers.get(rule.register)
        if register is None:
            raise ValueError(f"{entity.label}: регистр «{rule.register}» не найден.")
        if register.kind == "Сведения" and not rule.income:
            raise ValueError(f"{register.name}: у регистра сведений нет расхода.")
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
        lines.extend((
            f"{indent}movements.append(Movement(",
            f"{indent}    {register.name},",
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


def report_group_source(config: StudioReport) -> list[str]:
    if not config.group_by:
        return []
    if len(set(config.group_by)) != len(config.group_by) or not set(config.group_by) <= set(config.columns):
        raise ValueError(f"{config.title}: группировать можно по выбранным колонкам без повторений.")
    return [f"    group_by = {tuple(config.group_by)!r}"]


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
    imports.update(("Column", "Database", "Report", "report", "register_report_rows"))
    lines = [
        "@report", f"class {config.name}(Report):", f"    title = {config.title!r}",
        "    auto_run = True", *report_group_source(config), "    columns: ClassVar[list[Column]] = [",
    ]
    for column in config.columns:
        member = fields[column]
        is_number = member.kind == "Ресурс" and member.value_type in RESOURCE_TYPES
        options = ", align='right'" if is_number else ""
        if is_number and config.totals and column not in config.group_by:
            options += ", aggregate='sum'"
        lines.append(f"        Column({member.label!r}, {column!r}{options}),")
    lines.append("    ]")
    if config.as_of:
        imports.update(("DateParam", "datetime"))
        lines.append("    through = DateParam('На дату')")
    if selected is not None:
        imports.add("RefParam")
        lines.append(f"    selected = RefParam({selected.label!r}, {selected.target!r})")
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
    lines.extend((
        "        return register_report_rows(",
        f"            db, {register.name}, {config.columns!r},",
        *(f"            {argument}," for argument in extras),
        "        )",
    ))
    return "\n".join(lines)


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
                    if item.kind == "Choice" and raw not in [value.strip() for value in item.choices.split(",")]:
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
        statements[record.key] = f"db.save({record.entity}({', '.join(arguments)}), validate=True)"
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
            entity.list_columns = [new if name == old else name for name in entity.list_columns]
            if entity.list_order.removeprefix("-") == old:
                entity.list_order = ("-" if entity.list_order.startswith("-") else "") + new
            if entity.numbering_field == old:
                entity.numbering_field = new
            for report in self.reports:
                if report.source_kind == "entity" and report.entity == entity.name:
                    report.columns = [new if name == old else name for name in report.columns]
                    report.group_by = [new if name == old else name for name in report.group_by]
                    if report.ref_filter == old:
                        report.ref_filter = new
                    if report.order_by.removeprefix("-") == old:
                        report.order_by = ("-" if report.order_by.startswith("-") else "") + new
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
            for link in destination.fields:
                if not part:
                    if destination is entity:
                        _rename_key(link.basis_fields, old, new)
                    if link.target == entity.name:
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
    def read(cls, path: Path) -> StudioProject:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            raise ValueError("Версия проекта не поддерживается.")
        return cls(
            title=raw["title"], app_id=raw["app_id"], logic=raw.get("logic", ""),
            imports=raw.get("imports", ""),
            preview_role=raw.get("preview_role", ""),
            local_accounts=raw.get("local_accounts", False),
            column_renames=raw.get("column_renames", {}),
            table_part_renames=raw.get("table_part_renames", {}),
            initial_data=[StudioSeed(**item) for item in raw.get("initial_data", [])],
            reports=[StudioReport(**item) for item in raw.get("reports", [])],
            registers=[
                StudioRegister(item["name"], item["kind"], [StudioRegisterMember(**m) for m in item["members"]])
                for item in raw.get("registers", [])
            ],
            entities=[
                StudioEntity(**{
                    **item, "fields": [StudioField(**f) for f in item["fields"]],
                    "print_form": StudioPrintForm(**item["print_form"]) if item.get("print_form") else None,
                    "workflow": read_workflow(item.get("workflow")),
                    "movements": [StudioMovement(**m) for m in item.get("movements", [])],
                    "table_parts": [
                        StudioTablePart(
                            part["name"], part["label"], [StudioField(**f) for f in part["fields"]],
                            form_tab=part.get("form_tab", ""),
                        )
                        for part in item.get("table_parts", [])
                    ],
                })
                for item in raw["entities"]
            ],
        )

    def write(self, path: Path) -> None:
        from PySide6.QtCore import QIODevice, QSaveFile

        file = QSaveFile(str(path))
        data = (json.dumps({"version": 1, **asdict(self)}, ensure_ascii=False, indent=2) + "\n").encode()
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
                    valid = controller.kind == "Choice" and value in {
                        option.strip() for option in controller.choices.split(",") if option.strip()
                    }
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
                numbering = {"prefix": entity.numbering_prefix, "field": numbering_field}
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
                lines.append("    " + field_source(item, entity.name, names, imports))
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
                    lines.append("        " + field_source(item, part.name, names, imports))
            if entity.print_form is not None:
                imports.add("PrintForm")
                if entity.print_form.tables:
                    imports.add("PrintTable")
                lines.extend(entity.print_form.source(entity))
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
            if entity.code.strip():
                body = entity.code.strip("\n").splitlines()
                block_spans[len(blocks)] = StudioSourceSpan(
                    len(lines) + 2, len(body), len(entity.code) - len(entity.code.lstrip("\n")) + 1,
                    "Код объекта", entity.code, entity.name,
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
            fields = {item.name: item for item in by_entity[config.entity].fields}
            if not config.columns or any(col not in fields for col in config.columns):
                raise ValueError(f"{config.title}: выберите существующие поля для колонок.")
            if config.order_by and config.order_by.removeprefix("-") not in fields:
                raise ValueError(f"{config.title}: выберите существующее поле для сортировки.")
            if config.ref_filter and (
                config.ref_filter not in fields or fields[config.ref_filter].kind != "Ref"
            ):
                raise ValueError(f"{config.title}: для отбора нужно поле-ссылка.")
            names.add(config.name)
            imports.update(("Column", "Database", "Report", "report"))
            lines = [
                "@report", f"class {config.name}(Report):", f"    title = {config.title!r}",
                "    auto_run = True", *report_group_source(config), "    columns: ClassVar[list[Column]] = [",
            ]
            for column in config.columns:
                item = fields[column]
                is_measure = item.kind in ("Integer", "Decimal") and column not in config.group_by
                options = ", aggregate='sum', align='right'" if is_measure and config.totals else ""
                lines.append(f"        Column({item.label!r}, {column!r}{options}),")
            lines.append("    ]")
            if config.ref_filter:
                filter_field = fields[config.ref_filter]
                imports.add("RefParam")
                lines.append(f"    selected = RefParam({filter_field.label!r}, {filter_field.target!r})")
            lines.extend(("", "    def execute(self, db: Database, **params: object) -> list[dict[str, object]]:"))
            filters = ""
            if config.ref_filter:
                lines.extend((
                    '        selected = params.get("selected")',
                    (
                        f"        filters = {{{config.ref_filter!r}: selected}} if selected is not None "
                        f"and db.can_read_field({config.entity}, {config.ref_filter!r}) else {{}}"
                    ),
                ))
                filters = ", **filters"
            ordering = ""
            if config.order_by:
                sort_field = config.order_by.removeprefix("-")
                lines.append(
                    f"        order = {config.order_by!r} if db.can_read_field({config.entity}, {sort_field!r}) else None"
                )
                ordering = ", order_by=order"
            lines.append(f"        records = db.filter({config.entity}{ordering}{filters})")
            references = {col: fields[col].target for col in config.columns if fields[col].kind == "Ref"}
            if references:
                lines.append("        labels = {")
                for col, target in references.items():
                    lines.append(f"            {col!r}: {{item.id: str(item) for item in db.list({target})}},")
                lines.append("        }")
            lines.extend(("        rows: list[dict[str, object]] = []", "        for record in records:"))
            lines.append(f"            row: dict[str, object] = {{name: getattr(record, name) for name in {config.columns!r}}}")
            if references:
                default_refs = tuple(name for name in config.group_by if name in references)
                lines.extend((
                    f"            group_refs = {tuple(references)!r} if self._interactive_grouping else {default_refs!r}",
                    "            if group_refs:",
                    "                row['_group_keys'] = {name: row[name] for name in group_refs}",
                ))
                lines.extend((
                    "            for name, choices in labels.items():",
                    "                value = row[name]",
                    "                if isinstance(value, int):",
                    "                    row[name] = choices.get(value, value)",
                ))
            lines.extend((
                f"            row.update(_entity_class={config.entity}, _entity_id=record.id)",
                "            rows.append(row)",
                "        return rows",
            ))
            blocks.append("\n".join(lines))
        register_blocks = []
        for register in self.registers:
            if register.name in names:
                raise ValueError(f"Имя регистра «{register.name}» уже используется.")
            register_blocks.append(register.source(set(by_entity), imports))
            names.add(register.name)
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
        start = self.entities[0].name
        db_arg = f", db_path={str(preview_database)!r}" if preview_database is not None else ""
        rename_arg = ", column_renames=_DECUI_COLUMN_RENAMES" if self.column_renames else ""
        part_arg = ", table_part_renames=_DECUI_TABLE_PART_RENAMES" if self.table_part_renames else ""
        auth_arg = ", local_accounts=True" if (self.local_accounts or backup_has_accounts) and not preview_role else ""
        seed_arg = ", first_run=_seed_initial_data" if seed and backup_has_accounts is None else ""
        parse_studio_code(self.imports, "Код перед объектами")
        custom_code = parse_studio_code(self.logic, "Код после объектов")
        has_setup = any(isinstance(node, ast.FunctionDef) and node.name == "configure_app" for node in custom_code.body)
        setup_line = "\n    configure_app(app)" if has_setup else ""
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
                f"title={self.title!r}, app_id={self.app_id!r}, start={start}{db_arg}{auth_arg}{seed_arg}{rename_arg}{part_arg})"
                f"{setup_line}{role_line}\n    raise SystemExit(app.run())"
            ),
        ):
            append(section)
        source = "\n\n\n".join(chunks) + "\n"
        try:
            compile(source, "app.py", "exec")
        except SyntaxError as exc:
            raise StudioSyntaxError(exc, "app.py", source=source) from exc
        return source

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
