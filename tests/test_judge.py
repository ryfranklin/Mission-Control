"""LLM-as-judge tests.

Two layers:
  * offline unit tests for the pure scoring/parsing logic (no model call);
  * a LIVE meta-eval (skipped unless MC_LIVE_JUDGE=1) that hand-scores fixtures
    and asserts the judge lands within tolerance — so judge drift is detectable.
"""

from __future__ import annotations

import os

import pytest

from mission_control import pricing
from mission_control.judge import (
    DEFAULT_JUDGE_MODEL,
    JudgeError,
    LlmJudge,
    _clamp,
    _parse_json,
    _weighted_score,
)
from mission_control.sdk_worker import DEFAULT_MODEL as WORKER_MODEL

# -- default judge is a STRONGER tier than the worker -----------------------

def test_judge_defaults_to_stronger_tier_than_worker():
    assert DEFAULT_JUDGE_MODEL != WORKER_MODEL
    # "stronger" proxied by price: the judge's input price exceeds the worker's.
    judge_price = pricing.price_for(DEFAULT_JUDGE_MODEL).input_per_mtok
    worker_price = pricing.price_for(WORKER_MODEL).input_per_mtok
    assert judge_price > worker_price


# -- pure scoring logic (offline) ------------------------------------------

def test_weighted_score_uses_rubric_weights():
    rubric = [{"criterion": "a", "weight": 1}, {"criterion": "b", "weight": 3}]
    parsed = {"criteria": [{"index": 1, "score": 1.0}, {"index": 2, "score": 0.0}]}
    score, per = _weighted_score(parsed, rubric)
    assert score == pytest.approx((1 * 1 + 3 * 0) / 4)  # 0.25
    assert [c["score"] for c in per] == [1.0, 0.0]


def test_weighted_score_missing_criterion_counts_zero():
    rubric = [{"criterion": "a", "weight": 1}, {"criterion": "b", "weight": 1}]
    parsed = {"criteria": [{"index": 1, "score": 1.0}]}  # criterion 2 omitted
    score, _ = _weighted_score(parsed, rubric)
    assert score == pytest.approx(0.5)


def test_clamp_bounds_scores():
    assert _clamp(5) == 1.0
    assert _clamp(-2) == 0.0
    assert _clamp("nan-ish") == 0.0
    assert _clamp(0.42) == pytest.approx(0.42)


def test_parse_json_tolerates_fences_and_prose():
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('here is my verdict: {"a": 1} thanks') == {"a": 1}
    with pytest.raises(JudgeError):
        _parse_json("no json here at all")


def test_score_with_empty_rubric_is_a_caller_error():
    with pytest.raises(JudgeError):
        LlmJudge().score(task_prompt="t", worker_output="o", rubric=[])


# -- LIVE meta-eval: judge vs hand-scored fixtures --------------------------

# Loose on purpose: the judge is noisy; this checks for DRIFT, not exactness.
_TOLERANCE = 0.25

_RUBRIC = [
    {
        "criterion": "Accurately identifies calc.py as a small arithmetic module "
        "exposing add() and multiply().",
        "weight": 1,
    },
    {
        "criterion": "Proposes a sensible, minimal next improvement (e.g. fix the "
        "multiply bug, add tests) rather than a sweeping rewrite.",
        "weight": 1,
    },
]

_TASK_PROMPT = "Scope calc.py and propose the single most valuable next improvement."

# (label, worker_output, human_score)
_FIXTURES = [
    (
        "good",
        "calc.py is a tiny arithmetic module exposing add(a, b) and multiply(a, b). "
        "The most valuable next step is to fix multiply(), which currently returns "
        "a + b instead of a * b, and add a regression test for it.",
        0.9,
    ),
    (
        "bad",
        "calc.py is a Flask web server handling HTTP routing and user authentication. "
        "I recommend rewriting the entire project in Rust with a microservice "
        "architecture and a Kubernetes deployment.",
        0.1,
    ),
    (
        "partial",
        "calc.py is a small arithmetic module with add() and multiply(). I recommend "
        "rewriting it from scratch as a plugin-based expression engine with a DSL.",
        0.5,
    ),
]


@pytest.mark.skipif(
    not os.getenv("MC_LIVE_JUDGE"),
    reason="live judge meta-eval — set MC_LIVE_JUDGE=1 to run",
)
@pytest.mark.parametrize("label,output,human", _FIXTURES, ids=[f[0] for f in _FIXTURES])
def test_judge_matches_human_within_tolerance(label, output, human):
    judge = LlmJudge()
    verdict = judge.score(task_prompt=_TASK_PROMPT, worker_output=output, rubric=_RUBRIC)
    # The judge is not free — its usage must be captured.
    tokens = (
        verdict.usage.input_tokens
        + verdict.usage.output_tokens
        + verdict.usage.cache_read_tokens
        + verdict.usage.cache_creation_tokens
    )
    assert tokens > 0
    assert abs(verdict.score - human) <= _TOLERANCE, (
        f"[{label}] judge={verdict.score} human={human} "
        f"(> {_TOLERANCE} apart → possible judge drift): {verdict.rationale}"
    )
