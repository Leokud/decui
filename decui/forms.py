"""Declarative and programmable custom forms for DECUI entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar

if TYPE_CHECKING:
    from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget
    from decui.orm import Database
    from decui.ui.form import AutoForm

TForm = TypeVar("TForm", bound="EntityForm")


@dataclass(frozen=True)
class FormItem:
    """Base node for declarative DECUI form layouts."""


def _coerce_items(items: tuple[FormItem | str, ...]) -> tuple[FormItem, ...]:
    normalized: list[FormItem] = []
    for item in items:
        if isinstance(item, str):
            normalized.append(FormField(item))
        elif isinstance(item, FormItem):
            normalized.append(item)
        else:
            raise TypeError(f"Ожидался элемент формы FormItem или имя поля, получено {type(item).__name__}")
    return tuple(normalized)


@dataclass(frozen=True)
class FormField(FormItem):
    """Single entity field editor placed in a custom form layout."""

    name: str
    label: str | None = None
    placeholder: str = ""
    read_only: bool | None = None
    stretch: int = 1
    min_width: int | None = None
    visible_when: tuple[str, bool | str] | None = None


@dataclass(frozen=True)
class FormTable(FormItem):
    """Editable table part widget placed inside a custom form layout."""

    name: str
    title: str | None = None
    min_height: int | None = None
    read_only: bool = False


@dataclass(frozen=True)
class FormButton(FormItem):
    """Action button bound to an entity @command/@action or built-in action (save, post, unpost, print, close, movements)."""

    command: str
    label: str | None = None
    primary: bool = False


@dataclass(frozen=True)
class FormLabel(FormItem):
    """Static text, section caption, or status notice inside a form."""

    text: str
    style: Literal["normal", "heading", "muted", "info", "warning"] = "normal"


@dataclass(frozen=True)
class FormDivider(FormItem):
    """Horizontal visual separator line."""


@dataclass(frozen=True)
class FormRelated(FormItem):
    """Optional placement for an entity's related list widget."""

    target: type | str = ""
    field: str = ""
    title: str | None = None


@dataclass(frozen=True)
class FormWidget(FormItem):
    """Embed a custom Qt widget produced by ``factory(controller_or_form)``."""

    factory: Callable[[Any], QWidget]


@dataclass(frozen=True)
class FormRow(FormItem):
    """Horizontal layout container arranging child elements side by side."""

    items: tuple[FormItem, ...] = field(default_factory=tuple)
    spacing: int = 12

    def __init__(self, *items: FormItem | str, spacing: int = 12) -> None:
        object.__setattr__(self, "items", _coerce_items(items))
        object.__setattr__(self, "spacing", spacing)


@dataclass(frozen=True)
class FormColumn(FormItem):
    """Vertical column container used inside FormRow or nested layouts."""

    items: tuple[FormItem, ...] = field(default_factory=tuple)
    stretch: int = 1
    spacing: int = 8

    def __init__(self, *items: FormItem | str, stretch: int = 1, spacing: int = 8) -> None:
        object.__setattr__(self, "items", _coerce_items(items))
        object.__setattr__(self, "stretch", stretch)
        object.__setattr__(self, "spacing", spacing)


@dataclass(frozen=True)
class FormGroup(FormItem):
    """Titled group box container (optionally collapsible via checkbox header)."""

    title: str
    items: tuple[FormItem, ...] = field(default_factory=tuple)
    collapsible: bool = False
    collapsed: bool = False

    def __init__(
        self,
        title: str,
        *items: FormItem | str,
        collapsible: bool = False,
        collapsed: bool = False,
    ) -> None:
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "items", _coerce_items(items))
        object.__setattr__(self, "collapsible", collapsible)
        object.__setattr__(self, "collapsed", collapsed)


