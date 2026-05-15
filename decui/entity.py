from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from .commands import get_command_meta, Command
from .fields import BooleanField, DateTimeField, Field, StringField

if TYPE_CHECKING:
    from .orm import Database


# ── Table part ─────────────────────────────────────────────────────

class TablePart:
    """Metadata container for a table-part (line items within a document)."""

    def __init__(
        self,
        name: str,
        fields: dict[str, Field],
    ) -> None:
        self.name = name
        self.fields = fields
        self.orm_model: type | None = None
        self.table_name: str = ""


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
    for key, value in cls.__dict__.items():
        if isinstance(value, Field):
            value.name = key
            fields[key] = value
    for base in cls.__bases__:
        base_fields = getattr(base, _TAB_MARKER, None)
        if base_fields is not None:
            for k, v in base_fields.items():
                fields.setdefault(k, v)
    return TablePart(cls.__name__, fields)


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


class Entity(metaclass=EntityMeta):
    """Base class for all entities."""

    _meta: ClassVar[Metadata]

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
        for tp_name in self._meta.table_parts:
            self._table_data[tp_name] = list(kwargs.pop(tp_name, []))
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
                return self._table_data[name]
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
            self._table_data[name] = value
            return
        object.__setattr__(self, name, value)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self._id}>"


class Catalog(Entity):
    _entity_type: str = "catalog"


import datetime as _dt


class Document(Entity):
    _entity_type: str = "document"
    date = DateTimeField("Дата", default=_dt.datetime.now, required=True)
    number = StringField("Номер", default="")
    posted = BooleanField("Проведён", default=False, read_only=True)

    # ── Движения по регистрам (переопределяется в наследнике) ──

    def get_movements(self) -> list:
        """Вернуть список Movement для записи при проведении.
        Переопределяется в наследнике документа."""
        return []

    def write_movements(self, db: Database) -> None:
        from .movement import write_movements
        mv = self.get_movements()
        if mv:
            write_movements(db, mv)

    def clear_movements(self, db: Database) -> None:
        from .movement import clear_movements
        for mv in self.get_movements():
            clear_movements(db, mv.register,
                            {"period": mv.period} if mv.period else None)

    # ── Проверка последовательности ──

    def check_sequence(self, db: Database) -> None:
        seq = getattr(type(self), "__sequence__", None)
        if seq:
            from .sequence import check_sequence as _cs
            _cs(db, self)
