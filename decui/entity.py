from __future__ import annotations

import copy
import datetime as _dt
import inspect
from collections.abc import Iterator
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Self,
    TypeVar,
    Union,
    dataclass_transform,
    get_args,
    get_origin,
    overload,
)

from .commands import Command, get_command_meta
from .fields import (
    BooleanField,
    ChoiceField,
    DateField,
    DateTimeField,
    DecimalField,
    Field,
    IntegerField,
    RefField,
    StringField,
    TimeField,
)

from .workflow import Workflow

if TYPE_CHECKING:
    from .forms import EntityForm, Form
    from .orm import Database
    from .printing import PrintForm

TEntity = TypeVar("TEntity", bound="Entity")

_FIELD_SPECIFIERS = (
    Field,
    StringField,
    IntegerField,
    DecimalField,
    BooleanField,
    DateField,
    DateTimeField,
    TimeField,
    ChoiceField,
    RefField,
)


# ── Type-annotation inference helper ─────────────────────────────────

def _parse_annotation_string(raw: str) -> tuple[Any, bool]:
    """Parse common stringified annotations when eval_str is unavailable."""
    s = raw.strip()
    required = True
    if s.startswith("ClassVar[") or s == "ClassVar":
        return None, False
    if s.endswith("| None"):
        s = s[:-6].strip()
        required = False
    elif s.startswith("None | "):
        s = s[7:].strip()
        required = False
    elif s.startswith("Optional[") and s.endswith("]"):
        s = s[9:-1].strip()
        required = False
    return s, required


def _infer_field_from_annotation(
    name: str,
    ann_type: Any,
    default_val: Any = None,
) -> Field | None:
    """Infer a Field descriptor from Python type annotations (e.g. name: str, age: int = 18)."""
    from decimal import Decimal

    required = True
    if isinstance(ann_type, str):
        parsed, required = _parse_annotation_string(ann_type)
        if parsed is None:
            return None
        ann_type = parsed
    else:
        origin = get_origin(ann_type)
        if origin is ClassVar:
            return None
        args = get_args(ann_type)
        if origin is UnionType or origin is Union:
            non_none_args = [a for a in args if a is not type(None)]
            if len(non_none_args) == 1:
                ann_type = non_none_args[0]
                required = False

    has_default = default_val is not None
    req = required and not has_default

    if ann_type is str or ann_type == "str":
        return StringField(required=req, default=default_val)
    if ann_type is int or ann_type == "int":
        return IntegerField(required=req, default=default_val)
    if ann_type is float or ann_type is Decimal or ann_type in ("float", "Decimal"):
        return DecimalField(required=req, default=default_val)
    if ann_type is bool or ann_type == "bool":
        return BooleanField(default=default_val if default_val is not None else False)
    if ann_type is _dt.date or ann_type in ("datetime.date", "date", "_dt.date"):
        return DateField(required=req, default=default_val)
    if ann_type is _dt.datetime or ann_type in ("datetime.datetime", "datetime", "_dt.datetime"):
        return DateTimeField(required=req, default=default_val)
    if ann_type is _dt.time or ann_type in ("datetime.time", "time", "_dt.time"):
        return TimeField(required=req, default=default_val)
    if isinstance(ann_type, type) and issubclass(ann_type, Entity):
        return RefField(ann_type, required=req, default=default_val)
    return None


def _get_class_annotations(cls: type) -> dict[str, Any]:
    """Read annotations safely across Python 3.12-3.14 (including postponed strings)."""
    try:
        return inspect.get_annotations(cls, eval_str=True)
    except Exception:
        try:
            return inspect.get_annotations(cls, eval_str=False)
        except Exception:
            return dict(getattr(cls, "__annotations__", {}) or {})


# ── Table part ─────────────────────────────────────────────────────

