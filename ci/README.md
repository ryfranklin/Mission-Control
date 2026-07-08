# CI — eval gate pipeline (Liquibase pattern)

The `Jenkinsfile` at the repo root wires the G3 eval gate into a two-stage
promotion pipeline, mirroring the Liquibase nonprod→prod flow:

| Stage | What it does |
|-------|--------------|
| **NONPROD** | Runs the G3 gate CLI (`python -m mission_control.eval_gate`) automatically. A nonzero exit (quality **or** total-cost regression) **fails the build** and blocks promotion. The JSON result + human report are archived as build artifacts. |
| **PROMOTE (prod)** | A manual `input` step — the **Mission Control go/no-go gate**, literally. Only reachable when NONPROD passed (a failed stage aborts the pipeline first). |

## Run the demo locally (no Jenkins, no live infra)

The gate runs in `--demo` mode: a deterministic StubWorker, no LLM — so the
pipeline outcome is reproducible offline. Two committed baselines drive the two
outcomes (`ci/demo/setup.py` regenerates them):

- `ci/demo/baseline.pass.json` — the demo run lands on the mean → **PASS**.
- `ci/demo/baseline.regressed.json` — same run, cost band shifted 4× down → **REGRESSION** (a synthetic cost blow-up).

```sh
# clean: NONPROD goes green → reaches the manual go/no-go gate
AUTO_APPROVE=go ci/run_pipeline_demo.sh clean

# regression: NONPROD reddens → PROMOTE is never offered
ci/run_pipeline_demo.sh regression
```

`ci/run_pipeline_demo.sh` mirrors the Jenkins stages step-for-step (same gate
CLI, same fail-on-nonzero, same manual gate). `AUTO_APPROVE=go|no-go` answers the
manual gate non-interactively; omit it to be prompted.

## Run it in Jenkins

Point a Pipeline job at the root `Jenkinsfile`. The agent needs Python with the
package installed (`pip install -e .`). Build with parameter
`DEMO_SCENARIO=clean` to go green through to the manual **go/no-go** input, or
`DEMO_SCENARIO=regression` to see NONPROD fail and PROMOTE never appear. `K` and
`N` map to the gate's `--k` / `--n` (env `MC_GATE_K` / `MC_GATE_N`).

### Optional: dockerized Jenkins

```sh
docker run -d --name jenkins -p 8080:8080 -v jenkins_home:/var/jenkins_home jenkins/jenkins:lts
# then: create a Pipeline job → "Pipeline script from SCM" → this repo → Jenkinsfile
# ensure the agent image has python3 + `pip install -e .`
```

## For a real gate (not the demo)

Drop `--demo`: the gate calls the real worker + Opus judge and compares against
`golden/baseline.json`. Re-baseline (`python -m mission_control.baseline [N]`)
whenever the worker/judge model, the golden set, or the sandbox changes.
See `docs/EVAL_GATE.md`.
