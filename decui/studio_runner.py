"""Isolated subprocess helpers for live Studio report preview and DB seed capture."""

from __future__ import annotations

import datetime
import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from decui import Database
from decui.accounts import Account, set_current_account
from decui.fields import RefField
from decui.files import stage_decui_backup
from decui.registry import get_all_entities, get_all_reports
from decui.reports import ChoiceParam, DateParam, RefParam


def _json_encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, datetime.date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    if isinstance(value, type):
        return value.__name__
    if isinstance(value, dict):
        return {str(k): _json_encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_encode(v) for v in value]
    return value


def _open_preview_db(app_mod: Any, mode: str, db_path: str | None, work_dir: Path) -> Database:
    if mode == "db" and db_path:
        staged = work_dir / "staged_preview.sqlite3"
        stage_decui_backup(Path(db_path), staged)
        db = Database(str(staged))
    else:
        db = Database.memory()
        seed_fn = getattr(app_mod, "_seed_initial_data", None)
        if callable(seed_fn):
            set_current_account(Account("studio_preview", "admin"))
            db.initialize(seed_fn)
    return db


def run_report_preview(payload: dict[str, Any]) -> dict[str, Any]:
    sys.path.insert(0, str(Path.cwd()))
    app_mod = importlib.import_module("app")
    report_name = str(payload["report"])
    mode = str(payload.get("mode", "memory"))
    db_path = payload.get("db_path")
    raw_params: dict[str, Any] = payload.get("params") or {}

    set_current_account(Account("studio_preview", "admin"))
    try:
        db = _open_preview_db(app_mod, mode, db_path, Path.cwd())
        rep_cls = next((r for r in get_all_reports() if r.__name__ == report_name), None)
        if rep_cls is None:
            raise ValueError(f"Отчёт «{report_name}» не найден.")

        parsed_params: dict[str, Any] = {}
        params_meta: list[dict[str, Any]] = []
        for name, param in rep_cls._report_meta.params.items():
            meta: dict[str, Any] = {
                "name": name,
                "label": param.display_name,
                "type": type(param).__name__,
                "required": param.required,
            }
            if isinstance(param, RefParam):
                target = param.resolve_target()
                meta["choices"] = [
                    [item.id, str(getattr(item, param.representation_field, None) or item) if param.representation_field else str(item)]
                    for item in db.list(target) if item.id is not None
                ]
            elif isinstance(param, ChoiceParam):
                meta["choices"] = [[c, lbl] for c, lbl in param.choices]
            params_meta.append(meta)

            if name in raw_params:
                val = raw_params[name]
                if isinstance(param, DateParam) and isinstance(val, str) and val:
                    parsed_params[name] = datetime.date.fromisoformat(val)
                else:
                    parsed_params[name] = val

        rows = rep_cls().execute(db, **parsed_params)
        columns = [
            {
                "title": col.title,
                "field": col.field,
                "width": col.width,
                "align": col.align,
                "aggregate": col.aggregate,
                "format": col.format,
                "prefix": col.prefix,
                "suffix": col.suffix,
            }
            for col in rep_cls.columns
        ]
        return {
            "title": rep_cls._report_meta.title,
            "columns": columns,
            "group_by": list(rep_cls.group_by),
            "sortable": rep_cls.sortable,
            "chart": getattr(rep_cls, "chart", None),
            "params_meta": params_meta,
            "rows": _json_encode(rows),
        }
    finally:
        set_current_account(None)


def capture_seeds(db_path: str) -> list[dict[str, Any]]:
    sys.path.insert(0, str(Path.cwd()))
    importlib.import_module("app")
    staged = Path.cwd() / "capture_staged.sqlite3"
    stage_decui_backup(Path(db_path), staged)

    set_current_account(Account("studio_capture", "admin"))
    try:
        db = Database(str(staged))
        entities = get_all_entities()
        key_map: dict[tuple[str, int], str] = {}
        records_by_cls: list[tuple[type, list[Any]]] = []
        for cls in entities:
            items = db.list(cls, include_table_parts=True)
            records_by_cls.append((cls, items))
            prefix = cls.__name__.lower()
            for item in items:
                if item.id is not None:
                    key_map[(cls.__name__, item.id)] = f"{prefix}_{item.id}"

        def format_field(field: Any, value: Any) -> str | None:
            if value is None:
                return None
            if isinstance(field, RefField) and isinstance(value, int):
                target_cls = field.resolve_target()
                return key_map.get((target_cls.__name__, value))
            if isinstance(value, datetime.datetime):
                return value.isoformat(timespec="minutes")
            if isinstance(value, datetime.date):
                return value.isoformat()
            if isinstance(value, datetime.time):
                return value.strftime("%H:%M")
            if isinstance(value, bool):
                return "Да" if value else "Нет"
            return str(value)

        seeds: list[dict[str, Any]] = []
        for cls, items in records_by_cls:
            numbering = getattr(cls, "__numbering__", None) or {}
            num_field = numbering.get("field") or ("number" if cls._meta.is_document else "")
            skip_fields = {"posted", "number", "record_owner", "approval_assignee"}
            if num_field:
                skip_fields.add(num_field)
            if cls.__workflow__ is not None:
                skip_fields.add(cls.__workflow__.field)
            for item in items:
                if item.id is None:
                    continue
                values: dict[str, str] = {}
                for name, field in cls._meta.fields.items():
                    if name in skip_fields or field.read_only:
                        continue
                    formatted = format_field(field, item._data.get(name))
                    if formatted is not None and formatted != "":
                        values[name] = formatted
                tables: dict[str, list[dict[str, str]]] = {}
                for part_name, part in cls._meta.table_parts.items():
                    part_rows: list[dict[str, str]] = []
                    for row in item._table_data.get(part_name, []):
                        row_dict: dict[str, str] = {}
                        for col_name, col_field in part.fields.items():
                            if col_field.read_only:
                                continue
                            formatted = format_field(col_field, row.get(col_name))
                            if formatted is not None and formatted != "":
                                row_dict[col_name] = formatted
                        part_rows.append(row_dict)
                    if part_rows:
                        tables[part_name] = part_rows
                seeds.append({
                    "key": key_map[(cls.__name__, item.id)],
                    "entity": cls.__name__,
                    "values": values,
                    "tables": tables,
                    "post": bool(cls._meta.is_document and item._data.get("posted")),
                })
        return seeds
    finally:
        set_current_account(None)


def main() -> int:
    command = sys.argv[1]
    if command == "report-preview":
        payload = json.loads(sys.stdin.read())
        print(json.dumps(run_report_preview(payload), ensure_ascii=False))
        return 0
    if command == "capture-seeds":
        db_path = sys.argv[2]
        print(json.dumps(capture_seeds(db_path), ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
