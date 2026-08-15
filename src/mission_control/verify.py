"""Verification — run the target repo's OWN checks on a burn's output, before the gate.

The go/no-go gate answers "should this land?"; verification gives that decision
EVIDENCE instead of resting it entirely on a human reading a diff. Two axes:

* **Deterministic checks** — the project's own test/build/lint commands, detected
  from the worktree (or declared by the repo). Non-zero exit is an objective red.
* **Acceptance criteria** (optional) — the plan unit's "how will we know it's done"
  statements, scored by the LLM judge (reused from :mod:`.judge`). A SOFT signal
  (the judge is noisy, see ``eval_gate``), so advisory by default: it informs the
  human, and only *enforces* a block when explicitly configured.

Design stance, matching the rest of the package:

* **Fail closed.** A red deterministic check blocks. No detectable checks is
  ``UNVERIFIED`` — a distinct state, never silently "passed". The judge failing is
  advisory only (we never block a build because the *judge* broke).
* **Agnostic.** Detection keys off well-known tooling signals only (no org/host/
  stack hardcoding); a repo overrides everything with ``.mission-control/verify.yml``.
* **Isolated + bounded.** Checks run in the disposable worktree (the same sandbox
  the worker already used) under per-check and total wall-clock caps.

This module is pure/offline-testable: it runs subprocesses and (optionally) calls
an injected judge. Wiring into the run graph lives in :mod:`.graph`.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# Verification verdicts (functional labels, not metaphor vocabulary).
VERIFY_PASSED = "passed"          # every detected check was green
VERIFY_FAILED = "failed"          # a check went red, or acceptance fell below an enforced bar
VERIFY_UNVERIFIED = "unverified"  # nothing to run — never treated as a pass
VERIFY_ERROR = "error"            # verification itself could not run (malformed config, etc.)
VERIFY_SKIPPED = "skipped"        # not applicable (a sim, or a burn that changed nothing)

# A repo declares its own checks here; it travels IN the worktree, so the checkout
# already carries it (like the AI-DLC rules do). Absent → auto-detect.
OVERRIDE_FILE = Path(".mission-control") / "verify.yml"

_TAIL_CHARS = 2000  # keep the last slice of a check's output for the report (checkpoint-safe)


@dataclass(frozen=True)
class Check:
    """One command to run in the worktree. ``setup`` runs first (deps install, etc.)."""

    name: str
    command: tuple  # argv
    setup: tuple = ()  # tuple[tuple[str, ...], ...] — bounded prep commands


@dataclass
class CheckOutcome:
    name: str
    command: str
    exit_code: int
    duration_s: float
    output_tail: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "output_tail": self.output_tail,
        }


@dataclass
class VerifyPolicy:
    """How verification behaves and how its result steers the gate. Fail-closed defaults."""

    enabled: bool = True
    # on a red deterministic check: "block" (auto no-go, fail closed) or "gate"
    # (still let a human adjudicate the red, with evidence).
    on_failed: str = "block"
    # when NO checks were detected: "gate" (require a human), "block", or "pass".
    on_unverified: str = "gate"
    stop_on_first_fail: bool = True
    per_check_timeout_s: int = 600
    wall_cap_s: int = 1800
    # Acceptance (judge) is advisory unless enforced; then a score below the bar blocks.
    acceptance_enforced: bool = False
    acceptance_threshold: float = 0.7


# -- detection --------------------------------------------------------------

def _load_override(root: Path) -> Optional[list]:
    """Parse ``.mission-control/verify.yml`` if present. Malformed → raise (→ ERROR)."""
    path = Path(root) / OVERRIDE_FILE
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    raw = data.get("checks")
    if not isinstance(raw, list):
        raise ValueError(f"{OVERRIDE_FILE}: 'checks' must be a list")
    checks: list[Check] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or "command" not in item:
            raise ValueError(f"{OVERRIDE_FILE}: check {i} needs a 'command'")
        checks.append(
            Check(
                name=str(item.get("name") or f"check-{i + 1}"),
                command=tuple(_argv(item["command"])),
                setup=tuple(tuple(_argv(s)) for s in (item.get("setup") or [])),
            )
        )
    return checks


def _argv(command) -> list:
    """A command may be a string (shell-split) or an argv list."""
    if isinstance(command, (list, tuple)):
        return [str(x) for x in command]
    return shlex.split(str(command))


def _json_has_test_script(pkg: Path) -> bool:
    import json

    try:
        return "test" in (json.loads(pkg.read_text()).get("scripts") or {})
    except (ValueError, OSError):
        return False


def _js_runner(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _make_has_test(root: Path) -> bool:
    mk = root / "Makefile"
    try:
        return any(line.startswith("test:") for line in mk.read_text().splitlines())
    except OSError:
        return False


def detect_checks(root) -> list:
    """Detect the target's own checks. An explicit override wins; else auto-detect from
    well-known tooling signals. Precision over recall: only what we're confident about."""
    root = Path(root)
    override = _load_override(root)
    if override is not None:
        return override

    checks: list[Check] = []
    pkg = root / "package.json"
    if pkg.exists() and _json_has_test_script(pkg):
        runner = _js_runner(root)
        checks.append(Check("js-test", (runner, "test", "--silent"), setup=((runner, "ci"),)))
    if any((root / f).exists() for f in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg")):
        checks.append(Check("pytest", ("python", "-m", "pytest", "-q")))
    if (root / "go.mod").exists():
        checks.append(Check("go-test", ("go", "test", "./...")))
    if (root / "Cargo.toml").exists():
        checks.append(Check("cargo-test", ("cargo", "test", "--quiet")))
    if _make_has_test(root):
        checks.append(Check("make-test", ("make", "test")))
    return checks


# -- running ----------------------------------------------------------------

def _tail(text: str) -> str:
    text = text or ""
    return text if len(text) <= _TAIL_CHARS else "…" + text[-_TAIL_CHARS:]


def _exec(argv, *, cwd: Path, timeout_s: int) -> tuple:
    """Run one command; return (exit_code, combined_output). A missing binary or a
    timeout is a non-zero result with a legible message — never an exception here."""
    try:
        proc = subprocess.run(
            list(argv), cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout_s}s: {' '.join(map(str, argv))}"


def run_check(check: Check, *, cwd, timeout_s: int) -> CheckOutcome:
    """Run a check's setup then its command in ``cwd``. First non-zero step short-circuits."""
    cwd = Path(cwd)
    start = time.monotonic()
    out_parts: list = []
    for stp in check.setup:
        code, out = _exec(stp, cwd=cwd, timeout_s=timeout_s)
        out_parts.append(f"$ {' '.join(map(str, stp))}\n{out}")
        if code != 0:
            return CheckOutcome(check.name, " ".join(map(str, check.command)), code,
                                time.monotonic() - start, _tail("\n".join(out_parts)))
    code, out = _exec(check.command, cwd=cwd, timeout_s=timeout_s)
    out_parts.append(f"$ {' '.join(map(str, check.command))}\n{out}")
    return CheckOutcome(check.name, " ".join(map(str, check.command)), code,
                        time.monotonic() - start, _tail("\n".join(out_parts)))


def run_deterministic(root, policy: VerifyPolicy) -> tuple:
    """Detect + run the target's checks. Returns (status, report) where report is a
    list of CheckOutcome dicts. No checks → (UNVERIFIED, []). A malformed override
    raises (the caller maps it to ERROR)."""
    checks = detect_checks(root)
    if not checks:
        return VERIFY_UNVERIFIED, []

    report: list = []
    status = VERIFY_PASSED
    deadline = time.monotonic() + policy.wall_cap_s
    for chk in checks:
        if time.monotonic() > deadline:
            report.append({"name": chk.name, "command": " ".join(map(str, chk.command)),
                           "exit_code": 124, "duration_s": 0.0,
                           "output_tail": "skipped: verification wall-clock cap reached"})
            status = VERIFY_FAILED
            break
        outcome = run_check(chk, cwd=root, timeout_s=policy.per_check_timeout_s)
        report.append(outcome.as_dict())
        if not outcome.ok:
            status = VERIFY_FAILED
            if policy.stop_on_first_fail:
                break
    return status, report


# -- acceptance criteria (soft signal, via the judge) -----------------------

def acceptance_rubric(criteria) -> list:
    """Turn plan acceptance-criteria strings into the judge's weighted-rubric shape."""
    return [{"criterion": str(c).strip(), "weight": 1} for c in criteria if str(c).strip()]


def overall_status(det_status: str, acceptance: Optional[dict], policy: VerifyPolicy) -> str:
    """Fold the deterministic verdict and the (optional) acceptance score into one status.

    A red/errored deterministic result always wins (fail closed). An ENFORCED acceptance
    score below the bar is also a failure. Otherwise UNVERIFIED only when nothing ran at
    all (no checks AND no acceptance signal); else PASSED (acceptance stays advisory)."""
    if det_status in (VERIFY_FAILED, VERIFY_ERROR):
        return det_status
    score = acceptance.get("score") if acceptance else None
    scored = score is not None
    if scored and policy.acceptance_enforced and score < policy.acceptance_threshold:
        return VERIFY_FAILED
    if det_status == VERIFY_UNVERIFIED and not scored:
        return VERIFY_UNVERIFIED
    return VERIFY_PASSED
