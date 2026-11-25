"""Printable layouts for saved DECUI documents."""

from __future__ import annotations

import datetime
import html
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .fields import BooleanField, ChoiceField, Field, RefField

if TYPE_CHECKING:
    from .entity import Entity
    from .orm import Database


@dataclass(frozen=True)
class PrintTable:
    part: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class PrintForm:
    title: str
    fields: tuple[str, ...] = ()
    tables: tuple[PrintTable, ...] = ()

    def render_html(self, db: Database, entity: Entity) -> str:
        """Build printable HTML from a fresh, permission-filtered snapshot."""
        if entity.id is None:
            raise ValueError("Сначала сохраните документ.")
        snapshot = db.get(type(entity), entity.id)
        if snapshot is None:
            raise PermissionError("Документ недоступен.")
        definitions = snapshot._meta.fields
        reference_cache: dict[tuple[type, int], Entity | None] = {}

        def show(field: Field, value: Any) -> str:
            if value is None:
                return "—"
            if isinstance(field, RefField) and isinstance(value, int):
                target = field.resolve_target()
                key = (target, value)
                if key not in reference_cache:
                    reference_cache[key] = db.get(target, value)
                item = reference_cache[key]
                value = str(item) if item is not None else "—"
            elif isinstance(field, ChoiceField):
                value = field.format_display(value)
            elif isinstance(field, BooleanField):
                value = "Да" if value else "Нет"
            elif isinstance(value, datetime.datetime):
                value = value.strftime("%d.%m.%Y %H:%M")
            elif isinstance(value, datetime.date):
                value = value.strftime("%d.%m.%Y")
            return html.escape(str(value)).replace("\n", "<br>")

        body = [f'<h1 style="color:#16365c;font-size:22pt">{html.escape(self.title)}</h1>']
        for name in self.fields:
            if db.can_read_field(type(snapshot), name):
                field = definitions[name]
                body.append(f"<p><b>{html.escape(field.display_name)}</b><br>{show(field, snapshot._data[name])}</p>")
        for table in self.tables:
            part = snapshot._meta.table_parts[table.part]
            columns = [name for name in table.columns if db.can_read_field(type(snapshot), f"{table.part}.{name}")]
            if not columns:
                continue
            body.append(f'<h2 style="color:#16365c;font-size:14pt">{html.escape(part.verbose_name or table.part)}</h2>')
            rows = snapshot._table_data.get(table.part, [])
            if not rows:
                body.append("<p>Нет строк</p>")
                continue
            headings = "".join(f"<th>{html.escape(part.fields[name].display_name)}</th>" for name in columns)
            body.append(f'<table border="1" cellspacing="0" cellpadding="6" width="100%"><thead>'
                        f'<tr bgcolor="#eaf1fa">{headings}</tr></thead><tbody>')
            for row in rows:
                body.append("<tr>" + "".join(
                    f"<td>{show(part.fields[name], row.get(name))}</td>" for name in columns
                ) + "</tr>")
            body.append("</tbody></table>")
        return '<html><body style="color:#152c45;font-family:sans-serif;font-size:10pt">' + "".join(body) + "</body></html>"

    def export_pdf(self, db: Database, entity: Entity, path: Path | str) -> None:
        from PySide6.QtGui import QPageLayout, QPageSize, QTextDocument
        from PySide6.QtPrintSupport import QPrinter

        document = QTextDocument()
        document.setHtml(self.render_html(db, entity))
        destination = Path(path)
        fd, temp = tempfile.mkstemp(prefix=".decui-print-", suffix=".pdf", dir=destination.parent)
        os.close(fd)
        try:
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(temp)
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            if any(len(table.columns) > 6 for table in self.tables):
                printer.setPageOrientation(QPageLayout.Orientation.Landscape)
            document.print_(printer)
            with open(temp, "rb") as output:
                if printer.printerState() == QPrinter.PrinterState.Error or output.read(5) != b"%PDF-":
                    raise OSError("Не удалось создать PDF.")
            os.replace(temp, destination)
        finally:
            Path(temp).unlink(missing_ok=True)
