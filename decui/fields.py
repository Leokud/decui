from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from .entity import Entity

T = TypeVar("T")

_EntityClass = type["Entity"]


class Field(Generic[T], ABC):
    """Base descriptor for all entity fields.

    Stores metadata about a field and provides type conversion and validation.
    Field instances are class-level descriptors collected by EntityMeta.
    """

    name: str
    display_name: str
    field_type: type
    required: bool = False
    default: T | None = None
    read_only: bool = False
    indexed: bool = False
    unique: bool = False
    visible: bool = True

    def __init__(
        self,
        display_name: str,
        *,
        required: bool = False,
        default: T | None = None,
        read_only: bool = False,
        indexed: bool = False,
        unique: bool = False,
        visible: bool = True,
    ) -> None:
        self.display_name = display_name
        self.required = required
        self.default = default
        self.read_only = read_only
        self.indexed = indexed
        self.unique = unique
        self.visible = visible

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

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

    def __init__(
        self,
        display_name: str,
        *,
        max_length: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(display_name, **kwargs)
        self.max_length = max_length

    def _is_empty(self, value: str | None) -> bool:
        return value is None or value == ""

    def to_python(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)


class IntegerField(Field[int]):
    field_type: type = int
    min_value: int | None = None
    max_value: int | None = None

    def __init__(
        self,
        display_name: str,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(display_name, **kwargs)
        self.min_value = min_value
        self.max_value = max_value

    def to_python(self, value: Any) -> int | None:
        if value is None:
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
        display_name: str,
        *,
        precision: int = 10,
        scale: int = 2,
        min_value: Decimal | None = None,
        max_value: Decimal | None = None,
        currency: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(display_name, **kwargs)
        self.precision = precision
        self.scale = scale
        self.min_value = min_value
        self.max_value = max_value
        self.currency = currency  # "RUB", "USD", "EUR"

    def to_python(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value))

    def validate(self, value: Decimal | None) -> list[str]:
        errors = super().validate(value)
        if value is not None:
            if self.min_value is not None and value < self.min_value:
                errors.append(f"{self.display_name} не может быть меньше {self.min_value}")
            if self.max_value is not None and value > self.max_value:
                errors.append(f"{self.display_name} не может быть больше {self.max_value}")
        return errors


class BooleanField(Field[bool]):
    field_type: type = bool

    def to_python(self, value: Any) -> bool | None:
        if value is None:
            return None
        return bool(value)


class DateField(Field[datetime.date]):
    field_type: type = datetime.date

    def to_python(self, value: Any) -> datetime.date | None:
        if value is None:
            return None
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, str):
            return datetime.date.fromisoformat(value)
        raise TypeError(f"Cannot convert {type(value)} to date")


class DateTimeField(Field[datetime.datetime]):
    field_type: type = datetime.datetime

    def to_python(self, value: Any) -> datetime.datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, str):
            return datetime.datetime.fromisoformat(value)
        raise TypeError(f"Cannot convert {type(value)} to datetime")


class TimeField(Field[datetime.time]):
    """Time field (HH:MM) → QTimeEdit."""

    field_type: type = datetime.time

    def to_python(self, value: Any) -> datetime.time | None:
        if value is None:
            return None
        if isinstance(value, datetime.time):
            return value
        if isinstance(value, str):
            parts = value.split(":")
            return datetime.time(int(parts[0]), int(parts[1]))
        if isinstance(value, datetime.datetime):
            return value.time()
        raise TypeError(f"Cannot convert {type(value)} to time")


class ChoiceField(Field[str]):
    """Multi-choice field — checkboxes, stored as pipe-separated string."""

    field_type: type = str
    choices: list[tuple[str, str]]  # (value, display)
    separator: str = "|"

    def __init__(
        self,
        display_name: str,
        *,
        choices: list[tuple[str, str]] | None = None,
        separator: str = "|",
        **kwargs: Any,
    ) -> None:
        super().__init__(display_name, **kwargs)
        self.choices = choices or []
        self.separator = separator

    def to_python(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return self.separator.join(str(v) for v in value)
        return str(value)

    def to_list(self, value: str | None) -> list[str]:
        if not value:
            return []
        return [v for v in value.split(self.separator) if v]


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
        **kwargs: Any,
    ) -> None:
        super().__init__(display_name or "", **kwargs)
        self.target = target
        self.representation_field = representation_field
        self.auto_fill = auto_fill
        if not display_name and kwargs.get("name"):
            self.display_name = kwargs["name"]

    def __set_name__(self, owner: type, name: str) -> None:
        super().__set_name__(owner, name)
        if not self.display_name:
            self.display_name = name.capitalize()

    def resolve_target(self) -> type:
        if isinstance(self.target, str):
            from .registry import get_entity
            return get_entity(self.target)
        return self.target

    def to_python(self, value: Any) -> int | None:
        if value is None:
            return None
        if hasattr(value, '_id'):
            return value._id
        return int(value)
