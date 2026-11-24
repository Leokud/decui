import datetime
from decimal import Decimal
import pytest

from decui.fields import (
    BooleanField,
    ChoiceField,
    DateField,
    DateTimeField,
    DecimalField,
    IntegerField,
    StringField,
    TimeField,
)


def test_string_field():
    f = StringField("Имя", required=True, max_length=50)
    assert f.display_name == "Имя"
    assert f.required is True
    assert f.max_length == 50
    assert f.to_python(123) == "123"
    assert f.to_python(None) is None
    assert len(f.validate("")) == 1
    assert len(f.validate(None)) == 1
    assert len(f.validate("Valid")) == 0


def test_integer_field():
    f = IntegerField("Количество", required=True, min_value=1, max_value=100)
    assert f.to_python("42") == 42
    assert f.to_python(10) == 10
    assert f.to_python(None) is None
    assert len(f.validate(None)) == 1
    assert len(f.validate(0)) == 1  # < min_value
    assert len(f.validate(150)) == 1  # > max_value
    assert len(f.validate(50)) == 0


def test_decimal_field():
    f = DecimalField("Цена", required=True, precision=10, scale=2, min_value=Decimal("0.00"))
    assert f.to_python("12.34") == Decimal("12.34")
    assert f.to_python(15) == Decimal("15")
    assert len(f.validate(Decimal("-1.00"))) == 1
    assert len(f.validate(Decimal("100.50"))) == 0


def test_boolean_field():
    f = BooleanField("Активен", default=True)
    assert f.to_python(True) is True
    assert f.to_python(1) is True
    assert f.to_python(0) is False
    assert f.to_python(None) is None



def test_date_field():
    f = DateField("Дата", required=True)
    today = datetime.date.today()
    assert f.to_python(today) == today
    assert f.to_python("2026-05-15") == datetime.date(2026, 5, 15)
    assert len(f.validate(None)) == 1
    assert len(f.validate(today)) == 0


def test_time_field():
    f = TimeField("Время", required=True)
    t = datetime.time(14, 30)
    assert f.to_python(t) == t
    assert f.to_python("14:30") == t
    assert f.to_python(None) is None


def test_choice_field():
    choices = [("A", "Option A"), ("B", "Option B")]
    f = ChoiceField("Выбор", choices=choices)
    assert f.to_python(["A", "B"]) == "A|B"
    assert f.to_python("A|B") == "A|B"
