import datetime
from decimal import Decimal
import pytest

from decui import (
    Database,
    Dimension,
    Resource,
    Metric,
    Movement,
    LedgerEntry,
    accumulation,
    ledger,
    information,
    history_register,
    get_balance,
    get_ledger_balance,
    get_records,
    get_latest,
    set_record,
    clear_movements,
    write_movements,
)


@ledger
class StockRegister:
    item = Dimension("Товар")
    warehouse = Dimension("Склад")
    quantity = Metric("Количество", field_type=Decimal)


@history_register
class PriceHistory:
    item = Dimension("Товар")
    price = Resource("Цена", field_type=Decimal)


def test_balance_ledger():
    db = Database(":memory:")
    today = datetime.date(2026, 5, 20)

    # Приход 100 на Склад-1
    write_movements(db, [
        LedgerEntry(
            register=StockRegister,
            period=today,
            dimensions={"item": "Widget-A", "warehouse": "Main"},
            resources={"quantity": Decimal("100")},
            is_income=True,
            doc_id=1,
            doc_type="Receipt",
        )
    ])

    # Расход 30 со Склада-1
    write_movements(db, [
        Movement(
            register=StockRegister,
            period=today,
            dimensions={"item": "Widget-A", "warehouse": "Main"},
            resources={"quantity": Decimal("30")},
            is_income=False,
            doc_id=2,
            doc_type="Shipment",
        )
    ])

    # Проверка остатка: 100 - 30 = 70
    balance = get_ledger_balance(db, StockRegister, group_by=["item", "warehouse"])
    assert len(balance) == 1
    assert balance[0]["item"] == "Widget-A"
    assert balance[0]["warehouse"] == "Main"
    assert balance[0]["quantity"] == 70.0

    # Очистка движений документа 2
    clear_movements(db, StockRegister, doc_id=2, doc_type="Shipment")
    balance_after = get_balance(db, StockRegister, group_by=["item"])
    assert balance_after[0]["quantity"] == 100.0


def test_history_register_and_queries():
    db = Database(":memory:")

    # Запись через set_record
    set_record(
        db,
        PriceHistory,
        dimensions={"item": "Gadget-X"},
        resources={"price": Decimal("99.90")},
    )
    set_record(
        db,
        PriceHistory,
        dimensions={"item": "Gadget-X"},
        resources={"price": Decimal("109.90")},
    )

    # get_records
    records = get_records(db, PriceHistory, item="Gadget-X")
    assert len(records) == 2

    # get_latest (срез последних)
    latest = get_latest(db, PriceHistory, item="Gadget-X")
    assert latest is not None
    assert latest["item"] == "Gadget-X"
    assert Decimal(str(latest["price"])) == Decimal("109.90")


def test_register_queries_validate_ready_and_filters(tmp_path):
    bad_file = tmp_path / "not_a_db.sqlite3"
    bad_file.write_text("not sqlite", encoding="utf-8")
    broken_db = Database(str(bad_file))
    assert not broken_db._ready

    for fn in (get_balance, get_ledger_balance, get_records, get_latest):
        with pytest.raises(RuntimeError, match="База данных не инициализирована"):
            fn(broken_db, StockRegister)

    db = Database.memory()
    with pytest.raises(ValueError, match="Неизвестное измерение"):
        get_balance(db, StockRegister, missing_dim="A")
    with pytest.raises(ValueError, match="Неизвестное измерение"):
        get_ledger_balance(db, StockRegister, missing_dim="A")
    with pytest.raises(ValueError, match="Неизвестное измерение группировки"):
        get_balance(db, StockRegister, group_by=["missing_dim"])
    with pytest.raises(ValueError, match="Неизвестное измерение"):
        get_records(db, PriceHistory, missing_dim="A")
    with pytest.raises(ValueError, match="Неизвестное измерение"):
        get_latest(db, PriceHistory, missing_dim="A")


def test_multiple_databases_isolate_register_models():
    db1 = Database.memory()
    db2 = Database.memory()
    assert db1._get_register_model(StockRegister) is not db2._get_register_model(StockRegister)

    write_movements(db1, [
        Movement(
            register=StockRegister,
            period=datetime.date(2026, 6, 1),
            dimensions={"item": "OnlyIn1", "warehouse": "W1"},
            resources={"quantity": Decimal("5")},
            is_income=True,
            doc_id=1,
            doc_type="Receipt",
        )
    ])
    assert get_balance(db1, StockRegister)[0]["quantity"] == Decimal("5.00")
    assert get_balance(db2, StockRegister)[0]["quantity"] == Decimal("0.00")
