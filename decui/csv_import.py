"""Create or update catalog records from a CSV file."""

from __future__ import annotations

import csv
import datetime
import io
from dataclasses import dataclass
from decimal import InvalidOperation
from typing import TYPE_CHECKING, Any

from decui.fields import BooleanField, DecimalField, Field, RefField, TimeField

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from decui.entity import Entity
    from decui.orm import Database


@dataclass
class CatalogCSVRow:
    line: int
    record: Entity
    values: dict[str, Any]
    key: str | None = None


def _importable_fields(cls: type[Entity], db: Database) -> dict[str, Field]:
    excluded = {(cls.__numbering__ or {}).get("field"), cls.__owner_field__}
    return {
        name: field for name, field in cls._meta.fields.items()
        if field.visible and not field.read_only and name not in excluded and db.can_edit_field(cls, name)
    }


def catalog_csv_keys(cls: type[Entity], db: Database) -> dict[str, str]:
    return {
        name: field.display_name
        for name, field in _importable_fields(cls, db).items()
        if field.unique and field.field_type in (str, int) and not isinstance(field, RefField)
    }


def catalog_csv_template(cls: type[Entity], db: Database) -> bytes:
    """Build an Excel-compatible header for the fields available to import."""
    if not cls._meta.is_catalog or not (
        db.allows(cls, "create") or (db.allows(cls, "update") and catalog_csv_keys(cls, db))
    ):
        raise PermissionError("Импорт недоступен для этого списка.")
    columns = list(_importable_fields(cls, db))
    if not columns:
        raise ValueError("В справочнике нет полей, доступных для импорта.")
    stream = io.StringIO()
    csv.writer(stream, delimiter=";").writerow(columns)
    return stream.getvalue().encode("utf-8-sig")


def _find_record(db: Database, cls: type[Entity], key: str, value: Any) -> Entity | None:
    matches = db.filter(cls, **{f"{key}__eq": value})
    if len(matches) > 1:
        raise ValueError(f"Значение ключа «{key}» соответствует нескольким записям.")
    return db.get(cls, matches[0].id) if matches and matches[0].id is not None else None


def _resolve_reference(
    db: Database, field: RefField, text: str,
    labels: dict[tuple[type, str | None], dict[str, list[int]]],
) -> int:
    target = field.resolve_target()
    if text.startswith("название:"):
        text = text.removeprefix("название:").strip()
    else:
        try:
            identifier = int(text)
        except ValueError:
            pass
        else:
            if db.get(target, identifier) is None:
                raise ValueError("запись по указанному ID недоступна")
            return identifier

    display_field = field.representation_field
    if display_field and not db.can_read_field(target, display_field):
        raise ValueError("поиск по названию недоступен для этого поля; укажите ID")
    key = (target, display_field)
    if key not in labels:
        index: dict[str, list[int]] = {}
        for item in db.list(target):
            if item.id is None:
                continue
            value = getattr(item, display_field, None) if display_field else None
            label = str(value if value is not None else item).strip()
            index.setdefault(label, []).append(item.id)
        labels[key] = index

    matches = labels[key].get(text, [])
    if not matches:
        raise ValueError("запись с таким названием недоступна")
    if len(matches) > 1:
        raise ValueError("название соответствует нескольким записям; укажите ID")
    return matches[0]


def _convert(
    db: Database, field: Field, text: str,
    labels: dict[tuple[type, str | None], dict[str, list[int]]],
) -> Any:
    if isinstance(field, RefField):
        return _resolve_reference(db, field, text, labels)
    if isinstance(field, BooleanField):
        options = {"да": True, "нет": False, "true": True, "false": False, "1": True, "0": False}
        if text.casefold() not in options:
            raise ValueError("укажите да/нет, true/false или 1/0")
        return options[text.casefold()]
    if isinstance(field, DecimalField):
        value = field.to_python(text.replace(",", "."))
        if value is not None and not value.is_finite():
            raise ValueError("укажите конечное число")
        return value
    if isinstance(field, TimeField):
        return datetime.time.fromisoformat(text)
    return field.to_python(text)


