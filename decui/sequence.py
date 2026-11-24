"""Контроль последовательности документов, версионирование и роли."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from .entity import Entity

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
    doc_cls = type(doc)
    doc_date = getattr(doc, date_field, None)
    if doc_date is None:
        return
    doc_dt = _to_comparable_dt(doc_date)

    posted: list[tuple[int | None, datetime.datetime]] = []
    for other in db.list(doc_cls):
        if other._id == doc._id:
            continue
        other_date = getattr(other, date_field, None)
        if other_date and getattr(other, "posted", False):
            posted.append((other._id, _to_comparable_dt(other_date)))
    posted.sort(key=lambda x: x[1])
    for oid, od in posted:
        if od > doc_dt:
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


def current_role() -> str:
    return getattr(_current_user, "_role", "admin")


class _CurrentUser:
    _role: str = "admin"


_current_user = _CurrentUser()


def set_role(role: str) -> None:
    _current_user._role = role
