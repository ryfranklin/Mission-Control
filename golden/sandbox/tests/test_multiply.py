from calc import multiply


def test_multiply():
    # FAILS at baseline: multiply() has an intentional bug (returns a + b).
    # The `burn-fix-multiply-go` task fixes it → this flips to green (goes_green).
    assert multiply(3, 4) == 12
