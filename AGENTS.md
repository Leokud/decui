# DECUI — LLM Context

## Project Overview

Metadata-driven desktop ERP framework. User writes only metadata (class definitions
with field descriptors) and business logic — the framework auto-generates SQL models,
forms, lists, navigation, table parts, reports, and accumulation registers.

**Tech stack:** Python 3.12+ · PySide6 · SQLAlchemy 2.0+ · SQLite · PyInstaller

## File Map

```
decui/
├── __init__.py         # Public API exports + __all__
├── fields.py           # Field descriptors (String, Integer, Decimal, Ref, Time, Choice…)
├── commands.py         # @command decorator + Command dataclass
├── entity.py           # EntityMeta (metaclass), Metadata, Entity, Catalog, Document, @tab_part
├── registry.py         # Entity and report registration (catalog, document decorators)
├── orm.py              # SQLAlchemy: model generation, CRUD, numbering, auto-schema upgrade
├── numbering.py        # Sequential counter service (1C-style patterns: {year}/{month}/{seq})
├── movement.py         # Accumulation + Information registers, Movement, get_balance
├── reports.py          # Report base, Column (with aggregate/format/prefix/suffix), DateParam…
├── sequence.py         # Document sequence, roles/permissions
├── app.py              # ERPApplication, MainWindow (nav → list → form → report)
├── docs/               # Markdown documentation (9 files)
└── ui/
    ├── editors.py      # create_editor, get/set_editor_value for all field types
    ├── form.py         # AutoForm (catalog/doc buttons), TablePartWidget (with auto-calc)
    ├── listview.py     # ListView (QTableView + CRUD buttons)
    ├── navigation.py   # NavigationPanel (tree with sections)
    ├── report_view.py  # ReportView (params form + results table + summary + totals)
    └── chart.py        # BarChart widget (QPainter)
examples/
├── simple.py           # Catalog + Document + table part + reports
├── vunderkind.py       # Ages, Circles, Teachers, ClubStart, schedule report
└── comprehensive.py    # All features combined
build_exe.py            # PyInstaller build script
```

## Architecture Rules

### Metadata flow
```
User class (Product(Catalog)) → EntityMeta.__new__() → Metadata(fields, commands, table_parts)
→ Database._init_models() → SQLAlchemy dynamic model → CREATE TABLE
→ NavigationPanel._build_tree() → tree item in nav
→ User clicks → ListView (list) or AutoForm (form)
```

### Key patterns
- **No metaclass conflicts:** `EntityMeta` is the only metaclass
- **__section__** groups entities in nav: `__section__ = "Расписание"`
- **__verbose_name__** for Russian nav labels
- **__numbering__** for auto-numbering with 1C-style patterns
- **__defaults__** for new-record defaults (callables receive `db`)
- **`_data` dict** stores field values (accessed via `__getattr__`/`__setattr__`)
- **`_table_data` dict** stores table part rows (`list[dict]`)

### Document command execution
1. `_widgets_to_entity()` — reads form widgets into entity._data + _table_data
2. `cmd.execute(entity)` — runs user-defined business logic
3. `db.save(entity)` — auto-saves entity + table parts

## Coding Conventions

### Imports
```python
# Standard / Universal ERP style:
from decui import ERPApplication, Database
from decui import master_data, transaction, action, line_items, ledger, history_register, report
from decui import MasterData, Transaction, Report, QueryReport
from decui import String, Integer, Decimal as D, Ref, Date, DateTime, Time, Choice, Boolean
from decui import Column, DateParam, RefParam, StringParam, IntegerParam, DecimalParam
from decui import LedgerEntry, Dimension, RefDimension, Metric, get_ledger_balance, get_records, get_latest, set_record

# 1C style (100% backward compatible):
from decui import catalog, document, command, tab_part, accumulation, information
from decui import Catalog, Document
from decui import Movement, Resource, get_balance
```


### Entity definition order
```python
@catalog / @document
class Name(Catalog / Document):
    __numbering__ = {"prefix": "PRD", "field": "code"}
    __verbose_name__ = "Товары"
    __section__ = "Расписание"
    __defaults__ = {"field": lambda db: value}
    
    # Fields (required=True, read_only, default=…)
    name = String("Имя", required=True)
    
    # Table parts
    @tab_part
    class Items:
        ...
    
    # Commands (auto-save after execution)
    @command("Провести")
    def post(self):
        self._data["posted"] = True
```

### Field type quick reference
| Field | Widget | Params |
|---|---|---|
| `String("n")` | QLineEdit | `max_length`, `required`, `read_only` |
| `Integer("n")` | QSpinBox | `min_value`, `max_value`, `default` |
| `Decimal("n")` | QDoubleSpinBox | `precision`, `scale`, `currency`, `min_value`, `max_value` |
| `Boolean("n")` | QCheckBox | — |
| `Date("n")` | QDateEdit | `default` (callable) |
| `DateTime("n")` | QDateTimeEdit | `default` (callable) |
| `Time("n")` | QTimeEdit | `default` (callable) |
| `Choice("n")` | checkboxes | `choices=[("VAL","Label")]` |
| `Ref(Target,"n")` | QComboBox | `representation_field`, `auto_fill` |

### Numbering patterns
```python
__numbering__ = {"prefix": "INV", "field": "number",
                 "pattern": "{prefix}-{year}-{seq:06d}"}
# Default: {seq:08d} → "00000001"
# Variables: {prefix}, {year}, {month}, {seq:Nd}
```

### Report columns
```python
Column("Сумма", "total", width=120, align="right",
       aggregate="sum", format="{:,.2f}", suffix=" руб.")
```

## Common Bugs & Pitfalls

1. **`list()` doesn't load table parts** — use `db.get(cls, id)` for full load
2. **`_on_edit` must reload** from DB: `full = db.get(type(entity), entity._id)`
3. **`populate_ref_editor` clears selection** — now fixed with `blockSignals`
4. **`_auto_save` must NOT re-read widgets** after command — use `_save_entity_as_is`
5. **Movement sign:** expense resources should be stored as negatives (write_movements does this)
6. **Schema changes:** handled automatically via hash comparison in `_auto_upgrade()`

## Build & Run

```bash
# Run from source
cd decui && python examples/simple.py

# Build EXE
python build_exe.py   # → dist/ERP/ERP.exe

# Auto-deletes old .db files on build
```

## Testing

```bash
# Run full automated test suite (pytest)
QT_QPA_PLATFORM=offscreen uv run pytest -v

# Type checking (Pyrefly)
uv run pyrefly check decui

# Import test (no display needed)
$env:QT_QPA_PLATFORM='offscreen'; python -c "from decui import *"
```


## Key Architectural Decisions

| Decision | Rationale |
|---|---|
| Entity data in `_data` dict (not SQLAlchemy model) | Decouples business logic from ORM |
| `_table_data` as `list[dict]` | Simple, no ORM for line items |
| Widgets → entity → DB on save | Explicit data flow, easy to debug |
| Build-time defaults (`__defaults__`) | Compiled into EXE, no external config |
| Schema hash auto-migration | Drops+recreates tables on schema change |
| Commands auto-save | Matches 1C behavior (post = save) |
| No eval/exec | PyInstaller compatible, static analysis safe |
