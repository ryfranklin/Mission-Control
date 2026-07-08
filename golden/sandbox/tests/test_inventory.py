from inventory import cheapest, total_price

ITEMS = [
    {"name": "widget", "price": 2.0, "qty": 3},
    {"name": "gadget", "price": 5.0, "qty": 1},
]


def test_total_price():
    assert total_price(ITEMS) == 11.0


def test_cheapest():
    assert cheapest(ITEMS)["name"] == "widget"
