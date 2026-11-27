from decimal import Decimal
import datetime
import pytest
from sqlalchemy import event

from decui import (
    Catalog,
    Date,
    Document,
    String,
    Integer,
    Decimal as D,
    Ref,
    catalog,
    document,
    tab_part,
    Database,
)


@catalog
class Category(Catalog):
    title = String("Категория", required=True)


@catalog
class ProductModel(Catalog):
    __numbering__ = {"prefix": "PRD", "field": "code", "pattern": "{prefix}-{seq:06d}"}
    code = String("Код", read_only=True)
    name = String("Название", required=True)
    price = D("Цена", precision=10, scale=2, default=Decimal("0.00"))
    category = Ref(Category, "Категория")


@document
class SaleDoc(Document):
    __numbering__ = {"prefix": "SALE", "field": "number", "pattern": "{prefix}-{seq:06d}"}
    number = String("Номер", read_only=True)
    customer = String("Покупатель")
    delivery_date = Date("Дата доставки")


    @tab_part
    class Lines:
        product = Ref(ProductModel, "Товар")
        quantity = Integer("Кол-во", default=1)
        amount = D("Сумма", precision=10, scale=2, default=Decimal("0.00"))


@pytest.mark.parametrize("read", ["list", "filter"])
def test_partial_document_cannot_replace_persisted_rows(read):
    db = Database.memory()
    saved = db.save(SaleDoc(customer="А", Lines=[{"quantity": 3}]))
    header = getattr(db, read)(SaleDoc)[0]
    assert not header.Lines
    header.customer = "Б"
    for write in (db.save, db.post):
        with pytest.raises(ValueError, match=r"db\.get\(SaleDoc"):
            write(header)
    full = db.get(SaleDoc, saved.id)
    assert full.customer == "А" and not full.is_posted
    assert full.Lines[0]["quantity"] == 3
    full.Lines.clear()
    db.save(full)
    assert not db.get(SaleDoc, saved.id).Lines


