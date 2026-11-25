from __future__ import annotations

import datetime
import re
import threading
from typing import TYPE_CHECKING

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Session, declarative_base, sessionmaker

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_Base = declarative_base()


class _NumberingRow(_Base):
    """Persistent counter for auto-numbering prefixes."""

    __tablename__ = "_erp_numbering"

    id = Column(Integer, primary_key=True)
    prefix = Column(String(80), unique=True, nullable=False)
    counter = Column(Integer, nullable=False, default=0)


class NumberingService:
    """Thread-safe auto-numbering generator.

    Supports 1C-style patterns:
      {prefix}  — текстовый префикс (INV, PRD, PAY…)
      {seq}     — номер с ведущими нулями (000001)
      {year}    — год (2026) — при наличии сбрасывает счётчик раз в год
      {month}   — месяц (05) — при наличии сбрасывает счётчик раз в месяц
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        _Base.metadata.create_all(engine)

    def next_number(
        self,
        prefix: str,
        pattern: str = "{seq:08d}",
        period: datetime.date | None = None,
        *,
        session: Session | None = None,
    ) -> str:
        eff_date = period or datetime.date.today()
        counter_key = prefix
        if "{month}" in pattern:
            counter_key = f"{prefix}:{eff_date.year:04d}-{eff_date.month:02d}"
        elif "{year}" in pattern:
            counter_key = f"{prefix}:{eff_date.year:04d}"

        if session is None:
            factory = sessionmaker(bind=self._engine)
            with self._lock, factory.begin() as owned_session:
                owned_session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                seq = self._increment(owned_session, counter_key)
        else:
            seq = self._increment(session, counter_key)

        fmt = pattern

        def _fmt_seq(m: re.Match[str]) -> str:
            width = int(m.group(1)) if m.group(1) else 6
            return f"{seq:0{width}d}"

        fmt = re.sub(r"\{seq:(\d+)d\}", _fmt_seq, fmt)
        fmt = fmt.replace("{seq}", f"{seq:06d}")
        fmt = fmt.replace("{prefix}", str(prefix))
        fmt = fmt.replace("{year}", f"{eff_date.year:04d}")
        fmt = fmt.replace("{month}", f"{eff_date.month:02d}")
        return fmt

    @staticmethod
    def _increment(session: Session, counter_key: str) -> int:
        row = session.query(_NumberingRow).filter_by(prefix=counter_key).first()
        if row is None:
            row = _NumberingRow(prefix=counter_key, counter=0)
            session.add(row)
        row.counter += 1
        return row.counter
