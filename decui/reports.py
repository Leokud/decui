"""Report system — declarative reports with auto-generated parameter forms."""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from .orm import Database


# ── Output column descriptor ───────────────────────────────────────

@dataclass
class Column:
    title: str
    field: str
    width: int = 150
    align: str = "left"
    aggregate: str | None = None
    """One of 'sum', 'avg', 'count', 'min', 'max' or None."""
    format: str | None = None
    """Python format string, e.g. '{:,.2f}' for thousand separators."""
    prefix: str = ""
    """Text before value, e.g. '$'."""
    suffix: str = ""
    """Text after value, e.g. ' руб.'."""


def calc_aggregate(values: list[str], agg: str) -> str:
    from decimal import Decimal, InvalidOperation
    nums: list[Decimal] = []
    for v in values:
        try:
            nums.append(Decimal(str(v).replace(",", ".")))
        except (InvalidOperation, ValueError):
            continue
    if not nums:
        return ""
    if agg == "count":
        return str(len(nums))
    if agg == "sum":
        return str(sum(nums))
    if agg == "avg":
        return str(sum(nums) / len(nums))
    if agg == "min":
        return str(min(nums))
    if agg == "max":
        return str(max(nums))
    return ""


# ── Parameter descriptors ──────────────────────────────────────────

class ReportParam(ABC):
    """Base for report filter parameters."""

    name: str = ""
    display_name: str = ""
    required: bool = False

    def __init__(
        self,
        display_name: str,
        *,
        required: bool = False,
    ) -> None:
        self.display_name = display_name
        self.required = required


class StringParam(ReportParam):
    """Text filter parameter → QLineEdit."""


class DateParam(ReportParam):
    """Date filter parameter → QDateEdit."""

    def __init__(
        self,
        display_name: str,
        *,
        required: bool = False,
        default: Any = None,
    ) -> None:
        super().__init__(display_name, required=required)
        self.default = default


class RefParam(ReportParam):
    """Reference filter → QComboBox populated from an entity."""

    target: type | str
    representation_field: str | None = None

    def __init__(
        self,
        display_name: str,
        target: type | str,
        *,
        representation_field: str | None = None,
        required: bool = False,
    ) -> None:
        super().__init__(display_name, required=required)
        self.target = target
        self.representation_field = representation_field

    def resolve_target(self) -> type:
        if isinstance(self.target, str):
            from .registry import get_entity
            return get_entity(self.target)
        return self.target


# ── Report metadata ────────────────────────────────────────────────

@dataclass
class ReportMeta:
    name: str
    title: str
    params: dict[str, ReportParam]


# ── Report base class ──────────────────────────────────────────────

class Report:
    """Base class for all reports.

    Subclasses define:
      * ``title`` — display name
      * ``columns`` — list of ``Column`` for the output table
      * ``execute(self, db, **params)`` — business logic, returns list[dict]

    Parameter descriptors (``StringParam``, ``DateParam``, ``RefParam``)
    are collected automatically by ``__init_subclass__``.
    """

    _report_meta: ClassVar[ReportMeta]
    title: str = ""
    columns: list[Column] = field(default_factory=list)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        params: dict[str, ReportParam] = {}
        title = getattr(cls, "title", None) or cls.__name__

        for key, value in list(cls.__dict__.items()):
            if isinstance(value, ReportParam):
                value.name = key
                params[key] = value

        for base in cls.__bases__:
            base_meta: ReportMeta | None = getattr(base, "_report_meta", None)
            if base_meta is not None:
                for k, v in base_meta.params.items():
                    params.setdefault(k, v)

        cls._report_meta = ReportMeta(cls.__name__, title, params)

    def execute(self, db: Database, **params: Any) -> list[dict[str, Any]]:
        raise NotImplementedError(
            f"{type(self).__name__} must implement execute()"
        )


# ── @report decorator ──────────────────────────────────────────────

_REPORT_MARKER = "__erp_report__"


def report(cls: type) -> type:
    """Decorator that registers a Report subclass in the report registry."""
    from .registry import register_report

    register_report(cls)
    setattr(cls, _REPORT_MARKER, True)
    return cls