@dataclass(frozen=True)
class FormTab(FormItem):
    """Single tab page inside a FormTabs container."""

    title: str
    items: tuple[FormItem, ...] = field(default_factory=tuple)

    def __init__(self, title: str, *items: FormItem | str) -> None:
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "items", _coerce_items(items))


@dataclass(frozen=True)
class FormTabs(FormItem):
    """Tabbed container holding one or more FormTab pages."""

    tabs: tuple[FormTab, ...] = field(default_factory=tuple)

    def __init__(self, *tabs: FormTab) -> None:
        for tab in tabs:
            if not isinstance(tab, FormTab):
                raise TypeError(f"FormTabs принимает только FormTab, получено {type(tab).__name__}")
        object.__setattr__(self, "tabs", tabs)


@dataclass(frozen=True)
class Form:
    """Declarative form layout specification attached to an entity via ``__form__``."""

    items: tuple[FormItem, ...] = field(default_factory=tuple)
    title: str | None = None
    include_remaining: bool = False
    spacing: int = 12
    show_actions: bool = True

    def __init__(
        self,
        *items: FormItem | str,
        title: str | None = None,
        include_remaining: bool = False,
        spacing: int = 12,
        show_actions: bool = True,
    ) -> None:
        object.__setattr__(self, "items", _coerce_items(items))
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "include_remaining", include_remaining)
        object.__setattr__(self, "spacing", spacing)
        object.__setattr__(self, "show_actions", show_actions)

    def iter_items(self) -> tuple[FormItem, ...]:
        """Return all FormItem nodes depth-first (including nested children)."""
        collected: list[FormItem] = []

        def visit(node: FormItem) -> None:
            collected.append(node)
            if isinstance(node, (FormRow, FormColumn, FormGroup, FormTab)):
                for child in node.items:
                    visit(child)
            elif isinstance(node, FormTabs):
                for tab in node.tabs:
                    visit(tab)

        for root_item in self.items:
            visit(root_item)
        return tuple(collected)

    def placed_fields(self) -> set[str]:
        return {node.name for node in self.iter_items() if isinstance(node, FormField)}

    def placed_tables(self) -> set[str]:
        return {node.name for node in self.iter_items() if isinstance(node, FormTable)}


