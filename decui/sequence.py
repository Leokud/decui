"""Контроль последовательности документов и версионирование."""

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .entity import Entity
    from .orm import Database


# ── Последовательность документов ──────────────────────────────────

def check_sequence(
    db: Database,
    doc: Entity,
    date_field: str = "date",
) -> None:
    """Проверить, что документ проводится в хронологическом порядке."""
    from .entity import Entity

    doc_cls = type(doc)
    doc_date = getattr(doc, date_field, None)
    if doc_date is None:
        return
    posted = []
    for other in db.list(doc_cls):
        if other._id == doc._id:
            continue
        other_date = getattr(other, date_field, None)
        if other_date and hasattr(other, "posted") and other.posted:
            posted.append((other._id, other_date))
    posted.sort(key=lambda x: x[1])
    for oid, od in posted:
        if od > doc_date:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                None, "Нарушение последовательности",
                f"Документ #{oid} от {od} уже проведён после текущей даты.\n"
                "Проведение невозможно до исправления последовательности."
            )
            raise ValueError("Sequence violation")


# ── Версионирование ────────────────────────────────────────────────

def save_version(db: Database, entity: Entity) -> None:
    """Сохранить версию сущности перед изменением (заглушка)."""
    pass  # TODO: реализовать через отдельную таблицу


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
