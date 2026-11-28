"""Python source editor with syntax highlighting, LSP intelligence, diagnostics, and search/replace."""

from __future__ import annotations

import builtins
import io
import keyword
import re
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, QStringListModel, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QCompleter,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from decui.studio_lsp import LspCompletionItem, StudioLspBridge

_BUILTINS = frozenset(dir(builtins)) | {"self", "cls"}
_TOKENS = re.compile(
    r"""(?P<comment>\#.*)|(?P<triple>'''|"{3})|(?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")"""
    r"|(?P<decorator>@[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
    r"|(?P<word>\b[^\W\d]\w*\b)|(?P<number>\b\d[\w.]*)"
)
_QUOTES = {1: "'''", 2: '"' * 3}
_BLOCK_START = {
    "async", "class", "def", "if", "elif", "else", "for", "while", "try",
    "except", "finally", "with", "match", "case",
}
_AUTO_PAIRS = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}
_OPEN_BRACKETS = {"(": ")", "[": "]", "{": "}"}
_CLOSE_BRACKETS = {")": "(", "]": "[", "}": "{"}


@dataclass(frozen=True)
class EditorDiagnostic:
    line: int  # 0-based
    col: int
    end_line: int
    end_col: int
    severity: int  # 1=Error, 2=Warning, 3=Info, 4=Hint
    message: str


def _starts_suite(prefix: str) -> bool:
    stripped = prefix.lstrip()
    start = re.match(r"[a-z]+\b", stripped)
    if start is None or start.group() not in _BLOCK_START:
        return False
    try:
        tokens = [
            token for token in tokenize.generate_tokens(io.StringIO(stripped).readline)
            if token.type not in {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER}
        ]
    except (tokenize.TokenError, SyntaxError):
        return False
    return bool(tokens and tokens[-1].type == tokenize.OP and tokens[-1].string == ":")


class _PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, editor: QPlainTextEdit) -> None:
        super().__init__(editor.document())
        self._formats: dict[str, QTextCharFormat] = {}
        styles = (
            ("keyword", "#7045a2", False, False),
            ("string", "#17653e", False, False),
            ("comment", "#566577", False, True),
            ("number", "#92431d", False, False),
            ("decorator", "#0e7490", False, False),
            ("defname", "#1d4ed8", True, False),
            ("selfvar", "#b45309", False, True),
            ("builtin", "#0f766e", False, False),
        )
        for name, color, bold, italic in styles:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Weight.Bold)
            if italic:
                fmt.setFontItalic(True)
            self._formats[name] = fmt

    def highlightBlock(self, text: str) -> None:
        non_bmp = any(ord(character) > 0xFFFF for character in text)

        def paint(start: int, end: int, style: str) -> None:
            # Qt uses UTF-16 offsets, while Python's positions count Unicode characters.
            if non_bmp:
                offset = len(text[:start].encode("utf-16-le", errors="surrogatepass")) // 2
                length = len(text[start:end].encode("utf-16-le", errors="surrogatepass")) // 2
            else:
                offset, length = start, end - start
            self.setFormat(offset, length, self._formats[style])

        self.setCurrentBlockState(0)
        pos = 0
        state = self.previousBlockState()
        if state in _QUOTES:
            closing = text.find(_QUOTES[state])
            if closing < 0:
                paint(0, len(text), "string")
                self.setCurrentBlockState(state)
                return
            pos = closing + 3
            paint(0, pos, "string")

        prev_word = ""
        while match := _TOKENS.search(text, pos):
            start, pos = match.span()
            style = match.lastgroup
            if style == "triple":
                quote = match.group()
                closing = text.find(quote, pos)
                if closing < 0:
                    paint(start, len(text), "string")
                    self.setCurrentBlockState(1 if quote == "'''" else 2)
                    return
                pos = closing + 3
                paint(0, pos, "string") if start == 0 else paint(start, pos, "string")
                prev_word = ""
            elif style in {"string", "comment", "number", "decorator"}:
                paint(start, pos, style)
                prev_word = ""
            elif style == "word":
                word = match.group()
                if keyword.iskeyword(word):
                    paint(start, pos, "keyword")
                    prev_word = word
                elif prev_word in {"def", "class"}:
                    paint(start, pos, "defname")
                    prev_word = ""
                elif word in {"self", "cls"}:
                    paint(start, pos, "selfvar")
                    prev_word = ""
                elif word in _BUILTINS:
                    paint(start, pos, "builtin")
                    prev_word = ""
                else:
                    prev_word = ""


