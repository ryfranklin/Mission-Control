"""The Mission Control metaphor vocabulary — the ONLY place metaphor terms live.

Everything else in the package uses functional names (``Worker``, ``run_task``,
``investigate``). A future metaphor swap must be a one-file change: edit here and
nowhere else. Do not hardcode a metaphor term anywhere outside this module.
"""

# Concept -> metaphor term. Functional code imports these names; it never spells
# the string literals itself.
ORCHESTRATOR = "Flight Director"  # the orchestrator
WORKER = "Controller"            # a worker
SIM = "sim"                      # a read-only task
BURN = "burn"                    # a side-effectful task
GO = "go"                        # approval / merge gate: proceed
NO_GO = "no-go"                  # approval / merge gate: reject
SCRUB = "scrub"                  # kill a task (and tear it down)
PLAN = "Flight Plan"             # a plan (the planner's durable, hand-off-able output)
PLANNER = "Flight Planner"       # the planner persona in the interactive session
