from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Generic, Literal, Self, TypedDict, TypeVar, overload

if TYPE_CHECKING:
    from .entity import Entity

T = TypeVar("T")

_EntityClass = type["Entity"]


class BasisTable(TypedDict):
    source: str
    columns: dict[str, str]


class Field(Generic[T], ABC):
    """Base descriptor for all entity fields.

    Stores metadata about a field and provides type conversion and validation.
    Field instances are class-level descriptors collected by EntityMeta.
    """

    name: str
    display_name: str
    field_type: type
    required: bool = False
    default: T | Callable[[], T] | None = None
    read_only: bool = False
    indexed: bool = False
    unique: bool = False
    visible: bool = True

    def __init__(
        self,
        display_name: str | None = None,
        *,
        required: bool = False,
        default: T | Callable[[], T] | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        self.display_name = display_name or ""
        self.required = required
        self.default = default
        self.read_only = read_only
        self.indexed = indexed
        self.unique = unique
        self.visible = visible

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name
        if not self.display_name:
            self.display_name = name.replace("_", " ").capitalize()

    @overload
    def __get__(self, instance: None, owner: Any) -> Self: ...

    @overload
    def __get__(self, instance: object, owner: Any = None) -> T: ...

    def __get__(self, instance: object | None, owner: Any = None) -> Any:
        if instance is None:
            return self
        if hasattr(instance, "_data"):
            return getattr(instance, "_data").get(getattr(self, "name", ""))
        return None

    def __set__(self, instance: object, value: Any) -> None:
        if hasattr(instance, "_data"):
            getattr(instance, "_data")[getattr(self, "name", "")] = self.to_python(value)

    @abstractmethod
    def to_python(self, value: Any) -> T | None:
        pass

    def _is_empty(self, value: T | None) -> bool:
        return value is None

    def validate(self, value: T | None) -> list[str]:
        errors: list[str] = []
        if self.required and self._is_empty(value):
            errors.append(f"{self.display_name} обязательно")
        return errors


class StringField(Field[str]):
    field_type: type = str
    max_length: int | None = None
    multiline: bool = False

    def __init__(
        self,
        display_name: str | None = None,
        *,
        max_length: int | None = None,
        multiline: bool = False,
        required: bool = False,
        default: str | Callable[[], str] | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        super().__init__(
            display_name,
            required=required,
            default=default,
            read_only=read_only,
            indexed=indexed,
            unique=unique,
            visible=visible,
        )
        self.max_length = max_length
        self.multiline = multiline

    def _is_empty(self, value: str | None) -> bool:
        return value is None or value == ""

    def to_python(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def validate(self, value: str | None) -> list[str]:
        errors = super().validate(value)
        if value is not None and self.max_length is not None and len(value) > self.max_length:
            errors.append(
                f"{self.display_name} не может быть длиннее {self.max_length} симв."
            )
        return errors


class IntegerField(Field[int]):
    field_type: type = int
    min_value: int | None = None
    max_value: int | None = None

    def __init__(
        self,
        display_name: str | None = None,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
        required: bool = False,
        default: int | Callable[[], int] | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        super().__init__(
            display_name,
            required=required,
            default=default,
            read_only=read_only,
            indexed=indexed,
            unique=unique,
            visible=visible,
        )
        self.min_value = min_value
        self.max_value = max_value

    def to_python(self, value: Any) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    def validate(self, value: int | None) -> list[str]:
        errors = super().validate(value)
        if value is not None:
            if self.min_value is not None and value < self.min_value:
                errors.append(f"{self.display_name} не может быть меньше {self.min_value}")
            if self.max_value is not None and value > self.max_value:
                errors.append(f"{self.display_name} не может быть больше {self.max_value}")
        return errors


class DecimalField(Field[Decimal]):
    field_type: type = Decimal
    precision: int = 10
    scale: int = 2
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    currency: str | None = None

    def __init__(
        self,
        display_name: str | None = None,
        *,
        precision: int = 10,
        scale: int = 2,
        min_value: Decimal | int | float | None = None,
        max_value: Decimal | int | float | None = None,
        currency: str | None = None,
        required: bool = False,
        default: Decimal | int | float | Callable[[], Decimal] | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        norm_default: Decimal | Callable[[], Decimal] | None
        if default is None or callable(default) or isinstance(default, Decimal):
            norm_default = default
        else:
            norm_default = Decimal(str(default))

        super().__init__(
            display_name,
            required=required,
            default=norm_default,
            read_only=read_only,
            indexed=indexed,
            unique=unique,
            visible=visible,
        )
        self.precision = precision
        self.scale = scale
        self.min_value = (
            min_value if min_value is None or isinstance(min_value, Decimal)
            else Decimal(str(min_value))
        )
        self.max_value = (
            max_value if max_value is None or isinstance(max_value, Decimal)
            else Decimal(str(max_value))
        )
        self.currency = currency  # "RUB", "USD", "EUR"

    def to_python(self, value: Any) -> Decimal | None:
        if value is None or value == "":
            return None
        return Decimal(str(value))

    def validate(self, value: Decimal | None) -> list[str]:
        errors = super().validate(value)
        if value is not None:
            if not value.is_finite():
                errors.append(f"{self.display_name} должно быть конечным числом")
                return errors
            if self.min_value is not None and value < self.min_value:
                errors.append(f"{self.display_name} не может быть меньше {self.min_value}")
            if self.max_value is not None and value > self.max_value:
                errors.append(f"{self.display_name} не может быть больше {self.max_value}")
        return errors


class BooleanField(Field[bool]):
    field_type: type = bool

    def __init__(
        self,
        display_name: str | None = None,
        *,
        required: bool = False,
        default: bool | Callable[[], bool] | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        super().__init__(
            display_name,
            required=required,
            default=default,
            read_only=read_only,
            indexed=indexed,
            unique=unique,
            visible=visible,
        )

    def to_python(self, value: Any) -> bool | None:
        if value is None:
            return None
        return bool(value)


class DateField(Field[datetime.date]):
    field_type: type = datetime.date

    def __init__(
        self,
        display_name: str | None = None,
        *,
        required: bool = False,
        default: datetime.date | Callable[[], datetime.date] | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        super().__init__(
            display_name,
            required=required,
            default=default,
            read_only=read_only,
            indexed=indexed,
            unique=unique,
            visible=visible,
        )

    def to_python(self, value: Any) -> datetime.date | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, str):
            try:
                return datetime.date.fromisoformat(value)
            except ValueError:
                return datetime.datetime.fromisoformat(value).date()
        raise TypeError(f"Cannot convert {type(value)} to date")


class DateTimeField(Field[datetime.datetime]):
    field_type: type = datetime.datetime

    def __init__(
        self,
        display_name: str | None = None,
        *,
        required: bool = False,
        default: datetime.datetime | Callable[[], datetime.datetime] | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        super().__init__(
            display_name,
            required=required,
            default=default,
            read_only=read_only,
            indexed=indexed,
            unique=unique,
            visible=visible,
        )

    def to_python(self, value: Any) -> datetime.datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, datetime.date):
            return datetime.datetime.combine(value, datetime.time(0, 0))
        if isinstance(value, str):
            return datetime.datetime.fromisoformat(value)
        raise TypeError(f"Cannot convert {type(value)} to datetime")


class TimeField(Field[datetime.time]):
    """Time field (HH:MM) → QTimeEdit."""

    field_type: type = datetime.time

    def __init__(
        self,
        display_name: str | None = None,
        *,
        required: bool = False,
        default: datetime.time | Callable[[], datetime.time] | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        super().__init__(
            display_name,
            required=required,
            default=default,
            read_only=read_only,
            indexed=indexed,
            unique=unique,
            visible=visible,
        )

    def to_python(self, value: Any) -> datetime.time | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime.time):
            return value
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            parts = raw.split(":")
            if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
                raise ValueError(f"Некорректный формат времени: «{value}». Ожидается ЧЧ:ММ.")
            hour, minute = int(parts[0]), int(parts[1])
            second = int(parts[2]) if len(parts) == 3 else 0
            return datetime.time(hour, minute, second)
        if isinstance(value, datetime.datetime):
            return value.time()
        raise TypeError(f"Cannot convert {type(value)} to time")


class ChoiceField(Field[str]):
    """Choice field — multi-select checkboxes (``multi=True``) or single-select dropdown (``multi=False``)."""

    field_type: type = str
    choices: list[tuple[str, str]]  # (value, display)
    separator: str = "|"
    multi: bool = True

    def __init__(
        self,
        display_name: str | None = None,
        *,
        choices: list[tuple[str, str]] | None = None,
        separator: str = "|",
        multi: bool = True,
        required: bool = False,
        default: str | Callable[[], str] | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        super().__init__(
            display_name,
            required=required,
            default=default,
            read_only=read_only,
            indexed=indexed,
            unique=unique,
            visible=visible,
        )
        self.choices = choices or []
        self.separator = separator
        self.multi = multi

    def _is_empty(self, value: str | None) -> bool:
        return value is None or value == ""

    def to_python(self, value: Any) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, (list, tuple)):
            return self.separator.join(str(v) for v in value) if value else None
        return str(value)

    def to_list(self, value: str | None) -> list[str]:
        if not value:
            return []
        return [v for v in value.split(self.separator) if v]

    def format_display(self, value: str | None) -> str:
        """Convert stored value codes to human-readable labels."""
        if not value:
            return ""
        labels = dict(self.choices)
        if not self.multi:
            return labels.get(value, value)
        return ", ".join(labels.get(code, code) for code in self.to_list(value))

    def validate(self, value: str | None) -> list[str]:
        errors = super().validate(value)
        if value and self.choices:
            valid = {code for code, _ in self.choices}
            selected = self.to_list(value) if self.multi else [value]
            invalid = [c for c in selected if c not in valid]
            if invalid:
                errors.append(
                    f"{self.display_name}: недопустимые значения {', '.join(invalid)}"
                )
        return errors


class RefField(Field[int]):
    field_type: type = int
    target: type | str
    representation_field: str | None = None
    auto_fill: str | None = None  # field name on target to auto-fill into a like-named field

    def __init__(
        self,
        target: type | str,
        display_name: str | None = None,
        *,
        representation_field: str | None = None,
        auto_fill: str | None = None,
        on_delete: Literal["RESTRICT", "CASCADE"] = "RESTRICT",
        related_list: str | None = None,
        choice_filter: dict[str, str] | None = None,
        create_from: str | None = None,
        basis_fields: dict[str, str] | None = None,
        basis_tables: dict[str, BasisTable] | None = None,
        required: bool = False,
        default: int | Callable[[], int] | None = None,
        read_only: bool = False,
        indexed: bool = True,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        super().__init__(
            display_name or "",
            required=required,
            default=default,
            read_only=read_only,
            indexed=indexed,
            unique=unique,
            visible=visible,
        )
        self.target = target
        self.representation_field = representation_field
        self.auto_fill = auto_fill
        self.on_delete = on_delete
        self.related_list = related_list
        self.choice_filter = dict(choice_filter or {})
        self.create_from = create_from
        self.basis_fields = dict(basis_fields or {})
        self.basis_tables = dict(basis_tables or {})

    def resolve_target(self) -> type:
        if isinstance(self.target, str):
            from .registry import get_entity
            return get_entity(self.target)
        return self.target

    def to_python(self, value: Any) -> int | None:
        if value is None or value == "":
            return None
        if hasattr(value, "_id"):
            ent_id = getattr(value, "_id")
            if ent_id is None:
                raise ValueError(
                    f"Нельзя сослаться на несохранённый объект {type(value).__name__} (сначала сохраните его в БД)"
                )
            return int(ent_id)
        return int(value)
