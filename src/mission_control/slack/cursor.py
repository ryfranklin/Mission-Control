"""A durable GLOBAL cursor for the bridge's at-least-once consume position.

The outbox ``seq`` is global (across all runs), so the bridge tracks ONE integer: the
highest seq it has fully handled (posted or deliberately skipped). It's persisted to a
small local file after each handled notification, so a restart resumes just past it —
re-sending at most the one in-flight seq, never dropping one.

Kept deliberately tiny (a single int in a file); no server round-trip, no shared state
between machines — each box owns its own cursor.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Where the cursor file lives when not given explicitly.
ENV_CURSOR_PATH = "MC_SLACK_CURSOR"
_DEFAULT_PATH = "~/.mission-control/slack-cursor"


class CursorStore:
    """The bridge's durable consume position — a single global seq in a local file."""

    def __init__(self, path: os.PathLike | str) -> None:
        self._path = Path(path).expanduser()

    @classmethod
    def from_env(cls, env: dict | None = None) -> "CursorStore":
        source = env if env is not None else os.environ
        return cls(source.get(ENV_CURSOR_PATH) or _DEFAULT_PATH)

    def get(self) -> int:
        """The last fully-handled seq, or ``0`` when there's no cursor yet (start from
        the beginning of the outbox). A corrupt file is a hard error — better to fail
        fast than silently re-send the entire outbox."""
        try:
            text = self._path.read_text().strip()
        except FileNotFoundError:
            return 0
        if not text:
            return 0
        return int(text)  # ValueError on corruption → surfaces loudly

    def set(self, seq: int) -> None:
        """Persist ``seq`` as the new cursor, atomically (write-temp + os.replace) so a
        crash mid-write can never leave a half-written cursor."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".cursor-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(str(int(seq)))
            os.replace(tmp, self._path)
        except BaseException:
            # Never leave the temp file behind on failure.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
