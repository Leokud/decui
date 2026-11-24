from __future__ import annotations

from typing import Callable, Union

from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QWidget

_ItemTarget = Union[type, object]


class NavigationPanel(QTreeWidget):
    """Tree navigation with 1C-style subsystem hierarchy."""

    SEP = "/"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self._on_select: Callable[[_ItemTarget], None] | None = None
        self._item_map: dict[QTreeWidgetItem, _ItemTarget] = {}
        self._build_tree()
        self.itemClicked.connect(self._on_item_clicked)
        self.itemActivated.connect(self._on_item_clicked)

    def on_select(self, callback: Callable[[_ItemTarget], None]) -> None:
        self._on_select = callback

    def _build_tree(self) -> None:
        from decui.registry import get_all_entities, get_all_reports

        self.clear()
        self._item_map.clear()
        self._branch_cache: dict[str, QTreeWidgetItem] = {}

        def _branch(path: str) -> QTreeWidgetItem:
            """Get or create a tree branch for a subsystem path."""
            if path in self._branch_cache:
                return self._branch_cache[path]
            parts = path.split(self.SEP)
            parent: QTreeWidgetItem | QTreeWidget = self
            for i, p in enumerate(parts):
                sub = self.SEP.join(parts[: i + 1])
                if sub not in self._branch_cache:
                    node = QTreeWidgetItem(parent, [p])
                    node.setExpanded(True)
                    self._branch_cache[sub] = node
                parent = self._branch_cache[sub]
            return self._branch_cache[path]

        for cls in get_all_entities():
            sub = (
                getattr(cls, "__subsystem__", None)
                or getattr(cls, "__section__", None)
                or getattr(cls, "__category__", None)
                or cls._meta.section
                or ""
            )
            parent: QTreeWidgetItem | QTreeWidget = _branch(sub) if sub else self
            self._add_item(cls._meta.verbose_name, cls, parent)

        for cls in get_all_reports():
            sub = (
                getattr(cls, "__subsystem__", None)
                or getattr(cls, "__section__", None)
                or getattr(cls, "__category__", None)
                or ""
            )
            if sub:
                parent = _branch(sub)
            else:
                root_label = "Отчёты"
                if root_label not in self._branch_cache:
                    root = QTreeWidgetItem(self, [root_label])
                    root.setExpanded(True)
                    self._branch_cache[root_label] = root
                parent = self._branch_cache[root_label]

            meta = getattr(cls, "_report_meta", None)
            title = (
                (meta.title if meta and meta.title else None)
                or getattr(cls, "__verbose_name__", None)
                or cls.__name__
            )
            self._add_item(title, cls, parent)

    def _add_item(
        self,
        label: str,
        target: _ItemTarget,
        parent: QTreeWidgetItem | QTreeWidget,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [label])
        self._item_map[item] = target
        return item

    def refresh(self) -> None:
        self._build_tree()

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        target = self._item_map.get(item)
        if target is not None and self._on_select is not None:
            self._on_select(target)
