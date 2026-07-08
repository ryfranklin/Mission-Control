"""Generate the two demo baselines used by the pipeline demo.

- baseline.pass.json:  a real StubWorker baseline (deterministic) with a small
  synthetic cost stddev injected so the band is visible/illustrative. The gate's
  demo run lands ON the mean → PASS.
- baseline.regressed.json: same shape with the cost band shifted DOWN 4x, so the
  identical demo run now looks like a cost blow-up → REGRESSION.

Run:  python ci/demo/setup.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from mission_control.baseline import Baseline, build_baseline
from mission_control.worker import StubWorker

DEMO = Path(__file__).resolve().parent
COST_STDDEV_FRAC = 0.10  # inject a 10% band so thresholds are visible in the report
REGRESS_FACTOR = 0.25    # regressed baseline's cost mean is 4x below the real run


def _inject_cost_stddev(stat: dict) -> None:
    m = stat["mean"]
    sd = round(m * COST_STDDEV_FRAC, 8)
    stat["stddev"] = sd
    stat["min"] = round(m - sd, 8)
    stat["max"] = round(m + sd, 8)


def _scale_cost(stat: dict, f: float) -> None:
    for k in ("mean", "stddev", "min", "max"):
        stat[k] = round(stat[k] * f, 8)
    stat["samples"] = [round(s * f, 8) for s in stat["samples"]]


def main() -> None:
    tasks = sorted((DEMO / "tasks").glob("*.yaml"))
    build_baseline(
        tasks,
        sandbox_src=DEMO / "sandbox",
        worker=StubWorker(),
        n=3,
        out_dir=DEMO / "out",
        baseline_path=DEMO / "baseline.pass.json",
    )

    passd = json.loads((DEMO / "baseline.pass.json").read_text())
    _inject_cost_stddev(passd["aggregate"]["cost_usd"])
    for t in passd["per_task"].values():
        _inject_cost_stddev(t["cost_usd"])
    passd["created"] = "demo-pass"
    (DEMO / "baseline.pass.json").write_text(json.dumps(passd, indent=2, sort_keys=True) + "\n")

    regressed = copy.deepcopy(passd)
    _scale_cost(regressed["aggregate"]["cost_usd"], REGRESS_FACTOR)
    for t in regressed["per_task"].values():
        _scale_cost(t["cost_usd"], REGRESS_FACTOR)
    regressed["created"] = "demo-regressed"
    (DEMO / "baseline.regressed.json").write_text(json.dumps(regressed, indent=2, sort_keys=True) + "\n")

    # sanity: both load
    Baseline.load(DEMO / "baseline.pass.json")
    Baseline.load(DEMO / "baseline.regressed.json")
    agg = passd["aggregate"]["cost_usd"]
    print(f"wrote baseline.pass.json (cost mean ${agg['mean']:.6f} ± ${agg['stddev']:.6f})")
    print(f"wrote baseline.regressed.json (cost mean ${agg['mean'] * REGRESS_FACTOR:.6f}) "
          f"→ the same run reads as a cost regression")


if __name__ == "__main__":
    main()
