"""Live worker-activity telemetry: the per-turn progress event surfaced from inside
the run_worker step. Mirrors the step_metric contract across encode -> decode -> SSE."""

from mission_control.live import WorkerActivity, encode_worker_activity, _decode_custom
from mission_control.service.manager import _serialize


def test_worker_activity_encodes_and_decodes_round_trip():
    payload = encode_worker_activity({"turn": 2, "model": "claude-haiku-4-5", "tools": ["Edit", "Bash"], "text": "implementing slugify"})
    assert payload["type"] == "worker_activity"
    ev = _decode_custom(payload)
    assert isinstance(ev, WorkerActivity)
    assert ev.turn == 2
    assert ev.model == "claude-haiku-4-5"
    assert ev.tools == ["Edit", "Bash"]
    assert ev.text == "implementing slugify"


def test_worker_activity_serializes_to_sse_frame():
    ev = WorkerActivity(turn=1, model="m", tools=["Read"], text="looking at tests")
    frame = _serialize(ev)
    assert frame["event"] == "worker_activity"
    assert frame["data"] == {"turn": 1, "model": "m", "tools": ["Read"], "text": "looking at tests"}


def test_decode_ignores_unknown_custom_payloads():
    assert _decode_custom({"type": "something_else"}) is None
