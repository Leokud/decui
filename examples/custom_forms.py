"""Пример кастомных декларативных и программируемых форм в DECUI.

Показывает:
  - Client: декларативная компоновка через ``__form__ = Form(...)``
    (колонки, сворачиваемая группа, подсказки placeholder, условное поле).
  - ServiceOrder: программируемый контроллер ``@form_for(ServiceOrder)``
    на базе ``EntityForm`` с декларативным ``layout = Form(...)`` и хуками
    ``on_open``, ``on_field_changed``, ``on_table_changed``, ``build_extra``.

Запуск:
    python -m examples.custom_forms
    decui check examples/custom_forms.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from PySide6.QtWidgets import QLabel

from decui import (
    Boolean,
    Catalog,
    Choice,
    Database,
    Decimal as D,
    Document,
    EntityForm,
    ERPApplication,
    Form,
    FormButton,
    FormColumn,
    FormDivider,
    FormField,
    FormGroup,
    FormLabel,
    FormRow,
    FormTab,
    FormTable,
    FormTabs,
    Integer,
    Ref,
    String,
    catalog,
    command,
    document,
    form_for,
    tab_part,
)


@catalog
class Client(Catalog):
    __verbose_name__ = "Клиенты"
    __section__ = "Справочники"
    __numbering__ = {"prefix": "CLN", "field": "code", "pattern": "{prefix}-{seq:05d}"}

    code = String("Код", read_only=True)
    name = String("Наименование", required=True, placeholder="ООО «Ромашка» или ФИО")
    kind = Choice(
        "Тип клиента",
        choices=[("ORG", "Организация"), ("IND", "Частное лицо")],
        default="ORG",
    )
    phone = String("Телефон", placeholder="+7 (999) 000-00-00")
    email = String("Email", placeholder="info@example.com")
    inn = String("ИНН", placeholder="10 или 12 цифр")
    comment = String("Заметки", multiline=True, placeholder="Предпочтения по связи")

    __form__ = Form(
        FormLabel("Карточка клиента", style="heading"),
        FormRow(
            FormColumn("code", "kind"),
            FormColumn("name", FormField("inn", visible_when=("kind", "ORG"))),
        ),
        FormGroup("Контакты и заметки", FormRow("phone", "email"), "comment", collapsible=True),
        show_actions=True,
    )


@catalog
class ServiceCatalog(Catalog):
    __verbose_name__ = "Услуги"
    __section__ = "Справочники"
    __numbering__ = {"prefix": "SRV", "field": "code", "pattern": "{prefix}-{seq:05d}"}

    code = String("Код", read_only=True)
    name = String("Название", required=True, placeholder="Например, Диагностика")
    price = D("Базовая цена", default=Decimal("0.00"), min_value=Decimal("0"), currency="RUB")


@document
class ServiceOrder(Document):
    __verbose_name__ = "Заказ-наряды"
    __section__ = "Сервис"
    __numbering__ = {"prefix": "ORD", "field": "number", "pattern": "{prefix}-{year}-{seq:05d}"}

    client = Ref(Client, "Клиент", required=True)
    status = Choice(
        "Статус",
        choices=[("NEW", "Новый"), ("WORK", "В работе"), ("DONE", "Выполнен")],
        default="NEW",
    )
    need_delivery = Boolean("Нужен выезд мастера")
    address = String("Адрес выезда", placeholder="Город, улица, дом, офис")
    total = D("Итого", default=Decimal("0.00"), read_only=True, currency="RUB")
    notes = String("Комментарий", multiline=True, placeholder="Дополнительные условия")

    @tab_part
    class Lines:
        __verbose_name__ = "Работы и материалы"
        service = Ref(ServiceCatalog, "Услуга", required=True)
        qty = Integer("Кол-во", default=1, min_value=1, required=True)
        price = D("Цена", default=Decimal("0.00"), min_value=Decimal("0"))
        amount = D("Сумма", default=Decimal("0.00"), read_only=True)

        __auto_fill__ = {"service": "price"}
        __auto_calc__ = {"amount": "qty * price"}
        __totals__ = {"total": "sum(amount)"}

    @command("Рассчитать")
    def calc(self) -> None:
        total = Decimal("0.00")
        for row in self.Lines:
            qty = Decimal(str(row.get("qty") or 0))
            price = Decimal(str(row.get("price") or 0))
            row_sum = (qty * price).quantize(Decimal("0.01"))
            row["amount"] = row_sum
            total += row_sum
        self.total = total

    @command("Провести")
    def post(self) -> None:
        self.calc()
        if not list(self.Lines):
            raise ValueError("Добавьте хотя бы одну услугу в табличную часть.")
        self._data["posted"] = True
        self.status = "DONE"


@form_for(ServiceOrder)
class ServiceOrderForm(EntityForm):
    """Управляемая форма заказ-наряда с вкладками, сводкой и реактивными полями."""

    layout = Form(
        FormLabel("Заказ-наряд сервисного центра", style="heading"),
        FormRow(
            FormColumn("number", "date", "posted"),
            FormColumn("client", "status"),
        ),
        FormDivider(),
        FormTabs(
            FormTab(
                "Работы",
                FormTable("Lines", title="Состав работ", min_height=220),
                FormRow("total", FormButton("calc"), FormButton("post", primary=True)),
            ),
            FormTab(
                "Выезд и примечания",
                "need_delivery",
                FormField("address", visible_when=("need_delivery", True)),
                "notes",
            ),
        ),
        show_actions=True,
    )

    def build_extra(self, layout) -> None:
        self._summary_label = QLabel("")
        self._summary_label.setStyleSheet(
            "background: #eaf2fd; color: #163761; border: 1px solid #cbd6e6; border-radius: 6px; padding: 8px;"
        )
        layout.addWidget(self._summary_label)

    def on_open(self) -> None:
        self._refresh_summary()

    def on_field_changed(self, field_name: str, value: object) -> None:
        if field_name in {"need_delivery", "status", "total"}:
            self._refresh_summary()

    def on_table_changed(self, part_name: str) -> None:
        if part_name == "Lines":
            table = self.get_table("Lines")
            if table is not None:
                total = sum((Decimal(str(r.get("amount") or 0)) for r in table.get_rows()), Decimal("0.00"))
                self.set_value("total", total.quantize(Decimal("0.01")))
            self._refresh_summary()

    def _refresh_summary(self) -> None:
        label = getattr(self, "_summary_label", None)
        if label is None:
            return
        total = self.get_value("total") or Decimal("0.00")
        delivery = "с выездом мастера" if self.get_value("need_delivery") else "в сервисном центре"
        label.setText(f"Обслуживание: {delivery} · Сумма к оплате: {total} руб.")


def main() -> int:
    app = ERPApplication(title="Сервисный центр (Кастомные формы)", db_path=":memory:")
    with app.db.session() as db:
        if not db.list(Client):
            c1 = Client(name="ООО «Вектор»", kind="ORG", inn="7701234567", phone="+7 495 111-22-33")
            c2 = Client(name="Иванов Иван", kind="IND", phone="+7 926 555-44-33")
            db.save(c1)
            db.save(c2)
            s1 = ServiceCatalog(name="Диагностика оборудования", price=Decimal("1500.00"))
            s2 = ServiceCatalog(name="Профилактика и чистка", price=Decimal("2800.00"))
            db.save(s1)
            db.save(s2)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
