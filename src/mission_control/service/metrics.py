"""Shared metrics computation behind GET /metrics and the /ui/metrics dashboard.

The UI dashboard is a client of the SAME logic the JSON endpoint serves: a global
historical rollup from the DuckDB pass over the JSONL spine, plus an exact,
scoped registry rollup (per PHASE5A_FINDINGS Q2, history lives in JSONL/DuckDB +
the registry — never the SSE feed). Blocking (DuckDB + psycopg); callers run it
off the event loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import analytics


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Coerce a naive datetime (e.g. from an <input type=datetime-local>) to UTC so
    it compares cleanly against the tz-aware ``created_at`` in the registry."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def compute_metrics(
    manager,
    *,
    target: Optional[str] = None,
    created_from: Optional[datetime] = None,
    created_to: Optional[datetime] = None,
) -> dict:
    """A MetricsResponse-shaped dict: the global DuckDB rollup + a registry rollup
    narrowed to ``scope`` (target + time window) when any scope arg is given."""
    created_from, created_to = _utc(created_from), _utc(created_to)
    result = analytics.analyze()
    summary = manager.cost_summary(target=target, created_from=created_from, created_to=created_to)
    scoped = target is not None or created_from is not None or created_to is not None
    scope = None
    if scoped:
        scope = {"target": target,
                 "from": created_from.isoformat() if created_from else None,
                 "to": created_to.isoformat() if created_to else None}
    return {**result.to_dict(), "scope": scope, "runs_summary": summary}
