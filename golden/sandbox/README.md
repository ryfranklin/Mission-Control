# calc — golden-set sandbox target

A tiny arithmetic library used as the **target repo** for Phase 2 eval tasks.
Workers run against a fresh checkout of this tree in an isolated worktree.

- `add(a, b)` — correct, covered by `tests/test_calc.py` (green).
- `multiply(a, b)` — **intentional bug** (returns `a + b`), covered by
  `tests/test_multiply.py` (**failing at baseline**).

Baseline test state: **2 passing, 1 failing** (the `multiply` bug). This red
baseline is deliberate — it lets golden tasks assert both `goes_green` (fix the
bug) and `stays_green` (don't regress the passing tests).
