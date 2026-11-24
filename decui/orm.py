from __future__ import annotations

import datetime
import traceback
from contextlib import contextmanager
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Iterator

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

Base = declarative_base()


def _enable_fk(dbapi_con: Any, _con_record: Any) -> None:
    try:
        cursor = dbapi_con.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()
    except Exception:
        pass  # non-SQLite engines may not support this


class Database:
    """Persistence layer — creates SQLAlchemy models, manages sessions, CRUD.

    Architecture:
      * Owns engine + session factory
      * Dynamically generates SQLAlchemy models from entity metadata
      * Provides save / get / list / delete for Entity instances
      * Entity instances and ORM model instances are kept separate;
        Database translates between them.
    """

    def __init__(self, path: str = ":memory:", echo: bool = False) -> None:
        self._last_error: str | None = None
        self._path = path
        self._ready = False
        try:
            self._engine = create_engine(
                f"sqlite:///{path}", echo=echo,
                connect_args={"check_same_thread": False},
            )
            sa_event.listen(self._engine, "connect", _enable_fk)
            self._session_factory = sessionmaker(bind=self._engine)
            self.numbering = NumberingService(self._engine)
            self._auto_upgrade()
            self._init_models()
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
        """Compute a hash of current entity schemas for change detection."""
        import hashlib
        h = hashlib.md5()
        for cls in get_all_entities():
            h.update(cls.__name__.encode())
            for name, field in cls._meta.fields.items():
                info = f"{name}:{type(field).__name__}:{field.field_type}:{field.required}"
                if hasattr(field, 'precision'):
                    info += f":{field.precision}:{field.scale}"
                if hasattr(field, 'max_length'):
                    info += f":{field.max_length}"
                if hasattr(field, 'min_value'):
                    info += f":{field.min_value}"
                if hasattr(field, 'max_value'):
                    info += f":{field.max_value}"
                h.update(info.encode())
            for tp_name, tp in cls._meta.table_parts.items():
                h.update(f"tp:{tp_name}".encode())
                for fname, ffield in tp.fields.items():
                    h.update(f"{fname}:{type(ffield).__name__}:{ffield.field_type}".encode())
        return h.hexdigest()[:12]

    def _save_schema_hash(self) -> None:
        try:
            from sqlalchemy import text as sa_text
            with self._engine.connect() as conn:
                conn.execute(sa_text("CREATE TABLE IF NOT EXISTS _erp_config (key TEXT PRIMARY KEY, value TEXT)"))
                conn.execute(
                    sa_text("INSERT OR REPLACE INTO _erp_config (key, value) VALUES ('schema_hash', :h)"),
                    {"h": self._schema_hash()},
                )
                conn.commit()
        except Exception:
            pass

    def _auto_upgrade(self) -> None:
        """Drop and recreate tables if entity schema changed."""
        try:
            from sqlalchemy import inspect as sa_inspect
            inspector = sa_inspect(self._engine)
            existing = inspector.get_table_names()
            user_tables = [t for t in existing if not t.startswith("_erp_")]
            if not user_tables:
                self._save_schema_hash()
                return  # fresh DB — no upgrade needed
            # Check schema version
            from sqlalchemy import text as sa_text
            try:
                with self._engine.connect() as conn:
                    row = conn.execute(
                        sa_text("SELECT value FROM _erp_config WHERE key = 'schema_hash'")
                    ).fetchone()
                if row and row[0] == self._schema_hash():
                    return  # schema unchanged
            except Exception:
                pass  # no config table yet
            # Schema changed — reset
            self._reset_tables()
        except Exception:
            pass  # best effort

    def _reset_tables(self) -> None:
        """Drop all user tables and recreate."""
        Base.metadata.drop_all(self._engine)
        self._save_schema_hash()


    # ------------------------------------------------------------------
    # Model generation
    # ------------------------------------------------------------------

    def _init_models(self) -> None:
        for entity_class in get_all_entities():
            if entity_class._meta.orm_model is None:
                self._create_model(entity_class)
            for tp_name, tp in entity_class._meta.table_parts.items():
                if tp.orm_model is None:
                    self._create_tp_model(entity_class, tp_name, tp)
        create_register_models(self._engine, Base)
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

        model = type(
            f"{entity_class.__name__}Model", (Base,), attrs
        )
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

    _SA_TYPE_MAP: dict[type, tuple[Any, dict[str, Any]]] = {}

    def _column_for_field(self, field: Field) -> Column:
        nullable = not field.required

        if isinstance(field, StringField):
            if getattr(field, "multiline", False):
                return Column(Text, nullable=nullable)
            return Column(String(field.max_length or 255), nullable=nullable)

        if isinstance(field, IntegerField):
            return Column(Integer, nullable=nullable)

        if isinstance(field, DecimalField):
            return Column(Numeric(field.precision, field.scale), nullable=nullable)

        if isinstance(field, BooleanField):
            return Column(SABoolean, nullable=nullable, default=field.default)

        if isinstance(field, DateField):
            return Column(SADate, nullable=nullable)

        if isinstance(field, DateTimeField):
            return Column(SADateTime, nullable=nullable)

        if isinstance(field, TimeField):
            return Column(String(5), nullable=nullable)  # HH:MM

        if isinstance(field, ChoiceField):
            return Column(String(255), nullable=nullable)  # VAL1|VAL2|...

        if isinstance(field, RefField):
            target = field.resolve_target()
            target_table = self._table_name(target)
            return Column(
                Integer,
                ForeignKey(f"{target_table}.id"),
                nullable=nullable,
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
        for name in entity._meta.fields:
            field = entity._meta.fields[name]
            val = entity._data.get(name)
            if isinstance(field, TimeField) and isinstance(val, datetime.time):
                val = f"{val.hour:02d}:{val.minute:02d}"
            setattr(model, name, val)

    def _model_to_entity(
        self,
        model: Any,
        entity_class: type,
        sess: Session | None = None,
    ) -> Entity:
        data: dict[str, Any] = {}
        for name in entity_class._meta.fields:
            field = entity_class._meta.fields[name]
            val = getattr(model, name)
            if isinstance(field, TimeField) and isinstance(val, str) and ":" in val:
                parts = val.split(":")
                val = datetime.time(int(parts[0]), int(parts[1]))
            data[name] = val
        instance = entity_class(**data)
        object.__setattr__(instance, "_id", model.id)
        # Load table parts if session is provided
        if sess is not None:
            for tp_name, tp in entity_class._meta.table_parts.items():
                model_class = tp.orm_model
                rows = (
                    sess.query(model_class)
                    .filter_by(parent_id=model.id)
                    .all()
                )
                table_data: list[dict] = []
                for row in rows:
                    row_dict: dict = {}
                    for name in tp.fields:
                        row_dict[name] = getattr(row, name)
                    table_data.append(row_dict)
                from .entity import LineItemsCollection
                instance._table_data[tp_name] = LineItemsCollection(
                    table_data, parent=instance, tp_meta=tp
                )
        return instance

    def save(self, entity: Entity) -> Entity:
        """Persist an entity (insert if new, update if existing)."""
        if not self._ready:
            raise RuntimeError(f"База данных не инициализирована: {self._last_error}")
        self._assign_number(entity)
        model_class = entity._meta.orm_model
        if model_class is None:
            raise RuntimeError(f"Модель для {type(entity).__name__} не создана")
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
        return entity

    def _save_table_parts(self, entity: Entity, sess: Session) -> None:
        for tp_name, tp in entity._meta.table_parts.items():
            model_class = tp.orm_model
            parent_id = entity._id
            sess.query(model_class).filter_by(parent_id=parent_id).delete()
            rows = entity._table_data.get(tp_name, [])
            for row_data in rows:
                row = model_class(parent_id=parent_id)
                for name in tp.fields:
                    setattr(row, name, row_data.get(name))
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
        # Определяем период для {year}/{month} из поля date документа
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

    def get(self, entity_class: type, id: int) -> Entity | None:
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

    def list(self, entity_class: type) -> list[Entity]:
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

    def filter(self, entity_class: type, **kwargs: Any) -> list[Entity]:
        """Return entities matching field filter criteria."""
        self._check_ready()
        model_class = entity_class._meta.orm_model
        if model_class is None:
            raise RuntimeError(f"Модель для {entity_class.__name__} не создана")
        try:
            with self.session() as sess:
                query = sess.query(model_class)
                for k, v in kwargs.items():
                    if hasattr(model_class, k):
                        query = query.filter(getattr(model_class, k) == v)
                rows = query.all()
                return [self._model_to_entity(r, entity_class) for r in rows]
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Ошибка SQL при фильтрации: {exc}") from exc

    def seed(self, entities: list[Entity]) -> list[Entity]:
        """Save a batch list of entities."""
        return [self.save(e) for e in entities]

    def delete(self, entity: Entity) -> None:
        """Delete an entity from the database."""
        self._check_ready()
        model_class = entity._meta.orm_model
        if model_class is None:
            raise RuntimeError(f"Модель для {type(entity).__name__} не создана")
        try:
            with self.session() as sess:
                model = sess.get(model_class, entity._id)
                if model is not None:
                    sess.delete(model)
        except SQLAlchemyError as exc:
            raise RuntimeError(f"Ошибка SQL при удалении: {exc}") from exc

    def delete_by_id(self, entity_class: type, id: int) -> None:
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
        name = entity_class.__name__
        return Database._table_name_str(name)

    @staticmethod
    def _table_name_str(name: str) -> str:
        parts: list[str] = []
        for i, ch in enumerate(name):
            if ch.isupper() and i > 0:
                parts.append("_")
            parts.append(ch.lower())
        return "".join(parts)