class _LineNumberArea(QWidget):
    """Left gutter rendering line numbers and diagnostic markers for PythonEditor."""

    def __init__(self, editor: PythonEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor._gutter_width(), 0)

    def paintEvent(self, event: QPaintEvent) -> None:
        self._editor._paint_line_numbers(event)


class _EditorFindBar(QWidget):
    """Compact overlay search & replace bar opened via Ctrl+F / Ctrl+H inside PythonEditor."""

    def __init__(self, editor: PythonEditor) -> None:
        super().__init__(editor)
        self._editor = editor
        self.setObjectName("editorFindBar")
        self.setStyleSheet(
            "QWidget#editorFindBar { background: #ffffff; border: 1px solid #cbd6e6; border-radius: 6px; }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(4)

        find_row = QHBoxLayout()
        find_row.setContentsMargins(0, 0, 0, 0)
        find_row.setSpacing(4)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Найти в коде…")
        self.query.setMinimumWidth(160)
        self.query.returnPressed.connect(lambda: self._editor.find_text(self.query.text()))
        self.status = QLabel("")
        self.status.setStyleSheet("color: #65748a; font-size: 11px; padding: 0 4px;")
        prev_btn = QPushButton("↑")
        prev_btn.setFixedSize(26, 24)
        prev_btn.setProperty("compact", True)
        prev_btn.setToolTip("Предыдущее (Shift+F3)")
        prev_btn.clicked.connect(lambda: self._editor.find_text(self.query.text(), backward=True))
        next_btn = QPushButton("↓")
        next_btn.setFixedSize(26, 24)
        next_btn.setProperty("compact", True)
        next_btn.setToolTip("Следующее (F3)")
        next_btn.clicked.connect(lambda: self._editor.find_text(self.query.text()))
        close_btn = QPushButton("×")
        close_btn.setFixedSize(24, 24)
        close_btn.setProperty("compact", True)
        close_btn.setToolTip("Закрыть (Esc)")
        close_btn.clicked.connect(self.dismiss)
        find_row.addWidget(self.query)
        find_row.addWidget(self.status)
        find_row.addWidget(prev_btn)
        find_row.addWidget(next_btn)
        find_row.addWidget(close_btn)
        root.addLayout(find_row)

        self.replace_row = QWidget()
        rep_layout = QHBoxLayout(self.replace_row)
        rep_layout.setContentsMargins(0, 0, 0, 0)
        rep_layout.setSpacing(4)
        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText("Заменить на…")
        self.replacement.setMinimumWidth(160)
        rep_one = QPushButton("Заменить")
        rep_one.setProperty("compact", True)
        rep_one.clicked.connect(lambda: self._editor.replace_next(self.query.text(), self.replacement.text()))
        rep_all = QPushButton("Все")
        rep_all.setProperty("compact", True)
        rep_all.clicked.connect(lambda: self._editor.replace_all(self.query.text(), self.replacement.text()))
        rep_layout.addWidget(self.replacement)
        rep_layout.addWidget(rep_one)
        rep_layout.addWidget(rep_all)
        self.replace_row.hide()
        root.addWidget(self.replace_row)

        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self.dismiss)
        self.adjustSize()
        self.hide()

    def dismiss(self) -> None:
        self.hide()
        self._editor.setFocus()


class _SignaturePopup(QLabel):
    """Floating call-signature helper popup triggered by ``(`` or ``,``."""

    def __init__(self, editor: PythonEditor) -> None:
        super().__init__(editor, Qt.WindowType.ToolTip)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setWordWrap(True)
        self.setMaximumWidth(520)
        self.setStyleSheet(
            "QLabel { background: #ffffff; color: #172b46; border: 1px solid #cbd6e6; "
            "border-radius: 6px; padding: 6px 10px; font-family: monospace; font-size: 12px; }"
        )
        self.hide()


