from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Union

from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QWidget

if TYPE_CHECKING:
    pass

_ItemTarget = Union[type, object]  # Entity class or Report class


class NavigationPanel(QTreeWidget):
    """Tree-based navigation for catalogs, documents, and reports."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self._on_select: Callable[[_ItemTarget], None] | None = None
        self._item_map: dict[QTreeWidgetItem, _ItemTarget] = {}
        self._build_tree()
        self.itemClicked.connect(self._on_item_clicked)

    def on_select(self, callback: Callable[[_ItemTarget], None]) -> None:
        self._on_select = callback

    def _build_tree(self) -> None:
        from decui.registry import get_all_entities, get_all_reports

        self.clear()
        self._item_map.clear()

        # Organise entities by section
        section_map: dict[str, list] = {"": []}
        for cls in get_all_entities():
            sec = cls._meta.section or ""
            section_map.setdefault(sec, []).append(cls)

        for sec, classes in section_map.items():
            if sec:
                root = QTreeWidgetItem(self, [sec])
            else:
                root = self
            root.setExpanded(True) if sec else None
            for cls in classes:
                self._add_item(cls._meta.verbose_name, cls, root)

        # ── Reports ──
        reports_root = QTreeWidgetItem(self, ["Отчёты"])
        reports_root.setExpanded(True)
        for cls in get_all_reports():
            meta = getattr(cls, "_report_meta", None)
            name = meta.title if meta else cls.__name__
            self._add_item(name, cls, reports_root)

    def _add_item(
        self,
        label: str,
        target: _ItemTarget,
        parent: QTreeWidgetItem,
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
