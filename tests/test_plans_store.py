"""The PLAN store in Postgres (alongside the runs ledger + checkpoint tables).

Verifies the transcript appends in order, requirements accrete by key, and each
work-list unit's task_type is DERIVED from its phase (sim for INCEPTION, burn for
CONSTRUCTION) — never stored by hand. Skipped unless the Dockerized Postgres is
reachable."""

from __future__ import annotations

from uuid import uuid4

import pytest

from mission_control import roles
from mission_control.aidlc import Phase
from mission_control.graph import build_plans_store, postgres_checkpointer
from mission_control import plans_store as ps


@pytest.fixture
def store():
    """A set-up PLAN store over the checkpointer pool, or skip if Postgres is down."""
    try:
        _cp, pool = postgres_checkpointer(setup=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres unavailable (run `docker compose up -d`): {e}")
    plan_store = build_plans_store(pool, setup=True)
    try:
        yield plan_store
    finally:
        pool.close()


def _open(store, *, mode="greenfield", methodology="aidlc", cloud_target="aws") -> str:
    plan_id = f"plan-{uuid4().hex}"
    store.open_plan(
        plan_id, target="/tmp/target", mode=mode,
        methodology=methodology, cloud_target=cloud_target,
    )
    return plan_id


# -- header: open round-trips the given methodology/cloud ------------------

def test_open_plan_persists_header(store):
    plan_id = _open(store, methodology="aidlc", cloud_target="aws")
    row = store.get_plan(plan_id)
    assert row is not None
    assert row.mode == "greenfield"
    assert row.methodology == "aidlc"
    assert row.cloud_target == "aws"
    assert row.status == ps.STATUS_DRAFTING
    assert row.created_at is not None and row.updated_at is not None

    # Re-open is idempotent: no second row, no reset.
    store.open_plan(plan_id, target="/tmp/other", mode="brownfield",
                    methodology="x", cloud_target="y")
    again = store.get_plan(plan_id)
    assert again.mode == "greenfield" and again.methodology == "aidlc"
    assert store.count_plans({"id": plan_id}) == 1


# -- transcript: turns append in order -------------------------------------

def test_turns_append_in_order(store):
    plan_id = _open(store)
    a = store.append_turn(plan_id, ps.ROLE_OPERATOR, "first")
    b = store.append_turn(plan_id, ps.ROLE_PLANNER, "reply")
    c = store.append_turn(plan_id, ps.ROLE_OPERATOR, "second")
    assert [a.seq, b.seq, c.seq] == [0, 1, 2]

    turns = store.list_turns(plan_id)
    assert [t.seq for t in turns] == [0, 1, 2]                 # monotonic, gap-free
    assert [t.content for t in turns] == ["first", "reply", "second"]
    assert [t.role for t in turns] == [
        ps.ROLE_OPERATOR, ps.ROLE_PLANNER, ps.ROLE_OPERATOR,
    ]


# -- requirements: accrete by key (upsert, no duplicate) -------------------

def test_requirements_accrete_by_key(store):
    plan_id = _open(store)
    store.upsert_requirement(plan_id, "auth", value="tbd", state="open")
    store.upsert_requirement(plan_id, "db", value="postgres", state="ready")
    store.upsert_requirement(plan_id, "auth", value="oauth", state="ready")  # update

    reqs = {r.key: r for r in store.list_requirements(plan_id)}
    assert set(reqs) == {"auth", "db"}                         # no duplicate 'auth'
    assert reqs["auth"].value == "oauth" and reqs["auth"].state == "ready"


# -- units: task_type DERIVED from phase (sim/burn) ------------------------

def test_units_carry_task_type_per_phase(store):
    plan_id = _open(store)
    incep = store.upsert_unit(plan_id, 0, title="Requirements Analysis", phase=Phase.INCEPTION)
    const = store.upsert_unit(plan_id, 1, title="Build the API", phase=Phase.CONSTRUCTION,
                              depends_on=[0])

    assert incep.task_type == roles.SIM      # INCEPTION → read-only
    assert const.task_type == roles.BURN     # CONSTRUCTION → side-effectful
    assert const.depends_on == [0]

    # A string phase is accepted too, and re-upsert is idempotent (no dup row).
    store.upsert_unit(plan_id, 1, title="Build the API v2", phase="CONSTRUCTION", depends_on=[0])
    units = store.list_units(plan_id)
    assert [u.seq for u in units] == [0, 1]
    assert units[1].title == "Build the API v2" and units[1].task_type == roles.BURN


# -- listing: paged, filterable --------------------------------------------

def test_list_plans_paged_and_filtered(store):
    gf = _open(store, mode="greenfield")
    bf = _open(store, mode="brownfield")
    store.set_status(bf, ps.STATUS_FINALIZED)

    finalized = store.list_plans({"status": ps.STATUS_FINALIZED})
    assert bf in [p.id for p in finalized]
    assert gf not in [p.id for p in finalized]
    assert store.count_plans({"id": gf}) == 1