def test_filter_loads_matching_documents_and_their_rows_in_batches():
    db = Database.memory()
    day = datetime.date(2026, 11, 30)
    midnight = datetime.datetime.combine(day, datetime.time.min)
    end_of_day = datetime.datetime.combine(day, datetime.time.max)
    first = db.save(SaleDoc(date=midnight, delivery_date=day, Lines=[{"quantity": 2}, {"quantity": 3}]))
    second = db.save(SaleDoc(date=end_of_day, delivery_date=day, Lines=[{"quantity": 5}]))
    empty = db.save(SaleDoc(date=end_of_day, delivery_date=day))
    db.save(SaleDoc(date=midnight + datetime.timedelta(days=1)))
    statements = []

    def record_select(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT"):
            statements.append(statement)

    event.listen(db._engine, "before_cursor_execute", record_select)
    orders = db.filter(
        SaleDoc, date__date_range=(day, day), include_table_parts=True, order_by="-id", limit=3,
    )
    event.remove(db._engine, "before_cursor_execute", record_select)

    assert len(statements) == 2
    assert [order.id for order in orders] == [empty.id, second.id, first.id]
    assert [[line["quantity"] for line in order.Lines] for order in orders] == [[], [5], [2, 3]]
    assert [order.id for order in db.filter(SaleDoc, delivery_date__date_range=(day, None), order_by="-id")] == [
        order.id for order in orders
    ]
    assert len(db.filter(SaleDoc, date__date_range=(day, None))) == 4
    assert len(db.filter(SaleDoc, date__date_range=(None, day))) == 3
    assert len(db.filter(SaleDoc, date__date_range=(None, datetime.date.max))) == 4
    full = {order.id: order for order in SaleDoc.all(db, include_table_parts=True)}
    assert full[first.id].Lines == first.Lines
    limited = db.filter(SaleDoc, order_by="-id", limit=2, include_table_parts=True)
    assert len(limited) == 2 and limited[1].id == empty.id
    orders[-1].customer = "Б"
    db.save(orders[-1])
    assert db.get(SaleDoc, first.id).Lines == first.Lines


def test_date_range_rejects_invalid_bounds_and_non_date_fields():
    db = Database.memory()
    day = datetime.date(2026, 11, 30)
    with pytest.raises(ValueError, match="Начало периода"):
        db.filter(SaleDoc, date__date_range=(day, day - datetime.timedelta(days=1)))
    with pytest.raises(ValueError, match="две границы периода"):
        db.filter(SaleDoc, date__date_range=(datetime.datetime.now(), None))
    with pytest.raises(ValueError, match="только для полей Date и DateTime"):
        db.filter(SaleDoc, customer__date_range=(day, day))


def test_save_rolls_back_hook_changes_to_lines(monkeypatch):
    db = Database.memory()
    order = db.save(SaleDoc(Lines=[{"quantity": 3}]))
    lines = order.Lines

    def change_lines(self, db):
        self.Lines[0]["quantity"] = 7
        self.Lines.add(quantity=10)

    monkeypatch.setattr(SaleDoc, "before_save", change_lines)
    with pytest.raises(RuntimeError, match="rollback"):
        with db.transaction():
            db.save(order)
            assert [r["quantity"] for r in lines] == [7, 10]
            raise RuntimeError("rollback")
    assert order.Lines is lines
    assert [r["quantity"] for r in lines] == [3]
    assert db.get(SaleDoc, order.id).Lines == lines


def test_database_crud():
    db = Database(":memory:")
    assert db._ready

    # Create & Save Category
    cat = Category(title="Электроника")
    db.save(cat)
    assert cat._id is not None
    cat_id = cat._id

    # Get Category
    loaded_cat = db.get(Category, cat_id)
    assert loaded_cat is not None
    assert loaded_cat.title == "Электроника"

    # Save Product with numbering and Ref
    prod = ProductModel(name="Ноутбук", price=Decimal("75000.00"), category=cat_id)
    db.save(prod)
    assert prod._id is not None
    assert prod.code.startswith("PRD-")

    # Update Product
    prod.price = Decimal("72000.00")
    db.save(prod)
    updated_prod = db.get(ProductModel, prod._id)
    assert updated_prod.price == Decimal("72000.00")

    # List
    all_products = db.list(ProductModel)
    assert len(all_products) == 1
    assert all_products[0].name == "Ноутбук"

    # Save Document with Line Items
    doc = SaleDoc(customer="Иван")
    doc._table_data["Lines"] = [
        {"product": prod._id, "quantity": 1, "amount": Decimal("72000.00")},
        {"product": prod._id, "quantity": 2, "amount": Decimal("144000.00")},
    ]
    db.save(doc)
    assert doc._id is not None
    assert doc.number.startswith("SALE-")

    # Reload document with table parts
    loaded_doc = db.get(SaleDoc, doc._id)
    assert loaded_doc is not None
    assert len(loaded_doc._table_data.get("Lines", [])) == 2

    # Delete
    db.delete(loaded_doc)
    assert db.get(SaleDoc, doc._id) is None


def test_database_isolates_models_without_sa_warnings():
    import warnings
    from sqlalchemy.exc import SAWarning

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", SAWarning)
        db1 = Database.memory()
        db2 = Database.memory()
        assert db1._base is not db2._base
        assert db1._get_entity_model(Category) is not db2._get_entity_model(Category)
        assert db1._get_tp_model(SaleDoc, "Lines") is not db2._get_tp_model(SaleDoc, "Lines")

        cat1 = db1.save(Category(title="Только в первой"))
        assert db1.get(Category, cat1.id) is not None
        assert db2.list(Category) == []

        # Rebuilding models on another Database does not emit SAWarning
        Category._meta.orm_model = None
        db3 = Database.memory()
        assert db3._ready

    sa_warnings = [w for w in captured if issubclass(w.category, SAWarning)]
    assert sa_warnings == []


def test_sql_errors_translated_to_russian_without_raw_sql_or_traceback(tmp_path):
    @catalog
    class UniqueCodeItem(Catalog):
        code = String("Код", required=True, unique=True)

    db = Database.memory()
    db.save(UniqueCodeItem(code="A-01"))

    # UNIQUE violation
    with pytest.raises(ValueError) as unique_exc:
        db.save(UniqueCodeItem(code="A-01"))
    unique_msg = str(unique_exc.value)
    assert "Нарушена уникальность" in unique_msg
    assert "UNIQUE constraint" not in unique_msg
    assert "Traceback" not in unique_msg

    # NOT NULL violation (when saving without validate=True)
    with pytest.raises(ValueError) as null_exc:
        db.save(UniqueCodeItem(code=None))
    null_msg = str(null_exc.value)
    assert "обязательное поле" in null_msg
    assert "NOT NULL constraint" not in null_msg

    # FOREIGN KEY violation on RESTRICT delete
    cat = db.save(Category(title="Используемая"))
    db.save(ProductModel(name="Товар", category=cat.id))
    with pytest.raises(ValueError) as fk_exc:
        db.delete(cat)
    fk_msg = str(fk_exc.value)
    assert "ссылаются" in fk_msg
    assert "FOREIGN KEY" not in fk_msg

    # Unready Database error has no traceback or raw SQL
    bad_file = tmp_path / "corrupt.sqlite3"
    bad_file.write_text("not a sqlite file", encoding="utf-8")
    broken = Database(str(bad_file))
    assert not broken._ready
    assert broken._last_error is not None
    assert "Traceback" not in broken._last_error
    with pytest.raises(RuntimeError) as ready_exc:
        broken._check_ready()
    assert "База данных не инициализирована" in str(ready_exc.value)
    assert "Traceback" not in str(ready_exc.value)


def test_delete_document_clears_register_movements_atomically(monkeypatch):
    from decui import Dimension, LedgerEntry, Resource, get_balance, ledger

    @ledger
    class PlainDocStock:
        sku = Dimension("Артикул")
        qty = Resource("Количество", field_type=Decimal)

    @document
    class PlainMoveDoc(Document):
        sku = String("Артикул", required=True)
        qty = D("Количество", scale=2, default=Decimal("1.00"))

        def get_movements(self):
            return [LedgerEntry(
                register=PlainDocStock,
                dimensions={"sku": self.sku},
                resources={"qty": self.qty},
                is_income=True,
            )]

    db = Database.memory()
    doc = db.post(PlainMoveDoc(sku="SKU-1", qty=Decimal("5.00")))
    assert get_balance(db, PlainDocStock, sku="SKU-1")[0]["qty"] == Decimal("5.00")

    # Failure in after_delete rolls back both document delete and movement deletion
    def fail_after_delete(self, database):
        raise RuntimeError("Сбой после удаления")

    with monkeypatch.context() as patch:
        patch.setattr(PlainMoveDoc, "after_delete", fail_after_delete)
        with pytest.raises(RuntimeError, match="Сбой после удаления"):
            db.delete(doc)

    assert db.get(PlainMoveDoc, doc.id) is not None
    assert get_balance(db, PlainDocStock, sku="SKU-1")[0]["qty"] == Decimal("5.00")

    # Successful delete clears movements automatically even without permissions/owner
    db.delete(doc)
    assert db.get(PlainMoveDoc, doc.id) is None
    assert get_balance(db, PlainDocStock, sku="SKU-1")[0]["qty"] == Decimal("0.00")


def test_check_sequence_queries_all_owners_and_date_fields(monkeypatch):
    from decui import SequenceViolationError, check_sequence
    from decui.accounts import Account, current_account, set_current_account

    @document
    class OwnedSequenceDoc(Document):
        __owner_field__ = "owner"
        owner = String("Владелец")
        doc_day = Date("День")

    previous = current_account()
    try:
        db = Database.memory()
        set_current_account(Account("alice", "editor"))
        alice_later = db.post(OwnedSequenceDoc(
            date=datetime.datetime(2026, 12, 20, 12, 0),
            doc_day=datetime.date(2026, 12, 20),
        ))
        assert alice_later.owner == "alice"

        # Unposted later doc from another user must not block earlier doc
        set_current_account(Account("bob", "editor"))
        bob_draft = db.save(OwnedSequenceDoc(
            date=datetime.datetime(2026, 12, 25, 12, 0),
            doc_day=datetime.date(2026, 12, 25),
        ))
        assert not bob_draft.is_posted

        # Guard against regression back to db.list()
        def forbid_list(*_args, **_kwargs):
            raise AssertionError("check_sequence must not call db.list")

        monkeypatch.setattr(db, "list", forbid_list)

        earlier = OwnedSequenceDoc(
            date=datetime.datetime(2026, 12, 10, 12, 0),
            doc_day=datetime.date(2026, 12, 10),
        )
        # Bob does not see Alice's doc via db.list(), yet check_sequence still catches it
        with pytest.raises(SequenceViolationError):
            check_sequence(db, earlier)
        with pytest.raises(SequenceViolationError):
            check_sequence(db, earlier, date_field="doc_day")

        # Checking the already-posted document against itself succeeds
        set_current_account(Account("alice", "editor"))
        check_sequence(db, alice_later)
        check_sequence(db, alice_later, date_field="doc_day")
    finally:
        set_current_account(previous)


def test_auto_upgrade_creates_indexes_and_hashes_table_part_fields(tmp_path):
    from sqlalchemy import inspect as sa_inspect

    @document
    class UpgradeDoc(Document):
        title = String("Заголовок")

        @tab_part
        class Rows:
            note = String("Примечание")

    db_file = tmp_path / "upgrade_indexes.sqlite3"
    db1 = Database(str(db_file))
    assert db1._ready
    first_hash = db1._schema_hash()

    # Changing table part field indexed flag must change schema hash
    UpgradeDoc._meta.table_parts["Rows"].fields["note"].indexed = True
    assert db1._schema_hash() != first_hash

    # Adding indexed and unique header fields on existing table creates SQLite indexes
    code_field = String("Код", unique=True, indexed=True)
    code_field.name = "code"
    UpgradeDoc._meta.fields["code"] = code_field
    try:
        db2 = Database(str(db_file))
        assert db2._ready
        indexes = sa_inspect(db2._engine).get_indexes("upgrade_doc")
        indexed_cols = {col for idx in indexes for col in idx["column_names"]}
        assert "code" in indexed_cols
        rows_indexes = sa_inspect(db2._engine).get_indexes("upgrade_doc_rows")
        assert any("note" in idx["column_names"] for idx in rows_indexes)

        db2.save(UpgradeDoc(code="U-1"))
        with pytest.raises(ValueError, match="уникальность"):
            db2.save(UpgradeDoc(code="U-1"))
    finally:
        UpgradeDoc._meta.fields.pop("code", None)
        UpgradeDoc._meta.table_parts["Rows"].fields["note"].indexed = False


def test_seed_is_atomic_on_error():
    @catalog
    class AtomicSeedItem(Catalog):
        code = String("Код", required=True, unique=True)

    db = Database.memory()
    first = AtomicSeedItem(code="S-1")
    duplicate = AtomicSeedItem(code="S-1")
    with pytest.raises(ValueError, match="уникальность"):
        db.seed([first, duplicate])

    assert first.id is None
    assert duplicate.id is None
    assert db.list(AtomicSeedItem) == []
