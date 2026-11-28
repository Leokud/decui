"""Static AST importer converting DECUI Python apps into StudioProject models."""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

from decui.studio_project import (
    StudioEntity,
    StudioField,
    StudioFormConfig,
    StudioFormNode,
    StudioPrintForm,
    StudioProject,
    StudioRegister,
    StudioRegisterMember,
    StudioReminder,
    StudioReport,
    StudioTablePart,
    StudioTransition,
    StudioWorkflow,
    identifier,
)

_ENTITY_DECORATORS = {
    "catalog": "Справочник",
    "master_data": "Справочник",
    "lookup": "Справочник",
    "document": "Документ",
    "transaction": "Документ",
}
_ENTITY_BASES = {
    "Catalog": "Справочник",
    "MasterData": "Справочник",
    "Lookup": "Справочник",
    "Document": "Документ",
    "Transaction": "Документ",
}
_REGISTER_DECORATORS = {
    "accumulation": "Накопление",
    "ledger": "Накопление",
    "balance_ledger": "Накопление",
    "information": "Сведения",
    "history_register": "Сведения",
    "value_history": "Сведения",
}
_FIELD_CALLS = {
    "String": "String",
    "StringField": "String",
    "Integer": "Integer",
    "IntegerField": "Integer",
    "Decimal": "Decimal",
    "D": "Decimal",
    "DecimalField": "Decimal",
    "Boolean": "Boolean",
    "BooleanField": "Boolean",
    "Date": "Date",
    "DateField": "Date",
    "DateTime": "DateTime",
    "DateTimeField": "DateTime",
    "Time": "Time",
    "TimeField": "Time",
    "Choice": "Choice",
    "ChoiceField": "Choice",
    "Ref": "Ref",
    "RefField": "Ref",
}
_TAB_DECORATORS = {"tab_part", "line_items", "child_table"}
_CMD_DECORATORS = {"command", "action"}
_PY_FIELD_TYPE_MAP = {
    "str": "String",
    "int": "Integer",
    "float": "Decimal",
    "Decimal": "Decimal",
    "bool": "Boolean",
    "date": "Date",
    "datetime": "DateTime",
    "time": "Time",
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _const_eval(node: ast.AST, env: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        return node.id
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _const_eval(node.operand, env)
        if isinstance(inner, (int, float)):
            return -inner
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_const_eval(item, env) for item in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _const_eval(k, env): _const_eval(v, env)
            for k, v in zip(node.keys, node.values, strict=True)
            if k is not None
        }
    return ast.literal_eval(node)


def _safe_eval(node: ast.AST, env: dict[str, object]) -> object | None:
    try:
        return _const_eval(node, env)
    except (ValueError, TypeError, SyntaxError):
        return None


def _default_to_studio(kind: str, node: ast.AST | None, env: dict[str, object]) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name == "today" or (isinstance(node.func, ast.Attribute) and node.func.attr == "today"):
            return "today"
        if name == "now" or (isinstance(node.func, ast.Attribute) and node.func.attr == "now"):
            return "now"
        if name == "fromisoformat" and node.args:
            val = _safe_eval(node.args[0], env)
            return str(val) if val is not None else ""
        if name in ("Decimal", "DecimalValue", "D") and node.args:
            val = _safe_eval(node.args[0], env)
            return str(val) if val is not None else ""
    if isinstance(node, ast.Lambda):
        seg = ast.unparse(node.body)
        if "today" in seg:
            return "today"
        if "now" in seg and kind in ("DateTime", "Time"):
            return "now"
        return ""
    val = _safe_eval(node, env)
    if val is None:
        return ""
    if isinstance(val, bool):
        return "True" if val else "False"
    return str(val)


def _parse_field(
    attr_name: str,
    call: ast.Call,
    env: dict[str, object],
    entity_names: set[str],
) -> StudioField | None:
    kind = _FIELD_CALLS.get(_call_name(call.func))
    if kind is None:
        return None
    kw = {k.arg: k.value for k in call.keywords if k.arg}
    pos = list(call.args)
    target = ""
    if kind == "Ref":
        if pos:
            raw_t = pos.pop(0)
            target = raw_t.id if isinstance(raw_t, ast.Name) else str(_safe_eval(raw_t, env) or "")
        elif "target" in kw:
            raw_t = kw["target"]
            target = raw_t.id if isinstance(raw_t, ast.Name) else str(_safe_eval(raw_t, env) or "")
        if target and target not in entity_names:
            entity_names.add(target)

    label_val = _safe_eval(pos[0], env) if pos else _safe_eval(kw["display_name"], env) if "display_name" in kw else None
    label = str(label_val) if label_val else attr_name.replace("_", " ").capitalize()

    def kw_bool(key: str, default: bool = False) -> bool:
        val = _safe_eval(kw[key], env) if key in kw else None
        return bool(val) if isinstance(val, bool) else default

    def kw_str(key: str, default: str = "") -> str:
        val = _safe_eval(kw[key], env) if key in kw else None
        return str(val) if val is not None else default

    def kw_int(key: str, default: int | None = None) -> int | None:
        val = _safe_eval(kw[key], env) if key in kw else None
        return val if isinstance(val, int) and not isinstance(val, bool) else default

    choices_str = ""
    if kind == "Choice" and "choices" in kw:
        raw_choices = _safe_eval(kw["choices"], env)
        if isinstance(raw_choices, list):
            labels: list[str] = []
            for entry in raw_choices:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    c_code, c_lbl = str(entry[0]), str(entry[1])
                    labels.append(c_lbl if c_code == c_lbl else f"{c_code}: {c_lbl}")
                elif isinstance(entry, (list, tuple)) and len(entry) == 1:
                    labels.append(str(entry[0]))
                else:
                    labels.append(str(entry))
            choices_str = ", ".join(labels)
    multi = kw_bool("multi", True) if kind == "Choice" else False

    choice_filter: dict[str, str] = {}
    if kind == "Ref" and "choice_filter" in kw:
        raw_cf = _safe_eval(kw["choice_filter"], env)
        if isinstance(raw_cf, dict):
            choice_filter = {str(k): str(v) for k, v in raw_cf.items() if identifier(str(k)) and identifier(str(v))}

    basis_fields: dict[str, str] = {}
    if kind == "Ref" and "basis_fields" in kw:
        raw_bf = _safe_eval(kw["basis_fields"], env)
        if isinstance(raw_bf, dict):
            basis_fields = {str(k): str(v) for k, v in raw_bf.items()}

    basis_tables: dict[str, dict[str, object]] = {}
    if kind == "Ref" and "basis_tables" in kw:
        raw_bt = _safe_eval(kw["basis_tables"], env)
        if isinstance(raw_bt, dict):
            basis_tables = raw_bt

    def kw_num_str(key: str) -> str:
        if key not in kw:
            return ""
        return _default_to_studio(kind, kw[key], env)

    return StudioField(
        name=attr_name,
        label=label,
        kind=kind,
        required=kw_bool("required", False),
        multiline=kw_bool("multiline", False),
        target=target,
        related_list=kw_str("related_list"),
        choices=choices_str,
        default=_default_to_studio(kind, kw.get("default"), env),
        read_only=kw_bool("read_only", False),
        indexed=kw_bool("indexed", kind == "Ref"),
        unique=kw_bool("unique", False),
        cascade=kw_str("on_delete").upper() == "CASCADE",
        form_group="",
        form_tab="",
        create_from=kw_str("create_from"),
        basis_fields=basis_fields,
        basis_tables=basis_tables,  # type: ignore[arg-type]
        choice_filter=choice_filter,
        max_length=kw_int("max_length"),
        min_value=kw_num_str("min_value"),
        max_value=kw_num_str("max_value"),
        precision=kw_int("precision", 10) or 10,
        scale=kw_int("scale", 2) if kw_int("scale", 2) is not None else 2,
        currency=kw_str("currency"),
        multi=multi,
        representation_field=kw_str("representation_field"),
        auto_fill=kw_str("auto_fill"),
        visible=kw_bool("visible", True),
        placeholder=kw_str("placeholder"),
    )


def _parse_workflow(node: ast.Call, env: dict[str, object]) -> StudioWorkflow | None:
    kw = {k.arg: k.value for k in node.keywords if k.arg}
    args = list(node.args)
    field_name = str(_safe_eval(args[0], env) if args else _safe_eval(kw.get("field", ast.Constant("approval_status")), env) or "approval_status")
    initial = str(_safe_eval(args[1], env) if len(args) > 1 else _safe_eval(kw.get("initial", ast.Constant("")), env) or "")
    states_raw = _safe_eval(args[2], env) if len(args) > 2 else _safe_eval(kw.get("states", ast.Tuple([])), env)
    states = [str(s) for s in states_raw] if isinstance(states_raw, list) else []
    trans_node = args[3] if len(args) > 3 else kw.get("transitions")
    transitions: list[StudioTransition] = []
    if isinstance(trans_node, (ast.Tuple, ast.List)):
        for elem in trans_node.elts:
            if isinstance(elem, ast.Call) and _call_name(elem.func) == "Transition":
                tkw = {k.arg: k.value for k in elem.keywords if k.arg}
                targs = list(elem.args)
                lbl = str(_safe_eval(targs[0], env) if targs else _safe_eval(tkw.get("label", ast.Constant("")), env) or "")
                src = str(_safe_eval(targs[1], env) if len(targs) > 1 else _safe_eval(tkw.get("source", ast.Constant("")), env) or "")
                tgt = str(_safe_eval(targs[2], env) if len(targs) > 2 else _safe_eval(tkw.get("target", ast.Constant("")), env) or "")
                roles_raw = _safe_eval(tkw["roles"], env) if "roles" in tkw else []
                roles = [str(r) for r in roles_raw] if isinstance(roles_raw, list) else []
                assigned_only = bool(_safe_eval(tkw["assigned_only"], env)) if "assigned_only" in tkw else False
                transitions.append(StudioTransition(lbl, src, tgt, roles, assigned_only))
    post_raw = _safe_eval(kw["post_states"], env) if "post_states" in kw else []
    post_states = [str(s) for s in post_raw] if isinstance(post_raw, list) else []
    if not states:
        return None
    return StudioWorkflow(states, initial or states[0], transitions, post_states, field_name)


def _parse_form_node(node: ast.AST, env: dict[str, object]) -> StudioFormNode | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return StudioFormNode("field", target=node.value)
    if not isinstance(node, ast.Call):
        return None
    cname = _call_name(node.func)
    kw = {k.arg: k.value for k in node.keywords if k.arg}
    args = list(node.args)

    if cname == "FormField":
        target = str(_safe_eval(args[0], env) if args else _safe_eval(kw.get("name", ast.Constant("")), env) or "")
        lbl = str(_safe_eval(kw["label"], env) or "") if "label" in kw else ""
        ph = str(_safe_eval(kw["placeholder"], env) or "") if "placeholder" in kw else ""
        ro = _safe_eval(kw["read_only"], env) if "read_only" in kw else None
        st = _safe_eval(kw["stretch"], env) if "stretch" in kw else 1
        mw = _safe_eval(kw["min_width"], env) if "min_width" in kw else 0
        vf, vv = "", True
        if "visible_when" in kw:
            vw = _safe_eval(kw["visible_when"], env)
            if isinstance(vw, list) and len(vw) == 2:
                vf = str(vw[0])
                vv = vw[1] if isinstance(vw[1], bool) else str(vw[1])
        return StudioFormNode(
            "field", target=target, label=lbl, placeholder=ph,
            read_only=ro if isinstance(ro, bool) else None,
            stretch=int(st) if isinstance(st, int) else 1,
            min_width=int(mw) if isinstance(mw, int) else 0,
            visibility_field=vf, visibility_value=vv,
        )
    if cname == "FormTable":
        target = str(_safe_eval(args[0], env) if args else _safe_eval(kw.get("name", ast.Constant("")), env) or "")
        title = str(_safe_eval(kw["title"], env) or "") if "title" in kw else ""
        mh = _safe_eval(kw["min_height"], env) if "min_height" in kw else 0
        ro = bool(_safe_eval(kw["read_only"], env)) if "read_only" in kw else False
        return StudioFormNode("table", target=target, title=title, min_height=int(mh) if isinstance(mh, int) else 0, read_only=ro)
    if cname == "FormButton":
        cmd = str(_safe_eval(args[0], env) if args else _safe_eval(kw.get("command", ast.Constant("")), env) or "")
        lbl = str(_safe_eval(kw["label"], env) or "") if "label" in kw else ""
        prim = bool(_safe_eval(kw["primary"], env)) if "primary" in kw else False
        return StudioFormNode("button", target=cmd, label=lbl, primary=prim)
    if cname == "FormLabel":
        txt = str(_safe_eval(args[0], env) if args else _safe_eval(kw.get("text", ast.Constant("")), env) or "")
        style = str(_safe_eval(kw["style"], env) or "normal") if "style" in kw else "normal"
        return StudioFormNode("label", title=txt, style=style)
    if cname == "FormDivider":
        return StudioFormNode("divider")
    if cname in ("FormRow", "FormColumn", "FormGroup", "FormTab", "FormTabs"):
        children: list[StudioFormNode] = []
        if cname in ("FormGroup", "FormTab"):
            title = str(_safe_eval(args[0], env) if args else _safe_eval(kw.get("title", ast.Constant("")), env) or "")
            child_asts = args[1:]
        else:
            title = ""
            child_asts = args
        for ch in child_asts:
            parsed = _parse_form_node(ch, env)
            if parsed is None:
                return None
            children.append(parsed)
        if cname == "FormRow":
            sp = _safe_eval(kw["spacing"], env) if "spacing" in kw else 12
            return StudioFormNode("row", spacing=int(sp) if isinstance(sp, int) else 12, children=children)
        if cname == "FormColumn":
            st = _safe_eval(kw["stretch"], env) if "stretch" in kw else 1
            sp = _safe_eval(kw["spacing"], env) if "spacing" in kw else 8
            return StudioFormNode("column", stretch=int(st) if isinstance(st, int) else 1, spacing=int(sp) if isinstance(sp, int) else 8, children=children)
        if cname == "FormGroup":
            coll = bool(_safe_eval(kw["collapsible"], env)) if "collapsible" in kw else False
            colp = bool(_safe_eval(kw["collapsed"], env)) if "collapsed" in kw else False
            return StudioFormNode("group", title=title, collapsible=coll, collapsed=colp, children=children)
        if cname == "FormTab":
            return StudioFormNode("tab", title=title, children=children)
        return StudioFormNode("tabs", children=children)
    return None


def _parse_form(node: ast.Call, env: dict[str, object]) -> StudioFormConfig | None:
    if _call_name(node.func) != "Form":
        return None
    kw = {k.arg: k.value for k in node.keywords if k.arg}
    items: list[StudioFormNode] = []
    for arg in node.args:
        parsed = _parse_form_node(arg, env)
        if parsed is None:
            return None
        items.append(parsed)
    title = str(_safe_eval(kw["title"], env) or "") if "title" in kw else ""
    inc = bool(_safe_eval(kw["include_remaining"], env)) if "include_remaining" in kw else False
    show_act = bool(_safe_eval(kw["show_actions"], env)) if "show_actions" in kw else True
    return StudioFormConfig(enabled=True, title=title, include_remaining=inc, show_actions=show_act, items=items)


def _parse_print_form(node: ast.Call, env: dict[str, object]) -> StudioPrintForm | None:
    kw = {k.arg: k.value for k in node.keywords if k.arg}
    args = list(node.args)
    title = str(_safe_eval(args[0], env) if args else _safe_eval(kw.get("title", ast.Constant("")), env) or "")
    fields_raw = _safe_eval(args[1], env) if len(args) > 1 else _safe_eval(kw.get("fields", ast.Tuple([])), env)
    fields = [str(f) for f in fields_raw] if isinstance(fields_raw, list) else []
    tables_node = args[2] if len(args) > 2 else kw.get("tables")
    tables: dict[str, list[str]] = {}
    if isinstance(tables_node, (ast.Tuple, ast.List)):
        for elem in tables_node.elts:
            if isinstance(elem, ast.Call) and _call_name(elem.func) == "PrintTable":
                tkw = {k.arg: k.value for k in elem.keywords if k.arg}
                targs = list(elem.args)
                part = str(_safe_eval(targs[0], env) if targs else _safe_eval(tkw.get("part", ast.Constant("")), env) or "")
                cols_raw = _safe_eval(targs[1], env) if len(targs) > 1 else _safe_eval(tkw.get("columns", ast.Tuple([])), env)
                if part and isinstance(cols_raw, list):
                    tables[part] = [str(c) for c in cols_raw]
    return StudioPrintForm(title, fields, tables) if title else None


def _parse_visual_report(
    cls: ast.ClassDef, env: dict[str, object], entity_names: set[str],
) -> StudioReport | None:
    """Detect Studio-compatible register or entity reports for visual editing."""
    title = cls.name
    section = ""
    auto_run = True
    sortable = True
    group_by: list[str] = []
    columns: list[str] = []
    col_options: dict[str, dict[str, str]] = {}
    as_of = False
    ref_filter = ""
    ref_from_rec = False
    chart_label = ""
    chart_value = ""
    chart_title = ""
    chart_color = ""
    has_date_range = False
    choice_params: list[str] = []
    string_params: list[str] = []
    execute_fn: ast.FunctionDef | None = None

    def collect_column(elem: ast.Call) -> bool:
        ckw = {k.arg: k.value for k in elem.keywords if k.arg}
        cargs = list(elem.args)
        f_name = _safe_eval(cargs[1], env) if len(cargs) >= 2 else _safe_eval(ckw.get("field", ast.Constant("")), env)
        if not isinstance(f_name, str) or not f_name:
            return False
        columns.append(f_name)
        opts: dict[str, str] = {}
        for opt in ("aggregate", "format", "prefix", "suffix", "align"):
            if opt in ckw:
                v = _safe_eval(ckw[opt], env)
                if v not in (None, ""):
                    opts[opt] = str(v)
        if "width" in ckw:
            w = _safe_eval(ckw["width"], env)
            if isinstance(w, int):
                opts["width"] = str(w)
        if opts:
            col_options[f_name] = opts
        return True

    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            key = stmt.targets[0].id
            val = _safe_eval(stmt.value, env)
            if key == "title" and isinstance(val, str):
                title = val
            elif key in ("__section__", "__subsystem__") and isinstance(val, str):
                section = val
            elif key == "auto_run" and isinstance(val, bool):
                auto_run = val
            elif key == "sortable" and isinstance(val, bool):
                sortable = val
            elif key == "group_by" and isinstance(val, list):
                group_by = [str(x) for x in val]
            elif key == "chart" and isinstance(val, dict):
                chart_label = str(val.get("label", ""))
                chart_value = str(val.get("value", ""))
                chart_title = str(val.get("title", ""))
                chart_color = str(val.get("color", ""))
            elif key == "columns" and isinstance(stmt.value, ast.List):
                for elem in stmt.value.elts:
                    if not (isinstance(elem, ast.Call) and _call_name(elem.func) == "Column" and collect_column(elem)):
                        return None
            elif isinstance(stmt.value, ast.Call):
                pname = _call_name(stmt.value.func)
                if pname == "DateParam":
                    as_of = True
                    if key in ("date_from", "date_to"):
                        has_date_range = True
                elif pname == "RefParam":
                    pkw = {k.arg: k.value for k in stmt.value.keywords if k.arg}
                    ref_from_rec = bool(_safe_eval(pkw.get("from_record", ast.Constant(False)), env))
                    if key not in ("filters",):
                        ref_filter = ref_filter or key
                elif pname == "ChoiceParam":
                    choice_params.append(key)
                elif pname == "StringParam":
                    string_params.append(key)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "columns" and isinstance(stmt.value, ast.List):
            for elem in stmt.value.elts:
                if not (isinstance(elem, ast.Call) and _call_name(elem.func) == "Column" and collect_column(elem)):
                    return None
        elif isinstance(stmt, ast.FunctionDef) and stmt.name == "execute":
            execute_fn = stmt

    if execute_fn is None or not columns:
        return None
    totals = any(opts.get("aggregate") == "sum" for opts in col_options.values())

    # Branch 1: register report (returns register_report_rows(...))
    ret = next((s for s in execute_fn.body if isinstance(s, ast.Return)), None)
    if ret is not None and isinstance(ret.value, ast.Call) and _call_name(ret.value.func) == "register_report_rows":
        rkw = {k.arg: k.value for k in ret.value.keywords if k.arg}
        rargs = list(ret.value.args)
        if len(rargs) < 2 or not isinstance(rargs[1], ast.Name):
            return None
        reg_name = rargs[1].id
        order_by = str(_safe_eval(rkw["order_by"], env)) if "order_by" in rkw else ""
        latest_only = bool(_safe_eval(rkw["latest_only"], env)) if "latest_only" in rkw else False
        if "ref_filter" in rkw:
            rf = _safe_eval(rkw["ref_filter"], env)
            if isinstance(rf, str):
                ref_filter = rf
        return StudioReport(
            name=cls.name,
            title=title,
            entity=reg_name,
            columns=columns,
            order_by=order_by,
            ref_filter=ref_filter,
            source_kind="register",
            as_of=as_of,
            totals=totals,
            group_by=group_by,
            section=section,
            auto_run=auto_run,
            ref_filter_from_record=ref_from_rec,
            column_options=col_options,
            latest_only=latest_only,
            sortable=sortable,
            chart_label=chart_label,
            chart_value=chart_value,
            chart_title=chart_title,
            chart_color=chart_color,
        )

    # Branch 2: Studio-generated entity report (db.filter(Entity, ...) + row.update(_entity_class=Entity, ...))
    entity_name = ""
    order_by = ""
    posted_only = False
    has_entity_tag = False
    table_part = ""
    date_filter = ""
    choice_filter_field = ""
    text_filter_field = ""
    detected_ref = ""
    for node in ast.walk(execute_fn):
        if isinstance(node, ast.Call) and _call_name(node.func) == "filter" and node.args and isinstance(node.args[0], ast.Name):
            if node.args[0].id in entity_names:
                entity_name = node.args[0].id
                for kw in node.keywords:
                    if kw.arg == "order_by":
                        ov = _safe_eval(kw.value, env)
                        if isinstance(ov, str):
                            order_by = ov
                    elif kw.arg == "posted" and _safe_eval(kw.value, env) is True:
                        posted_only = True
        elif isinstance(node, ast.Call) and _call_name(node.func) == "update":
            for kw in node.keywords:
                if kw.arg == "_entity_class" and isinstance(kw.value, ast.Name) and kw.value.id in entity_names:
                    has_entity_tag = True
        elif isinstance(node, ast.Call) and _call_name(node.func) == "getattr":
            if (
                len(node.args) == 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "record"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                attr_s = node.args[1].value
                # Table part loops look like: for line in getattr(record, 'Items'):
                for parent in ast.walk(execute_fn):
                    if isinstance(parent, ast.For) and parent.iter is node:
                        table_part = attr_s
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "filters":
            s_val = _safe_eval(node.slice, env)
            if isinstance(s_val, str) and s_val.endswith("__gte") and has_date_range:
                date_filter = s_val.removesuffix("__gte")
            elif isinstance(s_val, str) and "__" not in s_val and s_val != "posted":
                # Could be ref_filter or choice_filter_field
                for parent in ast.walk(execute_fn):
                    if isinstance(parent, ast.Assign) and node in parent.targets:
                        val_name = parent.value.id if isinstance(parent.value, ast.Name) else ""
                        if val_name and val_name in choice_params:
                            choice_filter_field = s_val
                        elif val_name:
                            detected_ref = s_val
        elif isinstance(node, ast.Compare) and string_params:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value in columns:
                    text_filter_field = sub.value

    if entity_name and has_entity_tag:
        return StudioReport(
            name=cls.name,
            title=title,
            entity=entity_name,
            columns=columns,
            order_by=order_by,
            ref_filter=detected_ref,
            source_kind="entity",
            as_of=False,
            totals=totals,
            group_by=group_by,
            section=section,
            auto_run=auto_run,
            table_part=table_part,
            date_filter=date_filter,
            posted_only=posted_only,
            ref_filter_from_record=ref_from_rec,
            column_options=col_options,
            sortable=sortable,
            choice_filter_field=choice_filter_field,
            text_filter_field=text_filter_field,
            chart_label=chart_label,
            chart_value=chart_value,
            chart_title=chart_title,
            chart_color=chart_color,
        )
    return None


def import_python_project(
    source: str,
    *,
    default_title: str = "Импортированный проект",
    default_app_id: str = "imported-app",
) -> tuple[StudioProject, list[str]]:
    """Parse a DECUI Python source file into a StudioProject and summary notes."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"Синтаксическая ошибка в Python-файле (стр. {exc.lineno or 1}): {exc.msg}") from exc

    env: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            val = _safe_eval(node.value, env)
            if val is not None:
                env[node.targets[0].id] = val

    entities: list[StudioEntity] = []
    registers: list[StudioRegister] = []
    reports: list[StudioReport] = []
    reminders: list[StudioReminder] = []
    import_lines: list[str] = []
    logic_blocks: list[str] = []
    notes: list[str] = []
    entity_form_links: dict[str, str] = {}
    parsed_form_classes: dict[str, tuple[str, StudioFormConfig | None, str]] = {}

    entity_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and (
            any(_call_name(d) in _ENTITY_DECORATORS for d in node.decorator_list)
            or any(_call_name(b) in _ENTITY_BASES for b in node.bases)
        )
    }

    title = default_title
    app_id = default_app_id
    width, height = 1200, 800
    local_accounts = False
    start_target = ""
    column_renames: dict[str, dict[str, str]] = {}
    table_part_renames: dict[str, dict[str, str]] = {}
    first_run_fn: str = ""

    def inspect_app_calls(body: list[ast.stmt]) -> None:
        nonlocal title, app_id, width, height, local_accounts, start_target, first_run_fn
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if not isinstance(node, ast.Call):
                continue
            cname = _call_name(node.func)
            if cname == "ERPApplication":
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                if "title" in kw and isinstance(v := _safe_eval(kw["title"], env), str):
                    title = v
                if "app_id" in kw and isinstance(v := _safe_eval(kw["app_id"], env), str):
                    app_id = v
                elif "db_path" in kw and isinstance(v := _safe_eval(kw["db_path"], env), str):
                    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(v).stem).strip("-_")
                    if slug and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", slug):
                        app_id = slug
                if "window_size" in kw:
                    ws = _safe_eval(kw["window_size"], env)
                    if isinstance(ws, list) and len(ws) == 2 and all(isinstance(x, int) for x in ws):
                        width, height = ws[0], ws[1]
                if "local_accounts" in kw and isinstance(v := _safe_eval(kw["local_accounts"], env), bool):
                    local_accounts = v
                if "start" in kw and isinstance(kw["start"], ast.Name):
                    start_target = kw["start"].id
                if "first_run" in kw and isinstance(kw["first_run"], ast.Name):
                    first_run_fn = kw["first_run"].id
            elif cname == "enable_reminders" and node.args:
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                targets: list[str] = []
                if isinstance(node.args[0], (ast.List, ast.Tuple)):
                    targets = [el.id for el in node.args[0].elts if isinstance(el, ast.Name)]
                elif isinstance(node.args[0], ast.Name):
                    targets = [node.args[0].id]
                date_f = str(_safe_eval(kw["date_field"], env)) if "date_field" in kw else "deadline"
                done_f = str(_safe_eval(kw["done_field"], env)) if "done_field" in kw else "is_completed"
                title_f = str(_safe_eval(kw["title_field"], env)) if "title_field" in kw else "name"
                mins = _safe_eval(kw["notify_before_minutes"], env) if "notify_before_minutes" in kw else 60
                for ent_name in targets:
                    reminders.append(StudioReminder(
                        ent_name, date_f, done_f, title_f,
                        mins if isinstance(mins, int) else 60,
                    ))

    for node in tree.body:
        seg = ast.get_source_segment(source, node) or ""
        if isinstance(node, ast.Import):
            if not any(alias.name == "decui" for alias in node.names):
                import_lines.append(seg)
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module in ("__future__", "decui"):
                # Keep non-core imports from decui only if needed by logic (e.g. QueryReport)
                if node.module == "decui":
                    extras = [
                        alias.name if not alias.asname else f"{alias.name} as {alias.asname}"
                        for alias in node.names
                        if alias.name in (
                            "QueryReport", "StringParam", "IntegerParam", "NumberParam",
                            "DecimalParam", "BooleanParam", "ChoiceParam", "get_balance",
                            "get_ledger_balance", "get_records", "get_latest", "get_slice_last",
                            "set_record", "write_movements", "clear_movements", "define_role",
                            "LedgerEntry", "BarChart", "celebrate_goal_completed",
                        ) or alias.asname is not None
                    ]
                    if extras:
                        import_lines.append(f"from decui import {', '.join(extras)}")
                continue
            import_lines.append(seg)
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            var = node.targets[0].id
            if var == "_DECUI_COLUMN_RENAMES" and isinstance(v := _safe_eval(node.value, env), dict):
                column_renames = v  # type: ignore[assignment]
                continue
            if var == "_DECUI_TABLE_PART_RENAMES" and isinstance(v := _safe_eval(node.value, env), dict):
                table_part_renames = v  # type: ignore[assignment]
                continue
            if var in ("_project_root",):
                continue
            import_lines.append(seg)
            continue
        if isinstance(node, ast.If):
            # Likely `if __name__ == "__main__":`
            inspect_app_calls(node.body)
            continue
        if isinstance(node, ast.Expr):
            # Ignore top-level sys.path.insert etc.
            continue
        if isinstance(node, ast.FunctionDef):
            if node.name == "main":
                inspect_app_calls(node.body)
                continue
            if node.name == "_seed_initial_data":
                continue
            logic_blocks.append(seg)
            continue
        if isinstance(node, ast.ClassDef):
            dec_names = [_call_name(d) for d in node.decorator_list]
            base_names = [_call_name(b) for b in node.bases]
            ent_kind = next((_ENTITY_DECORATORS[d] for d in dec_names if d in _ENTITY_DECORATORS), None) or next(
                (_ENTITY_BASES[b] for b in base_names if b in _ENTITY_BASES), None
            )
            if ent_kind is not None:
                label = node.name
                section = ""
                num_prefix = ""
                num_field = ""
                num_pattern = ""
                check_seq = False
                owner_only = False
                permissions: dict[str, list[str]] | None = None
                form_groups: dict[str, str] = {}
                form_tabs: dict[str, str] = {}
                table_tabs: dict[str, str] = {}
                visible_when: dict[str, tuple[str, bool | str]] = {}
                req_visible: set[str] = set()
                list_columns: list[str] = []
                list_order = ""
                workflow: StudioWorkflow | None = None
                print_form: StudioPrintForm | None = None
                entity_form: StudioFormConfig | None = None
                fields: list[StudioField] = []
                table_parts: list[StudioTablePart] = []
                commands: dict[str, str] = {}
                command_roles: dict[str, list[str]] = {}
                field_write_roles: dict[str, list[str]] = {}
                field_read_roles: dict[str, list[str]] = {}
                method_blocks: list[str] = []

                for stmt in node.body:
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                        continue  # docstring
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                        attr = stmt.targets[0].id
                        val = _safe_eval(stmt.value, env)
                        if attr == "__verbose_name__" and isinstance(val, str):
                            label = val
                        elif attr in ("__section__", "__subsystem__") and isinstance(val, str):
                            section = val
                        elif attr == "__sequence__" and isinstance(val, bool):
                            check_seq = val
                        elif attr == "__owner_field__" and isinstance(val, str):
                            owner_only = True
                        elif attr == "__numbering__" and isinstance(val, dict):
                            num_prefix = str(val.get("prefix", ""))
                            num_field = str(val.get("field", ""))
                            num_pattern = str(val.get("pattern", ""))
                        elif attr == "__form_groups__" and isinstance(val, dict):
                            form_groups = {str(k): str(v) for k, v in val.items()}
                        elif attr == "__form_tabs__" and isinstance(val, dict):
                            form_tabs = {str(k): str(v) for k, v in val.items()}
                        elif attr == "__form_table_tabs__" and isinstance(val, dict):
                            table_tabs = {str(k): str(v) for k, v in val.items()}
                        elif attr == "__form_visible_when__" and isinstance(val, dict):
                            for fk, fv in val.items():
                                if isinstance(fv, list) and len(fv) == 2:
                                    visible_when[str(fk)] = (str(fv[0]), fv[1])  # type: ignore[assignment]
                        elif attr == "__required_when_visible__" and isinstance(val, list):
                            req_visible = {str(x) for x in val}
                        elif attr == "__list_columns__" and isinstance(val, list):
                            list_columns = [str(x) for x in val]
                        elif attr == "__list_order__" and isinstance(val, str):
                            list_order = val
                        elif attr == "__permissions__" and isinstance(val, dict):
                            permissions = {str(k): [str(r) for r in v] for k, v in val.items() if isinstance(v, list)}
                        elif attr == "__field_write_roles__" and isinstance(val, dict):
                            field_write_roles = {str(k): [str(r) for r in v] for k, v in val.items() if isinstance(v, list)}
                        elif attr == "__field_read_roles__" and isinstance(val, dict):
                            field_read_roles = {str(k): [str(r) for r in v] for k, v in val.items() if isinstance(v, list)}
                        elif attr == "__command_roles__" and isinstance(val, dict):
                            command_roles = {str(k): [str(r) for r in v] for k, v in val.items() if isinstance(v, list)}
                        elif attr == "__workflow__" and isinstance(stmt.value, ast.Call):
                            workflow = _parse_workflow(stmt.value, env)
                        elif attr == "__print_form__" and isinstance(stmt.value, ast.Call):
                            print_form = _parse_print_form(stmt.value, env)
                        elif attr in ("__form__", "__form_class__"):
                            if isinstance(stmt.value, ast.Call) and _call_name(stmt.value.func) == "Form":
                                entity_form = _parse_form(stmt.value, env)
                            elif isinstance(stmt.value, ast.Name):
                                entity_form_links[node.name] = stmt.value.id
                        elif isinstance(stmt.value, ast.Call):
                            parsed = _parse_field(attr, stmt.value, env, entity_names)
                            if parsed is not None:
                                fields.append(parsed)
                            elif _call_name(stmt.value.func) == "command":
                                # e.g. mark = command('Отметить')(mark)
                                if stmt.value.args and isinstance(stmt.value.args[0], ast.Call):
                                    cmd_call = stmt.value.args[0]
                                    if cmd_call.args:
                                        lbl = _safe_eval(cmd_call.args[0], env)
                                        if isinstance(lbl, str):
                                            commands[attr] = lbl
                    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        attr = stmt.target.id
                        if attr.startswith("_"):
                            continue
                        if isinstance(stmt.value, ast.Call):
                            parsed = _parse_field(attr, stmt.value, env, entity_names)
                            if parsed is not None:
                                fields.append(parsed)
                        else:
                            ann_name = _call_name(stmt.annotation)
                            inferred = _PY_FIELD_TYPE_MAP.get(ann_name)
                            if inferred is not None:
                                default_str = _default_to_studio(inferred, stmt.value, env)
                                fields.append(StudioField(
                                    attr, attr.replace("_", " ").capitalize(), inferred,
                                    required=stmt.value is None, default=default_str,
                                ))
                    elif isinstance(stmt, ast.ClassDef) and any(_call_name(d) in _TAB_DECORATORS for d in stmt.decorator_list):
                        part_label = stmt.name
                        part_fields: list[StudioField] = []
                        for p_stmt in stmt.body:
                            if isinstance(p_stmt, ast.Assign) and len(p_stmt.targets) == 1 and isinstance(p_stmt.targets[0], ast.Name):
                                p_attr = p_stmt.targets[0].id
                                if p_attr == "__verbose_name__" and isinstance(v := _safe_eval(p_stmt.value, env), str):
                                    part_label = v
                                elif isinstance(p_stmt.value, ast.Call):
                                    pf = _parse_field(p_attr, p_stmt.value, env, entity_names)
                                    if pf is not None:
                                        part_fields.append(pf)
                            elif isinstance(p_stmt, ast.AnnAssign) and isinstance(p_stmt.target, ast.Name) and isinstance(p_stmt.value, ast.Call):
                                pf = _parse_field(p_stmt.target.id, p_stmt.value, env, entity_names)
                                if pf is not None:
                                    part_fields.append(pf)
                        table_parts.append(StudioTablePart(stmt.name, part_label, part_fields))
                    elif isinstance(stmt, ast.FunctionDef):
                        if stmt.name == "__init__":
                            body_str = ast.unparse(stmt.body)
                            if body_str.strip() == "super().__init__(**values)":
                                continue
                        cmd_dec = next((d for d in stmt.decorator_list if isinstance(d, ast.Call) and _call_name(d.func) in _CMD_DECORATORS), None)
                        m_seg = textwrap.dedent(ast.get_source_segment(source, stmt) or "")
                        if cmd_dec is not None:
                            lbl = _safe_eval(cmd_dec.args[0], env) if cmd_dec.args else stmt.name
                            commands[stmt.name] = str(lbl)
                            # Strip the decorator line so Studio generates `@command` cleanly
                            lines = m_seg.splitlines()
                            for i, ln in enumerate(lines):
                                if ln.lstrip().startswith(("def ", "async def ")):
                                    m_seg = "\n".join(lines[i:])
                                    break
                        method_blocks.append(m_seg)

                # Apply layout/role/workflow metadata back onto fields & table parts
                if owner_only:
                    fields = [f for f in fields if f.name != "record_owner"]
                if workflow is not None:
                    fields = [f for f in fields if f.name not in {workflow.field_name, "approval_assignee"}]
                for f in fields:
                    f.form_group = form_groups.get(f.name, "")
                    f.form_tab = form_tabs.get(f.name, "")
                    if f.name in visible_when:
                        f.visibility_field, f.visibility_value = visible_when[f.name]
                    f.required_when_visible = f.name in req_visible
                    if f.name in field_write_roles:
                        f.write_roles = field_write_roles[f.name]
                    if f.name in field_read_roles:
                        f.read_roles = field_read_roles[f.name]
                for part in table_parts:
                    part.form_tab = table_tabs.get(part.name, "")
                    for f in part.fields:
                        key = f"{part.name}.{f.name}"
                        if key in field_write_roles:
                            f.write_roles = field_write_roles[key]
                        if key in field_read_roles:
                            f.read_roles = field_read_roles[key]

                entities.append(StudioEntity(
                    name=node.name,
                    label=label,
                    kind=ent_kind,
                    section=section,
                    fields=fields,
                    code="\n\n".join(method_blocks),
                    numbering_prefix=num_prefix,
                    numbering_field=num_field,
                    numbering_pattern=num_pattern,
                    check_sequence=check_seq,
                    table_parts=table_parts,
                    workflow=workflow,
                    owner_only=owner_only,
                    permissions=permissions,
                    print_form=print_form,
                    list_columns=list_columns,
                    list_order=list_order,
                    commands=commands,
                    command_roles=command_roles,
                    form=entity_form,
                ))
                continue

            if any(b in ("EntityForm", "CustomForm") for b in base_names):
                target_ent = ""
                for d in node.decorator_list:
                    if isinstance(d, ast.Call) and _call_name(d.func) == "form_for" and d.args and isinstance(d.args[0], ast.Name):
                        target_ent = d.args[0].id
                f_cfg: StudioFormConfig | None = None
                f_methods: list[str] = []
                for f_stmt in node.body:
                    if isinstance(f_stmt, ast.Expr) and isinstance(f_stmt.value, ast.Constant) and isinstance(f_stmt.value.value, str):
                        continue
                    if (
                        isinstance(f_stmt, ast.Assign)
                        and len(f_stmt.targets) == 1
                        and isinstance(f_stmt.targets[0], ast.Name)
                        and f_stmt.targets[0].id == "layout"
                        and isinstance(f_stmt.value, ast.Call)
                    ):
                        f_cfg = _parse_form(f_stmt.value, env)
                    elif isinstance(f_stmt, ast.FunctionDef):
                        f_methods.append(textwrap.dedent(ast.get_source_segment(source, f_stmt) or ""))
                if f_cfg is not None or f_methods:
                    parsed_form_classes[node.name] = (target_ent, f_cfg, "\n\n".join(f_methods))
                    continue
                logic_blocks.append(seg)
                continue

            reg_kind = next((_REGISTER_DECORATORS[d] for d in dec_names if d in _REGISTER_DECORATORS), None)
            if reg_kind is not None:
                members: list[StudioRegisterMember] = []
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and isinstance(stmt.value, ast.Call):
                        m_name = stmt.targets[0].id
                        call = stmt.value
                        c_name = _call_name(call.func)
                        kw = {k.arg: k.value for k in call.keywords if k.arg}
                        args = list(call.args)
                        req = bool(_safe_eval(kw.get("required", ast.Constant(True)), env))
                        if c_name == "RefDimension":
                            tgt_node = args[0] if args else kw.get("target")
                            tgt = tgt_node.id if isinstance(tgt_node, ast.Name) else str(_safe_eval(tgt_node, env) or "") if tgt_node is not None else ""
                            lbl = str(_safe_eval(args[1], env) if len(args) > 1 else _safe_eval(kw.get("name", ast.Constant(m_name)), env) or m_name)
                            rep = str(_safe_eval(kw["representation_field"], env) or "") if "representation_field" in kw else ""
                            members.append(StudioRegisterMember(m_name, lbl, "Измерение", "Ссылка", tgt, req, rep))
                        elif c_name in ("Dimension", "Resource", "Metric"):
                            role = "Измерение" if c_name == "Dimension" else "Ресурс"
                            lbl = str(_safe_eval(args[0], env) if args else _safe_eval(kw.get("name", ast.Constant(m_name)), env) or m_name)
                            ft_node = args[1] if len(args) > 1 else kw.get("field_type")
                            ft_name = _call_name(ft_node) if ft_node is not None else ("Decimal" if role == "Ресурс" else "str")
                            vt_map = {"str": "Строка", "int": "Целое", "date": "Дата", "bool": "Да / нет", "Decimal": "Десятичное", "DecimalValue": "Десятичное"}
                            v_type = vt_map.get(ft_name, "Десятичное" if role == "Ресурс" else "Строка")
                            members.append(StudioRegisterMember(m_name, lbl, role, v_type, "", req))
                registers.append(StudioRegister(node.name, reg_kind, members))
                continue

            if "report" in dec_names:
                parsed_report = _parse_visual_report(node, env, entity_names)
                if parsed_report is not None:
                    reports.append(parsed_report)
                else:
                    logic_blocks.append(seg)
                    notes.append(f"Отчёт «{node.name}» содержит пользовательскую логику и перенесён во вкладку «Приложение».")
                continue

            logic_blocks.append(seg)

    ent_by_name = {ent.name: ent for ent in entities}
    for form_cls_name, (target_ent, f_cfg, f_code) in parsed_form_classes.items():
        ent_name = target_ent or next((e for e, fc in entity_form_links.items() if fc == form_cls_name), "")
        ent = ent_by_name.get(ent_name)
        if ent is not None:
            if f_cfg is not None:
                ent.form = f_cfg
            if f_code:
                ent.form_code = f_code

    if first_run_fn and first_run_fn != "_seed_initial_data":
        if not any("def configure_app" in block for block in logic_blocks):
            logic_blocks.append(
                f"def configure_app(app: ERPApplication) -> None:\n    app.database.initialize({first_run_fn})"
            )
            notes.append(f"Функция начального заполнения «{first_run_fn}» подключена через configure_app().")

    project = StudioProject(
        title=title,
        app_id=app_id,
        entities=entities,
        imports="\n".join(import_lines),
        logic="\n\n\n".join(logic_blocks),
        reports=reports,
        registers=registers,
        local_accounts=local_accounts,
        column_renames=column_renames,
        table_part_renames=table_part_renames,
        start_target=start_target,
        window_width=width,
        window_height=height,
        reminders=reminders,
    )
    if not entities:
        raise ValueError("В выбранном Python-файле не найдено ни одной сущности DECUI (@catalog/@document).")
    notes.insert(0, f"Импортировано объектов: {len(entities)}, регистров: {len(registers)}, отчётов (визуальных): {len(reports)}.")
    return project, notes