def read_catalog_csv(
    path: Path, cls: type[Entity], db: Database, *, key: str | None = None,
    map_columns: Callable[[list[str], dict[str, str], list[str | None]], list[str | None] | None] | None = None,
) -> tuple[list[str], list[CatalogCSVRow]]:
    """Resolve headers, convert values and validate every record before writing."""
    if not cls._meta.is_catalog or not (db.allows(cls, "create") or (key and db.allows(cls, "update"))):
        raise PermissionError("Импорт недоступен для этого списка.")
    available = _importable_fields(cls, db)
    if key is not None and key not in catalog_csv_keys(cls, db):
        raise ValueError(f"Поле «{key}» недоступно как ключ импорта.")
    with path.open(encoding="utf-8-sig", newline="") as file:
        sample = file.read(8192)
        file.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(file, dialect, strict=True)
        headers = next(reader, [])
        if not headers:
            raise ValueError("Добавьте строку заголовков с названиями полей справочника.")
        mapping: list[str | None] = []
        for header in headers:
            name = header.strip()
            matches = [key for key, field in available.items() if field.display_name == name]
            field_name = name if name in available else matches[0] if len(matches) == 1 else None
            mapping.append(None if field_name in mapping else field_name)
        if None in mapping or (key is not None and key not in mapping):
            if map_columns is None:
                invalid = next((headers[index] for index, name in enumerate(mapping) if name is None), None)
                if invalid is not None:
                    raise ValueError(f"Заголовок «{invalid}»: поле недоступно, неоднозначно или указано дважды.")
                raise ValueError(f"Добавьте в CSV колонку ключа «{available[key].display_name}».")
            choices = {name: field.display_name for name, field in available.items()}
            configured = map_columns(headers, choices, mapping)
            if configured is None:
                return [], []
            mapping = configured

        columns = [name for name in mapping if name is not None]
        if (
            len(mapping) != len(headers) or not columns or len(set(columns)) != len(columns)
            or not set(columns).issubset(available) or (key is not None and key not in columns)
        ):
            raise ValueError("Проверьте сопоставление колонок CSV с полями справочника.")
        records: list[CatalogCSVRow] = []
        seen: set[Any] = set()
        labels: dict[tuple[type, str | None], dict[str, list[int]]] = {}
        for cells in reader:
            line = reader.line_num
            if not cells:
                continue
            if len(cells) != len(mapping):
                raise ValueError(f"Строка {line}: число значений отличается от числа заголовков.")
            values: dict[str, Any] = {}
            for name, cell in zip(mapping, cells, strict=True):
                if name is None or not cell.strip():
                    continue
                field = available[name]
                try:
                    values[name] = _convert(db, field, cell if field.field_type is str else cell.strip(), labels)
                except (ValueError, TypeError, InvalidOperation) as exc:
                    raise ValueError(f"Строка {line}, «{field.display_name}»: {exc}") from exc
            record = None
            if key is not None:
                key_value = values.get(key)
                if key_value is None:
                    raise ValueError(f"Строка {line}: заполните ключ «{available[key].display_name}».")
                if key_value in seen:
                    raise ValueError(f"Строка {line}: значение ключа повторяется в CSV.")
                seen.add(key_value)
                record = _find_record(db, cls, key, key_value)
            if record is None:
                if not db.allows(cls, "create"):
                    raise PermissionError(f"Строка {line}: создание записи недоступно.")
                defaults = {
                    name: default(db) if callable(default) else default
                    for name, default in cls._meta.defaults.items()
                }
                record = cls(**(defaults | values))
            else:
                if not db.can_edit(record):
                    raise PermissionError(f"Строка {line}: изменение записи недоступно.")
                for name, value in values.items():
                    setattr(record, name, value)
            errors = record.validate()
            if errors:
                raise ValueError(f"Строка {line}: {' '.join(errors)}")
            records.append(CatalogCSVRow(line, record, values, key))
    if not records:
        raise ValueError("В файле нет записей для импорта.")
    return columns, records


def save_catalog_csv(db: Database, records: list[CatalogCSVRow]) -> None:
    """Save the entire batch or roll it back on the first error."""
    with db.transaction():
        for row in records:
            try:
                record = row.record
                if row.key is not None:
                    current = _find_record(db, type(record), row.key, row.values[row.key])
                    if (current.id if current else None) != record.id:
                        raise ValueError("Запись изменилась после предпросмотра. Откройте импорт заново.")
                    if current is not None:
                        for name, value in row.values.items():
                            setattr(current, name, value)
                        record = current
                for name, value in row.values.items():
                    field = record._meta.fields[name]
                    if isinstance(field, RefField) and db.get(field.resolve_target(), value) is None:
                        raise ValueError(f"«{field.display_name}»: связанная запись больше недоступна.")
                db.save(record, validate=True)
            except Exception as exc:
                raise ValueError(f"Строка {row.line}: {exc}") from exc


def parse_tabular_rows(
    text: str, fields: list[tuple[str, Field]], db: Database,
) -> list[dict[str, Any]]:
    """Convert tab-separated spreadsheet rows without altering the form."""
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t", strict=True)
    records: list[dict[str, Any]] = []
    labels: dict[tuple[type, str | None], dict[str, list[int]]] = {}
    for cells in reader:
        if not any(cell.strip() for cell in cells):
            continue
        line = reader.line_num
        if len(cells) > len(fields):
            raise ValueError(f"Строка {line}: больше значений, чем доступных колонок.")
        row: dict[str, Any] = {}
        for (name, field), cell in zip(fields, cells, strict=False):
            if not cell.strip():
                continue
            try:
                value = _convert(db, field, cell if field.field_type is str else cell.strip(), labels)
                errors = field.validate(value)
                if errors:
                    raise ValueError(" ".join(errors))
            except (ValueError, TypeError, InvalidOperation) as exc:
                raise ValueError(f"Строка {line}, «{field.display_name}»: {exc}") from exc
            row[name] = value
        records.append(row)
    return records
