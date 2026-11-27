from __future__ import annotations

import datetime
import logging
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from sqlalchemy import (
    JSON,
    URL,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    false,
    func,
    select,
)
from sqlalchemy import (
    Boolean as SABoolean,
)
from sqlalchemy import (
    Date as SADate,
)
from sqlalchemy import (
    DateTime as SADateTime,
)
from sqlalchemy import event as sa_event
from sqlalchemy import (
    inspect as sa_inspect,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from .accounts import AccountStore, current_account
from .fields import (
    BooleanField,
    ChoiceField,
    DateField,
    DateTimeField,
    DecimalField,
    Field,
    IntegerField,
    RefField,
    StringField,
    TimeField,
)
from .files import copy_sqlite, publish_new_file
from .movement import create_register_models, get_all_registers
from .numbering import NumberingService
from .registry import get_all_entities
from .sequence import current_role
from .workflow import WorkflowEvent

if TYPE_CHECKING:
    from .entity import Document, Entity

T = TypeVar("T")
TEntity = TypeVar("TEntity", bound="Entity")

logger = logging.getLogger(__name__)

Base = declarative_base()


_WORKFLOW_EVENTS = Table(
    "_erp_workflow_events",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("entity", String, nullable=False),
    Column("entity_id", Integer, nullable=False),
    Column("occurred_at", SADateTime, nullable=False),
    Column("source", String),
    Column("target", String, nullable=False),
    Column("action", String),
    Column("role", String),
    Column("login", String),
    Index("ix_erp_workflow_record", "entity", "entity_id", "id"),
)


_REPORT_VARIANTS = Table(
    "_erp_report_variants",
    Base.metadata,
    Column("owner", String, primary_key=True),
    Column("report", String, primary_key=True),
    Column("name", String, primary_key=True),
    Column("params", JSON, nullable=False),
)


_LIST_SETTINGS = Table(
    "_erp_list_settings",
    Base.metadata,
    Column("owner", String, primary_key=True),
    Column("entity", String, primary_key=True),
    Column("settings", JSON, nullable=False),
)


_LIST_VARIANTS = Table(
    "_erp_list_variants",
    Base.metadata,
    Column("owner", String, primary_key=True),
    Column("entity", String, primary_key=True),
    Column("name", String, primary_key=True),
    Column("settings", JSON, nullable=False),
)


def _chunked(items: Iterable[T], size: int = 500) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _translate_sql_error(action: str, exc: SQLAlchemyError) -> ValueError | RuntimeError:
    """Log the underlying SQLAlchemy exception and return a user-safe Russian error."""
    logger.error("Ошибка базы данных при %s: %s", action, exc, exc_info=exc)
    orig: Any = getattr(exc, "orig", None)
    upper = str(orig if orig is not None else exc).upper()

    if "UNIQUE CONSTRAINT" in upper:
        return ValueError("Нарушена уникальность: запись с таким значением уже существует.")
    if "FOREIGN KEY CONSTRAINT" in upper:
        return ValueError(
            "Операция невозможна: на запись ссылаются другие данные или указана несуществующая ссылка."
        )
    if "NOT NULL CONSTRAINT" in upper:
        return ValueError("Не заполнено обязательное поле.")
    if "CHECK CONSTRAINT" in upper:
        return ValueError("Значение не соответствует ограничениям базы данных.")
    if isinstance(exc, IntegrityError):
        return ValueError(f"Нарушена целостность данных при {action}.")
    if "LOCKED" in upper or "BUSY" in upper:
        return RuntimeError("База данных временно заблокирована другим процессом. Повторите попытку.")
    if "READONLY" in upper or "READ-ONLY" in upper:
        return RuntimeError("База данных открыта только для чтения.")
    if "NO SUCH TABLE" in upper or "NO SUCH COLUMN" in upper:
        return RuntimeError("Структура базы данных не соответствует текущей схеме приложения.")
    if "DISK I/O" in upper or "MALFORMED" in upper or "NOT A DATABASE" in upper or "CORRUPT" in upper:
        return RuntimeError("Файл базы данных повреждён или недоступен для чтения и записи.")
    if "DISK" in upper and "FULL" in upper:
        return RuntimeError("Недостаточно места на диске для записи в базу данных.")
    if isinstance(exc, DataError):
        return ValueError(f"Некорректный формат или размер данных при {action}.")
    if isinstance(exc, OperationalError):
        return RuntimeError(f"Ошибка доступа к базе данных при {action}. Подробности записаны в журнал.")
    return RuntimeError(f"Ошибка базы данных при {action}. Подробности записаны в журнал.")


def _enable_fk(dbapi_con: Any, _con_record: Any) -> None:
    try:
        cursor = dbapi_con.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()
    except Exception:
        logger.warning("Не удалось настроить параметры подключения SQLite.", exc_info=True)


def _coerce_to_db(field: Field, val: Any) -> Any:
    """Convert an entity field value into a SQLite/SQLAlchemy compatible value."""
    if val is None:
        return None
    if isinstance(field, TimeField):
        if isinstance(val, datetime.time):
            return f"{val.hour:02d}:{val.minute:02d}"
        if isinstance(val, datetime.datetime):
            return f"{val.hour:02d}:{val.minute:02d}"
    if isinstance(field, DateField):
        if isinstance(val, datetime.datetime):
            return val.date()
        if isinstance(val, str) and val:
            return field.to_python(val)
    if isinstance(field, DateTimeField) and isinstance(val, str) and val:
        return field.to_python(val)
    if isinstance(field, RefField) and hasattr(val, "_id"):
        return field.to_python(val)
    if isinstance(field, ChoiceField) and isinstance(val, (list, tuple)):
        return field.to_python(val)
    return val


def _coerce_from_db(field: Field, val: Any) -> Any:
    """Convert a database column value back to Python entity field type."""
    if val is None:
        return None
    if isinstance(field, TimeField) and isinstance(val, str) and ":" in val:
        parts = val.split(":")
        return datetime.time(int(parts[0]), int(parts[1]))
    return val


def _date_range_conditions(column: Any, field: Field | None, bounds: Any) -> list[Any]:
    if not isinstance(field, (DateField, DateTimeField)):
        raise ValueError("Оператор __date_range доступен только для полей Date и DateTime.")
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 2 or any(
        bound is not None
        and (not isinstance(bound, datetime.date) or isinstance(bound, datetime.datetime))
        for bound in bounds
    ):
        raise ValueError("Передайте две границы периода как даты (datetime.date) или None.")

    start, end = bounds
    if start is not None and end is not None and start > end:
        raise ValueError("Начало периода должно быть не позже его окончания.")

    conditions = []
    if start is not None:
        lower = datetime.datetime.combine(start, datetime.time.min) if isinstance(field, DateTimeField) else start
        conditions.append(column >= lower)
    if end is not None:
        upper = datetime.datetime.combine(end, datetime.time.max) if isinstance(field, DateTimeField) else end
        conditions.append(column <= upper)
    return conditions


class Database:
    """Persistence layer — creates SQLAlchemy models, manages sessions, CRUD."""

    def __init__(
        self, path: str = ":memory:", echo: bool = False, *,
        column_renames: dict[str, dict[str, str]] | None = None,
        table_part_renames: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._last_error: str | None = None
        self._path = path
        self._ready = False
        self._migration_messages: list[str] = []
        self._base = declarative_base()
        for table in Base.metadata.tables.values():
            if table.name.startswith("_erp_"):
                table.to_metadata(self._base.metadata)
        self._workflow_events_table = self._base.metadata.tables["_erp_workflow_events"]
        self._report_variants_table = self._base.metadata.tables["_erp_report_variants"]
        self._list_settings_table = self._base.metadata.tables["_erp_list_settings"]
        self._list_variants_table = self._base.metadata.tables["_erp_list_variants"]
        self._entity_models: dict[type, type] = {}
        self._tp_models: dict[tuple[type, str], type] = {}
        self._register_models: dict[type, type] = {}
        self._table_names: dict[type, str] = {}
        self._tp_table_names: dict[tuple[type, str], str] = {}
        self._write_state: ContextVar[tuple[Session, list[Callable[[], None]]] | None] = (
            ContextVar("write_state", default=None)
        )
        try:
            self._engine = create_engine(
                URL.create("sqlite", database=path),
                echo=echo,
                connect_args={"check_same_thread": False},
            )
            sa_event.listen(self._engine, "connect", _enable_fk)
            self._session_factory = sessionmaker(bind=self._engine)
            self.numbering = NumberingService(self._engine)
            self._prepare_models()
            self._auto_upgrade(column_renames or {}, table_part_renames or {})
            self._base.metadata.create_all(self._engine)
            self._save_schema_hash()
            self._ready = True
        except SQLAlchemyError as exc:
            self._last_error = str(_translate_sql_error("инициализации базы данных", exc))
        except Exception as exc:
            logger.exception("Ошибка при инициализации базы данных")
            self._last_error = str(exc) or type(exc).__name__

    def _get_entity_model(self, entity_class: type) -> type | None:
        return self._entity_models.get(entity_class)

    def _get_tp_model(self, entity_class: type, tp_name: str) -> type | None:
        return self._tp_models.get((entity_class, tp_name))

    def _get_register_model(self, register_cls: type) -> type | None:
        return self._register_models.get(register_cls)

    @classmethod
    def memory(cls, echo: bool = False) -> Database:
        """Create an in-memory database."""
        return cls(":memory:", echo=echo)

    # ------------------------------------------------------------------
    # Auto schema upgrade
    # ------------------------------------------------------------------

    @staticmethod
    def _field_schema_signature(name: str, field: Field) -> str:
        info = (
            f"{name}:{type(field).__name__}:{field.field_type}:"
            f"{field.required}:{field.unique}:{field.indexed}"
        )
        if hasattr(field, "precision"):
            info += f":{getattr(field, 'precision')}:{getattr(field, 'scale')}"
        if hasattr(field, "max_length"):
            info += f":{getattr(field, 'max_length')}"
        if hasattr(field, "multiline"):
            info += f":{getattr(field, 'multiline')}"
        if hasattr(field, "min_value"):
            info += f":{getattr(field, 'min_value')}"
        if hasattr(field, "max_value"):
            info += f":{getattr(field, 'max_value')}"
        if hasattr(field, "target"):
            tgt = getattr(field, "target")
            tname = tgt.__name__ if isinstance(tgt, type) else str(tgt)
            info += f":{tname}"
            if isinstance(field, RefField) and field.on_delete != "RESTRICT":
                info += f":{field.on_delete}"
        return info

    def _schema_hash(self) -> str:
        """Compute a deterministic hash of registered entities and registers."""
        import hashlib

        h = hashlib.md5()
        h.update(b"_erp_workflow_events:role,login")
        for cls in sorted(get_all_entities(), key=lambda c: c.__name__):
            h.update(cls.__name__.encode())
            for name, field in cls._meta.fields.items():
                h.update(self._field_schema_signature(name, field).encode())
            for tp_name, tp in cls._meta.table_parts.items():
                h.update(f"tp:{tp_name}".encode())
                for fname, ffield in tp.fields.items():
                    h.update(self._field_schema_signature(fname, ffield).encode())
        for reg in sorted(get_all_registers(), key=lambda r: r.__name__):
            rmeta = reg._register_meta
            h.update(f"reg:{rmeta.name}:{rmeta.register_type}".encode())
            for dn, dv in rmeta.dimensions.items():
                tgt = getattr(dv, "target", None)
                tname = tgt.__name__ if isinstance(tgt, type) else (str(tgt) if tgt is not None else "")
                h.update(f"dim:{dn}:{dv.field_type}:{dv.required}:{tname}".encode())
            for rn, rv in rmeta.resources.items():
                h.update(f"res:{rn}:{rv.field_type}:{rv.required}".encode())
        return h.hexdigest()[:12]

    def _save_schema_hash(self) -> None:
        try:
            with self._engine.connect() as conn:
                conn.execute(
                    sa_text("CREATE TABLE IF NOT EXISTS _erp_config (key TEXT PRIMARY KEY, value TEXT)")
                )
                conn.execute(
                    sa_text("INSERT OR REPLACE INTO _erp_config (key, value) VALUES ('schema_hash', :h)"),
                    {"h": self._schema_hash()},
                )
                conn.commit()
        except Exception:
            logger.warning("Не удалось сохранить хеш схемы базы данных.", exc_info=True)

    def _column_rename_plan(self, renames: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        tables: dict[str, str] = {}
        for entity in get_all_entities():
            tables[entity.__name__] = self._table_names.get(entity, self._table_name(entity))
            tables.update({
                f"{entity.__name__}.{name}": self._tp_table_names.get(
                    (entity, name), f"{self._table_name(entity)}_{self._table_name_str(name)}",
                )
                for name in entity._meta.table_parts
            })
        result = {}
        for name, changes in renames.items():
            table_name = tables.get(name)
            if table_name is None:
                raise ValueError(f"Переименование столбцов: объект «{name}» не найден.")
            columns = set(self._base.metadata.tables[table_name].columns.keys())
            targets = set(changes.values())
            if (
                len(targets) != len(changes) or not targets <= columns
                or set(changes) & columns or targets & {"id", "parent_id"}
            ):
                raise ValueError(f"Переименование столбцов для «{name}»: проверьте старые и новые имена.")
            result[table_name] = changes
        return result

    def _table_part_rename_plan(self, renames: dict[str, dict[str, str]]) -> dict[str, str]:
        entities = {entity.__name__: entity for entity in get_all_entities()}
        plan: dict[str, str] = {}
        destinations: set[str] = set()
        known_tables = {name.casefold() for name in self._base.metadata.tables}
        for name, changes in renames.items():
            entity = entities.get(name)
            if entity is None:
                raise ValueError(f"Переименование таблиц: объект «{name}» не найден.")
            parent_table = self._table_names.get(entity, self._table_name(entity))
            for old, new in changes.items():
                part = entity._meta.table_parts.get(new)
                source = f"{parent_table}_{self._table_name_str(old)}"
                dest = self._tp_table_names.get((entity, new), part.table_name if part is not None else "")
                if (
                    part is None or source.casefold() in known_tables
                    or source in plan or dest in destinations
                ):
                    raise ValueError(f"Переименование таблиц для «{name}»: проверьте «{old}» и «{new}».")
                plan[source] = dest
                destinations.add(dest)
        return plan

    def _auto_upgrade(
        self, renames: dict[str, dict[str, str]], part_renames: dict[str, dict[str, str]],
    ) -> None:
        """Apply explicit renames and additive SQLite changes in one transaction."""
        if not renames and not part_renames and sa_inspect(self._engine).has_table("_erp_config"):
            with self._engine.connect() as conn:
                row = conn.execute(
                    sa_text("SELECT value FROM _erp_config WHERE key = 'schema_hash'")
                ).fetchone()
            if row and row[0] == self._schema_hash():
                return

        plan = self._column_rename_plan(renames) if renames else {}
        table_plan = self._table_part_rename_plan(part_renames) if part_renames else {}
        messages: list[str] = []
        with self._engine.begin() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            inspector = sa_inspect(conn)
            existing_tables = set(inspector.get_table_names())
            dialect = conn.dialect
            quote = dialect.identifier_preparer.quote
            for old, new in table_plan.items():
                if old not in existing_tables:
                    if new in existing_tables:
                        messages.append(f"Таблица «{new}» уже существует, «{old}» отсутствует.")
                    else:
                        messages.append(f"Таблица «{old}» отсутствует. Создаётся новая таблица «{new}».")
                    continue
                if new in existing_tables:
                    raise ValueError(f"Уже существуют обе таблицы «{old}» и «{new}».")
                conn.exec_driver_sql(f"ALTER TABLE {quote(old)} RENAME TO {quote(new)}")
                existing_tables.remove(old)
                existing_tables.add(new)
                messages.append(f"Переименована таблица: «{old}» → «{new}».")
            for table in self._base.metadata.sorted_tables:
                if table.name not in existing_tables:
                    if table.name in plan:
                        messages.append(f"Таблица «{table.name}» отсутствует. Колонки создаются вместе с ней.")
                    continue
                db_cols = {c["name"] for c in inspector.get_columns(table.name)}
                for old, new in plan.get(table.name, {}).items():
                    if old not in db_cols:
                        if new in db_cols:
                            messages.append(f"{table.name}: колонка «{new}» уже существует, «{old}» отсутствует.")
                        else:
                            messages.append(f"{table.name}: колонка «{old}» отсутствует. Добавляется «{new}».")
                        continue
                    if new in db_cols:
                        raise ValueError(f"Таблица {table.name}: уже существуют оба столбца «{old}» и «{new}».")
                    conn.exec_driver_sql(
                        f"ALTER TABLE {quote(table.name)} RENAME COLUMN {quote(old)} TO {quote(new)}"
                    )
                    db_cols.remove(old)
                    db_cols.add(new)
                    messages.append(f"{table.name}: переименована колонка «{old}» → «{new}».")
                removed = db_cols - set(table.columns.keys())
                if removed:
                    raise ValueError(
                        f"Таблица {table.name}: требуется миграция для столбцов {', '.join(sorted(removed))}"
                    )
                for col in table.columns:
                    if col.name in db_cols:
                        continue
                    col_type = col.type.compile(dialect=dialect)
                    conn.exec_driver_sql(
                        f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(col.name)} {col_type}"
                    )
                    if col.unique:
                        uq_name = f"uq_{table.name}_{col.name}"
                        conn.exec_driver_sql(
                            f"CREATE UNIQUE INDEX IF NOT EXISTS {quote(uq_name)} "
                            f"ON {quote(table.name)} ({quote(col.name)})"
                        )
                existing_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
                for idx in table.indexes:
                    if idx.name and idx.name not in existing_indexes:
                        idx.create(conn)

        self._migration_messages = messages

    # ------------------------------------------------------------------
    # Model generation
    # ------------------------------------------------------------------

    def _prepare_models(self) -> None:
        entities = get_all_entities()
        for entity_class in entities:
            entity_class._meta.prepare_choice_filters()
        for entity_class in entities:
            owner = entity_class.__owner_field__
            fields = entity_class._meta.fields
            restrictions = (entity_class.__field_write_roles__ or {}).keys() | (entity_class.__field_read_roles__ or {}).keys()
            known_fields = set(fields) | {
                f"{name}.{column}" for name, part in entity_class._meta.table_parts.items()
                for column in part.fields
            }
            route = entity_class.__workflow__
            system = {"date", "number", "posted", owner}
            if route is not None:
                system.update((route.field, route.assignee_field))
            if system & (entity_class.__field_read_roles__ or {}).keys():
                raise ValueError(f"{entity_class.__name__}: системное поле нельзя скрыть по роли.")
            if restrictions - known_fields:
                raise ValueError(f"{entity_class.__name__}: неизвестный реквизит в правах на изменение.")
            if owner is not None and not isinstance(fields.get(owner), StringField):
                raise ValueError(f"{entity_class.__name__}: для владельца требуется строковое поле.")
            if owner is not None or entity_class.__permissions__ is not None:
                if any(isinstance(item, RefField) and item.on_delete == "CASCADE" for item in fields.values()):
                    raise ValueError(f"{entity_class.__name__}: защищённая запись требует удаления через API DECUI.")
            if entity_class not in self._entity_models:
                self._create_model(entity_class)
            for tp_name, tp in entity_class._meta.table_parts.items():
                if (entity_class, tp_name) not in self._tp_models:
                    self._create_tp_model(entity_class, tp_name, tp)
        create_register_models(self._engine, self._base, models=self._register_models)

    def _init_models(self) -> None:
        """Backward-compatible wrapper ensuring models and tables exist."""
        self._prepare_models()
        self._base.metadata.create_all(self._engine)

    def _create_model(self, entity_class: type) -> type:
        table_name = self._table_name(entity_class)
        attrs: dict[str, Any] = {
            "__tablename__": table_name,
            "__table_args__": {"extend_existing": True},
            "id": Column(Integer, primary_key=True, autoincrement=True),
        }
        for name, field in entity_class._meta.fields.items():
            attrs[name] = self._column_for_field(field)

        model = type(f"{entity_class.__name__}Model", (self._base,), attrs)
        self._entity_models[entity_class] = model
        self._table_names[entity_class] = table_name
        entity_class._meta.orm_model = model
        entity_class._meta.table_name = table_name
        return model

    def _create_tp_model(
        self,
        parent_class: type,
        tp_name: str,
        tp: Any,
    ) -> type:
        parent_table = self._table_name(parent_class)
        table_name = f"{parent_table}_{self._table_name_str(tp_name)}"
        attrs: dict[str, Any] = {
            "__tablename__": table_name,
            "__table_args__": {"extend_existing": True},
            "id": Column(Integer, primary_key=True, autoincrement=True),
            "parent_id": Column(
                Integer,
                ForeignKey(f"{parent_table}.id", ondelete="CASCADE"),
                nullable=False,
            ),
        }
        for name, field in tp.fields.items():
            attrs[name] = self._column_for_field(field)
        model = type(f"{parent_class.__name__}_{tp_name}Model", (self._base,), attrs)
        self._tp_models[(parent_class, tp_name)] = model
        self._tp_table_names[(parent_class, tp_name)] = table_name
        tp.orm_model = model
        tp.table_name = table_name
        return model

    # ------------------------------------------------------------------
    # Column mapping
    # ------------------------------------------------------------------

    def _column_for_field(self, field: Field) -> Column:
        nullable = not field.required
        col_kwargs: dict[str, Any] = {
            "nullable": nullable,
            "index": bool(field.indexed),
            "unique": bool(field.unique),
        }

        if isinstance(field, StringField):
            if getattr(field, "multiline", False):
                return Column(Text, **col_kwargs)
            return Column(String(field.max_length or 255), **col_kwargs)

        if isinstance(field, IntegerField):
            return Column(Integer, **col_kwargs)

        if isinstance(field, DecimalField):
            return Column(Numeric(field.precision, field.scale), **col_kwargs)

        if isinstance(field, BooleanField):
            return Column(SABoolean, default=field.default, **col_kwargs)

        if isinstance(field, DateField):
            return Column(SADate, **col_kwargs)

        if isinstance(field, DateTimeField):
            return Column(SADateTime, **col_kwargs)

        if isinstance(field, TimeField):
            return Column(String(5), **col_kwargs)  # HH:MM

        if isinstance(field, ChoiceField):
            return Column(String(255), **col_kwargs)  # VAL1|VAL2|...

        if isinstance(field, RefField):
            target = field.resolve_target()
            target_table = self._table_name(target)
            return Column(
                Integer,
                ForeignKey(f"{target_table}.id", ondelete=field.on_delete),
                **col_kwargs,
            )

        raise TypeError(f"Unsupported field type: {type(field).__name__}")

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[Session]:
        state = self._write_state.get()
        if state is not None:
            yield state[0]
            return
        sess = self._session_factory()
        try:
            yield sess
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
        finally:
            sess.close()

    def list_settings(self, entity: type) -> dict[str, Any] | None:
        self._check_ready()
        account = current_account()
        table = self._list_settings_table
        try:
            with self.session() as session:
                return session.scalar(select(table.c.settings).where(
                    table.c.owner == (account.login if account else ""),
                    table.c.entity == entity.__name__,
                ))
        except SQLAlchemyError as exc:
            raise _translate_sql_error("чтении настроек списка", exc) from exc

    def save_list_settings(self, entity: type, settings: dict[str, Any]) -> None:
        self._check_ready()
        account = current_account()
        table = self._list_settings_table
        statement = sqlite_insert(table).values(
            owner=account.login if account else "", entity=entity.__name__, settings=settings,
        )
        try:
            with self.session() as session:
                session.execute(statement.on_conflict_do_update(
                    index_elements=["owner", "entity"], set_={"settings": statement.excluded.settings},
                ))
        except SQLAlchemyError as exc:
            raise _translate_sql_error("сохранении настроек списка", exc) from exc

    def list_variants(self, entity: type) -> dict[str, dict[str, Any]]:
        self._check_ready()
        account = current_account()
        table = self._list_variants_table
        query = select(table.c.name, table.c.settings).where(
            table.c.owner == (account.login if account else ""),
            table.c.entity == entity.__name__,
        ).order_by(table.c.name)
        try:
            with self.session() as session:
                return {name: settings for name, settings in session.execute(query)}
        except SQLAlchemyError as exc:
            raise _translate_sql_error("чтении вариантов списка", exc) from exc

    def save_list_variant(self, entity: type, name: str, settings: dict[str, Any]) -> None:
        self._check_ready()
        account = current_account()
        table = self._list_variants_table
        statement = sqlite_insert(table).values(
            owner=account.login if account else "", entity=entity.__name__, name=name, settings=settings,
        )
        try:
            with self.session() as session:
                session.execute(statement.on_conflict_do_update(
                    index_elements=["owner", "entity", "name"], set_={"settings": statement.excluded.settings},
                ))
        except SQLAlchemyError as exc:
            raise _translate_sql_error("сохранении варианта списка", exc) from exc

    def delete_list_variant(self, entity: type, name: str) -> None:
        self._check_ready()
        account = current_account()
        table = self._list_variants_table
        try:
            with self.session() as session:
                session.execute(table.delete().where(
                    table.c.owner == (account.login if account else ""),
                    table.c.entity == entity.__name__, table.c.name == name,
                ))
        except SQLAlchemyError as exc:
            raise _translate_sql_error("удалении варианта списка", exc) from exc

    def report_variants(self, report: str) -> dict[str, dict[str, Any]]:
        """Read named report settings for the current application user."""
        self._check_ready()
        account = current_account()
        variants = self._report_variants_table
        query = select(variants.c.name, variants.c.params).where(
            variants.c.owner == (account.login if account else ""),
            variants.c.report == report,
        ).order_by(variants.c.name)
        try:
            with self.session() as session:
                return {name: params for name, params in session.execute(query)}
        except SQLAlchemyError as exc:
            raise _translate_sql_error("чтении вариантов отчёта", exc) from exc

    def save_report_variant(self, report: str, name: str, params: dict[str, Any]) -> None:
        self._check_ready()
        account = current_account()
        variants = self._report_variants_table
        statement = sqlite_insert(variants).values(
            owner=account.login if account else "", report=report, name=name, params=params,
        )
        try:
            with self.session() as session:
                session.execute(statement.on_conflict_do_update(
                    index_elements=["owner", "report", "name"], set_={"params": statement.excluded.params},
                ))
        except SQLAlchemyError as exc:
            raise _translate_sql_error("сохранении варианта отчёта", exc) from exc

    def delete_report_variant(self, report: str, name: str) -> None:
        self._check_ready()
        account = current_account()
        variants = self._report_variants_table
        try:
            with self.session() as session:
                session.execute(variants.delete().where(
                    variants.c.owner == (account.login if account else ""),
                    variants.c.report == report,
                    variants.c.name == name,
                ))
        except SQLAlchemyError as exc:
            raise _translate_sql_error("удалении варианта отчёта", exc) from exc

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group related writes into one SQLite transaction."""
        self._check_ready()
        with self._write_transaction():
            yield

    @contextmanager
    def _write_transaction(self) -> Iterator[Session]:
        state = self._write_state.get()
        if state is not None:
            sess, restore = state
            start = len(restore)
            try:
                with sess.begin_nested():
                    yield sess
            except BaseException:
                for undo in reversed(restore[start:]):
                    undo()
                del restore[start:]
                raise
            return

        restore: list[Callable[[], None]] = []
        try:
            with self.session() as sess:
                # Start the transaction before hook reads and SQLite savepoints.
                sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
                token = self._write_state.set((sess, restore))
                try:
                    yield sess
                finally:
                    self._write_state.reset(token)
        except BaseException:
            for undo in reversed(restore):
                undo()
            raise

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def allows(entity_class: type, action: str) -> bool:
        rules = entity_class.__permissions__
        if rules is None:
            return True
        account = current_account()
        if account is None:
            return False
        if account.role == "admin":
            return True
        return account.role in rules.get("read", ()) and account.role in rules.get(action, ())

    def _require_permission(self, entity_class: type, action: str) -> None:
        if not self.allows(entity_class, action):
            raise PermissionError("Операция недоступна для текущей роли.")

    @staticmethod
    def _owner_condition(entity_class: type, model: Any) -> Any:
        owner = entity_class.__owner_field__
        account = current_account()
        if owner is None or (account and account.role == "admin"):
            return None
        return getattr(model, owner) == account.login if account else false()

    def _read_condition(self, entity_class: type, model: Any) -> Any:
        if not self.allows(entity_class, "read"):
            return false()
        condition = self._owner_condition(entity_class, model)
        account = current_account()
        route = entity_class.__workflow__
        if condition is not None and account and route and route.uses_assignment:
            condition = condition | (getattr(model, route.assignee_field) == account.login)
        return condition

    def _visible_query(self, sess: Session, entity_class: type, model: Any, *, write: bool = False) -> Any:
        query = sess.query(model)
        condition = self._owner_condition(entity_class, model) if write else self._read_condition(entity_class, model)
        return query if condition is None else query.filter(condition)

    @staticmethod
    def can_edit(entity: Entity) -> bool:
        action = "create" if entity._id is None else "update"
        if not Database.allows(type(entity), action):
            return False
        return Database._can_write_record(entity)

    @staticmethod
    def can_read_field(entity_class: type, field: str) -> bool:
        roles = (entity_class.__field_read_roles__ or {}).get(field)
        if roles is None:
            return True
        account = current_account()
        return bool(account and (account.role == "admin" or account.role in roles))

    def masked_fields(self, entity_class: type) -> set[str]:
        return {field for field in (entity_class.__field_read_roles__ or {})
                if not self.can_read_field(entity_class, field)}

    @staticmethod
    def can_edit_field(entity_class: type, field: str) -> bool:
        if not Database.can_read_field(entity_class, field):
            return False
        roles = (entity_class.__field_write_roles__ or {}).get(field)
        if roles is None:
            return True
        account = current_account()
        return bool(account and (account.role == "admin" or account.role in roles))

    def _check_field_changes(self, entity: Entity, model: Any, sess: Session, *, creating: bool) -> None:
        masked = self.masked_fields(type(entity))
        for name in (entity.__field_write_roles__ or {}).keys() | masked:
            if "." in name or self.can_edit_field(type(entity), name):
                continue
            field = entity._meta.fields[name]
            expected = (
                _coerce_to_db(field, entity._protected_defaults.get(name))
                if creating else getattr(model, name)
            )
            actual = _coerce_to_db(field, entity._data.get(name))
            if not creating and name in masked and actual is None:
                continue
            if actual != expected:
                raise PermissionError(f"Текущая роль не может изменить реквизит «{field.display_name}».")

        for part_name, part in entity._meta.table_parts.items():
            protected = {
                name: field for name, field in part.fields.items()
                if not self.can_edit_field(type(entity), f"{part_name}.{name}")
            }
            if not protected:
                continue
            rows = entity._table_data.get(part_name, [])
            stored = []
            part_model = self._get_tp_model(type(entity), part_name)
            if not creating and part_model is not None:
                stored = sess.query(part_model).filter_by(parent_id=entity._id).order_by(part_model.id).all()
            if len(rows) < len(stored):
                raise PermissionError(f"Текущая роль не может удалить строки из «{part.verbose_name}».")
            for index, row in enumerate(rows):
                for name, field in protected.items():
                    if index < len(stored):
                        expected = getattr(stored[index], name)
                    else:
                        default = field.default() if callable(field.default) else field.default
                        expected = _coerce_to_db(field, field.to_python(default)) if default is not None else None
                    actual = _coerce_to_db(field, row.get(name))
                    if index < len(stored) and f"{part_name}.{name}" in masked and actual is None:
                        continue
                    if actual != expected:
                        raise PermissionError(
                            f"Текущая роль не может изменить «{field.display_name}» в строке {index + 1}."
                        )

    @staticmethod
    def _can_write_record(entity: Entity) -> bool:
        owner = entity.__owner_field__
        account = current_account()
        return owner is None or bool(account and (
            account.role == "admin" or entity._id is None or entity._data.get(owner) == account.login
        ))

    def can_delete(self, entity: Entity) -> bool:
        return self.allows(type(entity), "delete") and self._can_write_record(entity)

    @staticmethod
    def _require_full_entity(entity: Entity) -> None:
        if not entity._table_parts_loaded:
            raise ValueError(
                f"Перед изменением загрузите табличные части через db.get({type(entity).__name__}, {entity.id})."
            )

    def _remember_entity_for_rollback(self, entity: Entity) -> None:
        data = deepcopy(entity._data)
        tables = entity._table_data.copy()
        rows = {name: deepcopy(list(items)) for name, items in tables.items()}
        identifier = entity._id
        workflow_state = getattr(entity, "_workflow_state", None)

        def restore() -> None:
            entity._data.clear()
            entity._data.update(data)
            for name, items in tables.items():
                items[:] = rows[name]
            entity._table_data = tables
            object.__setattr__(entity, "_id", identifier)
            if entity.__workflow__ is not None:
                object.__setattr__(entity, "_workflow_state", workflow_state)

        state = self._write_state.get()
        assert state is not None
        state[1].append(restore)

    def _refresh_document_movements(self, document: Document) -> None:
        document.clear_movements(self)
        if document.is_posted:
            document.write_movements(self)

    @staticmethod
    def allows_command(entity_class: type, name: str) -> bool:
        if name not in entity_class._meta.commands:
            return False
        roles = entity_class.__command_roles__.get(name)
        account = current_account()
        return roles is None or bool(account and (account.role == "admin" or account.role in roles))

    def execute_command(self, entity: TEntity, name: str) -> TEntity:
        """Run a declared command and save its effects in one transaction."""
        from .entity import Document

        self._check_ready()
        if not self.can_edit(entity):
            raise PermissionError("Изменение записи недоступно.")
        command = entity._meta.commands.get(name)
        if command is None:
            raise ValueError(f"Команда «{name}» не найдена.")
        if not self.allows_command(type(entity), name):
            raise PermissionError("Команда недоступна для текущей роли.")
        self._require_full_entity(entity)
        with self._write_transaction():
            self._remember_entity_for_rollback(entity)
            command.execute(entity)
            self.save(entity, validate=True)
            if isinstance(entity, Document) and self.can_post(entity):
                self._refresh_document_movements(entity)
        return entity

    def post(self, document: TEntity) -> TEntity:
        """Validate and post a document, replacing its register movements atomically."""
        from .entity import Document

        self._check_ready()
        if not isinstance(document, Document):
            raise TypeError("Провести можно только документ.")
        if not self.can_post(document):
            raise PermissionError("Проведение документа недоступно.")
        self._require_full_entity(document)
        errors = document.validate()
        if errors:
            raise ValueError("\n".join(errors))

        with self._write_transaction():
            self._remember_entity_for_rollback(document)
            document.check_sequence(self)
            for name, command in document._meta.commands.items():
                if command.name in ("Провести", "Submit", "Post") or command.func.__name__ in ("post", "submit"):
                    if not self.allows_command(type(document), name):
                        raise PermissionError("Команда проведения недоступна для текущей роли.")
                    command.execute(document)
                    break
            else:
                document._data["posted"] = True
            self.save(document, validate=True)
            self._refresh_document_movements(document)
        return document

    def unpost(self, document: TEntity) -> TEntity:
        """Cancel posting on the persisted document and return the refreshed record."""
        from .entity import Document

        self._check_ready()
        if not isinstance(document, Document) or document.id is None:
            raise ValueError("Отменить проведение можно только у сохранённого документа.")
        with self._write_transaction():
            stored = self.get(type(document), document.id)
            if stored is None or not self.can_post(stored):
                raise PermissionError("Отмена проведения недоступна.")
            state = self._write_state.get()
            assert state is not None
            if stored.is_posted:
                state[1].append(lambda: stored._data.__setitem__("posted", True))
                stored.unpost()
                self.save(stored)
                if stored.is_posted:
                    raise ValueError("Документ не удалось вернуть в черновик.")
                stored.clear_movements(self)
            previous = document._data["posted"]
            state[1].append(lambda: document._data.__setitem__("posted", previous))
            document._data["posted"] = False
        return stored

    def document_movements(self, document: Entity) -> dict[type, list[dict[str, Any]]]:
        """Read register rows written by this document and visible to the current user."""
        from .movement import get_records

        self._check_ready()
        if not document._meta.is_document or document.id is None:
            return {}
        entity = type(document)
        if self.masked_fields(entity) or self.get(entity, document.id) is None:
            raise PermissionError("Движения документа недоступны.")
        result: dict[type, list[dict[str, Any]]] = {}
        for register in get_all_registers():
            rows = get_records(self, register, doc_type=entity.__name__, doc_id=document.id)
            if rows:
                result[register] = rows
        return result

    def can_post(self, entity: Entity) -> bool:
        return self.can_edit(entity) and self.allows(type(entity), "post") and not self.masked_fields(type(entity))

    def _register_query(self, sess: Session, model: Any, *, write: bool = False) -> Any:
        query = sess.query(model)
        for entity_class in get_all_entities():
            if not entity_class.__owner_field__ and entity_class.__permissions__ is None and not entity_class.__field_read_roles__:
                continue
            document_model = self._get_entity_model(entity_class)
            if document_model is None:
                continue
            action = "post" if write else "read"
            condition = (
                self._owner_condition(entity_class, document_model)
                if self.allows(entity_class, action) and not self.masked_fields(entity_class) else false()
            )
            if condition is not None:
                visible_ids = select(document_model.id).where(condition)
                # NULL document types belong to independent register records.
                query = query.filter(
                    model.doc_type.is_(None) | (model.doc_type != entity_class.__name__)
                    | model.doc_id.in_(visible_ids)
                )
        return query

    def _require_register_document(self, sess: Session, doc_type: str | None, doc_id: int | None) -> None:
        for entity_class in get_all_entities():
            if entity_class.__name__ != doc_type:
                continue
            self._require_permission(entity_class, "post")
            if self.masked_fields(entity_class):
                raise PermissionError("Закрытые реквизиты документа не позволяют записать движения.")
            if not entity_class.__owner_field__ and entity_class.__permissions__ is None:
                return
            model = self._get_entity_model(entity_class)
            if model is None or self._visible_query(sess, entity_class, model, write=True).filter(model.id == doc_id).first() is None:
                raise PermissionError("Документ регистра недоступен для текущего пользователя.")
            return

    def _entity_to_model(self, entity: Entity, model: Any) -> None:
        masked = self.masked_fields(type(entity)) if entity._id is not None else set()
        for name, field in entity._meta.fields.items():
            if name in masked:
                continue
            val = _coerce_to_db(field, entity._data.get(name))
            setattr(model, name, val)

    def _model_to_entity(
        self,
        model: Any,
        entity_class: type[TEntity],
    ) -> TEntity:
        data: dict[str, Any] = {}
        masked = self.masked_fields(entity_class)
        for name, field in entity_class._meta.fields.items():
            if name in masked:
                data[name] = None
                continue
            raw_val = getattr(model, name)
            data[name] = _coerce_from_db(field, raw_val)
        workflow = entity_class.__workflow__
        if workflow is not None:
            data[workflow.field] = workflow.state(data[workflow.field])
        instance = entity_class(**data)
        object.__setattr__(instance, "_masked_fields", masked)
        if workflow is not None:
            object.__setattr__(instance, "_workflow_state", data[workflow.field])
        object.__setattr__(instance, "_id", model.id)
        instance._table_parts_loaded = not entity_class._meta.table_parts
        return instance

    def _read_entities(
        self, sess: Session, query: Any, entity_class: type[TEntity], include_table_parts: bool,
    ) -> list[TEntity]:
        parts = entity_class._meta.table_parts if include_table_parts else {}
        if include_table_parts and (parts or entity_class.__owner_field__) and self._write_state.get() is None:
            # SQLite's default transaction mode does not start a transaction on SELECT.
            sess.connection().exec_driver_sql("BEGIN")
        entities = [self._model_to_entity(model, entity_class) for model in query.all()]
        if parts and entities:
            self._load_table_parts(sess, entity_class, entities)
        return entities

    def _load_table_parts(self, sess: Session, entity_class: type[TEntity], entities: list[TEntity]) -> None:
        from .entity import LineItemsCollection

        by_id = {entity.id: entity for entity in entities}
        ids = list(by_id)
        masked = self.masked_fields(entity_class)
        for name, part in entity_class._meta.table_parts.items():
            model = self._get_tp_model(entity_class, name)
            for entity in entities:
                entity._table_data[name] = LineItemsCollection(parent=entity, tp_meta=part)
            if model is None:
                continue
            # Keep IN lists within SQLite's bind-parameter limit.
            for batch in _chunked(ids, 500):
                rows = sess.query(model).filter(model.parent_id.in_(batch)).order_by(model.id)
                for row in rows:
                    by_id[row.parent_id]._table_data[name].append({
                        field_name: (
                            None if f"{name}.{field_name}" in masked
                            else _coerce_from_db(field, getattr(row, field_name))
                        )
                        for field_name, field in part.fields.items()
                    })
        for entity in entities:
            entity._table_parts_loaded = True

    def save(
        self, entity: TEntity, *, validate: bool = False, _workflow_action: str | None = None, _assigning_workflow: bool = False, _approval_only: bool = False,
    ) -> TEntity:
        """Persist an entity (insert if new, update if existing)."""
        self._check_ready()
        self._require_permission(type(entity), "read" if _approval_only else ("create" if entity._id is None else "update"))
        self._require_full_entity(entity)
        if validate:
            errs = entity.validate()
            if errs:
                raise ValueError("\n".join(errs))

        model_class = self._get_entity_model(type(entity))
        if model_class is None:
            self._prepare_models()
            self._base.metadata.create_all(self._engine)
            model_class = self._get_entity_model(type(entity))
        if model_class is None:
            raise RuntimeError(f"Модель для {type(entity).__name__} не создана")

        workflow = entity.__workflow__
        original_state = getattr(entity, "_workflow_state", None)

        try:
            with self._write_transaction() as sess:
                self._remember_entity_for_rollback(entity)
                creating = entity._id is None
                if creating:
                    model = model_class()
                else:
                    model = self._visible_query(
                        sess, type(entity), model_class, write=not _approval_only,
                    ).filter_by(id=entity._id).first()
                    if model is None:
                        if entity.__owner_field__:
                            raise PermissionError("Запись недоступна для текущего пользователя.")
                        raise ValueError(f"{type(entity).__name__} id={entity._id} not found")
                if _approval_only:
                    account = current_account()
                    if (
                        creating or workflow is None or not workflow.uses_assignment
                        or not account or getattr(model, workflow.assignee_field) != account.login
                        or not any(step.assigned_only and step.label == _workflow_action
                                   for step in workflow.actions(getattr(model, workflow.field)))
                    ):
                        raise PermissionError("В карточке доступен только назначенный переход.")
                owner = entity.__owner_field__
                if owner is not None:
                    account = current_account()
                    if account is None:
                        raise PermissionError("Войдите в приложение, чтобы сохранить запись.")
                    saved_owner = account.login if creating else getattr(model, owner)
                    entity._data[owner] = saved_owner
                if not _approval_only:
                    self._check_field_changes(entity, model, sess, creating=creating)
                entity.before_save(self)
                if owner is not None:
                    entity._data[owner] = saved_owner
                if entity._meta.is_document and not _approval_only:
                    was_posted = False if creating else bool(model.posted)
                    if bool(entity._data.get("posted")) != was_posted:
                        if self.masked_fields(type(entity)):
                            raise PermissionError("Закрытые реквизиты документа не позволяют провести его.")
                        self._require_permission(type(entity), "post")
                self._assign_number(entity, sess)
                workflow_event: tuple[str | None, str] | None = None
                event_action = _workflow_action
                role = current_role()
                account = current_account()
                login = account.login if account else None
                if workflow is not None:
                    persisted = workflow.initial if creating else workflow.state(getattr(model, workflow.field))
                    if original_state is not None and not creating and original_state != persisted:
                        raise ValueError("Статус записи изменился. Откройте запись заново.")
                    new_state = workflow.state(entity._data.get(workflow.field))
                    assignee = None
                    if workflow.uses_assignment:
                        assignee = None if creating else getattr(model, workflow.assignee_field)
                        desired = entity._data.get(workflow.assignee_field)
                        if desired != assignee and not (
                            _assigning_workflow and account is not None and account.role == "admin"
                            and new_state == persisted and not creating
                        ):
                            raise PermissionError("Назначение меняет администратор через действие согласования.")
                        if desired != assignee:
                            workflow_event = (persisted, persisted)
                            event_action = f"Назначен согласующий: {desired}"
                    workflow.validate_change(
                        persisted, new_state, creating=creating, role=role, action=_workflow_action,
                        login=login, assignee=assignee,
                    )
                    if creating or new_state != persisted:
                        workflow_event = (None if creating else persisted, new_state)
                    if workflow.post_states and entity._data.get("posted") and new_state not in workflow.post_states:
                        raise ValueError(f"Проведение недоступно в статусе «{new_state}».")
                    entity._data[workflow.field] = new_state
                    object.__setattr__(entity, "_workflow_state", new_state)
                if _approval_only and workflow is not None:
                    setattr(model, workflow.field, new_state)
                else:
                    self._entity_to_model(entity, model)
                if creating:
                    sess.add(model)
                    sess.flush()
                    object.__setattr__(entity, "_id", model.id)
                if not _approval_only:
                    self._save_table_parts(entity, sess, creating=creating)
                if workflow_event is not None:
                    previous, target = workflow_event
                    entity_table = self._table_names.get(type(entity), entity._meta.table_name)
                    sess.execute(self._workflow_events_table.insert().values(
                        entity=entity_table, entity_id=entity._id,
                        occurred_at=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
                        source=previous, target=target, action=event_action, role=role, login=login,
                    ))
                sess.flush()
                entity.after_save(self)
        except SQLAlchemyError as exc:
            raise _translate_sql_error("сохранении", exc) from exc
        masked = self.masked_fields(type(entity))
        if masked:
            object.__setattr__(entity, "_masked_fields", masked)
            for path in masked:
                part, separator, name = path.partition(".")
                if separator:
                    for row in entity._table_data.get(part, []):
                        row[name] = None
                else:
                    entity._data[part] = None
        return entity

    def transition(self, entity: TEntity, label: str) -> TEntity:
        """Apply a named workflow action to a saved record."""
        workflow = entity.__workflow__
        if workflow is None or entity._id is None:
            raise ValueError("Сначала сохраните запись с маршрутом согласования.")
        previous = entity._data.get(workflow.field)
        try:
            with self.transaction():
                current = self.get(type(entity), entity._id)
                if current is None or current._data.get(workflow.field) != workflow.state(previous):
                    raise ValueError("Статус записи изменился. Откройте запись заново.")
                step = next((item for item in workflow.actions(previous) if item.label == label), None)
                if step is None:
                    raise ValueError(f"Действие «{label}» недоступно для текущего статуса.")
                account = current_account()
                assignee = current._data.get(workflow.assignee_field) if workflow.uses_assignment else None
                if not step.allows(current_role(), account.login if account else None, assignee):
                    raise PermissionError("Действие недоступно текущему пользователю.")
                state = self._write_state.get()
                assert state is not None
                state[1].append(lambda: entity._data.__setitem__(workflow.field, previous))
                if not self.can_edit(current):
                    current._data[workflow.field] = step.target
                    return self.save(current, validate=True, _workflow_action=label, _approval_only=True)
                entity._data[workflow.field] = step.target
                return self.save(entity, validate=True, _workflow_action=label)
        except Exception:
            entity._data[workflow.field] = previous
            raise

    def assign_workflow(self, entity: TEntity, login: str) -> TEntity:
        workflow = entity.__workflow__
        account = current_account()
        if account is None or account.role != "admin":
            raise PermissionError("Назначать согласующего может администратор.")
        if workflow is None or not workflow.uses_assignment or entity._id is None:
            raise ValueError("Сначала сохраните запись с назначаемым согласованием.")
        login = login.strip().casefold()
        with self.transaction():
            if login not in {item.login for item in AccountStore(self).list_accounts()}:
                raise ValueError("Учётная запись согласующего не найдена.")
            field = workflow.assignee_field
            previous = entity._data.get(field)
            state = self._write_state.get()
            assert state is not None
            state[1].append(lambda: entity._data.__setitem__(field, previous))
            entity._data[field] = login
            return self.save(entity, validate=True, _assigning_workflow=True)

    def workflow_history(self, entity: Entity) -> list[WorkflowEvent]:
        """Return the current record's workflow history, latest event first."""
        self._check_ready()
        if entity._id is None or entity.__workflow__ is None:
            return []
        workflow_events = self._workflow_events_table
        events = workflow_events.c
        entity_table = self._table_names.get(type(entity), entity._meta.table_name)
        record = (events.entity == entity_table, events.entity_id == entity._id)
        start = (
            select(func.coalesce(func.max(events.id), 0))
            .where(*record, events.source.is_(None))
            .scalar_subquery()
        )
        # A creation event separates records if SQLite reuses a deleted row's id.
        query = select(workflow_events).where(*record, events.id >= start).order_by(events.id.desc())
        model = self._get_entity_model(type(entity))
        if model is not None:
            condition = self._read_condition(type(entity), model)
            if condition is not None:
                query = query.where(events.entity_id.in_(select(model.id).where(condition)))
        try:
            with self.session() as session:
                return [
                    WorkflowEvent(
                        event.occurred_at.replace(tzinfo=datetime.UTC), event.source, event.target, event.action, event.role, event.login,
                    )
                    for event in session.execute(query)
                ]
        except SQLAlchemyError as exc:
            raise _translate_sql_error("чтении истории согласования", exc) from exc

    def _save_table_parts(self, entity: Entity, sess: Session, *, creating: bool) -> None:
        masked = self.masked_fields(type(entity)) if not creating else set()
        for tp_name, tp in entity._meta.table_parts.items():
            model_class = self._get_tp_model(type(entity), tp_name)
            if model_class is None:
                continue
            parent_id = entity._id
            query = sess.query(model_class).filter_by(parent_id=parent_id)
            hidden_columns = [name for name in tp.fields if f"{tp_name}.{name}" in masked]
            preserved = []
            if hidden_columns:
                preserved = [{name: getattr(row, name) for name in hidden_columns}
                             for row in query.order_by(model_class.id).all()]
            query.delete()
            rows = entity._table_data.get(tp_name, [])
            for index, row_data in enumerate(rows):
                row = model_class(parent_id=parent_id)
                for name, fdef in tp.fields.items():
                    val = (preserved[index][name] if index < len(preserved) and name in hidden_columns
                           else _coerce_to_db(fdef, row_data.get(name)))
                    setattr(row, name, val)
                sess.add(row)

    def _assign_number(self, entity: Entity, session: Session) -> None:
        """Auto-assign a generated number if the entity has ``__numbering__``."""
        numbering_cfg = getattr(type(entity), "__numbering__", None)
        if not numbering_cfg or entity._id is not None:
            return
        prefix = numbering_cfg.get("prefix")
        if not prefix:
            return
        field_name = numbering_cfg.get(
            "field",
            "number" if entity._meta.is_document else "code",
        )
        pattern = numbering_cfg.get("pattern", "{seq:08d}")
        current = entity._data.get(field_name)
        if current:
            return
        period = None
        if entity._meta.is_document:
            doc_date = entity._data.get("date")
            if doc_date:
                if hasattr(doc_date, "date"):
                    doc_date = doc_date.date()
                period = doc_date
        entity._data[field_name] = self.numbering.next_number(
            prefix, pattern, period, session=session
        )

    def backup(self, destination: str | Path) -> Path:
        """Save a consistent SQLite snapshot to a new file."""
        self._check_ready()
        if self._write_state.get() is not None:
            raise RuntimeError("Завершите изменение данных перед резервным копированием.")
        path = Path(destination)
        with closing(self._engine.raw_connection()) as connection:
            source = connection.driver_connection
            protected = any(
                item.__owner_field__ or item.__permissions__ is not None or item.__field_read_roles__
                for item in get_all_entities()
            )
            with closing(source.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_erp_accounts'"
            )) as cursor:
                protected = protected or cursor.fetchone() is not None
            account = current_account()
            if protected and (account is None or account.role != "admin"):
                raise PermissionError("Резервное копирование доступно администратору.")
            if os.path.lexists(path):
                raise FileExistsError(f"Файл уже существует: {path}")
            with TemporaryDirectory(prefix=".decui-backup-", dir=path.parent) as directory:
                temporary = Path(directory) / "snapshot.sqlite3"
                with closing(sqlite3.connect(temporary)) as target:
                    copy_sqlite(source, target)
                temporary.chmod(0o600)
                publish_new_file(temporary, path)
        return path

    def _check_ready(self) -> None:
        if not self._ready:
            raise RuntimeError(
                f"База данных не инициализирована: {self._last_error or 'неизвестная ошибка'}"
            )

    def get(self, entity_class: type[TEntity], id: int) -> TEntity | None:
        """Load a single entity by primary key (includes table part rows)."""
        self._check_ready()
        model_class = self._get_entity_model(entity_class)
        if model_class is None:
            raise RuntimeError(f"Модель для {entity_class.__name__} не создана")
        try:
            with self.session() as sess:
                query = self._visible_query(sess, entity_class, model_class).filter_by(id=id).limit(1)
                entities = self._read_entities(sess, query, entity_class, include_table_parts=True)
                return entities[0] if entities else None
        except SQLAlchemyError as exc:
            raise _translate_sql_error("загрузке", exc) from exc

    def list(self, entity_class: type[TEntity], *, include_table_parts: bool = False) -> list[TEntity]:
        """Return all visible entities, optionally including table part rows."""
        self._check_ready()
        model_class = self._get_entity_model(entity_class)
        if model_class is None:
            raise RuntimeError(f"Модель для {entity_class.__name__} не создана")
        try:
            with self.session() as sess:
                query = self._visible_query(sess, entity_class, model_class)
                return self._read_entities(sess, query, entity_class, include_table_parts)
        except SQLAlchemyError as exc:
            raise _translate_sql_error("чтении списка", exc) from exc

    def filter(
        self,
        entity_class: type[TEntity],
        *,
        order_by: str | None = None,
        limit: int | None = None,
        include_table_parts: bool = False,
        **kwargs: Any,
    ) -> list[TEntity]:
        """Return entities matching criteria.

        Supports Django-style lookup suffixes:
          ``__gt``, ``__gte``, ``__lt``, ``__lte``, ``__ne``,
          ``__in``, ``__contains``, ``__icontains``, ``__isnull``, ``__between=(lower, upper)``.
        Use ``__date_range=(start, end)`` for inclusive calendar dates on Date or DateTime fields.
        Either bound may be None. Set ``include_table_parts=True`` to load table part rows.
        """
        self._check_ready()
        model_class = self._get_entity_model(entity_class)
        if model_class is None:
            raise RuntimeError(f"Модель для {entity_class.__name__} не создана")
        try:
            with self.session() as sess:
                query = self._visible_query(sess, entity_class, model_class)
                for key, raw_val in kwargs.items():
                    if "__" in key:
                        field_name, op = key.rsplit("__", 1)
                    else:
                        field_name, op = key, "eq"

                    if not self.can_read_field(entity_class, field_name):
                        raise PermissionError("Поле отбора недоступно для текущей роли.")
                    if not hasattr(model_class, field_name):
                        raise ValueError(
                            f"Поле '{field_name}' не существует в {entity_class.__name__}"
                        )
                    col = getattr(model_class, field_name)
                    fdef = entity_class._meta.fields.get(field_name)

                    if op == "date_range":
                        query = query.filter(*_date_range_conditions(col, fdef, raw_val))
                        continue
                    if op == "isnull":
                        query = query.filter(col.is_(None) if raw_val else col.is_not(None))
                        continue
                    if op == "in":
                        vals = [
                            _coerce_to_db(fdef, x) if fdef else x
                            for x in raw_val
                        ]
                        query = query.filter(col.in_(vals))
                        continue

                    if op == "between":
                        lower, upper = raw_val
                        if fdef is not None:
                            lower, upper = _coerce_to_db(fdef, lower), _coerce_to_db(fdef, upper)
                        query = query.filter(col.between(lower, upper))
                        continue

                    val = _coerce_to_db(fdef, raw_val) if fdef else raw_val
                    if op == "eq":
                        query = query.filter(col == val)
                    elif op == "ne":
                        query = query.filter(col != val)
                    elif op == "gt":
                        query = query.filter(col > val)
                    elif op == "gte":
                        query = query.filter(col >= val)
                    elif op == "lt":
                        query = query.filter(col < val)
                    elif op == "lte":
                        query = query.filter(col <= val)
                    elif op == "literal_contains":
                        query = query.filter(col.contains(val, autoescape=True))
                    elif op == "contains":
                        query = query.filter(col.contains(val))
                    elif op == "icontains":
                        query = query.filter(col.ilike(f"%{val}%"))
                    else:
                        raise ValueError(f"Неподдерживаемый оператор фильтрации: __{op}")

                if order_by:
                    desc = order_by.startswith("-")
                    col_name = order_by[1:] if desc else order_by
                    if not self.can_read_field(entity_class, col_name):
                        raise PermissionError("Поле сортировки недоступно для текущей роли.")
                    if not hasattr(model_class, col_name):
                        raise ValueError(
                            f"Поле сортировки '{col_name}' не найдено в {entity_class.__name__}"
                        )
                    ocol = getattr(model_class, col_name)
                    query = query.order_by(ocol.desc() if desc else ocol.asc())

                if limit is not None:
                    query = query.limit(limit)

                return self._read_entities(sess, query, entity_class, include_table_parts)
        except SQLAlchemyError as exc:
            raise _translate_sql_error("фильтрации", exc) from exc

    def initialize(self, setup: Callable[[Database], None]) -> bool:
        """Run a first-launch callback once per database, in one transaction."""
        with self.transaction(), self.session() as session:
            conn = session.connection()
            if conn.exec_driver_sql("PRAGMA user_version").scalar():
                return False
            setup(self)
            conn.exec_driver_sql("PRAGMA user_version = 1")
        return True

    def seed(self, entities: list[TEntity]) -> list[TEntity]:
        """Save a batch list of entities atomically in one transaction."""
        self._check_ready()
        if not entities:
            return []
        with self._write_transaction():
            return [self.save(e) for e in entities]

    def delete(self, entity: Entity) -> None:
        """Delete an entity and run its hooks in the same transaction."""
        self._check_ready()
        if entity._id is None:
            return
        self._require_permission(type(entity), "delete")
        model_class = self._get_entity_model(type(entity))
        if model_class is None:
            raise RuntimeError(f"Модель для {type(entity).__name__} не создана")
        try:
            with self._write_transaction() as sess:
                model = self._visible_query(sess, type(entity), model_class, write=True).filter_by(id=entity._id).first()
                if model is None and entity.__owner_field__:
                    raise PermissionError("Запись недоступна для текущего пользователя.")
                if model is not None:
                    entity.before_delete(self)
                    if entity._meta.is_document:
                        for register in get_all_registers():
                            movement = self._get_register_model(register)
                            if movement is not None:
                                sess.query(movement).filter_by(doc_id=entity._id, doc_type=type(entity).__name__).delete()
                    sess.delete(model)
                    sess.flush()
                    entity.after_delete(self)
        except SQLAlchemyError as exc:
            raise _translate_sql_error("удалении", exc) from exc

    def delete_by_id(self, entity_class: type[TEntity], id: int) -> None:
        """Delete an entity by primary key, including its business hooks."""
        entity = self.get(entity_class, id)
        if entity is not None:
            self.delete(entity)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _table_name(entity_class: type) -> str:
        return Database._table_name_str(entity_class.__name__)

    @staticmethod
    def _table_name_str(name: str) -> str:
        parts: list[str] = []
        for i, ch in enumerate(name):
            if ch.isupper() and i > 0:
                parts.append("_")
            parts.append(ch.lower())
        return "".join(parts)