_DEFAULT_COMPLETIONS = (
    "self", "db", "Database", "Decimal", "ROUND_HALF_UP", "datetime", "date", "time",
    "before_save", "after_save", "before_delete", "after_delete", "on_validate",
    "prepare_copy", "get_movements", "get_ledger_entries", "configure_app",
    "on_open", "on_field_changed", "on_table_changed", "on_before_save", "build_extra", "build",
    "get_latest", "get_balance", "get_ledger_balance", "get_records", "set_record",
    "Movement", "LedgerEntry", "Column", "Report", "QueryReport", "DateParam",
    "RefParam", "ChoiceParam", "StringParam", "IntegerParam", "BooleanParam",
    "Form", "FormRow", "FormColumn", "FormGroup", "FormTabs", "FormTab",
    "FormField", "FormTable", "FormButton", "FormLabel", "FormDivider",
    "EntityForm", "form_for",
    "db.get", "db.list", "db.filter", "db.save", "db.delete", "db.post", "db.unpost",
    "enable_reminders", "define_role", "ValueError", "PermissionError",
)


class PythonEditor(QPlainTextEdit):
    """IDE code editor with syntax highlighting, LSP completion/hover/signature/goto-def, diagnostics, and find/replace."""

    cursor_location_changed = Signal(int, int)  # 1-based line, 1-based col

    def __init__(self) -> None:
        super().__init__()
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance("    "))
        self.setMouseTracking(True)
        self._highlighter = _PythonHighlighter(self)
        self._line_numbers = _LineNumberArea(self)
        self._find_bar = _EditorFindBar(self)
        self._sig_popup = _SignaturePopup(self)
        self._error_line: int | None = None
        self._diagnostics: list[EditorDiagnostic] = []
        self._project_words: list[str] = []
        self._lsp_details: dict[str, str] = {}
        self._bridge: StudioLspBridge | None = None
        self._lsp_location: str = ""
        self._lsp_entity_getter: Callable[[], str] = lambda: ""
        self._last_comp_req: int = -1
        self._last_comp_base: str = ""
        self._last_comp_force: bool = False
        self._last_hover_req: int = -1
        self._last_hover_pos: QPoint = QPoint()
        self._last_sig_req: int = -1
        self._last_def_req: int = -1
        self._on_definition: Callable[[object], None] | None = None

        self._completion_model = QStringListModel(list(_DEFAULT_COMPLETIONS), self)
        self._completer = QCompleter(self._completion_model, self)
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.activated.connect(self._insert_completion)
        self._completer.highlighted.connect(self._show_completion_detail)

        self.blockCountChanged.connect(self._update_gutter_margins)
        self.updateRequest.connect(self._update_gutter_area)
        self.cursorPositionChanged.connect(self._on_cursor_moved)
        self._update_gutter_margins(0)
        self._highlight_current_line()

        find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find_shortcut.activated.connect(self.open_find)
        replace_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        replace_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        replace_shortcut.activated.connect(lambda: self.open_find(replace=True))
        goto_shortcut = QShortcut(QKeySequence("Ctrl+G"), self)
        goto_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        goto_shortcut.activated.connect(self.go_to_line)
        comment_shortcut = QShortcut(QKeySequence("Ctrl+/"), self)
        comment_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        comment_shortcut.activated.connect(self.toggle_comment)
        complete_shortcut = QShortcut(QKeySequence("Ctrl+Space"), self)
        complete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        complete_shortcut.activated.connect(lambda: self.trigger_completion(force=True))
        sig_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Space"), self)
        sig_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sig_shortcut.activated.connect(self.trigger_signature_help)
        def_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F12), self)
        def_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        def_shortcut.activated.connect(self.go_to_definition)
        next_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F3), self)
        next_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        next_shortcut.activated.connect(lambda: self.find_text(self._find_bar.query.text()))
        prev_shortcut = QShortcut(QKeySequence("Shift+F3"), self)
        prev_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        prev_shortcut.activated.connect(lambda: self.find_text(self._find_bar.query.text(), backward=True))

    def bind_lsp(
        self,
        bridge: StudioLspBridge,
        location: str,
        entity_getter: Callable[[], str] | None = None,
        on_definition: Callable[[object], None] | None = None,
    ) -> None:
        """Connect this editor to a shared StudioLspBridge."""
        self._bridge = bridge
        self._lsp_location = location
        self._lsp_entity_getter = entity_getter or (lambda: "")
        self._on_definition = on_definition
        bridge.completion_ready.connect(self._on_lsp_completions)
        bridge.hover_ready.connect(self._on_lsp_hover)
        bridge.signature_ready.connect(self._on_lsp_signature)
        bridge.definition_ready.connect(self._on_lsp_definition)

    def _gutter_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 20 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_margins(self, _count: int) -> None:
        self.setViewportMargins(self._gutter_width(), 0, 0, 0)

    def _update_gutter_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self._line_numbers.scroll(0, dy)
        else:
            self._line_numbers.update(0, rect.y(), self._line_numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_margins(0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_numbers.setGeometry(QRect(cr.left(), cr.top(), self._gutter_width(), cr.height()))
        self._position_find_bar()

    def _position_find_bar(self) -> None:
        if self._find_bar.isVisible():
            self._find_bar.adjustSize()
            vp = self.viewport().geometry()
            x = max(vp.left() + 8, vp.right() - self._find_bar.width() - 10)
            self._find_bar.move(x, vp.top() + 8)

    def _paint_line_numbers(self, event: QPaintEvent) -> None:
        painter = QPainter(self._line_numbers)
        painter.fillRect(event.rect(), QColor("#f0f4fa"))
        painter.setPen(QColor("#d6dfec"))
        painter.drawLine(self._line_numbers.width() - 1, event.rect().top(),
                         self._line_numbers.width() - 1, event.rect().bottom())

        diag_by_line: dict[int, int] = {}
        for d in self._diagnostics:
            prev = diag_by_line.get(d.line)
            if prev is None or d.severity < prev:
                diag_by_line[d.line] = d.severity

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current_line = self.textCursor().blockNumber()
        line_height = self.fontMetrics().height()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                is_current = block_number == current_line
                painter.setPen(QColor("#172b46" if is_current else "#7d8da3"))
                font = painter.font()
                font.setBold(is_current)
                painter.setFont(font)
                painter.drawText(
                    0, top, self._line_numbers.width() - 7, line_height,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(block_number + 1),
                )
                sev = diag_by_line.get(block_number)
                if sev is not None:
                    painter.save()
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor("#dc2626" if sev == 1 else "#d97706"))
                    cy = top + line_height // 2
                    painter.drawEllipse(4, cy - 3, 6, 6)
                    painter.restore()
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            block_number += 1

    def set_error_line(self, line: int | None) -> None:
        """Highlight 1-based line number as an error, or clear when None."""
        self._error_line = line
        self._highlight_current_line()

    def set_diagnostics(self, diagnostics: list[EditorDiagnostic]) -> None:
        """Replace live LSP diagnostics displayed in this editor."""
        self._diagnostics = list(diagnostics)
        self._highlight_current_line()

    def set_completion_words(self, words: list[str]) -> None:
        """Update autocomplete suggestions with project-specific symbols."""
        self._project_words = list(dict.fromkeys([*_DEFAULT_COMPLETIONS, *keyword.kwlist, *words]))
        self._completion_model.setStringList(self._project_words)

    def _on_cursor_moved(self) -> None:
        cursor = self.textCursor()
        self.cursor_location_changed.emit(cursor.blockNumber() + 1, cursor.positionInBlock() + 1)
        self._highlight_current_line()

    def _matching_bracket_positions(self) -> tuple[int, int] | None:
        doc = self.document()
        pos = self.textCursor().position()
        total = doc.characterCount() - 1
        for candidate in (pos, pos - 1):
            if candidate < 0 or candidate >= total:
                continue
            ch = doc.characterAt(candidate)
            if ch in _OPEN_BRACKETS:
                target = _OPEN_BRACKETS[ch]
                depth = 1
                idx = candidate + 1
                while idx < total:
                    c = doc.characterAt(idx)
                    if c == ch:
                        depth += 1
                    elif c == target:
                        depth -= 1
                        if depth == 0:
                            return candidate, idx
                    idx += 1
            elif ch in _CLOSE_BRACKETS:
                target = _CLOSE_BRACKETS[ch]
                depth = 1
                idx = candidate - 1
                while idx >= 0:
                    c = doc.characterAt(idx)
                    if c == ch:
                        depth += 1
                    elif c == target:
                        depth -= 1
                        if depth == 0:
                            return candidate, idx
                    idx -= 1
        return None

    def _highlight_current_line(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(QColor("#eef4fc"))
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        selections.append(selection)
        if self._error_line is not None and self._error_line >= 1:
            block = self.document().findBlockByNumber(self._error_line - 1)
            if block.isValid():
                err_sel = QTextEdit.ExtraSelection()
                err_sel.format.setBackground(QColor("#fde8e8"))
                err_sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
                err_sel.cursor = QTextCursor(block)
                err_sel.cursor.clearSelection()
                selections.append(err_sel)
        for diag in self._diagnostics:
            start_block = self.document().findBlockByNumber(max(0, diag.line))
            end_block = self.document().findBlockByNumber(max(0, diag.end_line))
            if not start_block.isValid():
                continue
            if not end_block.isValid():
                end_block = start_block
            d_sel = QTextEdit.ExtraSelection()
            d_sel.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            d_sel.format.setUnderlineColor(QColor("#dc2626" if diag.severity == 1 else "#d97706"))
            cur = QTextCursor(start_block)
            s_off = min(max(0, diag.col), len(start_block.text()))
            cur.setPosition(start_block.position() + s_off)
            e_off = min(max(0, diag.end_col), len(end_block.text()))
            e_pos = end_block.position() + e_off
            if e_pos <= cur.position():
                e_pos = min(start_block.position() + len(start_block.text()), cur.position() + 1)
            cur.setPosition(e_pos, QTextCursor.MoveMode.KeepAnchor)
            d_sel.cursor = cur
            selections.append(d_sel)
        brackets = self._matching_bracket_positions()
        if brackets is not None:
            for pos in brackets:
                b_sel = QTextEdit.ExtraSelection()
                b_sel.format.setBackground(QColor("#dbeafe"))
                cur = self.textCursor()
                cur.setPosition(pos)
                cur.setPosition(pos + 1, QTextCursor.MoveMode.KeepAnchor)
                b_sel.cursor = cur
                selections.append(b_sel)
        self.setExtraSelections(selections)
        self._line_numbers.update()

    def open_find(self, *, replace: bool = False) -> None:
        """Show the inline search (and optional replace) bar."""
        selected = self.textCursor().selectedText()
        if selected and "\u2029" not in selected and len(selected) <= 120:
            self._find_bar.query.setText(selected)
        self._find_bar.status.setText("")
        self._find_bar.replace_row.setVisible(replace and not self.isReadOnly())
        self._find_bar.show()
        self._position_find_bar()
        self._find_bar.query.selectAll()
        self._find_bar.query.setFocus()

    def find_text(self, query: str, *, backward: bool = False) -> bool:
        """Find next or previous occurrence of query and highlight it."""
        if not query:
            self._find_bar.status.setText("")
            return False
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        found = self.find(query, flags)
        if not found:
            cursor = self.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start
            )
            self.setTextCursor(cursor)
            found = self.find(query, flags)
        self._find_bar.status.setText("" if found else "Не найдено")
        return found

    def replace_next(self, query: str, replacement: str) -> bool:
        """Replace current match (or find next) when editor is writable."""
        if self.isReadOnly() or not query:
            return False
        cursor = self.textCursor()
        if cursor.hasSelection() and cursor.selectedText() == query:
            cursor.insertText(replacement)
        return self.find_text(query)

    def replace_all(self, query: str, replacement: str) -> int:
        """Replace all occurrences of query in the document."""
        if self.isReadOnly() or not query:
            return 0
        text = self.toPlainText()
        count = text.count(query)
        if count:
            cursor = self.textCursor()
            cursor.beginEditBlock()
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.insertText(text.replace(query, replacement))
            cursor.endEditBlock()
        self._find_bar.status.setText(f"Замен: {count}" if count else "Не найдено")
        return count

    def go_to_line(self, line_number: int | None = None, col_number: int = 1) -> None:
        """Move cursor to the requested 1-based line (and optional 1-based column) number."""
        total = max(1, self.blockCount())
        if line_number is None:
            current = self.textCursor().blockNumber() + 1
            num, accepted = QInputDialog.getInt(self, "Перейти к строке", f"Номер строки (1–{total}):", current, 1, total)
            if not accepted:
                return
            line_number = num
        block = self.document().findBlockByNumber(max(0, min(line_number - 1, total - 1)))
        if block.isValid():
            cursor = QTextCursor(block)
            offset = max(0, min(col_number - 1, len(block.text())))
            if offset:
                cursor.movePosition(QTextCursor.MoveOperation.Right, n=offset)
            self.setTextCursor(cursor)
            self.centerCursor()
            self.setFocus()

    def toggle_comment(self) -> None:
        """Toggle Python `# ` comments across the current line or selected block."""
        if self.isReadOnly():
            return
        cursor = self.textCursor()
        doc = self.document()
        start = doc.findBlock(cursor.selectionStart()).blockNumber()
        end_pos = cursor.selectionEnd()
        end_block = doc.findBlock(end_pos)
        if end_pos > end_block.position() or start == end_block.blockNumber():
            end = end_block.blockNumber()
        else:
            end = max(start, end_block.blockNumber() - 1)
        blocks = [doc.findBlockByNumber(n) for n in range(start, end + 1)]
        non_empty = [b.text() for b in blocks if b.text().strip()]
        uncomment = bool(non_empty) and all(t.lstrip().startswith("#") for t in non_empty)
        cursor.beginEditBlock()
        try:
            for block in blocks:
                text = block.text()
                if not text.strip():
                    continue
                lc = QTextCursor(block)
                indent_len = len(text) - len(text.lstrip(" \t"))
                lc.movePosition(QTextCursor.MoveOperation.Right, n=indent_len)
                if uncomment:
                    rest = text[indent_len:]
                    remove = 2 if rest.startswith("# ") else (1 if rest.startswith("#") else 0)
                    if remove:
                        lc.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, remove)
                        lc.removeSelectedText()
                else:
                    lc.insertText("# ")
        finally:
            cursor.endEditBlock()

    def _completion_prefix(self) -> str:
        cursor = self.textCursor()
        line = cursor.block().text()[:cursor.positionInBlock()]
        match = re.search(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)$", line)
        if match:
            return match.group(1)
        dot_match = re.search(r"([A-Za-z_]\w*\.)$", line)
        return dot_match.group(1) if dot_match else ""

    def _insert_completion(self, completion: str) -> None:
        prefix = self._completion_prefix()
        cursor = self.textCursor()
        if prefix:
            cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, len(prefix))
        cursor.insertText(completion)
        self.setTextCursor(cursor)

    def _show_completion_detail(self, text: str) -> None:
        detail = self._lsp_details.get(text)
        if detail and self._completer.popup().isVisible():
            rect = self._completer.popup().geometry()
            QToolTip.showText(QPoint(rect.right() + 6, rect.top()), detail, self._completer.popup())

    def trigger_completion(self, *, force: bool = False) -> None:
        if self.isReadOnly():
            return
        prefix = self._completion_prefix()
        if not force and len(prefix) < 2 and not prefix.endswith("."):
            self._completer.popup().hide()
            return
        if self._bridge is not None and self._bridge.is_ready:
            cursor = self.textCursor()
            self._last_comp_base = prefix.rpartition(".")[0] if "." in prefix else ""
            self._last_comp_force = force
            self._last_comp_req = self._bridge.request_completion(
                self._lsp_location, self._lsp_entity_getter(),
                cursor.blockNumber(), cursor.positionInBlock(),
            )
        self._show_completion_popup(prefix)

    def _show_completion_popup(self, prefix: str) -> None:
        self._completer.setCompletionPrefix(prefix)
        if self._completer.completionCount() == 0:
            self._completer.popup().hide()
            return
        self._completer.popup().setCurrentIndex(self._completer.completionModel().index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(max(240, self._completer.popup().sizeHintForColumn(0) + 28))
        self._completer.complete(rect)

    def _on_lsp_completions(self, req_id: int, items: list[LspCompletionItem]) -> None:
        if req_id != self._last_comp_req or not self.hasFocus():
            return
        base = self._last_comp_base
        lsp_words: list[str] = []
        self._lsp_details.clear()
        for item in sorted(items, key=lambda i: (i.sort_text or i.label).lower()):
            raw = item.insert_text or item.label
            word = f"{base}.{raw}" if base and "." not in raw else raw
            lsp_words.append(word)
            detail_parts = [p for p in (item.detail, item.documentation) if p]
            if detail_parts:
                self._lsp_details[word] = "\n".join(detail_parts)[:360]
        base_pool = self._project_words or list(_DEFAULT_COMPLETIONS)
        merged = list(dict.fromkeys([*lsp_words, *base_pool]))
        self._completion_model.setStringList(merged)
        prefix = self._completion_prefix()
        if self._last_comp_force or len(prefix) >= 2 or prefix.endswith("."):
            self._show_completion_popup(prefix)

    def trigger_signature_help(self) -> None:
        if self._bridge is None or not self._bridge.is_ready:
            return
        cursor = self.textCursor()
        self._last_sig_req = self._bridge.request_signature(
            self._lsp_location, self._lsp_entity_getter(),
            cursor.blockNumber(), cursor.positionInBlock(),
        )

    def _on_lsp_signature(self, req_id: int, payload: dict[str, Any]) -> None:
        if req_id != self._last_sig_req or not self.hasFocus():
            return
        signatures = payload.get("signatures") or []
        if not signatures:
            self._sig_popup.hide()
            return
        active_sig = int(payload.get("activeSignature", 0) or 0)
        sig = signatures[min(active_sig, len(signatures) - 1)]
        label = str(sig.get("label", ""))
        if not label:
            self._sig_popup.hide()
            return
        active_param = int(payload.get("activeParameter", sig.get("activeParameter", 0)) or 0)
        params = sig.get("parameters") or []
        import html as _html
        rendered = _html.escape(label)
        if 0 <= active_param < len(params):
            p_lbl = params[active_param].get("label", "")
            if isinstance(p_lbl, str) and p_lbl:
                esc_p = _html.escape(p_lbl)
                rendered = rendered.replace(esc_p, f"<b style='color:#1d4ed8'>{esc_p}</b>", 1)
        self._sig_popup.setText(rendered)
        self._sig_popup.adjustSize()
        pos = self.viewport().mapToGlobal(self.cursorRect().topLeft())
        self._sig_popup.move(pos.x(), pos.y() - self._sig_popup.height() - 6)
        self._sig_popup.show()

    def go_to_definition(self) -> None:
        if self._bridge is None or not self._bridge.is_ready:
            return
        cursor = self.textCursor()
        self._last_def_req = self._bridge.request_definition(
            self._lsp_location, self._lsp_entity_getter(),
            cursor.blockNumber(), cursor.positionInBlock(),
        )

    def _on_lsp_definition(self, req_id: int, locations: list[Any]) -> None:
        if req_id != self._last_def_req or not locations:
            return
        target = locations[0]
        if self._on_definition is not None:
            self._on_definition(target)

    def _on_lsp_hover(self, req_id: int, text: str) -> None:
        if req_id != self._last_hover_req or not text:
            return
        QToolTip.showText(self._last_hover_pos, text[:800], self)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and hasattr(event, "pos"):
            help_ev = event  # type: ignore[assignment]
            vp_pos = self.viewport().mapFrom(self, help_ev.pos())  # type: ignore[attr-defined]
            cursor = self.cursorForPosition(vp_pos)
            line_0 = cursor.blockNumber()
            col_0 = cursor.positionInBlock()
            matches = [
                d.message for d in self._diagnostics
                if d.line <= line_0 <= d.end_line
                and (line_0 > d.line or col_0 >= max(0, d.col - 1))
                and (line_0 < d.end_line or col_0 <= d.end_col + 1)
            ]
            global_pos = help_ev.globalPos()  # type: ignore[attr-defined]
            if matches:
                QToolTip.showText(global_pos, "\n".join(matches), self)
                return True
            if self._bridge is not None and self._bridge.is_ready:
                self._last_hover_pos = global_pos
                self._last_hover_req = self._bridge.request_hover(
                    self._lsp_location, self._lsp_entity_getter(), line_0, col_0,
                )
        return super().event(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        if (event.modifiers() & Qt.KeyboardModifier.ControlModifier) and event.button() == Qt.MouseButton.LeftButton:
            self.go_to_definition()

    def _indent_selection(self, *, dedent: bool) -> None:
        cursor = self.textCursor()
        doc = self.document()
        start = doc.findBlock(cursor.selectionStart()).blockNumber()
        end_pos = cursor.selectionEnd()
        end_block = doc.findBlock(end_pos)
        if end_pos > end_block.position() or start == end_block.blockNumber():
            end = end_block.blockNumber()
        else:
            end = max(start, end_block.blockNumber() - 1)

        cursor.beginEditBlock()
        try:
            for block_num in range(start, end + 1):
                block = doc.findBlockByNumber(block_num)
                if not block.isValid():
                    continue
                line_cursor = QTextCursor(block)
                if dedent:
                    text = block.text()
                    remove = min(4, len(text) - len(text.lstrip(" ")))
                    if remove == 0 and text.startswith("\t"):
                        remove = 1
                    if remove > 0:
                        line_cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, remove)
                        line_cursor.removeSelectedText()
                else:
                    line_cursor.insertText("    ")
        finally:
            cursor.endEditBlock()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._completer.popup().isVisible():
            if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Tab):
                completion = self._completer.currentCompletion()
                if completion:
                    self._insert_completion(completion)
                self._completer.popup().hide()
                return
            if event.key() == Qt.Key.Key_Escape:
                self._completer.popup().hide()
                return
        key = event.key()
        modifiers = event.modifiers()
        if not self.isReadOnly():
            if key == Qt.Key.Key_Escape:
                self._sig_popup.hide()
            if key == Qt.Key.Key_Backtab:
                self._indent_selection(dedent=True)
                return
            if key == Qt.Key.Key_Tab and modifiers in {Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier}:
                cursor = self.textCursor()
                if cursor.hasSelection():
                    self._indent_selection(dedent=False)
                else:
                    cursor.insertText(" " * (4 - cursor.positionInBlock() % 4))
                    self.setTextCursor(cursor)
                return
            if modifiers in {Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier}:
                if key in {Qt.Key.Key_Enter, Qt.Key.Key_Return}:
                    self._sig_popup.hide()
                    cursor = self.textCursor()
                    prefix = QTextCursor(cursor)
                    prefix.setPosition(cursor.selectionStart())
                    prefix.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
                    before = prefix.selectedText()
                    indent = before[:len(before) - len(before.lstrip(" \t"))]
                    cursor.insertText("\n" + indent + ("    " if _starts_suite(before) else ""))
                    self.setTextCursor(cursor)
                    return
                typed = event.text()
                cursor = self.textCursor()
                if not cursor.hasSelection() and typed:
                    block_text = cursor.block().text()
                    pos_in = cursor.positionInBlock()
                    next_ch = block_text[pos_in] if pos_in < len(block_text) else ""
                    prev_ch = block_text[pos_in - 1] if pos_in > 0 else ""
                    if key == Qt.Key.Key_Backspace and prev_ch in _AUTO_PAIRS and next_ch == _AUTO_PAIRS[prev_ch]:
                        cursor.deletePreviousChar()
                        cursor.deleteChar()
                        self.setTextCursor(cursor)
                        return
                    if typed in _AUTO_PAIRS.values() and next_ch == typed:
                        cursor.movePosition(QTextCursor.MoveOperation.Right)
                        self.setTextCursor(cursor)
                        if typed == ")":
                            self._sig_popup.hide()
                        return
                    if typed in _AUTO_PAIRS and (not next_ch or next_ch.isspace() or next_ch in ")]},:"):
                        if typed not in ("'", '"') or not (prev_ch.isalnum() or prev_ch == "_"):
                            cursor.insertText(typed + _AUTO_PAIRS[typed])
                            cursor.movePosition(QTextCursor.MoveOperation.Left)
                            self.setTextCursor(cursor)
                            if typed == "(":
                                self.trigger_signature_help()
                            return
        super().keyPressEvent(event)
        if not self.isReadOnly() and event.text() and (event.text().isalnum() or event.text() in "._"):
            self.trigger_completion(force=False)
        elif not self.isReadOnly() and event.text() in ("(", ","):
            self.trigger_signature_help()
        elif not self.isReadOnly() and event.text() == ")":
            self._sig_popup.hide()
        elif self._completer.popup().isVisible() and event.key() == Qt.Key.Key_Backspace:
            self.trigger_completion(force=False)
        elif not event.text() and event.key() not in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt):
            self._completer.popup().hide()
