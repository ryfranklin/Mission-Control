def total_price(items):
    """Sum of price * qty over all line items."""
    return sum(item["price"] * item["qty"] for item in items)


def cheapest(items):
    """Return the line item with the lowest unit price."""
    return min(items, key=lambda item: item["price"])
