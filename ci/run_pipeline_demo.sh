#!/usr/bin/env bash
# Local mirror of the Jenkinsfile — demonstrates the gate → go/no-go pipeline
# without Jenkins or live infra. Same G3 gate CLI, deterministic --demo mode.
#
#   ci/run_pipeline_demo.sh clean         # green → reaches the manual go/no-go
#   ci/run_pipeline_demo.sh regression    # red  → promotion never offered
#
# Non-interactive: set AUTO_APPROVE=go (or no-go) to answer the manual gate.
set -uo pipefail

SCENARIO="${1:-clean}"
case "$SCENARIO" in
  clean)      BASELINE="ci/demo/baseline.pass.json" ;;
  regression) BASELINE="ci/demo/baseline.regressed.json" ;;
  *) echo "usage: $0 [clean|regression]"; exit 2 ;;
esac

PY="${PYTHON:-.venv/bin/python}"
ART="ci/demo/out/artifacts"
mkdir -p "$ART"

# Stage 0: Prepare (mirrors the Jenkins 'Prepare' stage)
"$PY" ci/demo/setup.py >/dev/null

echo "==================================================================="
echo " STAGE 1/2: NONPROD — G3 eval gate   (scenario: $SCENARIO)"
echo "==================================================================="
set -o pipefail
"$PY" -m mission_control.eval_gate --demo \
  --tasks ci/demo/tasks --sandbox ci/demo/sandbox \
  --baseline "$BASELINE" \
  --json "$ART/gate-result.json" --out-dir "$ART/evals" \
  | tee "$ART/gate-report.txt"
code=${PIPESTATUS[0]}
echo "[nonprod] gate exit code: $code"
echo "[nonprod] archived artifacts: $ART/gate-result.json, $ART/gate-report.txt"

if [ "$code" -ne 0 ]; then
  echo
  echo ">>> NONPROD FAILED — build is RED. PROMOTE is NOT offered. <<<"
  exit "$code"
fi
echo ">>> NONPROD PASSED — build is GREEN. <<<"

echo
echo "==================================================================="
echo " STAGE 2/2: PROMOTE — prod go/no-go (Mission Control gate)"
echo "==================================================================="
echo "Mission Control go/no-go — promote to prod? [go/no-go]"
if [ "${AUTO_APPROVE:-}" = "go" ] || [ "${AUTO_APPROVE:-}" = "no-go" ]; then
  decision="$AUTO_APPROVE"
  echo "> (non-interactive) $decision"
else
  read -r -p "> " decision
fi

if [ "$decision" = "go" ]; then
  echo "[promote] go — deploying to prod."
  exit 0
else
  echo "[promote] no-go — promotion rejected at the gate."
  exit 1
fi
