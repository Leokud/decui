from __future__ import annotations

from typing import TYPE_CHECKING, Any, dataclass_transform

from .entity import Entity, EntityMeta
from .fields import Field

_entity_registry: dict[str, type[Entity]] = {}
_report_registry: dict[str, type] = {}


# ── Entity registry ────────────────────────────────────────────────

def register_entity(cls: type[Entity]) -> type[Entity]:
    name = cls._meta.name
    _entity_registry[name] = cls
    return cls


def get_entity(name: str) -> type[Entity]:
    if name not in _entity_registry:
        raise KeyError(f"Entity '{name}' is not registered")
    return _entity_registry[name]


def get_all_entities() -> list[type[Entity]]:
    return list(_entity_registry.values())


# ── Report registry ────────────────────────────────────────────────

def register_report(cls: type) -> type:
    _report_registry[cls.__name__] = cls
    return cls


def get_all_reports() -> list[type]:
    return list(_report_registry.values())


def get_report(name: str) -> type:
    if name not in _report_registry:
        raise KeyError(f"Report '{name}' is not registered")
    return _report_registry[name]


# ── Full introspection ─────────────────────────────────────────────

def get_all_metadata_types() -> list[type]:
    return get_all_entities() + get_all_reports()


def clear() -> None:
    _entity_registry.clear()
    _report_registry.clear()


# ── Decorators ─────────────────────────────────────────────────────

@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(Field,),
)
def catalog(cls: type) -> type:
    """Register a master data / catalog entity (subclass of ``Catalog`` / ``MasterData``)."""
    if not isinstance(cls, EntityMeta):
        raise TypeError(f"@catalog requires an Entity subclass, got {type(cls)}")
    cls._meta.entity_type = "catalog"
    register_entity(cls)
    return cls


# Universal aliases for @catalog
lookup = catalog
master_data = catalog


@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(Field,),
)
def document(cls: type) -> type:
    """Register a transaction / document entity (subclass of ``Document`` / ``Transaction``)."""
    if not isinstance(cls, EntityMeta):
        raise TypeError(f"@document requires an Entity subclass, got {type(cls)}")
    cls._meta.entity_type = "document"
    register_entity(cls)
    return cls


# Universal alias for @document
transaction = document

