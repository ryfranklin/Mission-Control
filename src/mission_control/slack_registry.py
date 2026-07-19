"""The NON-SECRET Slack profile registry.

A run may opt into Slack notification by naming a *profile*. The service only ever
loads this NON-SECRET registry — profile name, channel, approvers, and the ENV-VAR
NAMES that hold each profile's tokens. It NEVER needs the token values themselves;
resolving a name to a real token is a bridge concern, out of the seam.

The registry is loaded from the path in ``MC_SLACK_REGISTRY`` (a JSON file). When
that var is unset the registry is empty: no profile validates, so a run can only be
launched with ``slack_profile=None`` (a silent run). ``None`` is always accepted —
Slack is strictly opt-in.

No profile name / channel / user id is ever hardcoded here — they come only from the
registry file, so an operator owns their fleet's Slack wiring end to end.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# The env var naming the registry JSON file. Unset → empty registry (opt-in only).
ENV_REGISTRY_PATH = "MC_SLACK_REGISTRY"

# The canonical label for the opt-out (no-Slack, silent run) choice — surfaced to
# clients so a CLI/UI can render an explicit "None" option alongside the real
# profiles. The wire value for opt-out is a null ``slack_profile``.
OPT_OUT_LABEL = "None"


class UnknownSlackProfile(ValueError):
    """A launch named a ``slack_profile`` that is not in the registry."""

    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.known = known
        known_str = ", ".join(known) if known else "(none configured)"
        super().__init__(
            f"unknown slack_profile {name!r}; known profiles: {known_str}"
        )


@dataclass(frozen=True)
class SlackProfile:
    """One NON-SECRET profile row. ``token_env`` / ``app_token_env`` are the NAMES of
    the env vars holding the profile's Slack BOT token (``xoxb-``, for posting) and
    APP-level token (``xapp-``, for the Socket Mode connection) — never the token
    values. ``approvers`` are Slack user ids allowed to resolve a gate (metadata for
    the bridge; gate interaction is not wired in this slice)."""

    name: str
    channel: Optional[str] = None
    approvers: list[str] = field(default_factory=list)
    token_env: Optional[str] = None        # env-var NAME for the bot token (xoxb-)
    app_token_env: Optional[str] = None    # env-var NAME for the app token (xapp-)

    def public(self) -> dict:
        """The non-secret view a client may see: name + channel only (NO token env
        name, NO approvers — those are bridge-side detail, and never any token)."""
        return {"name": self.name, "channel": self.channel}


@dataclass(frozen=True)
class SlackRegistry:
    """An immutable set of NON-SECRET profiles, keyed by name."""

    profiles: dict[str, SlackProfile] = field(default_factory=dict)

    # -- constructors ------------------------------------------------------

    @classmethod
    def empty(cls) -> "SlackRegistry":
        return cls(profiles={})

    @classmethod
    def from_profiles(cls, profiles: list[SlackProfile]) -> "SlackRegistry":
        return cls(profiles={p.name: p for p in profiles})

    @classmethod
    def from_mapping(cls, data: dict) -> "SlackRegistry":
        """Parse a registry mapping (``{"profiles": [ {name, channel, ...}, ... ]}``).
        Tolerates a bare list of profile dicts too."""
        raw = data.get("profiles", data) if isinstance(data, dict) else data
        profiles = [
            SlackProfile(
                name=str(entry["name"]),
                channel=entry.get("channel"),
                approvers=list(entry.get("approvers", []) or []),
                token_env=entry.get("token_env"),
                app_token_env=entry.get("app_token_env"),
            )
            for entry in (raw or [])
        ]
        return cls.from_profiles(profiles)

    @classmethod
    def from_path(cls, path: os.PathLike | str) -> "SlackRegistry":
        text = Path(path).expanduser().read_text()
        return cls.from_mapping(json.loads(text))

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "SlackRegistry":
        """Load from the file named by ``MC_SLACK_REGISTRY``; an empty registry when
        the var is unset (opt-in only)."""
        source = env if env is not None else os.environ
        path = source.get(ENV_REGISTRY_PATH)
        if not path:
            return cls.empty()
        return cls.from_path(path)

    # -- queries -----------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self.profiles)

    def get(self, name: str) -> Optional[SlackProfile]:
        return self.profiles.get(name)

    def public_profiles(self) -> list[dict]:
        """The non-secret list for ``GET /slack/profiles`` — names + channel, no
        tokens, alphabetical."""
        return [self.profiles[name].public() for name in self.names()]

    def validate(self, name: Optional[str]) -> None:
        """Accept ``None`` (a silent run) or a known profile name; otherwise raise
        :class:`UnknownSlackProfile`. Called at launch so a bad name fails early,
        before any run row is written."""
        if name is None:
            return
        if name not in self.profiles:
            raise UnknownSlackProfile(name, self.names())
