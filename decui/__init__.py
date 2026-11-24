from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .commands import Action, Command, action, command
from .entity import (
    Catalog,
    Document,
    Entity,
    LineItems,
    LineItemsCollection,
    Lookup,
    MasterData,
    Metadata,
    TablePart,
    Transaction,
    child_table,
    line_items,
    tab_part,
)
from .fields import (
    BooleanField as Boolean,
    ChoiceField as Choice,
    DateField as Date,
    DateTimeField as DateTime,
    DecimalField as Decimal,
    Field,
    IntegerField as Integer,
    RefField as Ref,
    StringField as String,
    TimeField as Time,
)
from .movement import (
    AccumulationRegister,
    Dimension,
    HistoryRegister,
    InformationRegister,
    Ledger,
    LedgerEntry,
    Metric,
    Movement,
    RefDimension,
    Register,
    RegisterMeta,
    Resource,
    ValueHistory,
    accumulation,
    balance_ledger,
    clear_movements,
    get_balance,
    get_latest,
    get_ledger_balance,
    get_records,
    get_slice_last,
    history_register,
    information,
    ledger,
    set_record,
    value_history,
    write_movements,
)
from .orm import Database
from .registry import (
    catalog,
    document,
    lookup,
    master_data,
    transaction,
)
from .reports import (
    BooleanParam,
    ChoiceParam,
    Column,
    DateParam,
    DecimalParam,
    IntegerParam,
    NumberParam,
    QueryReport,
    RefParam,
    Report,
    ReportMeta,
    ReportParam,
    StringParam,
    report,
)
from .sequence import (
    SequenceViolationError,
    check_sequence,
    current_role,
    define_role,
    has_permission,
    set_role,
)

if TYPE_CHECKING:
    from .app import ERPApplication
    from .reminders import ReminderDialog, ReminderService, celebrate_goal_completed
    from .ui.chart import BarChart

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "ERPApplication": ("decui.app", "ERPApplication"),
    "ReminderService": ("decui.reminders", "ReminderService"),
    "ReminderDialog": ("decui.reminders", "ReminderDialog"),
    "celebrate_goal_completed": ("decui.reminders", "celebrate_goal_completed"),
    "BarChart": ("decui.ui.chart", "BarChart"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        mod_name, attr_name = _LAZY_IMPORTS[name]
        import importlib
        mod = importlib.import_module(mod_name)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module 'decui' has no attribute '{name}'")


__all__ = [
    # Application & Database
    "ERPApplication",
    "Database",
    "ReminderService",
    "ReminderDialog",
    "celebrate_goal_completed",
    "BarChart",
    # Decorators (standard + aliases)
    "catalog",
    "master_data",
    "lookup",
    "document",
    "transaction",
    "command",
    "action",
    "tab_part",
    "line_items",
    "child_table",
    "report",
    "accumulation",
    "ledger",
    "balance_ledger",
    "information",
    "history_register",
    "value_history",
    # Entity base classes & metadata
    "Entity",
    "Metadata",
    "Catalog",
    "MasterData",
    "Lookup",
    "Document",
    "Transaction",
    "TablePart",
    "LineItems",
    "LineItemsCollection",
    "Command",
    "Action",
    # Field descriptors
    "Field",
    "String",
    "Integer",
    "Decimal",
    "Boolean",
    "Date",
    "DateTime",
    "Time",
    "Choice",
    "Ref",
    # Registers, ledgers and movements
    "Register",
    "RegisterMeta",
    "Ledger",
    "AccumulationRegister",
    "InformationRegister",
    "HistoryRegister",
    "ValueHistory",
    "Movement",
    "LedgerEntry",
    "write_movements",
    "clear_movements",
    "Dimension",
    "RefDimension",
    "Resource",
    "Metric",
    "get_balance",
    "get_ledger_balance",
    "get_records",
    "get_latest",
    "get_slice_last",
    "set_record",
    # Sequence & Roles
    "SequenceViolationError",
    "check_sequence",
    "define_role",
    "has_permission",
    "current_role",
    "set_role",
    # Reports and parameters
    "Report",
    "QueryReport",
    "ReportMeta",
    "ReportParam",
    "Column",
    "StringParam",
    "IntegerParam",
    "NumberParam",
    "DecimalParam",
    "BooleanParam",
    "DateParam",
    "ChoiceParam",
    "RefParam",
]
