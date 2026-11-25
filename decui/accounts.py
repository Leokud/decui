"""Opt-in local application accounts."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Column, MetaData, String, Table, select

if TYPE_CHECKING:
    from .orm import Database

_ACCOUNTS = Table(
    "_erp_accounts", MetaData(),
    Column("login", String, primary_key=True),
    Column("role", String, nullable=False),
    Column("salt", String, nullable=False),
    Column("password_hash", String, nullable=False),
)


@dataclass(frozen=True)
class Account:
    login: str
    role: str


_account: ContextVar[Account | None] = ContextVar("decui_account", default=None)


def current_account() -> Account | None:
    return _account.get()


def set_current_account(account: Account | None) -> None:
    _account.set(account)


def _derive(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000).hex()


class AccountStore:
    """Credential storage for trusted application code and the login dialog."""

    def __init__(self, db: Database) -> None:
        db._check_ready()
        self._db = db
        with db.transaction(), db.session() as session:
            _ACCOUNTS.create(session.connection(), checkfirst=True)

    def has_accounts(self) -> bool:
        with self._db.session() as session:
            return session.scalar(select(_ACCOUNTS.c.login).limit(1)) is not None

    def create(self, login: str, password: str, *, role: str) -> Account:
        return self._create(login, password, role=role, first=False)

    def create_first_admin(self, login: str, password: str) -> Account:
        return self._create(login, password, role="admin", first=True)

    def _create(self, login: str, password: str, *, role: str, first: bool) -> Account:
        login = login.strip().casefold()
        role = role.strip()
        if not login or len(login) > 100 or not 1 <= len(role) <= 100:
            raise ValueError("Укажите логин и роль.")
        if not 8 <= len(password) <= 256:
            raise ValueError("Пароль должен содержать от 8 до 256 символов.")
        salt = secrets.token_bytes(16)
        digest = _derive(password, salt)
        with self._db.transaction(), self._db.session() as session:
            if first and self.has_accounts():
                raise ValueError("Первая учётная запись уже создана. Войдите в приложение.")
            if session.scalar(select(_ACCOUNTS.c.login).where(_ACCOUNTS.c.login == login)) is not None:
                raise ValueError("Этот логин уже используется.")
            session.execute(_ACCOUNTS.insert().values(
                login=login, role=role, salt=salt.hex(), password_hash=digest,
            ))
        return Account(login, role)

    def list_accounts(self) -> list[Account]:
        with self._db.session() as session:
            return [Account(row.login, row.role) for row in session.execute(
                select(_ACCOUNTS.c.login, _ACCOUNTS.c.role).order_by(_ACCOUNTS.c.login)
            )]

    def authenticate(self, login: str, password: str) -> Account | None:
        if len(password) > 256:
            return None
        with self._db.session() as session:
            row = session.execute(
                select(_ACCOUNTS).where(_ACCOUNTS.c.login == login.strip().casefold()),
            ).first()
        salt = bytes.fromhex(row.salt) if row is not None else bytes(16)
        expected = row.password_hash if row is not None else "0" * 64
        valid = hmac.compare_digest(_derive(password, salt), expected)
        return Account(row.login, row.role) if row is not None and valid else None