class EntityForm:
    """Programmable form controller for custom entity forms and event hooks.

    Attach to an entity with ``__form__ = MyForm``, ``__form_class__ = MyForm``,
    or the ``@form_for(EntityClass)`` decorator.
    """

    layout: ClassVar[Form | None] = None
    entity_class: ClassVar[type | None] = None

    def __init__(self, form: AutoForm, db: Database) -> None:
        self.form = form
        self.db = db

    @property
    def entity(self) -> Any:
        """Return the entity currently loaded in the form."""
        return self.form.entity

    @property
    def is_new(self) -> bool:
        """Return True when creating a record that has not been saved yet."""
        ent = self.form.entity
        return ent is None or getattr(ent, "_id", None) is None

    def get_value(self, field_name: str) -> Any:
        """Read current editor value (or entity field value if not rendered)."""
        return self.form.get_field_value(field_name)

    def set_value(self, field_name: str, value: Any) -> None:
        """Update editor widget value and backing entity data."""
        self.form.set_field_value(field_name, value)

    def get_widget(self, field_name: str) -> QWidget | None:
        """Return the underlying Qt editor widget for a field."""
        return self.form.get_field_widget(field_name)

    def get_table(self, part_name: str) -> Any | None:
        """Return the table part widget for ``part_name``."""
        return self.form.get_table_widget(part_name)

    def set_visible(self, field_name: str, visible: bool) -> None:
        """Explicitly override visibility of a field control on the form."""
        self.form.set_field_visible(field_name, visible)

    def set_enabled(self, field_name: str, enabled: bool) -> None:
        """Enable or disable a field editor on the form."""
        self.form.set_field_enabled(field_name, enabled)

    def set_label(self, field_name: str, text: str) -> None:
        """Update the label text displayed next to a field editor."""
        self.form.set_field_label(field_name, text)

    def refresh(self) -> None:
        """Reload reference lists and dependent data."""
        self.form.refresh_related_data()

    def save(self) -> bool:
        """Validate and save the current record."""
        return self.form._save()

    def post(self) -> bool:
        """Validate and post the current document."""
        return self.form._run_post()

    def close(self) -> None:
        """Request closing the form (prompting if dirty)."""
        self.form._request_close()

    def show_message(self, text: str, *, title: str = "Сообщение") -> None:
        """Display an informational message box parented to the form."""
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.information(self.form, title, text)

    def show_warning(self, text: str, *, title: str = "Предупреждение") -> None:
        """Display a warning message box parented to the form."""
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.warning(self.form, title, text)

    def create_field_widget(
        self,
        field_name: str,
        *,
        label: str | None = None,
        placeholder: str = "",
        read_only: bool | None = None,
    ) -> tuple[QLabel, QWidget]:
        """Create and register a DECUI field editor pair for use inside custom ``build()``."""
        return self.form.create_field_editor_pair(
            field_name, label=label, placeholder=placeholder, read_only=read_only,
        )

    def create_table_widget(
        self,
        part_name: str,
        *,
        title: str | None = None,
        min_height: int | None = None,
        read_only: bool = False,
    ) -> QWidget:
        """Create and register a DECUI table part widget for use inside custom ``build()``."""
        return self.form.create_table_editor(
            part_name, title=title, min_height=min_height, read_only=read_only,
        )

    def create_action_button(
        self,
        command: str,
        *,
        label: str | None = None,
        primary: bool = False,
    ) -> QPushButton:
        """Create a button wired to an entity command or built-in action."""
        return self.form.create_form_button(command, label=label, primary=primary)

    # ── Overridable lifecycle hooks ──

    def build(self, container: QWidget) -> bool:
        """Override and return True to build a completely custom Qt layout inside ``container``."""
        return False

    def build_extra(self, layout: QVBoxLayout) -> None:
        """Override to append custom Qt widgets into the form body."""

    def on_open(self) -> None:
        """Called whenever a new or existing entity is loaded into the form."""

    def on_field_changed(self, field_name: str, value: Any) -> None:
        """Called when the user changes a field editor value."""

    def on_table_changed(self, part_name: str) -> None:
        """Called when rows in a table part are added, deleted, or pasted."""

    def on_before_save(self) -> bool | None:
        """Called before save/post; return False to abort saving."""
        return None


CustomForm = EntityForm


def form_for(entity_class: type) -> Callable[[type[TForm]], type[TForm]]:
    """Class decorator binding an ``EntityForm`` subclass to an entity class."""

    def decorator(form_cls: type[TForm]) -> type[TForm]:
        if not isinstance(form_cls, type) or not issubclass(form_cls, EntityForm):
            raise TypeError("@form_for должен применяться к подклассу EntityForm")
        form_cls.entity_class = entity_class
        setattr(entity_class, "__form_class__", form_cls)
        if getattr(entity_class, "__form__", None) is None:
            setattr(entity_class, "__form__", form_cls)
        return form_cls

    return decorator


def resolve_form_config(entity_class: type) -> tuple[Form | None, type[EntityForm] | None]:
    """Resolve declarative Form layout and/or EntityForm controller class for an entity."""
    controller_cls: type[EntityForm] | None = getattr(entity_class, "__form_class__", None)
    raw_form = getattr(entity_class, "__form__", None)
    if isinstance(raw_form, type) and issubclass(raw_form, EntityForm):
        if controller_cls is None:
            controller_cls = raw_form
        raw_form = getattr(raw_form, "layout", None)
    elif controller_cls is not None and raw_form is None:
        raw_form = getattr(controller_cls, "layout", None)
    form_spec = raw_form if isinstance(raw_form, Form) else None
    return form_spec, controller_cls
