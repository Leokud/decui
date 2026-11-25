"""Python source editor used by DECUI Studio."""

from __future__ import annotations

import io
import keyword
import re
import tokenize

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor,
    QFontDatabase,
    QKeyEvent,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import QPlainTextEdit

_TOKENS = re.compile(
    r"""(?P<comment>\#.*)|(?P<triple>'''|"{3})|(?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")"""
    r"|(?P<word>\b[^\W\d]\w*\b)|(?P<number>\b\d[\w.]*)"
)
_QUOTES = {1: "'''", 2: '"' * 3}
_BLOCK_START = {
    "async", "class", "def", "if", "elif", "else", "for", "while", "try",
    "except", "finally", "with", "match", "case",
}


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
        for name, color in (
            ("keyword", "#7045a2"), ("string", "#17653e"),
            ("comment", "#566577"), ("number", "#92431d"),
        ):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
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
                paint(start, pos, "string")
            elif style in {"string", "comment", "number"}:
                paint(start, pos, style)
            elif style == "word" and keyword.iskeyword(match.group()):
                paint(start, pos, "keyword")


class PythonEditor(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance("    "))
        self._highlighter = _PythonHighlighter(self)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        modifiers = event.modifiers()
        if not self.isReadOnly() and modifiers in {Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier}:
            if key in {Qt.Key.Key_Enter, Qt.Key.Key_Return}:
                cursor = self.textCursor()
                prefix = QTextCursor(cursor)
                prefix.setPosition(cursor.selectionStart())
                prefix.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
                before = prefix.selectedText()
                indent = before[:len(before) - len(before.lstrip(" \t"))]
                cursor.insertText("\n" + indent + ("    " if _starts_suite(before) else ""))
                self.setTextCursor(cursor)
                return
            if key == Qt.Key.Key_Tab and not self.textCursor().hasSelection():
                cursor = self.textCursor()
                cursor.insertText(" " * (4 - cursor.positionInBlock() % 4))
                self.setTextCursor(cursor)
                return
        super().keyPressEvent(event)
