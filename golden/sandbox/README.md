# toolbox — golden-set sandbox target

**toolbox** is a small utility library used as the target repo for the eval
tasks. Workers run against a fresh checkout of this tree in an isolated worktree.

## Modules
- `calc.py` — arithmetic: `add` (correct), `multiply` (**intentional bug**: returns `a + b`).
- `strings.py` — `shout`, `reverse` (both correct).
- `inventory.py` — `total_price`, `cheapest` (both correct).

## Tests
`tests/` holds the suite. Baseline state: **6 passing, 1 failing** — the single
failing test is `tests/test_multiply.py`, pinned to the `multiply` bug. That red
baseline is deliberate: it lets a task assert `goes_green` (fix the bug) while
others assert `stays_green` (don't regress the passing tests).
