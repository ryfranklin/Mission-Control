"""A single live feed over a durable run: node transitions + priced telemetry.

The runtime already produces two separate streams of truth about a run:

* **node transitions** — LangGraph's ``updates`` stream (which node just ran, its
  state delta, and the durable go/no-go gate when a burn pauses); and
* **priced telemetry** — :class:`~mission_control.telemetry.StepEvent` records,
  the exact lines written to the JSONL bronze spine.

This module converges them into ONE ordered async iterator of typed events by
running ``graph.astream(..., stream_mode=["updates", "custom"])``: node
transitions arrive on ``updates`` and priced telemetry arrives on ``custom``
(emitted by a node via :func:`langgraph.config.get_stream_writer`). The graph
shape is unchanged — this is purely a *view*.

This is a LIVE VIEW only. The JSONL files remain the durable historical record
and are written byte-for-byte as before (see
:func:`mission_control.telemetry.events_from_steps`); nothing here moves
telemetry into Postgres.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Optional, Union

from .telemetry import StepEvent

# Tag distinguishing our custom-stream payloads from any other custom events.
_STEP_METRIC = "step_metric"

# Sentinel node key LangGraph uses in the `updates` stream for an interrupt.
_INTERRUPT_KEY = "__interrupt__"


@dataclass
class NodeTransition:
    """A graph node just completed; ``update`` is the state delta it returned."""

    node: str
    update: Any


@dataclass
class StepMetric:
    """One priced telemetry record, surfaced live.

    ``event`` is the very same :class:`StepEvent` written to the JSONL spine, so a
    live consumer sees identical cost/token data without reading the file.
    """

    event: StepEvent


@dataclass
class GateWaiting:
    """The run durably paused at the go/no-go gate, awaiting a human decision.

    ``value`` is the interrupt payload the gate raised (task id + worker summary).
    """

    value: Any


LiveEvent = Union[NodeTransition, StepMetric, GateWaiting]


# -- custom-stream payload contract (shared by emitter + consumer) ---------

def encode_step_metric(event: StepEvent) -> dict:
    """Encode a priced :class:`StepEvent` as a custom-stream payload.

    A plain, JSON-round-trippable dict so the emitting node and the consumer
    agree on the wire shape without sharing objects.
    """
    return {"type": _STEP_METRIC, "event": asdict(event)}


def _decode_custom(payload: Any) -> Optional[LiveEvent]:
    if isinstance(payload, dict) and payload.get("type") == _STEP_METRIC:
        return StepMetric(event=StepEvent(**payload["event"]))
    return None


# -- the multiplexer -------------------------------------------------------

_STREAM_MODES = ["updates", "custom"]


def _decode_chunk(mode: str, chunk: Any):
    """One ``(mode, chunk)`` pair from LangGraph → 0+ typed :data:`LiveEvent`s.

    The shared core of both the async and sync feeds — the two differ only in how
    they iterate the graph (``astream`` vs ``stream``); the mapping is identical.
    """
    if mode == "custom":
        event = _decode_custom(chunk)
        if event is not None:
            yield event
    elif mode == "updates":
        for node, update in chunk.items():
            if node == _INTERRUPT_KEY:
                interrupts = update or ()
                yield GateWaiting(value=interrupts[0].value if interrupts else None)
            else:
                yield NodeTransition(node=node, update=update)


async def stream_run(graph, inp: Any, config: dict) -> AsyncIterator[LiveEvent]:
    """Run one leg of ``graph`` and yield a single ordered stream of typed events.

    Multiplexes the ``updates`` stream (node transitions + gate interrupt) and the
    ``custom`` stream (priced telemetry) into one iterator, preserving the order
    LangGraph produces them.

    ``inp`` is whatever a leg is started with: an initial ``RunState`` dict to begin
    a run, or a ``Command(resume=...)`` to continue a run paused at the gate. A burn
    leg ends by yielding :class:`GateWaiting`; resume it with a fresh call.

    Async; requires an async-capable checkpointer (e.g. ``MemorySaver``). For the
    sync ``PostgresSaver`` the rest of the codebase uses, drive :func:`stream_run_sync`
    in a worker thread instead.
    """
    async for mode, chunk in graph.astream(inp, config=config, stream_mode=_STREAM_MODES):
        for event in _decode_chunk(mode, chunk):
            yield event


def stream_run_sync(graph, inp: Any, config: dict):
    """Synchronous twin of :func:`stream_run`, over ``graph.stream``.

    Yields the identical ordered typed events, but works with the sync
    ``PostgresSaver`` checkpointer (whose async methods are unimplemented). The
    service drives this in a worker thread and marshals events back to its loop."""
    for mode, chunk in graph.stream(inp, config=config, stream_mode=_STREAM_MODES):
        yield from _decode_chunk(mode, chunk)
