"""Каталог (Catalog) — базовый справочник.

Запуск:
    cd F:/projects/decui
    python examples/features/catalog.py
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from decimal import Decimal
from decui import catalog, Catalog, String, Decimal as D


@catalog
class Product(Catalog):
    __verbose_name__ = "Товары"
    name  = String("Наименование", required=True)
    price = D("Цена", precision=10, scale=2, required=True,
              min_value=0, currency="RUB")


def _seed(db):
    if db.list(Product):
        return
    db.save(Product(name="Кофе", price=Decimal("500")))
    db.save(Product(name="Чай",  price=Decimal("300")))
    print("  Товары: Кофе, Чай")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="catalog.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
