from strings import reverse, shout


def test_shout():
    assert shout("hi") == "HI"


def test_reverse():
    assert reverse("abc") == "cba"
