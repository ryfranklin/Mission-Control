"""The verify/evaluation report is persisted on the run and surfaced in RunDetail.

The verify node already scores acceptance criteria; these lock that the report reaches
the API as `evaluation` (what Homebase's "Contrail" tracker renders).
"""

from mission_control.runs_store import RunRow
from mission_control.service.models import RunDetail


def _row(**over):
    base = dict(
        run_id="r1", thread_id="r1", target=None, task_type="burn", status="applied",
        cost_usd=0.0, created_at=None, started_at=None, ended_at=None, detail=None,
    )
    base.update(over)
    return RunRow(**base)


def test_run_detail_surfaces_verify_json_as_evaluation():
    report = {
        "checks": [{"name": "pytest", "exit_code": 0}],
        "acceptance": {
            "score": 0.86,
            "threshold": 0.7,
            "per_criterion": [{"index": 0, "score": 0.95, "reason": "ok", "statement": "slugify passes tests"}],
        },
    }
    detail = RunDetail.from_row(_row(verify_json=report))
    assert detail.evaluation == report
    assert detail.evaluation["acceptance"]["score"] == 0.86
    assert detail.evaluation["acceptance"]["per_criterion"][0]["statement"] == "slugify passes tests"


def test_run_detail_evaluation_is_none_without_verify():
    assert RunDetail.from_row(_row(task_type="sim", status="done")).evaluation is None