class TablePart:
    """Metadata container & descriptor for a table-part (line items within a document)."""

    def __init__(
        self,
        name: str,
        fields: dict[str, Field],
        verbose_name: str | None = None,
    ) -> None:
        self.name = name
        self.verbose_name = verbose_name or name
        self.fields = fields
        self.orm_model: type | None = None
        self.table_name: str = ""

    def clone(self) -> TablePart:
        cloned_fields = {k: copy.copy(v) for k, v in self.fields.items()}
        return TablePart(self.name, cloned_fields, self.verbose_name)

    @overload
    def __get__(self, instance: None, owner: Any) -> Self: ...

    @overload
    def __get__(self, instance: object, owner: Any = None) -> LineItemsCollection: ...

    def __get__(self, instance: object | None, owner: Any = None) -> Any:
        if instance is None:
            return self
        table_data = getattr(instance, "_table_data", {})
        items = table_data.get(self.name)
        if items is None:
            items = LineItemsCollection([], parent=instance, tp_meta=self)
            table_data[self.name] = items
        elif not isinstance(items, LineItemsCollection):
            items = LineItemsCollection(items, parent=instance, tp_meta=self)
            table_data[self.name] = items
        return items


class LineItemsCollection(list):
    """Convenience list wrapper for table parts / child tables.

    Preserves 100% list compatibility (indexing, iteration, append) while adding:
      - .add(**kwargs): creates, converts, and appends a row dict
      - .total(field="amount"): computes sum of specified field
      - .clear(): clears rows
    """

    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        *,
        parent: Any = None,
        tp_meta: TablePart | None = None,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._tp_meta = tp_meta
        for item in items or []:
            self.append(item)

    def _normalize_row(self, raw: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        if self._tp_meta is not None:
            for fname, field in self._tp_meta.fields.items():
                default = field.default() if callable(field.default) else field.default
                val = raw.get(fname, default)
                if val is not None:
                    val = field.to_python(val)
                row[fname] = val
        for k, v in raw.items():
            if k not in row:
                row[k] = v
        return row

    def append(self, item: dict[str, Any]) -> None:  # type: ignore[override]
        super().append(self._normalize_row(item))

    def add(self, **kwargs: Any) -> dict[str, Any]:
        """Add a new line item row using keyword arguments."""
        row = self._normalize_row(kwargs)
        super().append(row)
        return row

    def total(self, field: str = "amount") -> Any:
        """Sum values of a field across all line items."""
        tot: Any = 0
        for row in self:
            val = row.get(field)
            if val is not None:
                tot += val
        return tot


_TAB_MARKER = "__erp_tab_part__"


def tab_part(cls: type) -> TablePart:
    """Decorator: transforms a nested class into a ``TablePart`` descriptor."""
    fields: dict[str, Field] = {}
    verbose_name: str | None = None
    defaults_map: dict[str, Any] = {}

    for key, value in cls.__dict__.items():
        if isinstance(value, Field):
            f_copy = copy.copy(value)
            f_copy.name = key
            if not f_copy.display_name:
                f_copy.display_name = key.replace("_", " ").capitalize()
            fields[key] = f_copy
        elif key == "__verbose_name__":
            verbose_name = value
        elif not key.startswith("_") and not inspect.isfunction(value) and not isinstance(
            value, (classmethod, staticmethod, property)
        ):
            defaults_map[key] = value

    for key, ann_type in _get_class_annotations(cls).items():
        if key.startswith("_") or key in fields:
            continue
        f_inferred = _infer_field_from_annotation(key, ann_type, defaults_map.get(key))
        if f_inferred is not None:
            f_inferred.name = key
            if not f_inferred.display_name:
                f_inferred.display_name = key.replace("_", " ").capitalize()
            fields[key] = f_inferred

    for base in cls.__bases__:
        base_fields = getattr(base, _TAB_MARKER, None)
        if isinstance(base_fields, dict):
            for k, v in base_fields.items():
                fields.setdefault(k, copy.copy(v))
        elif isinstance(base, TablePart):
            for k, v in base.fields.items():
                fields.setdefault(k, copy.copy(v))

    return TablePart(cls.__name__, fields, verbose_name)


# Universal aliases for TablePart and @tab_part
LineItems = TablePart
line_items = tab_part
child_table = tab_part


# ── Entity metadata ────────────────────────────────────────────────

class Metadata:
    """Holds all introspection metadata for an entity type."""

    def __init__(
        self,
        name: str,
        fields: dict[str, Field],
        commands: dict[str, Command],
        table_parts: dict[str, TablePart] | None = None,
        entity_type: str = "entity",
        verbose_name: str | None = None,
        defaults: dict[str, Any] | None = None,
        section: str | None = None,
    ) -> None:
        self.name = name
        self.verbose_name = verbose_name or name
        self.fields = fields
        self.commands = commands
        self.table_parts = table_parts or {}
        self.entity_type = entity_type
        self.orm_model: type | None = None
        self.table_name: str = ""
        self.defaults = defaults or {}
        self.section = section or ""
        self.choice_refs: dict[str, RefField] = {}

    def prepare_choice_filters(self) -> None:
        """Validate header mappings and order editors after their dependencies."""
        for part in self.table_parts.values():
            for name, field in part.fields.items():
                if isinstance(field, RefField) and field.choice_filter:
                    raise ValueError(
                        f"{self.name}.{part.name}.{name}: choice_filter доступен только в шапке карточки."
                    )

        ordered: dict[str, RefField] = {}
        visiting: set[str] = set()

        def visit(name: str, field: RefField) -> None:
            path = f"{self.name}.{name}"
            if name in visiting:
                raise ValueError(f"{path}: цикл зависимостей choice_filter.")
            if name in ordered:
                return
            visiting.add(name)
            target = field.resolve_target()
            for target_name, source_name in field.choice_filter.items():
                if not isinstance(target_name, str) or not isinstance(source_name, str):
                    raise ValueError(f"{path}: choice_filter сопоставляет имена полей.")
                source = self.fields.get(source_name)
                destination = target._meta.fields.get(target_name)
                if source is None:
                    raise ValueError(f"{path}: неизвестное поле источника «{source_name}».")
                if destination is None:
                    raise ValueError(f"{path}: неизвестное поле отбора «{target.__name__}.{target_name}».")
                if (source.field_type != destination.field_type
                    or isinstance(source, RefField) != isinstance(destination, RefField)
                    or (isinstance(source, RefField) and isinstance(destination, RefField)
                        and source.resolve_target() is not destination.resolve_target())):
                    raise ValueError(f"{path}: несовместимые поля «{source_name}» и «{target.__name__}.{target_name}».")
                if isinstance(source, RefField) and source.choice_filter:
                    visit(source_name, source)
            visiting.remove(name)
            ordered[name] = field

        for name, field in self.fields.items():
            if isinstance(field, RefField) and field.choice_filter:
                visit(name, field)
        self.choice_refs = ordered

    @property
    def is_catalog(self) -> bool:
        return self.entity_type == "catalog"

    @property
    def is_document(self) -> bool:
        return self.entity_type == "document"


@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(
        Field,
        StringField,
        IntegerField,
        DecimalField,
        BooleanField,
        DateField,
        DateTimeField,
        TimeField,
        ChoiceField,
        RefField,
    ),
)
class EntityMeta(type):
    """Metaclass that processes entity class bodies."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> EntityMeta:
        fields: dict[str, Field] = {}
        commands: dict[str, Command] = {}
        table_parts: dict[str, TablePart] = {}

        for key, value in list(namespace.items()):
            if isinstance(value, Field):
                value.name = key
                if not value.display_name:
                    value.display_name = key.replace("_", " ").capitalize()
                fields[key] = value
                namespace.pop(key)
            elif isinstance(value, TablePart):
                table_parts[key] = value
                namespace.pop(key)
            else:
                cmd = get_command_meta(value)
                if cmd is not None:
                    commands[key] = cmd

        for base in bases:
            base_meta: Metadata | None = base.__dict__.get("_meta")
            if base_meta:
                for k, v in base_meta.fields.items():
                    fields.setdefault(k, copy.copy(v))
                for k, v in base_meta.commands.items():
                    commands.setdefault(k, v)
                for k, v in base_meta.table_parts.items():
                    table_parts.setdefault(k, v.clone())

        cls = super().__new__(mcs, name, bases, namespace)

        # Process type annotations for fields defined with type hints (e.g. name: str, age: int = 18)
        for key, ann_type in _get_class_annotations(cls).items():
            if (
                key.startswith("_")
                or key == "id"
                or key in fields
                or key in table_parts
                or key in commands
            ):
                continue
            default_val = getattr(cls, key, None)
            if (
                isinstance(default_val, (classmethod, staticmethod, property))
                or inspect.isfunction(default_val)
                or inspect.ismethod(default_val)
            ):
                continue
            field_obj = _infer_field_from_annotation(key, ann_type, default_val)
            if field_obj is not None:
                field_obj.name = key
                if not field_obj.display_name:
                    field_obj.display_name = key.replace("_", " ").capitalize()
                fields[key] = field_obj
                try:
                    delattr(cls, key)
                except (AttributeError, TypeError):
                    pass

        entity_type = getattr(cls, "_entity_type", "entity")
        verbose_name: str | None = namespace.get("__verbose_name__")
        if verbose_name is None:
            verbose_name = getattr(cls, "__verbose_name__", None)
        raw_defaults: dict[str, Any] = namespace.get("__defaults__", {}) or {}
        defaults: dict[str, Any] = {}
        for base in bases:
            bm: Metadata | None = getattr(base, "_meta", None)
            if bm:
                defaults.update(bm.defaults)
        defaults.update(raw_defaults)
        section: str | None = namespace.get("__section__")
        if section is None:
            section = getattr(cls, "__section__", None)
        cls._meta = Metadata(
            name, fields, commands, table_parts, entity_type, verbose_name, defaults, section
        )
        return cls

    def __getattr__(cls, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        meta = cls.__dict__.get("_meta")
        if meta:
            if name in meta.fields:
                return meta.fields[name]
            if name in meta.table_parts:
                return meta.table_parts[name]
        raise AttributeError(f"type object '{cls.__name__}' has no attribute '{name}'")


@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(
        Field,
        StringField,
        IntegerField,
        DecimalField,
        BooleanField,
        DateField,
        DateTimeField,
        TimeField,
        ChoiceField,
        RefField,
    ),
)
class Entity(metaclass=EntityMeta):
    """Base class for all entities."""

    _meta: ClassVar[Metadata]
    _id: int | None
    _data: dict[str, Any]
    _table_data: dict[str, Any]
    _table_parts_loaded: bool = True

    # Class-level metadata annotations for IDE autocompletion
    __verbose_name__: ClassVar[str | None] = None
    __section__: ClassVar[str | None] = None
    __command_roles__: ClassVar[dict[str, tuple[str, ...]]] = {}
    __list_columns__: ClassVar[tuple[str, ...]] = ()
    __list_order__: ClassVar[str | None] = None
    __form_table_tabs__: ClassVar[dict[str, str]] = {}
    __form_tabs__: ClassVar[dict[str, str]] = {}
    __required_when_visible__: ClassVar[tuple[str, ...]] = ()
    __form_visible_when__: ClassVar[dict[str, tuple[str, bool | str]]] = {}
    __form_groups__: ClassVar[dict[str, str]] = {}
    __subsystem__: ClassVar[str | None] = None
    __numbering__: ClassVar[dict[str, Any] | None] = None
    __defaults__: ClassVar[dict[str, Any] | None] = None
    __primary_key__: ClassVar[str] = "id"
    __workflow__: ClassVar[Workflow | None] = None
    __owner_field__: ClassVar[str | None] = None
    __print_form__: ClassVar[PrintForm | None] = None
    __form__: ClassVar[Form | type[EntityForm] | None] = None
    __form_class__: ClassVar[type[EntityForm] | None] = None
    __permissions__: ClassVar[dict[str, tuple[str, ...]] | None] = None
    __field_write_roles__: ClassVar[dict[str, tuple[str, ...]] | None] = None
    __field_read_roles__: ClassVar[dict[str, tuple[str, ...]] | None] = None

    @property
    def id(self) -> int | None:
        """Primary key assigned after persisting into database."""
        return self._id

    @classmethod
    def fields(cls) -> dict[str, Field]:
        """Return a mapping of field names to Field descriptors for this entity."""
        return dict(cls._meta.fields)

    @classmethod
    def field_names(cls) -> list[str]:
        """Return the list of field names defined on this entity."""
        return list(cls._meta.fields.keys())

    @classmethod
    def table_parts_meta(cls) -> dict[str, TablePart]:
        """Return a mapping of table part names to TablePart descriptors."""
        return dict(cls._meta.table_parts)

    def __init__(self, **kwargs: Any) -> None:
        object.__setattr__(self, "_data", {})
        object.__setattr__(self, "_id", None)
        object.__setattr__(self, "_table_data", {})
        protected_defaults: dict[str, Any] = {}
        object.__setattr__(self, "_protected_defaults", protected_defaults)
        for name, field in self._meta.fields.items():
            default = field.default() if callable(field.default) else field.default
            if name in (self.__field_write_roles__ or {}) or name in (self.__field_read_roles__ or {}):
                protected_defaults[name] = field.to_python(default) if default is not None else None
            value = kwargs.pop(name, default)
            if value is not None:
                value = field.to_python(value)
            self._data[name] = value
        for tp_name, tp in self._meta.table_parts.items():
            tp_val = kwargs.pop(tp_name, None)
            raw_rows = list(tp_val) if tp_val is not None else []
            self._table_data[tp_name] = LineItemsCollection(raw_rows, parent=self, tp_meta=tp)
        if kwargs:
            raise TypeError(
                f"Unexpected arguments for {type(self).__name__}: {list(kwargs)}"
            )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            try:
                items = self._table_data[name]
                if not isinstance(items, LineItemsCollection):
                    tp = self._meta.table_parts.get(name)
                    items = LineItemsCollection(items, parent=self, tp_meta=tp)
                    self._table_data[name] = items
                return items
            except KeyError:
                raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_data" or name == "_table_data" or name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if hasattr(self, "_data") and name in self._data:
            field = self._meta.fields.get(name)
            if field is not None:
                value = field.to_python(value)
            self._data[name] = value
            return
        if hasattr(self, "_table_data") and name in self._table_data:
            if not isinstance(value, LineItemsCollection):
                tp = self._meta.table_parts.get(name)
                raw = list(value) if value is not None else []
                value = LineItemsCollection(raw, parent=self, tp_meta=tp)
            self._table_data[name] = value
            return
        object.__setattr__(self, name, value)

    # ── Validation & lifecycle hooks ──

    def validate(self) -> list[str]:
        """Validate all header fields, table part rows, and custom on_validate()."""
        errors: list[str] = []
        masked = getattr(self, "_masked_fields", ())
        for name, field in self._meta.fields.items():
            if name in masked:
                continue
            errors.extend(self._validate_field(name, field))
        for part, row, _, error in self._table_validation_errors():
            table = self._meta.table_parts[part]
            errors.append(f"{table.verbose_name or part} (стр. {row + 1}): {error}")
        errors.extend(self.on_validate() or [])
        return errors

    def _table_validation_errors(self) -> Iterator[tuple[str, int, str, str]]:
        masked = getattr(self, "_masked_fields", ())
        for part, table in self._meta.table_parts.items():
            for index, row in enumerate(self._table_data.get(part, [])):
                for name, field in table.fields.items():
                    if f"{part}.{name}" in masked:
                        continue
                    for error in field.validate(row.get(name)):
                        yield part, index, name, error

    def _validate_field(self, name: str, field: Field) -> list[str]:
        value = self._data.get(name)
        errors = field.validate(value)
        condition = self.__form_visible_when__.get(name)
        if not field.required and name in self.__required_when_visible__ and condition is not None:
            source, expected = condition
            if (
                source not in getattr(self, "_masked_fields", ())
                and self._data.get(source) == expected and field._is_empty(value)
            ):
                errors.append(f"{field.display_name} обязательно")
        return errors

    def on_validate(self) -> list[str]:
        """Override in subclass to run custom cross-field or business validation."""
        return []

    def before_save(self, db: Database) -> None:
        """Hook called right before persisting this entity into the database."""
        pass

    def after_save(self, db: Database) -> None:
        """Hook called right after persisting this entity into the database."""
        pass

    def before_delete(self, db: Database) -> None:
        """Hook called before deleting this entity from the database."""
        pass

    def after_delete(self, db: Database) -> None:
        """Update dependent data after deletion, before the transaction commits."""

    def prepare_copy(self, db: Database) -> None:
        """Adjust the new entity before opening it in the copy form."""

    def copy_for_edit(self, db: Database) -> Self:
        """Create an unsaved copy, resetting system-managed fields."""
        data = self.to_dict()
        data.pop("id", None)
        data.pop("_id", None)
        numbering = getattr(type(self), "__numbering__", None)
        if numbering:
            number_field = "number" if self._meta.is_document else "code"
            data.pop(numbering.get("field", number_field), None)
        if self._meta.is_document:
            data["posted"] = False
        if self.__workflow__ is not None:
            data[self.__workflow__.field] = self.__workflow__.initial
            if self.__workflow__.uses_assignment:
                data.pop(self.__workflow__.assignee_field, None)
        if self.__owner_field__ is not None:
            data.pop(self.__owner_field__, None)
        for name in (self.__field_write_roles__ or {}).keys() | (self.__field_read_roles__ or {}).keys():
            if db.can_edit_field(type(self), name):
                continue
            part, separator, column = name.partition(".")
            if not separator:
                data.pop(name, None)
            else:
                field = self._meta.table_parts[part].fields[column]
                for row in data.get(part, []):
                    default = field.default() if callable(field.default) else field.default
                    row[column] = field.to_python(default) if default is not None else None
        clone = type(self).from_dict(data)
        clone.prepare_copy(db)
        return clone

    # ── Active Record ──

    def save(self, db: Database, *, validate: bool = False) -> Self:
        """Save this entity into database (Active Record pattern)."""
        return db.save(self, validate=validate)

    def delete(self, db: Database) -> None:
        """Delete this entity from database."""
        db.delete(self)

    @classmethod
    def get(cls: type[TEntity], db: Database, id: int) -> TEntity | None:
        """Fetch an entity instance by ID."""
        return db.get(cls, id)

    @classmethod
    def all(cls: type[TEntity], db: Database, *, include_table_parts: bool = False) -> list[TEntity]:
        """Fetch all visible entities, optionally including table part rows."""
        return db.list(cls, include_table_parts=include_table_parts)

    @classmethod
    def filter(cls: type[TEntity], db: Database, **kwargs: Any) -> list[TEntity]:
        """Fetch entity instances matching field filter values."""
        return db.filter(cls, **kwargs)

    def to_dict(self, include_table_parts: bool = True) -> dict[str, Any]:
        """Convert entity data and table parts into a plain dictionary."""
        res: dict[str, Any] = {"id": self._id}
        res.update(self._data)
        if include_table_parts:
            for tp_name, items in self._table_data.items():
                res[tp_name] = [dict(row) for row in items]
        return res

    @classmethod
    def from_dict(cls: type[TEntity], data: dict[str, Any]) -> TEntity:
        """Instantiate an entity from dictionary data."""
        data_copy = dict(data)
        ent_id = data_copy.pop("id", None)
        if ent_id is None:
            ent_id = data_copy.pop("_id", None)
        instance = cls(**data_copy)
        if ent_id is not None:
            object.__setattr__(instance, "_id", ent_id)
        return instance

    def __repr__(self) -> str:
        name_val = self._data.get("name") or self._data.get("number") or self._data.get("code")
        if name_val:
            return f"<{type(self).__name__} id={self._id} '{name_val}'>"
        return f"<{type(self).__name__} id={self._id}>"

    def __str__(self) -> str:
        name_val = self._data.get("name") or self._data.get("number") or self._data.get("code")
        if name_val:
            return str(name_val)
        return f"{type(self).__name__}(id={self._id})"


class Catalog(Entity):
    """Base class for Master Data / Catalogs (Products, Customers, etc.)."""
    _entity_type: str = "catalog"


# Universal aliases for Catalog
MasterData = Catalog
Lookup = Catalog


class Document(Entity):
    """Base class for Business Transactions / Documents (Invoices, Orders, etc.)."""
    _entity_type: str = "document"
    date: _dt.datetime = DateTimeField(
        "Дата",
        default=lambda: _dt.datetime.now().replace(second=0, microsecond=0),
        required=True,
    )
    number: str = StringField("Номер", default="")
    posted: bool = BooleanField("Проведён", default=False, read_only=True)

    # ── Жизненный цикл / проведение ──

    def post(self) -> None:
        """Пометить документ как проведённый / согласованный."""
        self._data["posted"] = True

    def unpost(self) -> None:
        """Снять проведение / вернуть в черновик."""
        self._data["posted"] = False

    def submit(self) -> None:
        """Universal alias for post()."""
        self.post()

    def cancel(self) -> None:
        """Universal alias for unpost()."""
        self.unpost()

    @property
    def is_posted(self) -> bool:
        """Проверен / проведён ли документ."""
        return bool(self._data.get("posted", False))

    # ── Движения по регистрам (переопределяется в наследнике) ──

    def get_ledger_entries(self) -> list:
        """Universal alias: return list of movements / ledger entries on post."""
        return []

    def get_movements(self) -> list:
        """Вернуть список Movement для записи при проведении.
        Переопределяется в наследнике документа (или get_ledger_entries)."""
        entries = self.get_ledger_entries()
        return entries if entries else []

    def write_movements(self, db: Database) -> None:
        from .movement import write_movements
        movements = self.get_movements() or self.get_ledger_entries()
        mv_list = self._attach_doc_info(movements)
        if mv_list:
            write_movements(db, mv_list)

    def clear_movements(self, db: Database) -> None:
        """Очистить все движения этого документа из задействованных регистров."""
        if self._id is None:
            return
        from .movement import clear_movements, get_all_registers

        doc_type = type(self).__name__
        for register in get_all_registers():
            clear_movements(db, register, self._id, doc_type)

    def _attach_doc_info(self, movements: list) -> list:
        for mv in movements:
            mv.doc_id = self._id
            mv.doc_type = type(self).__name__
            if mv.period is None and self._data.get("date") is not None:
                d = self._data["date"]
                mv.period = d.date() if isinstance(d, _dt.datetime) else d
        return movements

    # ── Проверка последовательности ──

    def check_sequence(self, db: Database) -> None:
        seq = getattr(type(self), "__sequence__", None)
        if seq:
            from .sequence import check_sequence as _cs
            _cs(db, self)


# Universal alias for Document
Transaction = Document
