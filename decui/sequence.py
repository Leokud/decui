"""Контроль последовательности документов, версионирование и роли."""

from __future__ import annotations

import datetime
from contextvars import ContextVar
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from .entity import Entity
from .fields import DateField, DateTimeField

if TYPE_CHECKING:
    from .orm import Database


class SequenceViolationError(ValueError):
    """Raised when attempting to post a document out of chronological order."""


# ── Последовательность документов ──────────────────────────────────

def _to_comparable_dt(val: datetime.date | datetime.datetime) -> datetime.datetime:
    if isinstance(val, datetime.datetime):
        return val.replace(tzinfo=None)
    return datetime.datetime.combine(val, datetime.time(0, 0))


def check_sequence(
    db: Database,
    doc: Entity,
    date_field: str = "date",
) -> None:
    """Проверить, что документ проводится в хронологическом порядке."""
    from .orm import _translate_sql_error

    db._check_ready()
    doc_cls = type(doc)
    doc_date = getattr(doc, date_field, None)
    if doc_date is None:
        return
    doc_dt = _to_comparable_dt(doc_date)

    model_class = db._get_entity_model(doc_cls)
    if model_class is None:
        raise RuntimeError(f"Модель для {doc_cls.__name__} не создана")
    if not hasattr(model_class, date_field) or not hasattr(model_class, "posted"):
        return

    fdef = doc_cls._meta.fields.get(date_field)
    threshold: datetime.date | datetime.datetime = (
        doc_dt.date()
        if isinstance(fdef, DateField) and not isinstance(fdef, DateTimeField)
        else doc_dt
    )
    date_col = getattr(model_class, date_field)

    try:
        with db.session() as sess:
            query = sess.query(model_class.id, date_col).filter(
                model_class.posted.is_(True),
                date_col.is_not(None),
                date_col > threshold,
            )
            if doc._id is not None:
                query = query.filter(model_class.id != doc._id)
            conflict = query.order_by(date_col.asc(), model_class.id.asc()).first()
    except SQLAlchemyError as exc:
        raise _translate_sql_error("проверке последовательности", exc) from exc

    if conflict is not None:
        oid, raw_od = conflict
        od = _to_comparable_dt(raw_od)
        raise SequenceViolationError(
            f"Документ #{oid} от {od:%d.%m.%Y %H:%M} уже проведён позже текущей даты ({doc_dt:%d.%m.%Y %H:%M}). "
            "Проведение невозможно до исправления последовательности."
        )


# ── Версионирование ────────────────────────────────────────────────

def save_version(db: Database, entity: Entity) -> None:
    """Сохранить версию сущности перед изменением."""
    pass


class Version(Entity):
    """История изменений (зарезервировано)."""
    pass


# ── Роли и права ───────────────────────────────────────────────────

_ROLES: dict[str, list[str]] = {}  # role_name -> [permission, ...]


def define_role(name: str, permissions: list[str]) -> None:
    _ROLES[name] = permissions


def has_permission(role: str, permission: str) -> bool:
    return permission in _ROLES.get(role, [])


_current_role: ContextVar[str] = ContextVar("decui_current_role", default="admin")


def current_role() -> str:
    return _current_role.get()


def set_role(role: str) -> None:
    _current_role.set(role)
