"""Регистры: накопления (остатки) и сведений (периодические значения)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from .entity import Entity

if TYPE_CHECKING:
    from .orm import Database


# ── Дескрипторы измерений и ресурсов ──────────────────────────────

class Dimension:
    """Измерение регистра (аналитика)."""

    def __init__(
        self,
        name: str,
        field_type: Any = str,
        *,
        required: bool = True,
    ) -> None:
        self.name = name
        self.field_type = field_type
        self.required = required


class RefDimension(Dimension):
    """Измерение-ссылка на справочник."""

    target: type | str
    representation_field: str | None = None

    def __init__(
        self,
        target: type | str,
        name: str,
        *,
        representation_field: str | None = None,
        required: bool = True,
    ) -> None:
        super().__init__(name, int, required=required)
        self.target = target
        self.representation_field = representation_field

    def resolve_target(self) -> type:
        if isinstance(self.target, str):
            from .registry import get_entity
            return get_entity(self.target)
        return self.target


class Resource:
    """Ресурс регистра (числовое значение / метрика)."""

    def __init__(
        self,
        name: str,
        *,
        field_type: Any = Decimal,
        required: bool = True,
    ) -> None:
        self.name = name
        self.field_type = field_type
        self.required = required


# Universal alias for Resource
Metric = Resource


# ── Метаданные регистра ────────────────────────────────────────────

class RegisterMeta:
    """Метаданные регистра."""

    def __init__(
        self,
        name: str,
        dimensions: dict[str, Dimension],
        resources: dict[str, Resource],
        register_type: str = "accumulation",
    ) -> None:
        self.name = name
        self.dimensions = dimensions
        self.resources = resources
        self.register_type = register_type  # 'accumulation' or 'information'
        self.orm_model: type | None = None
        self.table_name: str = ""


# ── Базовый класс регистра ────────────────────────────────────────

class Register:
    """Базовый класс для всех регистров (Ledgers / History Stores)."""

    _register_meta: ClassVar[RegisterMeta]

    def __init__(self, **kwargs: Any) -> None:
        self._data: dict[str, Any] = {}
        for name, dim in self._register_meta.dimensions.items():
            self._data[name] = kwargs.get(name)
        for name, res in self._register_meta.resources.items():
            self._data[name] = kwargs.get(name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        elif "_data" in object.__getattribute__(self, "__dict__") and name in self._data:
            self._data[name] = value
        else:
            object.__setattr__(self, name, value)


TReg = TypeVar("TReg")

# Universal aliases for Register
Ledger = Register
AccumulationRegister = Register
InformationRegister = Register
HistoryRegister = Register
ValueHistory = Register


# ── Движение ───────────────────────────────────────────────────────

@dataclass
class Movement:
    """Одна запись движения по регистру (Ledger Entry)."""

    register: type[Any]
    period: datetime.date | None = None
    dimensions: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    is_income: bool = True
    doc_id: int | None = None
    doc_type: str | None = None


# Universal alias for Movement
LedgerEntry = Movement


# ── Регистрация ────────────────────────────────────────────────────

_REGISTRY: dict[str, type[Any]] = {}


def register_register(cls: type[TReg]) -> type[TReg]:
    _REGISTRY[cls.__name__] = cls
    return cls


def get_all_registers() -> list[type[Any]]:
    return list(_REGISTRY.values())


# ── Декоратор регистра ─────────────────────────────────────────────

def accumulation(cls: type[TReg]) -> type[TReg]:
    """Декоратор: регистр накопления (Balance Ledger / остатки и обороты)."""
    dims, ress = _collect_members(cls)
    cls._register_meta = RegisterMeta(cls.__name__, dims, ress, "accumulation")  # type: ignore[attr-defined]
    register_register(cls)
    return cls


# Universal aliases for @accumulation
ledger = accumulation
balance_ledger = accumulation


def information(cls: type[TReg]) -> type[TReg]:
    """Декоратор: регистр сведений (Value History / периодические значения и срезы)."""
    dims, ress = _collect_members(cls)
    cls._register_meta = RegisterMeta(cls.__name__, dims, ress, "information")  # type: ignore[attr-defined]
    register_register(cls)
    return cls


# Universal aliases for @information
history_register = information
value_history = information


def _collect_members(cls: type) -> tuple[dict[str, Dimension], dict[str, Resource]]:
    dims: dict[str, Dimension] = {}
    ress: dict[str, Resource] = {}
    for key, val in cls.__dict__.items():
        if isinstance(val, Dimension):
            dims[key] = val
        elif isinstance(val, Resource):
            ress[key] = val
    for base in cls.__bases__:
        base_meta: RegisterMeta | None = getattr(base, "_register_meta", None)
        if base_meta:
            for k, v in base_meta.dimensions.items():
                dims.setdefault(k, v)
            for k, v in base_meta.resources.items():
                ress.setdefault(k, v)
    return dims, ress


# ── ORM: создание таблиц ──────────────────────────────────────────

def create_register_models(engine: Any, base: Any) -> None:
    """Создать SQL-модели для всех зарегистрированных регистров."""
    from sqlalchemy import Boolean, Column, Date, Integer, Numeric, String

    for cls in get_all_registers():
        meta = cls._register_meta
        if meta.orm_model is not None:
            continue
        table_name = _reg_table_name(meta.name)
        attrs: dict[str, Any] = {
            "__tablename__": table_name,
            "__table_args__": {"extend_existing": True},
            "id": Column(Integer, primary_key=True, autoincrement=True),
            "period": Column(Date, nullable=(meta.register_type != "accumulation"), index=True),
        }
        for name, dim in meta.dimensions.items():
            if isinstance(dim, RefDimension) or dim.field_type is int:
                attrs[name] = Column(Integer, nullable=not dim.required, index=True)
            elif dim.field_type is datetime.date:
                attrs[name] = Column(Date, nullable=not dim.required, index=True)
            elif dim.field_type is bool:
                attrs[name] = Column(Boolean, nullable=not dim.required, index=True)
            else:
                attrs[name] = Column(String(255), nullable=not dim.required, index=True)
        attrs["doc_type"] = Column(String(50), nullable=True, index=True)
        attrs["doc_id"] = Column(Integer, nullable=True, index=True)
        for name, res in meta.resources.items():
            if res.field_type == Decimal:
                attrs[name] = Column(Numeric(15, 2), nullable=not res.required)
            elif res.field_type == int:
                attrs[name] = Column(Integer, nullable=not res.required)
            else:
                attrs[name] = Column(String(255), nullable=not res.required)
        model = type(f"{meta.name}Model", (base,), attrs)
        meta.orm_model = model
        meta.table_name = table_name


def _reg_table_name(name: str) -> str:
    parts: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            parts.append("_")
        parts.append(ch.lower())
    return "reg_" + "".join(parts)


def _norm_dim_val(val: Any) -> Any:
    if isinstance(val, Entity):
        return val._id
    if hasattr(val, "_id"):
        return val._id
    return val


# ── Работа с движениями ───────────────────────────────────────────

def write_movements(db: Database, movements: list[Movement]) -> None:
    """Записать движения в регистры (расход — отрицательные числа)."""
    from decimal import Decimal as _D
    from sqlalchemy.orm import sessionmaker

    db._check_ready()
    Session = sessionmaker(bind=db._engine)
    with Session() as session:
        for mv in movements:
            meta = mv.register._register_meta
            model_class = meta.orm_model
            if model_class is None:
                continue
            row = model_class()
            eff_period = mv.period
            if eff_period is None:
                eff_period = datetime.date.today()
            elif isinstance(eff_period, datetime.datetime):
                eff_period = eff_period.date()
            row.period = eff_period

            if mv.doc_type:
                row.doc_type = mv.doc_type
            if mv.doc_id is not None:
                row.doc_id = mv.doc_id
            for k, v in mv.dimensions.items():
                if not hasattr(row, k):
                    raise ValueError(f"Измерение '{k}' не найдено в регистре {meta.name}")
                setattr(row, k, _norm_dim_val(v))
            sign = 1 if mv.is_income else -1
            for k, v in mv.resources.items():
                if not hasattr(row, k):
                    raise ValueError(f"Ресурс '{k}' не найден в регистре {meta.name}")
                res_meta = meta.resources.get(k)
                if res_meta and res_meta.field_type == Decimal:
                    dec_val = v if isinstance(v, _D) else _D(str(v))
                    setattr(row, k, dec_val * sign)
                elif isinstance(v, _D):
                    setattr(row, k, v * sign)
                elif isinstance(v, (int, float)):
                    setattr(row, k, v * sign)
                else:
                    setattr(row, k, v)
            session.add(row)
        session.commit()


def clear_movements(
    db: Database,
    register_cls: type[Any],
    doc_id: int | None = None,
    doc_type: str | None = None,
) -> None:
    """Очистить движения регистра по документу."""
    if doc_id is None and not doc_type:
        return  # Guard against wiping an entire register accidentally
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db._engine)
    meta = register_cls._register_meta
    model_class = meta.orm_model
    if model_class is None:
        return
    with Session() as session:
        q = session.query(model_class)
        if doc_id is not None:
            q = q.filter(model_class.doc_id == doc_id)
        if doc_type:
            q = q.filter(model_class.doc_type == doc_type)
        q.delete()
        session.commit()


def get_balance(
    db: Database,
    register_cls: type[Any],
    on_date: datetime.date | None = None,
    group_by: list[str] | None = None,
    **dim_filters: Any,
) -> list[dict[str, Any]]:
    """Получить остатки/обороты по регистру накопления."""
    from sqlalchemy import func as sa_func
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db._engine)
    meta = register_cls._register_meta
    model_class = meta.orm_model
    if model_class is None:
        return []
    with Session() as session:
        q = session.query(model_class)
        if on_date:
            eff_date = on_date.date() if isinstance(on_date, datetime.datetime) else on_date
            q = q.filter(model_class.period <= eff_date)
        for k, v in dim_filters.items():
            q = q.filter(getattr(model_class, k) == _norm_dim_val(v))
        res_columns = [sa_func.sum(getattr(model_class, r)) for r in meta.resources]
        if group_by:
            group_cols = [getattr(model_class, g) for g in group_by]
            q = q.with_entities(*group_cols, *res_columns).group_by(*group_cols)
        else:
            q = q.with_entities(*res_columns)
        rows = q.all()
        result: list[dict[str, Any]] = []
        res_items = list(meta.resources.items())
        for row in rows:
            entry: dict[str, Any] = {}
            offset = 0
            if group_by:
                for i, g in enumerate(group_by):
                    entry[g] = row[i]
                offset = len(group_by)
            for i, (rn, res_def) in enumerate(res_items):
                val = row[offset + i]
                if val is None:
                    entry[rn] = Decimal("0.00") if res_def.field_type == Decimal else 0
                elif res_def.field_type == Decimal:
                    entry[rn] = val if isinstance(val, Decimal) else Decimal(str(val))
                else:
                    entry[rn] = float(val)
            result.append(entry)
    return result


# Universal alias for get_balance
get_ledger_balance = get_balance


def get_records(
    db: Database,
    register_cls: type[Any],
    on_date: datetime.date | None = None,
    order_by: str | None = None,
    desc: bool = False,
    limit: int | None = None,
    **dim_filters: Any,
) -> list[dict[str, Any]]:
    """Получить сырые записи регистра с фильтрацией (для регистров сведений и логов)."""
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=db._engine)
    meta = register_cls._register_meta
    model_class = meta.orm_model
    if model_class is None:
        return []
    with Session() as session:
        q = session.query(model_class)
        if on_date and hasattr(model_class, "period"):
            eff_date = on_date.date() if isinstance(on_date, datetime.datetime) else on_date
            q = q.filter(model_class.period <= eff_date)
        for k, v in dim_filters.items():
            if hasattr(model_class, k):
                q = q.filter(getattr(model_class, k) == _norm_dim_val(v))
        if order_by and hasattr(model_class, order_by):
            col = getattr(model_class, order_by)
            q = q.order_by(col.desc() if desc else col.asc())
        elif hasattr(model_class, "period") and hasattr(model_class, "id"):
            pcol = getattr(model_class, "period")
            icol = getattr(model_class, "id")
            if desc:
                q = q.order_by(pcol.desc(), icol.desc())
            else:
                q = q.order_by(pcol.asc(), icol.asc())
        elif hasattr(model_class, "id"):
            col = getattr(model_class, "id")
            q = q.order_by(col.desc() if desc else col.asc())
        if limit is not None:
            q = q.limit(limit)
        rows = q.all()
        result: list[dict[str, Any]] = []
        for r in rows:
            row_dict: dict[str, Any] = {}
            if hasattr(r, "id"):
                row_dict["id"] = r.id
            if hasattr(r, "period"):
                row_dict["period"] = r.period
            if hasattr(r, "doc_type"):
                row_dict["doc_type"] = r.doc_type
            if hasattr(r, "doc_id"):
                row_dict["doc_id"] = r.doc_id
            for d in meta.dimensions:
                row_dict[d] = getattr(r, d, None)
            for rn in meta.resources:
                val = getattr(r, rn, None)
                row_dict[rn] = val
            result.append(row_dict)
        return result


def get_latest(
    db: Database,
    register_cls: type[Any],
    on_date: datetime.date | None = None,
    **dim_filters: Any,
) -> dict[str, Any] | None:
    """Получить самую свежую запись регистра сведений (срез последних по дате и ID)."""
    records = get_records(
        db,
        register_cls,
        on_date=on_date,
        order_by=None,
        desc=True,
        limit=1,
        **dim_filters,
    )
    return records[0] if records else None


# 1C alias for get_latest
get_slice_last = get_latest


def set_record(
    db: Database,
    register_cls: type[Any],
    dimensions: dict[str, Any] | None = None,
    resources: dict[str, Any] | None = None,
    period: datetime.date | None = None,
    doc_id: int | None = None,
    doc_type: str | None = None,
    is_income: bool = True,
) -> None:
    """Быстрая запись единичной записи/движения в регистр."""
    write_movements(db, [
        Movement(
            register=register_cls,
            period=period,
            dimensions=dimensions or {},
            resources=resources or {},
            is_income=is_income,
            doc_id=doc_id,
            doc_type=doc_type,
        )
    ])
