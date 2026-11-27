"""Покупатель и адрес доставки. Запуск: python -m examples.features.dependent_refs."""

from decui import Catalog, Document, ERPApplication, Ref, String, catalog, document


@catalog
class Customer(Catalog):
    __verbose_name__ = "Покупатели"
    name = String("Имя", required=True)


@catalog
class Address(Catalog):
    __verbose_name__ = "Адреса"
    owner = Ref(Customer, "Покупатель", required=True, related_list="Адреса покупателя")
    name = String("Адрес", required=True)


@document
class Order(Document):
    __verbose_name__ = "Заказы"
    customer = Ref(Customer, "Покупатель", required=True)
    address = Ref(Address, "Адрес доставки", choice_filter={"owner": "customer"})

    def before_save(self, db):
        if self.address is None:
            return
        address = db.get(Address, self.address)
        if address is None or address.owner != self.customer:
            raise ValueError("Выберите адрес доставки этого покупателя.")


def seed(db):
    first = db.save(Customer(name="Анна"))
    second = db.save(Customer(name="Борис"))
    db.save(Address(owner=first.id, name="Лесная, 1"))
    db.save(Address(owner=second.id, name="Полевая, 2"))


if __name__ == "__main__":
    app = ERPApplication(title="Доставка заказов", app_id="delivery-example", start=Order, first_run=seed)
    raise SystemExit(app.run())
