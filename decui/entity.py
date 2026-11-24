from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, dataclass_transform

from .commands import get_command_meta, Command
from .fields import BooleanField, DateTimeField, Field, StringField

if TYPE_CHECKING:
    from .orm import Database

TEntity = TypeVar("TEntity", bound="Entity")


# ── Table part ─────────────────────────────────────────────────────

class TablePart:
    """Metadata container for a table-part (line items within a document)."""

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
        parent: Entity | None = None,
        tp_meta: TablePart | None = None,
    ) -> None:
        super().__init__(items or [])
        self._parent = parent
        self._tp_meta = tp_meta

    def add(self, **kwargs: Any) -> dict[str, Any]:
        """Add a new line item row using keyword arguments."""
        row: dict[str, Any] = {}
        if self._tp_meta is not None:
            for fname, field in self._tp_meta.fields.items():
                default = field.default() if callable(field.default) else field.default
                val = kwargs.get(fname, default)
                if val is not None:
                    try:
                        val = field.to_python(val)
                    except Exception:
                        pass
                row[fname] = val
        for k, v in kwargs.items():
            if k not in row:
                row[k] = v
        self.append(row)
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
    """Decorator: transforms a nested class into a ``TablePart`` descriptor.

    Usage inside a ``@document`` class::

        @document
        class Invoice(Document):
            number = String("Номер")

            @tab_part
            class Items:
                product = Ref(Product, "Товар")
                quantity = Integer("Кол-во")
                price = Decimal("Цена")
                amount = Decimal("Сумма", read_only=True)
    """
    fields: dict[str, Field] = {}
    verbose_name: str | None = None
    for key, value in cls.__dict__.items():
        if isinstance(value, Field):
            value.name = key
            if not value.display_name:
                value.display_name = key.replace("_", " ").capitalize()
            fields[key] = value
        elif key == "__verbose_name__":
            verbose_name = value
    for base in cls.__bases__:
        base_fields = getattr(base, _TAB_MARKER, None)
        if base_fields is not None:
            for k, v in base_fields.items():
                fields.setdefault(k, v)
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

    @property
    def is_catalog(self) -> bool:
        return self.entity_type == "catalog"

    @property
    def is_document(self) -> bool:
        return self.entity_type == "document"


def _infer_field_from_annotation(name: str, ann_type: Any, default_val: Any = None) -> Field | None:
    """Infer a Field descriptor from Python type annotations (e.g. name: str, age: int = 18)."""
    from types import UnionType
    from typing import Union
    import datetime as _dt
    from decimal import Decimal
    from .fields import (
        BooleanField,
        DateField,
        DateTimeField,
        DecimalField,
        IntegerField,
        RefField,
        StringField,
        TimeField,
    )

    required = True
    origin = getattr(ann_type, "__origin__", None)
    args = getattr(ann_type, "__args__", ())
    if origin is UnionType or origin is Union:
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            ann_type = non_none_args[0]
            required = False

    if ann_type is str or ann_type == "str":
        return StringField(required=required and default_val is None, default=default_val)
    if ann_type is int or ann_type == "int":
        return IntegerField(required=required and default_val is None, default=default_val if default_val is not None else 0)
    if ann_type is float or ann_type is Decimal or ann_type in ("float", "Decimal"):
        return DecimalField(required=required and default_val is None, default=default_val if default_val is not None else Decimal("0.00"))
    if ann_type is bool or ann_type == "bool":
        return BooleanField(default=default_val if default_val is not None else False)
    if ann_type is _dt.date or ann_type in ("datetime.date", "date"):
        return DateField(required=required and default_val is None, default=default_val or _dt.date.today)
    if ann_type is _dt.datetime or ann_type in ("datetime.datetime", "datetime"):
        return DateTimeField(required=required and default_val is None, default=default_val or _dt.datetime.now)
    if ann_type is _dt.time or ann_type in ("datetime.time", "time"):
        return TimeField(required=required and default_val is None, default=default_val or _dt.time(9, 0))
    if isinstance(ann_type, type) and issubclass(ann_type, Entity):
        return RefField(ann_type, required=required and default_val is None)
    return None


