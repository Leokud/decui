# DECUI — LLM Context

## Working in this repository

This file is the shared project guide for all coding agents. Keep project rules
here. Client-specific instruction files load or point to this file.

1. From the repository root, inspect `git status --short` and the diff for the
   files you plan to edit. Preserve work already in progress.
2. Use Python 3.12+ and uv. Run `uv sync --locked --inexact` to install the
   versions from `uv.lock` while preserving extra local development tools.
3. Read the relevant guide in `decui/docs/`, the public exports in
   `decui/__init__.py`, and a matching example or test before changing a feature.
4. When multiple agents work concurrently, agree on file ownership or use
   separate Git worktrees. At handoff, record the intended behavior, files
   changed, checks run and remaining work in the task or pull request.

## Project Overview

Metadata-driven desktop ERP framework. User writes only metadata (class definitions
with field descriptors) and business logic — the framework auto-generates SQL models,
forms, lists, navigation, table parts, reports, and accumulation registers.

**Tech stack:** Python 3.12+ · PySide6 · SQLAlchemy 2.0+ · SQLite · PyInstaller

## Framework capability targets

- Generate persistent CRUD, navigation, and forms for linked master data and
  documents. Support parent-child views from a record, reference choices that
  depend on other fields, conditional inputs, and related-record creation with
  defaults from the current context.
- Support resource scheduling through date and time fields, a calendar that
  opens event cards, and validation hooks for overlapping bookings, cancellations,
  and suggestions for free slots.
- Support editable line items with context-dependent defaults, recalculation of
  amounts and totals, and user-facing warnings driven by business rules.
- Keep effective-dated values by multiple keys, such as item and supplier.
  Retrieve the latest value as of a date for document defaults and reports.
  Support posting and reversal for operations that affect register balances.
- Build reports with period and reference filters, grouping, totals, charts,
  conditional formatting, and contextual opening from a record with filters set.
- Support dated activity logs, notes with attachments, recurring reminders,
  and summaries over configurable periods.
- Preserve existing records when extending a working application. Provide
  repeatable demo data and a packaged desktop executable.

Use the framework for reusable forms, persistence, and extension points. Keep
booking rules, deadlines, scoring, budgets, and pricing formulas in application
code. Check the current public API before assuming a capability listed here
is implemented.

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
├── cli.py              # decui command-line entry point
├── studio.py           # DECUI Studio UI
├── studio_project.py   # Studio project model and source handling
├── docs/               # Markdown documentation
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
- **`_table_data` dict** stores table part rows in `LineItemsCollection`

### Document command execution
1. `_widgets_to_entity()` reads form values into the entity and its table parts.
2. `Database.post()` validates the document, runs its posting command, saves it
   and replaces its register movements in one transaction.
3. Use `Database.unpost()` to reverse posting.

## Coding Conventions

Type public functions and use a leading underscore for private methods. Keep
user-facing labels and error messages in Russian. At UI boundaries, present
actionable errors and keep raw SQLAlchemy exceptions out of user messages.
Use public imports in application code and examples.

When adding a field type, update its descriptor in `decui/fields.py`, SQL
mapping in `Database._column_for_field`, editor creation and value round-trip
in `decui/ui/editors.py`, public exports, and the field guide in
`decui/docs/02-entities.md`. Check persistence and form editing together.

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
4. Read widgets before running a command so its changes are preserved on save.
5. **Movement sign:** expense resources should be stored as negatives (write_movements does this)
6. **Schema changes:** `_auto_upgrade()` applies additive changes and explicit
   renames. Verify upgrades against a database containing existing records.

## Build & Run

```bash
# Run from source
uv run python -m examples.simple

# Run DECUI Studio
uv run decui studio

# Build EXE
uv run python build_exe.py

# --clean-db explicitly deletes root-level *.db files before building
```

## Documentation and GitHub Pages

- When a change affects public APIs, CLI commands, Studio, or the application
  development workflow, update the relevant examples, `README.md`, and guides
  in `decui/docs/` as part of the same change. Keep the quickstart runnable.
- Update the affected tutorial steps, code samples, and API reference in
  `docs/index.html`. This file is the source of the GitHub Pages tutorial.
- Verify changed snippets against the current API and preview changed HTML
  locally. `.github/workflows/deploy-pages.yml` publishes `docs/` after a push to
  `master` and also supports manual runs. When publishing, verify that the
  workflow succeeds and that the changed tutorial sections work on the live site.

## Testing

```bash
# Run full automated test suite (pytest)
QT_QPA_PLATFORM=offscreen uv run --locked pytest -v

# Type checking (Pyrefly)
uv run --locked pyrefly check decui

# Import test (no display needed)
QT_QPA_PLATFORM=offscreen uv run --locked python -c "from decui import *"
```


In PowerShell, set `$env:QT_QPA_PLATFORM = "offscreen"` once, then run the
same `uv run --locked ...` commands without the shell variable prefix.

## Key Architectural Decisions

| Decision | Rationale |
|---|---|
| Entity data in `_data` dict (not SQLAlchemy model) | Decouples business logic from ORM |
| `LineItemsCollection` | List-like access to table part rows |
| Widgets → entity → DB on save | Explicit data flow, easy to debug |
| Build-time defaults (`__defaults__`) | Compiled into EXE, no external config |
| Additive schema migration and explicit renames | Preserve existing data across supported upgrades |
| Commands auto-save | Matches 1C behavior (post = save) |
| No eval/exec | PyInstaller compatible, static analysis safe |
