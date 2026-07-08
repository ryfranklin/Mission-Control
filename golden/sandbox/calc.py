def add(a, b):
    return a + b


def multiply(a, b):
    # BUG (intentional, for the golden set): uses addition, not multiplication.
    return a + b
