# Mission Control

An **agent-orchestration runtime**. Mission Control spawns workers, isolates each
in its own git worktree, supervises them, records per-step telemetry, and gates
their side effects behind an approval step.

Skeleton only — no runtime logic yet.

## Requirements

- Python 3.12+
- [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/)

## Setup

```sh
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Or with stock tooling:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Test

```sh
pytest
```

## Layout

```
src/mission_control/    package (src layout)
  roles.py              the metaphor vocabulary — the ONLY place metaphor terms live
tests/                  test suite
```

See `CLAUDE.MD` for build-scope conventions (the metaphor rule, worktree
isolation, telemetry fields, and the AI-DLC boundary).
