"""The Slack bridge — a SEPARATE long-running client process (NOT part of the FastAPI
app). It connects OUT to Slack via Socket Mode and consumes the service's fleet-wide
notification outbox over HTTP, routing each milestone to the profile it names.

Entry point: ``mission-control-slack`` (see :func:`bridge.main`).
"""

from __future__ import annotations

from .bridge import (
    ActiveProfile,
    BridgeConfigError,
    ServiceClient,
    SlackBridge,
    assemble,
    resolve_active_profiles,
)
from .cursor import CursorStore
from .message import build_digest_message, build_message, build_terminal_message

__all__ = [
    "ActiveProfile",
    "BridgeConfigError",
    "ServiceClient",
    "SlackBridge",
    "assemble",
    "resolve_active_profiles",
    "CursorStore",
    "build_message",
    "build_digest_message",
    "build_terminal_message",
]
