from __future__ import annotations

import datetime
import traceback
from contextlib import contextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterator, TypeVar

from sqlalchemy import (
    Boolean as SABoolean,
    Column,
    Date as SADate,
    DateTime as SADateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    inspect as sa_inspect,
    text as sa_text,
)
from sqlalchemy import event as sa_event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

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
from .movement import create_register_models, get_all_registers
from .numbering import NumberingService
from .registry import get_all_entities

if TYPE_CHECKING:
    from .entity import Entity

TEntity = TypeVar("TEntity", bound="Entity")

Base = declarative_base()


def _enable_fk(dbapi_con: Any, _con_record: Any) -> None:
    try:
        cursor = dbapi_con.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()
    except Exception:
        pass  # non-SQLite engines may not support this


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


class Database:
    """Persistence layer — creates SQLAlchemy models, manages sessions, CRUD."""

    def __init__(self, path: str = ":memory:", echo: bool = False) -> None:
        self._last_error: str | None = None
        self._path = path
        self._ready = False
        try:
            self._engine = create_engine(
                f"sqlite:///{path}",
                echo=echo,
                connect_args={"check_same_thread": False},
            )
            sa_event.listen(self._engine, "connect", _enable_fk)
            self._session_factory = sessionmaker(bind=self._engine)
            self.numbering = NumberingService(self._engine)
            self._prepare_models()
            self._auto_upgrade()
            Base.metadata.create_all(self._engine)
            self._save_schema_hash()
            self._ready = True
        except SQLAlchemyError as exc:
            self._last_error = f"Ошибка SQL: {exc}\n{traceback.format_exc()}"
        except Exception as exc:
            self._last_error = f"Ошибка: {exc}\n{traceback.format_exc()}"

    @classmethod
    def memory(cls, echo: bool = False) -> Database:
        """Create an in-memory database."""
        return cls(":memory:", echo=echo)

    # ------------------------------------------------------------------
    # Auto schema upgrade
    # ------------------------------------------------------------------

    def _schema_hash(self) -> str:
        """Compute a deterministic hash of registered entities and registers."""
        import hashlib

        h = hashlib.md5()
        for cls in sorted(get_all_entities(), key=lambda c: c.__name__):
            h.update(cls.__name__.encode())
            for name, field in cls._meta.fields.items():
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
                h.update(info.encode())
            for tp_name, tp in cls._meta.table_parts.items():
                h.update(f"tp:{tp_name}".encode())
                for fname, ffield in tp.fields.items():
                    h.update(
                        f"{fname}:{type(ffield).__name__}:{ffield.field_type}:{ffield.required}".encode()
                    )
        for reg in sorted(get_all_registers(), key=lambda r: r.__name__):
            rmeta = reg._register_meta
            h.update(f"reg:{rmeta.name}:{rmeta.register_type}".encode())
            for dn, dv in rmeta.dimensions.items():
                h.update(f"dim:{dn}:{dv.field_type}".encode())
            for rn, rv in rmeta.resources.items():
                h.update(f"res:{rn}:{rv.field_type}".encode())
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
            pass

    def _auto_upgrade(self) -> None:
        """Non-destructive schema upgrade via ALTER TABLE ADD COLUMN when additive, else reset."""
        try:
            inspector = sa_inspect(self._engine)
            existing_tables = set(inspector.get_table_names())
            user_tables = [t for t in existing_tables if not t.startswith("_erp_")]
            if not user_tables:
                return  # fresh DB

            # Check recorded schema hash
            try:
                with self._engine.connect() as conn:
                    row = conn.execute(
                        sa_text("SELECT value FROM _erp_config WHERE key = 'schema_hash'")
                    ).fetchone()
                if row and row[0] == self._schema_hash():
                    return
            except Exception:
                pass

            # Inspect each registered table; apply additive ALTER TABLE ADD COLUMN where possible
            dialect = self._engine.dialect
            incompatible = False
            with self._engine.begin() as conn:
                for table in Base.metadata.sorted_tables:
                    if table.name not in existing_tables:
                        continue  # create_all will create it safely
                    db_cols = {c["name"]: c for c in inspector.get_columns(table.name)}
                    expected_names = {c.name for c in table.columns}
                    # If columns were removed from model -> incompatible in SQLite without rebuild
                    removed = set(db_cols.keys()) - expected_names
                    if removed:
                        incompatible = True
                        break
                    # Add missing new columns safely without losing user rows
                    for col in table.columns:
                        if col.name not in db_cols:
                            col_type_sql = col.type.compile(dialect=dialect)
                            # In SQLite ALTER TABLE ADD COLUMN, columns must be nullable or have DEFAULT
                            conn.execute(
                                sa_text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type_sql}')
                            )

            if incompatible:
                Base.metadata.drop_all(self._engine)
        except Exception:
            # Fallback clean reset only if inspection fails catastrophically
            try:
                Base.metadata.drop_all(self._engine)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Model generation
    # ------------------------------------------------------------------

    def _prepare_models(self) -> None:
        for entity_class in get_all_entities():
            if entity_class._meta.orm_model is None:
                self._create_model(entity_class)
            for tp_name, tp in entity_class._meta.table_parts.items():
                if tp.orm_model is None:
                    self._create_tp_model(entity_class, tp_name, tp)
        create_register_models(self._engine, Base)

    def _init_models(self) -> None:
        """Backward-compatible wrapper ensuring models and tables exist."""
        self._prepare_models()
        Base.metadata.create_all(self._engine)

    def _create_model(self, entity_class: type) -> type:
        table_name = self._table_name(entity_class)
        attrs: dict[str, Any] = {
            "__tablename__": table_name,
            "__table_args__": {"extend_existing": True},
            "id": Column(Integer, primary_key=True, autoincrement=True),
        }
        for name, field in entity_class._meta.fields.items():
            attrs[name] = self._column_for_field(field)

        model = type(f"{entity_class.__name__}Model", (Base,), attrs)
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
        model = type(f"{parent_class.__name__}_{tp_name}Model", (Base,), attrs)
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
                ForeignKey(f"{target_table}.id", ondelete="RESTRICT"),
                **col_kwargs,
            )

        raise TypeError(f"Unsupported field type: {type(field).__name__}")

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[Session]:
        sess = self._session_factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def _entity_to_model(self, entity: Entity, model: Any) -> None:
        for name, field in entity._meta.fields.items():
            val = _coerce_to_db(field, entity._data.get(name))
            setattr(model, name, val)

    def _model_to_entity(
        self,
        model: Any,
        entity_class: type[TEntity],
        sess: Session | None = None,
    ) -> TEntity:
        data: dict[str, Any] = {}
        for name, field in entity_class._meta.fields.items():
            raw_val = getattr(model, name)
            data[name] = _coerce_from_db(field, raw_val)
        instance = entity_class(**data)
        object.__setattr__(instance, "_id", model.id)
        if sess is not None:
            from .entity import LineItemsCollection
            for tp_name, tp in entity_class._meta.table_parts.items():
                model_class = tp.orm_model
                rows = (
                    sess.query(model_class)
                    .filter_by(parent_id=model.id)
                    .all()
                )
                table_data: list[dict[str, Any]] = []
                for row in rows:
                    row_dict: dict[str, Any] = {}
                    for fname, fdef in tp.fields.items():
                        row_dict[fname] = _coerce_from_db(fdef, getattr(row, fname))
                    table_data.append(row_dict)
                instance._table_data[tp_name] = LineItemsCollection(
                    table_data, parent=instance, tp_meta=tp
                )
        return instance

    def save(self, entity: TEntity, *, validate: bool = False) -> TEntity:
        """Persist an entity (insert if new, update if existing)."""
        self._check_ready()
        if validate:
            errs = entity.validate()
            if errs:
                raise ValueError("\n".join(errs))

        entity.before_save(self)

        model_class = entity._meta.orm_model
        if model_class is None:
            self._prepare_models()
            Base.metadata.create_all(self._engine)
            model_class = entity._meta.orm_model
        if model_class is None:
            raise RuntimeError(f"Модель для {type(entity).__name__} не создана")

        self._assign_number(entity)
        try:
            with self.session() as sess:
                if entity._id is None:
                    model = model_class()
                    self._entity_to_model(entity, model)
                    sess.add(model)
                    sess.flush()
                    object.__setattr__(entity, "_id", model.id)
                else:
                    model = sess.get(model_class, entity._id)
                    if model is None:
                        raise ValueError(
                            f"{type(entity).__name__} id={entity._id} not found"
                        )
                    self._entity_to_model(entity, model)
                self._save_table_parts(entity, sess)
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Ошибка SQL при сохранении: {exc}") from exc

        entity.after_save(self)
        return entity

    def _save_table_parts(self, entity: Entity, sess: Session) -> None:
        for tp_name, tp in entity._meta.table_parts.items():
            model_class = tp.orm_model
            parent_id = entity._id
            sess.query(model_class).filter_by(parent_id=parent_id).delete()
            rows = entity._table_data.get(tp_name, [])
            for row_data in rows:
                row = model_class(parent_id=parent_id)
                for name, fdef in tp.fields.items():
                    val = _coerce_to_db(fdef, row_data.get(name))
                    setattr(row, name, val)
                sess.add(row)

    def _assign_number(self, entity: Entity) -> None:
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
        entity._data[field_name] = self.numbering.next_number(prefix, pattern, period)

    def _check_ready(self) -> None:
        if not self._ready:
            raise RuntimeError(
                f"База данных не инициализирована: {self._last_error or 'неизвестная ошибка'}"
            )

    def get(self, entity_class: type[TEntity], id: int) -> TEntity | None:
        """Load a single entity by primary key (includes table part rows)."""
        self._check_ready()
        model_class = entity_class._meta.orm_model
        if model_class is None:
            raise RuntimeError(f"Модель для {entity_class.__name__} не создана")
        try:
            with self.session() as sess:
                model = sess.get(model_class, id)
                if model is None:
                    return None
                return self._model_to_entity(model, entity_class, sess)
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Ошибка SQL при загрузке: {exc}") from exc

    def list(self, entity_class: type[TEntity]) -> list[TEntity]:
        """Return all entities (without table part rows — use ``get`` for those)."""
        self._check_ready()
        model_class = entity_class._meta.orm_model
        if model_class is None:
            raise RuntimeError(f"Модель для {entity_class.__name__} не создана")
        try:
            with self.session() as sess:
                rows = sess.query(model_class).all()
                return [self._model_to_entity(r, entity_class) for r in rows]
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Ошибка SQL при чтении списка: {exc}") from exc

    def filter(
        self,
        entity_class: type[TEntity],
        *,
        order_by: str | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> list[TEntity]:
        """Return entities matching criteria.

        Supports Django-style lookup suffixes:
          ``__gt``, ``__gte``, ``__lt``, ``__lte``, ``__ne``,
          ``__in``, ``__contains``, ``__icontains``, ``__isnull``.
        """
        self._check_ready()
        model_class = entity_class._meta.orm_model
        if model_class is None:
            raise RuntimeError(f"Модель для {entity_class.__name__} не создана")
        try:
            with self.session() as sess:
                query = sess.query(model_class)
                for key, raw_val in kwargs.items():
                    if "__" in key:
                        field_name, op = key.rsplit("__", 1)
                    else:
                        field_name, op = key, "eq"

                    if not hasattr(model_class, field_name):
                        raise ValueError(
                            f"Поле '{field_name}' не существует в {entity_class.__name__}"
                        )
                    col = getattr(model_class, field_name)
                    fdef = entity_class._meta.fields.get(field_name)

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
                    elif op == "contains":
                        query = query.filter(col.contains(val))
                    elif op == "icontains":
                        query = query.filter(col.ilike(f"%{val}%"))
                    else:
                        raise ValueError(f"Неподдерживаемый оператор фильтрации: __{op}")

                if order_by:
                    desc = order_by.startswith("-")
                    col_name = order_by[1:] if desc else order_by
                    if not hasattr(model_class, col_name):
                        raise ValueError(
                            f"Поле сортировки '{col_name}' не найдено в {entity_class.__name__}"
                        )
                    ocol = getattr(model_class, col_name)
                    query = query.order_by(ocol.desc() if desc else ocol.asc())

                if limit is not None:
                    query = query.limit(limit)

                rows = query.all()
                return [self._model_to_entity(r, entity_class) for r in rows]
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Ошибка SQL при фильтрации: {exc}") from exc

    def seed(self, entities: list[TEntity]) -> list[TEntity]:
        """Save a batch list of entities."""
        return [self.save(e) for e in entities]

    def delete(self, entity: Entity) -> None:
        """Delete an entity from the database."""
        self._check_ready()
        if entity._id is None:
            return
        model_class = entity._meta.orm_model
        if model_class is None:
            raise RuntimeError(f"Модель для {type(entity).__name__} не создана")
        entity.before_delete(self)
        try:
            with self.session() as sess:
                model = sess.get(model_class, entity._id)
                if model is not None:
                    sess.delete(model)
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Ошибка SQL при удалении: {exc}") from exc

    def delete_by_id(self, entity_class: type[TEntity], id: int) -> None:
        """Delete an entity by its primary key."""
        self._check_ready()
        model_class = entity_class._meta.orm_model
        if model_class is None:
            raise RuntimeError(f"Модель для {entity_class.__name__} не создана")
        try:
            with self.session() as sess:
                model = sess.get(model_class, id)
                if model is not None:
                    sess.delete(model)
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Ошибка SQL при удалении: {exc}") from exc

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
