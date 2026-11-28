"""Pyrefly Language Server Protocol (LSP) client and Studio source-span bridge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QObject, QTimer, Signal

if TYPE_CHECKING:
    from decui.studio_project import StudioProject, StudioSourceSpan


@dataclass(frozen=True)
class LspDiagnostic:
    line: int
    col: int
    end_line: int
    end_col: int
    severity: int  # 1=Error, 2=Warning, 3=Information, 4=Hint
    message: str
    source: str = "pyrefly"


@dataclass(frozen=True)
class LspCompletionItem:
    label: str
    kind: int = 0
    detail: str = ""
    documentation: str = ""
    insert_text: str = ""
    sort_text: str = ""


@dataclass(frozen=True)
class LspLocation:
    path: Path
    line: int
    col: int


@dataclass(frozen=True)
class LspSymbol:
    name: str
    kind: int
    detail: str
    line: int
    col: int
    children: tuple[LspSymbol, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MappedDiagnostic:
    location: str
    entity_name: str
    line: int  # 0-based in editor (or virtual app.py)
    col: int
    end_line: int
    end_col: int
    severity: int
    message: str


def _uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return Path(uri)
    raw = unquote(parsed.path)
    if sys.platform == "win32" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


def _format_markup(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("value", "")).strip()
    if isinstance(value, list):
        return "\n\n".join(_format_markup(item) for item in value if item).strip()
    return ""


def _parse_symbol(raw: dict[str, Any]) -> LspSymbol:
    sel = raw.get("selectionRange") or raw.get("range") or {}
    start = sel.get("start") or {}
    children_raw = raw.get("children") or []
    return LspSymbol(
        name=str(raw.get("name", "")),
        kind=int(raw.get("kind", 0)),
        detail=str(raw.get("detail", "")),
        line=int(start.get("line", 0)),
        col=int(start.get("character", 0)),
        children=tuple(_parse_symbol(ch) for ch in children_raw if isinstance(ch, dict)),
    )


class PyreflyLspClient(QObject):
    """Asynchronous stdio JSON-RPC client for ``python -m pyrefly lsp``."""

    ready_changed = Signal(bool, str)
    diagnostics_received = Signal(str, object)
    completions_received = Signal(int, object)
    hover_received = Signal(int, str)
    signature_received = Signal(int, object)
    definition_received = Signal(int, object)
    symbols_received = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._pending: dict[int, str] = {}
        self._next_id = 1
        self._doc_versions: dict[str, int] = {}
        self._ready = False
        self._stopping = False
        self._init_req_id: int | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready and self._proc is not None and self._proc.poll() is None

    def start(self, workspace_dir: Path) -> bool:
        """Start the Pyrefly LSP subprocess rooted at ``workspace_dir``."""
        if self._proc is not None:
            return True
        self._stopping = False
        self._ready = False
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "pyrefly", "lsp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(workspace_dir),
                creationflags=creation_flags,
            )
        except OSError as exc:
            self.ready_changed.emit(False, f"LSP недоступен: {exc}")
            return False

        self._reader = threading.Thread(target=self._read_loop, name="decui-pyrefly-lsp", daemon=True)
        self._reader.start()

        self._init_req_id = self._send_request("initialize", {
            "processId": os.getpid(),
            "rootUri": workspace_dir.resolve().as_uri(),
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": False, "dynamicRegistration": False},
                    "completion": {
                        "completionItem": {
                            "snippetSupport": False,
                            "documentationFormat": ["markdown", "plaintext"],
                        },
                    },
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "signatureHelp": {
                        "signatureInformation": {"documentationFormat": ["markdown", "plaintext"]},
                    },
                    "publishDiagnostics": {"relatedInformation": False},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                },
            },
        })
        return True

    def stop(self) -> None:
        """Gracefully shut down the LSP server and terminate its subprocess."""
        self._stopping = True
        self._ready = False
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                self._send_raw({"jsonrpc": "2.0", "id": self._alloc_id(), "method": "shutdown", "params": None})
                self._send_raw({"jsonrpc": "2.0", "method": "exit"})
                try:
                    proc.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        finally:
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            self._doc_versions.clear()
            with self._write_lock:
                self._pending.clear()

    def sync_document(self, uri: str, text: str) -> None:
        """Open or update a Python document in the LSP session."""
        if not self.is_ready:
            return
        if uri not in self._doc_versions:
            self._doc_versions[uri] = 1
            self._send_notification("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri,
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                },
            })
        else:
            version = self._doc_versions[uri] + 1
            self._doc_versions[uri] = version
            self._send_notification("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            })

    def request_completion(self, uri: str, line: int, col: int) -> int:
        return self._send_request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

    def request_hover(self, uri: str, line: int, col: int) -> int:
        return self._send_request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

    def request_signature(self, uri: str, line: int, col: int) -> int:
        return self._send_request("textDocument/signatureHelp", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

    def request_definition(self, uri: str, line: int, col: int) -> int:
        return self._send_request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        })

    def request_symbols(self, uri: str) -> int:
        return self._send_request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })

    def _alloc_id(self) -> int:
        with self._write_lock:
            req_id = self._next_id
            self._next_id += 1
            return req_id

    def _send_raw(self, payload: dict[str, Any]) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return False
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._write_lock:
            try:
                proc.stdin.write(frame)
                proc.stdin.flush()
                return True
            except OSError:
                return False

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._send_raw({"jsonrpc": "2.0", "method": method, "params": params})

    def _send_request(self, method: str, params: dict[str, Any]) -> int:
        req_id = self._alloc_id()
        with self._write_lock:
            self._pending[req_id] = method
        if not self._send_raw({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}):
            with self._write_lock:
                self._pending.pop(req_id, None)
            return -1
        return req_id

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        while not self._stopping:
            try:
                headers: dict[str, str] = {}
                while True:
                    line = stdout.readline()
                    if not line:
                        if not self._stopping:
                            self._ready = False
                            self.ready_changed.emit(False, "LSP остановлен")
                        return
                    text_line = line.decode("ascii", errors="replace")
                    if text_line in ("\r\n", "\n", ""):
                        break
                    k, _, v = text_line.partition(":")
                    headers[k.strip().lower()] = v.strip()
                raw_len = headers.get("content-length")
                if not raw_len:
                    continue
                length = int(raw_len)
                data = stdout.read(length)
                if len(data) < length:
                    return
                msg = json.loads(data.decode("utf-8", errors="replace"))
            except (OSError, ValueError):
                return
            self._handle_message(msg)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:
            # Server-initiated request (e.g. workspace/configuration)
            self._send_raw({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            return

        if "method" in msg:
            if msg["method"] == "textDocument/publishDiagnostics":
                params = msg.get("params") or {}
                uri = str(params.get("uri", ""))
                diags: list[LspDiagnostic] = []
                for item in params.get("diagnostics") or ():
                    rng = item.get("range") or {}
                    start = rng.get("start") or {}
                    end = rng.get("end") or {}
                    diags.append(LspDiagnostic(
                        line=int(start.get("line", 0)),
                        col=int(start.get("character", 0)),
                        end_line=int(end.get("line", start.get("line", 0))),
                        end_col=int(end.get("character", start.get("character", 0))),
                        severity=int(item.get("severity", 1)),
                        message=str(item.get("message", "")),
                        source=str(item.get("source", "pyrefly")),
                    ))
                self.diagnostics_received.emit(uri, diags)
            return

        req_id = msg.get("id")
        if not isinstance(req_id, int):
            return
        with self._write_lock:
            method = self._pending.pop(req_id, "")
        if not method or "error" in msg:
            if req_id == self._init_req_id:
                self._ready = False
                self.ready_changed.emit(False, "Ошибка инициализации LSP")
            return
        result = msg.get("result")

        if method == "initialize":
            self._ready = True
            self._send_notification("initialized", {})
            self.ready_changed.emit(True, "LSP: Pyrefly готов")
            return

        if method == "textDocument/completion":
            raw_items = result.get("items", []) if isinstance(result, dict) else (result or [])
            items = [
                LspCompletionItem(
                    label=str(entry.get("label", "")),
                    kind=int(entry.get("kind", 0)),
                    detail=str(entry.get("detail", "")),
                    documentation=_format_markup(entry.get("documentation")),
                    insert_text=str(entry.get("insertText") or entry.get("label") or ""),
                    sort_text=str(entry.get("sortText") or entry.get("label") or ""),
                )
                for entry in raw_items
                if isinstance(entry, dict) and entry.get("label")
            ]
            self.completions_received.emit(req_id, items)
        elif method == "textDocument/hover":
            text = _format_markup(result.get("contents")) if isinstance(result, dict) else ""
            self.hover_received.emit(req_id, text)
        elif method == "textDocument/signatureHelp":
            self.signature_received.emit(req_id, result if isinstance(result, dict) else {})
        elif method == "textDocument/definition":
            locs: list[LspLocation] = []
            raw_locs = result if isinstance(result, list) else ([result] if isinstance(result, dict) else [])
            for entry in raw_locs:
                if not isinstance(entry, dict):
                    continue
                uri_str = str(entry.get("uri") or entry.get("targetUri") or "")
                rng = entry.get("range") or entry.get("targetSelectionRange") or entry.get("targetRange") or {}
                start = rng.get("start") or {}
                if uri_str:
                    locs.append(LspLocation(_uri_to_path(uri_str), int(start.get("line", 0)), int(start.get("character", 0))))
            self.definition_received.emit(req_id, locs)
        elif method == "textDocument/documentSymbol":
            symbols = [_parse_symbol(item) for item in (result or []) if isinstance(item, dict)]
            self.symbols_received.emit(req_id, symbols)


class StudioLspBridge(QObject):
    """Map Studio split code editors and full project code onto a live Pyrefly LSP workspace."""

    status_changed = Signal(bool, str)
    mapped_diagnostics = Signal(object)  # list[MappedDiagnostic]
    symbols_updated = Signal(object)     # list[LspSymbol]
    completion_ready = Signal(int, object)
    hover_ready = Signal(int, str)
    signature_ready = Signal(int, object)
    definition_ready = Signal(int, object)

    def __init__(
        self,
        snapshot_provider: Callable[[], tuple[StudioProject, str | None]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._provider = snapshot_provider
        self._client = PyreflyLspClient(self)
        self._workspace: TemporaryDirectory[str] | None = None
        self._app_path: Path | None = None
        self._app_uri = ""
        self._spans: list[StudioSourceSpan] = []
        self._full_source_mode = False
        self._last_text = ""
        self._latest_symbol_req = -1

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self.sync_now)

        self._client.ready_changed.connect(self._on_ready_changed)
        self._client.diagnostics_received.connect(self._on_diagnostics)
        self._client.completions_received.connect(self.completion_ready)
        self._client.hover_received.connect(self.hover_ready)
        self._client.signature_received.connect(self.signature_ready)
        self._client.definition_received.connect(self._on_definition)
        self._client.symbols_received.connect(self._on_symbols)

    @property
    def is_ready(self) -> bool:
        return self._client.is_ready

    @property
    def app_uri(self) -> str:
        return self._app_uri

    def start(self) -> bool:
        if os.environ.get("DECUI_DISABLE_LSP") == "1":
            self.status_changed.emit(False, "LSP отключён")
            return False
        if self._workspace is None:
            self._workspace = TemporaryDirectory(prefix="decui-lsp-")
            ws_dir = Path(self._workspace.name)
            decui_root = str(Path(__file__).resolve().parent.parent)
            (ws_dir / "pyrefly.toml").write_text(
                f'search-path = [".", {json.dumps(decui_root)}]\n'
                f'python-interpreter-path = {json.dumps(sys.executable)}\n',
                encoding="utf-8",
            )
            self._app_path = ws_dir / "app.py"
            self._app_path.write_text("", encoding="utf-8")
            self._app_uri = self._app_path.resolve().as_uri()
        assert self._workspace is not None
        return self._client.start(Path(self._workspace.name))

    def stop(self) -> None:
        self._debounce.stop()
        self._client.stop()
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None
            self._app_path = None
            self._app_uri = ""

    def schedule_sync(self) -> None:
        if self._client.is_ready:
            self._debounce.start()

    def sync_now(self) -> None:
        if not self._client.is_ready or self._app_path is None:
            return
        try:
            project, source_override = self._provider()
            if source_override is not None:
                text = source_override
                self._spans = []
                self._full_source_mode = True
            else:
                text, self._spans = project.lsp_document()
                self._full_source_mode = False
        except Exception:
            return
        if text != self._last_text:
            self._last_text = text
            try:
                self._app_path.write_text(text, encoding="utf-8")
            except OSError:
                return
            self._client.sync_document(self._app_uri, text)
            self._latest_symbol_req = self._client.request_symbols(self._app_uri)

    def _on_ready_changed(self, ready: bool, message: str) -> None:
        self.status_changed.emit(ready, message)
        if ready:
            self._last_text = ""
            self.sync_now()

    def map_editor_to_virtual(
        self, location: str, entity_name: str, line_0: int, col_0: int,
    ) -> tuple[int, int] | None:
        if location == "Код проекта":
            return line_0, col_0
        if self._full_source_mode:
            return None
        for span in self._spans:
            if span.location == location and span.entity_name == entity_name:
                target_line = (span.generated_start - 1) + (line_0 + 1 - span.editor_start)
                return max(0, target_line), max(0, col_0 + span.col_offset)
        return None

    def map_virtual_to_editor(self, line_0: int, col_0: int) -> tuple[str, str, int, int]:
        if not self._full_source_mode:
            gen_line_1 = line_0 + 1
            for span in self._spans:
                if span.generated_start <= gen_line_1 < span.generated_start + span.line_count:
                    ed_line_0 = (gen_line_1 - span.generated_start) + (span.editor_start - 1)
                    ed_col_0 = max(0, col_0 - span.col_offset)
                    return span.location, span.entity_name, ed_line_0, ed_col_0
        return "Код проекта", "", line_0, col_0

    def request_completion(self, location: str, entity_name: str, line_0: int, col_0: int) -> int:
        self.sync_now()
        pos = self.map_editor_to_virtual(location, entity_name, line_0, col_0)
        if pos is None or not self._client.is_ready:
            return -1
        return self._client.request_completion(self._app_uri, pos[0], pos[1])

    def request_hover(self, location: str, entity_name: str, line_0: int, col_0: int) -> int:
        pos = self.map_editor_to_virtual(location, entity_name, line_0, col_0)
        if pos is None or not self._client.is_ready:
            return -1
        return self._client.request_hover(self._app_uri, pos[0], pos[1])

    def request_signature(self, location: str, entity_name: str, line_0: int, col_0: int) -> int:
        self.sync_now()
        pos = self.map_editor_to_virtual(location, entity_name, line_0, col_0)
        if pos is None or not self._client.is_ready:
            return -1
        return self._client.request_signature(self._app_uri, pos[0], pos[1])

    def request_definition(self, location: str, entity_name: str, line_0: int, col_0: int) -> int:
        self.sync_now()
        pos = self.map_editor_to_virtual(location, entity_name, line_0, col_0)
        if pos is None or not self._client.is_ready:
            return -1
        return self._client.request_definition(self._app_uri, pos[0], pos[1])

    def _on_diagnostics(self, uri: str, raw_diags: list[LspDiagnostic]) -> None:
        if uri != self._app_uri:
            return
        mapped: list[MappedDiagnostic] = []
        for diag in raw_diags:
            loc, ent, ed_line, ed_col = self.map_virtual_to_editor(diag.line, diag.col)
            end_loc, end_ent, ed_end_line, ed_end_col = self.map_virtual_to_editor(diag.end_line, diag.end_col)
            if (end_loc, end_ent) != (loc, ent):
                ed_end_line, ed_end_col = ed_line, ed_col + 1
            mapped.append(MappedDiagnostic(
                location=loc,
                entity_name=ent,
                line=ed_line,
                col=ed_col,
                end_line=ed_end_line,
                end_col=ed_end_col,
                severity=diag.severity,
                message=diag.message,
            ))
        self.mapped_diagnostics.emit(mapped)

    def _on_definition(self, req_id: int, raw_locs: list[LspLocation]) -> None:
        resolved: list[tuple[str, str, int, int] | LspLocation] = []
        app_resolved = self._app_path.resolve() if self._app_path is not None else None
        for loc in raw_locs:
            try:
                is_virtual = app_resolved is not None and loc.path.resolve() == app_resolved
            except OSError:
                is_virtual = False
            if is_virtual:
                resolved.append(self.map_virtual_to_editor(loc.line, loc.col))
            else:
                resolved.append(loc)
        self.definition_ready.emit(req_id, resolved)

    def _on_symbols(self, req_id: int, symbols: list[LspSymbol]) -> None:
        if req_id == self._latest_symbol_req:
            self.symbols_updated.emit(symbols)
