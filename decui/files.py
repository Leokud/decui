"""File publication shared by build and database export."""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile


def publish_new_file(source: Path, destination: Path) -> None:
    """Publish a file while preserving any existing destination."""
    with NamedTemporaryFile(dir=destination.parent, prefix=".decui-file-", delete=False) as file:
        temporary = Path(file.name)
    try:
        shutil.copy2(source, temporary)
        try:
            os.link(temporary, destination)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
                raise
            # Some removable filesystems cannot create links.
            with destination.open("xb") as final:
                try:
                    with temporary.open("rb") as original:
                        shutil.copyfileobj(original, final)
                    final.flush()
                    shutil.copymode(temporary, destination)
                except OSError:
                    final.close()
                    destination.unlink(missing_ok=True)
                    raise
    finally:
        temporary.unlink(missing_ok=True)


def copy_sqlite(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    """Copy a committed SQLite state with bounded waiting for a locked database."""
    if source.in_transaction:
        raise RuntimeError("Завершите изменение данных перед резервным копированием.")
    started = time.monotonic()

    def check_progress(status: int, _left: int, _total: int) -> None:
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() - started >= 5:
            raise TimeoutError("База занята. Повторите резервное копирование после записи.")

    source.backup(destination, pages=256, progress=check_progress, sleep=0.05)
    destination.execute("PRAGMA trusted_schema=OFF")
    if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("Не удалось проверить целостность резервной копии.")


def stage_decui_backup(source: Path, destination: Path) -> bool:
    """Copy a DECUI snapshot into a private workspace and report whether it has accounts."""
    if not source.is_file():
        raise FileNotFoundError(f"Файл резервной копии не найден: {source}")
    if os.path.lexists(destination):
        raise FileExistsError(f"Файл уже существует: {destination}")
    uri = source.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as original, closing(sqlite3.connect(destination)) as copy:
        copy_sqlite(original, copy)
        names = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "_erp_config" not in names or copy.execute(
            "SELECT 1 FROM _erp_config WHERE key = 'schema_hash'"
        ).fetchone() is None:
            raise ValueError("Выбранный файл не содержит базы DECUI.")
        return "_erp_accounts" in names
