from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_Base = declarative_base()


class _NumberingRow(_Base):
    """Persistent counter for auto-numbering prefixes."""

    __tablename__ = "_erp_numbering"

    id = Column(Integer, primary_key=True)
    prefix = Column(String(50), unique=True, nullable=False)
    counter = Column(Integer, nullable=False, default=0)


class NumberingService:
    """Thread-safe auto-numbering generator.

    Supports 1C-style patterns:
      {prefix}  — текстовый префикс (INV, PRD, PAY…)
      {seq}     — номер с ведущими нулями (000001)
      {year}    — год (2026)
      {month}   — месяц (05)
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        _Base.metadata.create_all(engine)

    def next_number(
        self,
        prefix: str,
        pattern: str = "{seq:08d}",
        period: datetime.date | None = None,
    ) -> str:
        Session = sessionmaker(bind=self._engine)
        with Session() as session, session.begin():
            row = (
                session.query(_NumberingRow)
                .filter_by(prefix=prefix)
                .with_for_update()
                .first()
            )
            if row is None:
                row = _NumberingRow(prefix=prefix, counter=0)
                session.add(row)
            row.counter += 1
            seq = row.counter
        fmt = pattern
        # Handle {seq:Nd} patterns
        import re as _re
        def _fmt_seq(m):
            width = int(m.group(1)) if m.group(1) else 6
            return f"{seq:0{width}d}"
        fmt = _re.sub(r"\{seq:(\d+)d\}", _fmt_seq, fmt)
        fmt = fmt.replace("{seq}", f"{seq:06d}")
        fmt = fmt.replace("{prefix}", str(prefix))
        if period:
            fmt = fmt.replace("{year}", f"{period.year:04d}")
            fmt = fmt.replace("{month}", f"{period.month:02d}")
        else:
            today = datetime.date.today()
            fmt = fmt.replace("{year}", f"{today.year:04d}")
            fmt = fmt.replace("{month}", f"{today.month:02d}")
        return fmt