@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(Field,),
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
                    fields.setdefault(k, v)
                for k, v in base_meta.commands.items():
                    commands.setdefault(k, v)
                for k, v in base_meta.table_parts.items():
                    table_parts.setdefault(k, v)

        cls = super().__new__(mcs, name, bases, namespace)

        # Process type annotations for fields defined with type hints (e.g. name: str, age: int = 18)
        import inspect
        try:
            annotations = inspect.get_annotations(cls)
        except Exception:
            annotations = getattr(cls, "__annotations__", {}) or {}

        for key, ann_type in annotations.items():
            if key.startswith("_") or key in fields or key in table_parts or key in commands:
                continue
            default_val = getattr(cls, key, None)
            if isinstance(default_val, (classmethod, staticmethod, property)) or callable(default_val):
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
        verbose_name: str | None = namespace.pop("__verbose_name__", None)
        if verbose_name is None:
            verbose_name = getattr(cls, "__verbose_name__", None)
        raw_defaults: dict[str, Any] = namespace.pop("__defaults__", {}) or {}
        defaults: dict[str, Any] = {}
        for base in bases:
            bm: Metadata | None = getattr(base, "_meta", None)
            if bm:
                defaults.update(bm.defaults)
        defaults.update(raw_defaults)
        section: str | None = namespace.pop("__section__", None)
        if section is None:
            section = getattr(cls, "__section__", None)
        cls._meta = Metadata(name, fields, commands, table_parts, entity_type, verbose_name, defaults, section)
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
    field_specifiers=(Field,),
)
class Entity(metaclass=EntityMeta):
    """Base class for all entities."""

    _meta: ClassVar[Metadata]

    # Class-level type hints for IDE autocompletion:
    id: int | None = None
    _id: int | None = None
    _data: dict[str, Any]
    _table_data: dict[str, Any]

    __verbose_name__: ClassVar[str | None] = None
    __section__: ClassVar[str | None] = None
    __subsystem__: ClassVar[str | None] = None
    __numbering__: ClassVar[dict[str, Any] | None] = None
    __defaults__: ClassVar[dict[str, Any] | None] = None
    __primary_key__: ClassVar[str] = "id"

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
        for name, field in self._meta.fields.items():
            default = field.default() if callable(field.default) else field.default
            value = kwargs.pop(name, default)
            if value is not None:
                try:
                    value = field.to_python(value)
                except Exception:
                    pass
            self._data[name] = value
        for tp_name, tp in self._meta.table_parts.items():
            raw_rows = list(kwargs.pop(tp_name, []))
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
                try:
                    value = field.to_python(value)
                except Exception:
                    pass
            self._data[name] = value
            return
        if hasattr(self, "_table_data") and name in self._table_data:
            if not isinstance(value, LineItemsCollection):
                tp = self._meta.table_parts.get(name)
                value = LineItemsCollection(list(value), parent=self, tp_meta=tp)
            self._table_data[name] = value
            return
        object.__setattr__(self, name, value)

    def save(self, db: Database) -> Entity:
        """Save this entity into database (Active Record pattern)."""
        return db.save(self)

    def delete(self, db: Database) -> None:
        """Delete this entity from database."""
        db.delete(self)

    @classmethod
    def get(cls: type[TEntity], db: Database, id: int) -> TEntity | None:
        """Fetch an entity instance by ID."""
        return db.get(cls, id)  # type: ignore

    @classmethod
    def all(cls: type[TEntity], db: Database) -> list[TEntity]:
        """Fetch all entity instances."""
        return db.list(cls)  # type: ignore

    @classmethod
    def filter(cls: type[TEntity], db: Database, **kwargs: Any) -> list[TEntity]:
        """Fetch entity instances matching field filter values."""
        return db.filter(cls, **kwargs)  # type: ignore

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


import datetime as _dt


class Document(Entity):
    """Base class for Business Transactions / Documents (Invoices, Orders, etc.)."""
    _entity_type: str = "document"
    date = DateTimeField("Дата",
        default=lambda: _dt.datetime.now().replace(second=0, microsecond=0),
        required=True)
    number = StringField("Номер", default="")
    posted = BooleanField("Проведён", default=False, read_only=True)

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
        from .movement import clear_movements
        seen = set()
        movements = self.get_movements() or self.get_ledger_entries()
        for mv in movements:
            key = id(mv.register)
            if key not in seen:
                seen.add(key)
                clear_movements(db, mv.register, self._id,
                                type(self).__name__)

    def _attach_doc_info(self, movements: list) -> list:
        for mv in movements:
            mv.doc_id = self._id
            mv.doc_type = type(self).__name__
        return movements

    # ── Проверка последовательности ──

    def check_sequence(self, db: Database) -> None:
        seq = getattr(type(self), "__sequence__", None)
        if seq:
            from .sequence import check_sequence as _cs
            _cs(db, self)


# Universal alias for Document
Transaction = Document

