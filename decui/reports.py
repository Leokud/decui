"""Report system — declarative reports with auto-generated parameter forms."""

from __future__ import annotations

import csv
import io
from abc import ABC
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

if TYPE_CHECKING:
    from .orm import Database

TReport = TypeVar("TReport", bound="Report")


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


def _parse_numeric_str(raw: Any) -> Any:
    """Parse number strings safely, supporting space thousand separators and comma decimals."""
    from decimal import Decimal, InvalidOperation

    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw))
    s = str(raw).strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None
    # Handle "1,234.56" vs "1234,56"
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) != 3:
            s = s.replace(",", ".")
        elif len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3:
            # e.g. "10,500" -> likely thousand separator, or 3-dec float; default replace comma as decimal only when ambiguous? Wait: standard comma decimal if <=2 chars
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def calc_aggregate(values: list[Any], agg: str) -> str:
    nums = []
    for v in values:
        parsed = _parse_numeric_str(v)
        if parsed is not None:
            nums.append(parsed)
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
    default: Any = None

    def __init__(
        self,
        display_name: str,
        *,
        required: bool = False,
        default: Any = None,
    ) -> None:
        self.display_name = display_name
        self.required = required
        self.default = default


class StringParam(ReportParam):
    """Text filter parameter → QLineEdit."""

    def __init__(
        self,
        display_name: str,
        *,
        required: bool = False,
        default: str = "",
    ) -> None:
        super().__init__(display_name, required=required, default=default)


class IntegerParam(ReportParam):
    """Integer filter parameter → QSpinBox."""

    def __init__(
        self,
        display_name: str,
        *,
        required: bool = False,
        default: int = 0,
        min_value: int = -999999999,
        max_value: int = 999999999,
    ) -> None:
        super().__init__(display_name, required=required, default=default)
        self.min_value = min_value
        self.max_value = max_value


NumberParam = IntegerParam


class DecimalParam(ReportParam):
    """Decimal filter parameter → QDoubleSpinBox."""

    def __init__(
        self,
        display_name: str,
        *,
        required: bool = False,
        default: float = 0.0,
        precision: int = 15,
        scale: int = 2,
    ) -> None:
        super().__init__(display_name, required=required, default=default)
        self.precision = precision
        self.scale = scale


class BooleanParam(ReportParam):
    """Boolean filter parameter → QCheckBox."""

    def __init__(
        self,
        display_name: str,
        *,
        default: bool = False,
    ) -> None:
        super().__init__(display_name, required=False, default=default)


class DateParam(ReportParam):
    """Date filter parameter → QDateEdit."""


class ChoiceParam(ReportParam):
    """Single-choice dropdown filter parameter → QComboBox."""

    def __init__(
        self,
        display_name: str,
        choices: list[tuple[str, str]],
        *,
        required: bool = False,
        default: str | None = None,
    ) -> None:
        super().__init__(display_name, required=required, default=default)
        self.choices = choices


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
        default: Any = None,
    ) -> None:
        super().__init__(display_name, required=required, default=default)
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
      * ``auto_run`` — automatically run report when opened (optional)
    """

    _report_meta: ClassVar[ReportMeta]
    title: ClassVar[str] = ""
    columns: ClassVar[list[Column]] = []
    auto_run: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        params: dict[str, ReportParam] = {}
        title = getattr(cls, "title", None) or getattr(cls, "__verbose_name__", None) or cls.__name__

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

    @classmethod
    def to_csv(
        cls,
        rows: list[dict[str, Any]],
        file_path: str | None = None,
    ) -> str:
        """Export report result rows to UTF-8 CSV string (and optionally write to file)."""
        cols = cls.columns or (
            [Column(k, k) for k in rows[0].keys() if not str(k).startswith("_")]
            if rows
            else []
        )
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        writer.writerow([c.title for c in cols])
        for row in rows:
            writer.writerow([row.get(c.field, "") for c in cols])
        content = buf.getvalue()
        if file_path:
            with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(content)
        return content


# Universal alias for Report
QueryReport = Report


# ── @report decorator ──────────────────────────────────────────────

_REPORT_MARKER = "__erp_report__"


def report(cls: type[TReport]) -> type[TReport]:
    """Decorator that registers a Report subclass in the report registry."""
    from .registry import register_report

    register_report(cls)
    setattr(cls, _REPORT_MARKER, True)
    return cls
