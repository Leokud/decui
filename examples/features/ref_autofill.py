"""Ref с auto_fill — автоподстановка поля при выборе ссылки.

Запуск:
    cd F:/projects/decui
    python examples/features/ref_autofill.py
"""

import sys
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from decimal import Decimal
from decui import (
    catalog, document, command,
    Catalog, Document, String, Integer, Decimal as D, Ref,
)


@catalog
class Product(Catalog):
    __verbose_name__ = "Товары"
    name  = String("Название")
    price = D("Цена", min_value=0)


@document
class Order(Document):
    __verbose_name__ = "Заказы"
    product  = Ref(Product, "Товар", representation_field="name",
                   auto_fill="price")
    price    = D("Цена", read_only=True)
    quantity = Integer("Кол-во", default=1, min_value=1)
    total    = D("Итого", read_only=True, default=0)

    @command("Рассчитать")
    def calc(self):
        from decimal import Decimal as _D
        qty = self._data.get("quantity", 0)
        prc = self._data.get("price")
        if qty is None or prc is None:
            return
        self._data["total"] = _D(str(qty)) * (_D(str(prc)) if not isinstance(prc, _D) else prc)


def _seed(db):
    if db.list(Product):
        return
    db.save(Product(name="Ноутбук", price=Decimal("75000")))
    db.save(Product(name="Мышь",    price=Decimal("1500")))
    print("  Товары: Ноутбук (75000), Мышь (1500)")


def main():
    from decui.app import ERPApplication
    app = ERPApplication(db_path="autofill.db")
    _seed(app.database)
    app.run()


if __name__ == "__main__":
    main()
