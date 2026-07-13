"""The planner engine — the interactive INCEPTION walk behind ``POST /plans/{id}/turns``.

It is a **client of the seam / runtime**: it reads/writes the PLAN store, reuses
:mod:`mission_control.aidlc` for the stage catalog / question format / steering /
readiness gate, and — for a brownfield target — reuses the EXISTING run launch path
to reverse-engineer via a real read-only ``sim`` (no second code-reading path). It
adds NO orchestration to ``graph.py``. INCEPTION is read-only: the engine only reads
the target (probing for an install; investigating via the sandboxed sim) and writes
the plan.

Two modes after workspace detection:

* **greenfield** — walk the INCEPTION stages (requirements → [user stories] → workflow
  → units), each laid down as an INCEPTION unit; ready when the required stages are in.
* **brownfield** — LAUNCH a sim to reverse-engineer the codebase (summary folded into
  requirements), then LOOP requirements clarification with the operator until the
  requirements-readiness gate is green (scope, components, acceptance, well-formed
  units).

Split of responsibilities: the **engine** owns sequencing, steering, persistence, the
sim launch, and token streaming; the **brain** owns, per turn, whether the operator's
answer is sufficient plus structured extraction. A brain's ``advance`` is a generator:
it *yields* reply tokens and *returns* a :class:`StageOutcome`.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Protocol

from .. import aidlc
from ..aidlc import BrownfieldCriterion, InceptionStage, Phase
from ..plans_store import (
    ROLE_OPERATOR,
    ROLE_PLANNER,
    STATUS_READY,
    PlanRow,
    PlanStore,
    PlanTurn,
)

# A requirement with this key is the durable "user stories are warranted" signal —
# a real artifact (personas exist), not a hidden flag.
WARRANT_REQUIREMENT_KEY = "personas"

# The read-only sim prompt that reverse-engineers a brownfield target. The sim itself
# probes the target's AI-DLC install and composes it in (see aidlc + the worker).
_RE_PROMPT = (
    "Reverse-engineer this repository for an AI-DLC INCEPTION review. Read-only: do "
    "NOT modify anything. Summarize the architecture, key components, entry points, "
    "and behavior as a concise codebase summary."
)

# The planner's role, composed with the target's (or default) AI-DLC steering into the
# SDK brain's system prompt. Explicit context only — nothing auto-loaded.
_PLANNER_SYSTEM = (
    "You are the Mission Control planner. You walk an operator through the AI-DLC "
    "INCEPTION phase as an interactive, READ-ONLY planning conversation — you never "
    "modify the target repository. For the current step, decide whether the "
    "operator's answer specifies it well enough to advance; if not, ask concise "
    "clarifying questions in the AI-DLC question format. Keep replies short."
)


# -- the token/stage/done events the engine streams ------------------------

@dataclass
class TokenEvent:
    """One chunk of the planner's reply, streamed as it is produced."""

    text: str


@dataclass
class StageEvent:
    """A stage/criterion was just laid down 'in place' (or the plan reached ``ready``)."""

    stage: str
    status: str


@dataclass
class DoneEvent:
    """The turn is complete: the persisted planner reply + a plan-state snapshot."""

    turn: PlanTurn
    plan: PlanRow


EngineEvent = object  # TokenEvent | StageEvent | DoneEvent


# -- the brain contract ----------------------------------------------------

@dataclass
class StageContext:
    """What a brain needs to work the one step in play this turn. Exactly one of
    ``stage`` (an INCEPTION stage) or ``criterion`` (a brownfield gate criterion) is
    set; ``detected_mode`` is the workspace-detection result when known."""

    plan: PlanRow
    operator_content: str
    steering_text: str
    cloud_target: str
    transcript: list = field(default_factory=list)
    stage: Optional[InceptionStage] = None
    criterion: Optional[BrownfieldCriterion] = None
    detected_mode: Optional[str] = None


@dataclass
class StageOutcome:
    """A brain's structured verdict for the step (returned from ``advance``)."""

    stage_complete: bool
    mode: Optional[str] = None                       # workspace detection may set this
    requirements: list = field(default_factory=list)   # (key, value, state) to upsert
    units: list = field(default_factory=list)          # (title, Phase, depends_on) to append
    warranted: Optional[bool] = None                 # user-stories warrant (requirements stage)


class PlannerBrain(Protocol):
    """Works one step: yields reply tokens, returns a :class:`StageOutcome`."""

    def advance(self, ctx: StageContext) -> Iterator[str]:
        """A generator: ``yield`` reply tokens; ``return`` a :class:`StageOutcome`."""
        ...


class SimRunner(Protocol):
    """The runtime capability the engine reuses to reverse-engineer a target: launch a
    read-only sim and return its summary. :class:`~.manager.RunManager` implements it."""

    def run_sim(self, *, target: str, prompt: str):
        ...


# -- helpers ---------------------------------------------------------------

def tokenize(text: str) -> Iterator[str]:
    """Split text into word-ish chunks (trailing whitespace kept) for a streamed feel."""
    for chunk in re.findall(r"\S+\s*", text):
        yield chunk


def _mode_from_answer(content: str, default: str) -> str:
    """Derive greenfield/brownfield from the operator's workspace-detection answer,
    falling back to the plan's declared mode."""
    low = content.lower()
    if "brownfield" in low or "existing" in low:
        return aidlc.MODE_BROWNFIELD
    if "greenfield" in low or "new project" in low or "from scratch" in low:
        return aidlc.MODE_GREENFIELD
    return default


def _is_noncode(name: str) -> bool:
    """A repo file that does NOT count as 'existing code' for workspace detection —
    dotfiles and the usual top-level docs/metadata."""
    n = name.lower()
    if n.startswith("."):
        return True
    return any(n.startswith(p) for p in (
        "readme", "license", "licence", "changelog", "contributing",
        "code_of_conduct", "notice",
    ))


def detect_existing_code(target: Optional[str]) -> bool:
    """Whether the target repository already contains code (→ brownfield). Read-only:
    lists tracked files (falling back to a filesystem walk), ignoring docs/metadata."""
    if not target:
        return False
    root = Path(target)
    if not root.is_dir():
        return False
    files: list[str] = []
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=10,
        )
        files = [f for f in out.stdout.splitlines() if f.strip()]
    except (OSError, subprocess.SubprocessError):
        files = []
    if not files:
        files = [
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts
        ]
    return any(not _is_noncode(Path(f).name) for f in files)


# -- the stub brain (deterministic, offline) -------------------------------

class StubPlannerBrain:
    """A deterministic brain: any substantive operator answer specifies the current
    step, and each step extracts canned, well-formed artifacts. No LLM — so the walk
    (greenfield stages and the brownfield criteria loop) is fully reproducible."""

    def advance(self, ctx: StageContext) -> Iterator[str]:
        content = ctx.operator_content.strip()
        if not content:
            yield "I need a bit more detail before I can move on. "
            return StageOutcome(stage_complete=False)

        # Brownfield criterion step (the requirements-clarification loop).
        if ctx.criterion is not None:
            c = ctx.criterion
            if c.req_key is not None:
                yield f"Recorded the {c.label.lower()}. "
                return StageOutcome(
                    stage_complete=True,
                    requirements=[(c.req_key, content[:300], aidlc.REQ_READY)],
                )
            # the units criterion — emit the CONSTRUCTION work-list
            yield "Decomposing the change into a CONSTRUCTION work-list. "
            return StageOutcome(stage_complete=True, units=_construction_units(ctx.cloud_target))

        # INCEPTION stage step.
        key = ctx.stage.key if ctx.stage else ""
        if key == "workspace_detection":
            mode = ctx.detected_mode or _mode_from_answer(content, ctx.plan.mode)
            yield f"Noted — treating this as a {mode} build. "
            return StageOutcome(stage_complete=True, mode=mode)

        if key == "requirements_analysis":
            warranted = bool(
                re.search(r"\b(users?|personas?|login|ui|customers?)\b", content.lower())
            )
            reqs = [("core_problem", content[:200], aidlc.REQ_READY)]
            if warranted:
                reqs.append((WARRANT_REQUIREMENT_KEY, "identified from requirements",
                             aidlc.REQ_READY))
            yield "Captured the core requirements. "
            return StageOutcome(stage_complete=True, requirements=reqs, warranted=warranted)

        if key == "user_stories":
            yield "Recorded the primary personas and their goals. "
            return StageOutcome(
                stage_complete=True,
                requirements=[("user_goals", content[:200], aidlc.REQ_READY)],
            )

        if key == "workflow_planning":
            yield "Locked in the build sequence. "
            return StageOutcome(
                stage_complete=True,
                requirements=[("sequencing", content[:200], aidlc.REQ_READY)],
            )

        if key == "units_generation":
            yield "Decomposing into a CONSTRUCTION work-list. "
            return StageOutcome(stage_complete=True, units=_construction_units(ctx.cloud_target))

        yield "Let's continue. "
        return StageOutcome(stage_complete=False)


def _construction_units(cloud_target: str) -> list:
    """A small, well-formed CONSTRUCTION work-list (title, phase, depends_on)."""
    return [
        (f"Scaffold the project (targeting {cloud_target})", Phase.CONSTRUCTION, []),
        ("Implement the core logic", Phase.CONSTRUCTION, []),
        ("Add tests and wire CI", Phase.CONSTRUCTION, []),
    ]


# -- the real brain (LLM-driven, read-only, streaming) ---------------------

class SdkPlannerBrain:
    """The LLM planner over the Claude Agent SDK. Runs with ``setting_sources=[]`` and
    NO mutating tools, in a throwaway cwd (never the target) — INCEPTION stays
    read-only. Streams the model's prose as tokens and parses a trailing JSON block for
    the structured :class:`StageOutcome`."""

    def __init__(self, model: str = "claude-opus-4-8", max_turns: int = 4) -> None:
        self.model = model
        self.max_turns = max_turns

    def advance(self, ctx: StageContext) -> Iterator[str]:
        import asyncio

        raw = asyncio.run(self._ask(ctx))
        prose, outcome = _split_reply(raw)
        for tok in tokenize(prose):
            yield tok
        return outcome

    async def _ask(self, ctx: StageContext) -> str:
        import shutil
        import tempfile

        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        _MUTATING = ["Write", "Edit", "NotebookEdit", "Bash", "KillShell"]
        workdir = tempfile.mkdtemp(prefix="mc-planner-")
        options = ClaudeAgentOptions(
            model=self.model,
            setting_sources=[],
            system_prompt=ctx.steering_text,
            cwd=workdir,
            max_turns=self.max_turns,
            permission_mode="bypassPermissions",
            disallowed_tools=list(_MUTATING),
        )
        prompt = aidlc.apply_invocation(
            _stage_prompt(ctx), greenfield=ctx.plan.mode == aidlc.MODE_GREENFIELD
        )
        texts: list[str] = []
        result: Optional[ResultMessage] = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                elif isinstance(message, ResultMessage):
                    result = message
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return (result.result if result and result.result else "\n".join(texts)).strip()


def _stage_prompt(ctx: StageContext) -> str:
    """The per-turn instruction to the LLM planner for the current step."""
    step = ctx.criterion.label if ctx.criterion else (ctx.stage.title if ctx.stage else "")
    # The decomposition step (greenfield units-generation / brownfield units criterion)
    # MUST return a non-empty units list — the prose plan is not enough for the runtime.
    is_units_step = (ctx.stage is not None and ctx.stage.key == "units_generation") or (
        ctx.criterion is not None and ctx.criterion.req_key is None)
    units_rule = (
        "This is the DECOMPOSITION step. `units` MUST be a non-empty list. Break the "
        "work into the SMALLEST sensible steps: each unit is ONE focused change that a "
        "single engineer could finish in one sitting — roughly one module / file / "
        "concern, a handful of tool actions, NOT a whole feature. A unit like 'Build "
        "the ingestion pipeline' is TOO BIG; split it into e.g. 'add the file-watcher', "
        "'add the queue', 'add the worker loop', 'add a test'. Prefer MANY small units "
        "over a few large ones, ordered so each builds on the previous. Each will run "
        "as its own gated worker task with a bounded turn budget, so oversized units "
        "will fail. Do NOT leave `units` empty or describe the work only in prose."
        if is_units_step else
        "Emit `units` only once you are decomposing the build into concrete items; "
        "otherwise use an empty list."
    )
    example_units = (
        '["Add the config schema module", "CONSTRUCTION"], '
        '["Add the file-watcher that enqueues new files", "CONSTRUCTION"], '
        '["Write the PDF text-extraction function", "CONSTRUCTION"], '
        '["Add a unit test for extraction", "CONSTRUCTION"]'
        if is_units_step else
        '["Set up the project skeleton", "CONSTRUCTION"]'
    )
    return (
        f"Current step: {step}.\n"
        f"Cloud target: {ctx.cloud_target}.\n\n"
        f"The operator just said:\n{ctx.operator_content}\n\n"
        "Reply with a short acknowledgement. Then, on a FINAL line, emit a fenced "
        "```json block with keys: stage_complete (bool), mode (\"greenfield\"|"
        "\"brownfield\"|null), warranted (bool|null), requirements (list of [key, "
        "value, \"ready\"]), units (list of [title, \"CONSTRUCTION\"|\"INCEPTION\"]).\n"
        f"{units_rule}\n"
        '```json\n{"stage_complete": true, "mode": null, "warranted": null, '
        f'"requirements": [], "units": [{example_units}]}}```'
    )


def _split_reply(raw: str) -> tuple[str, StageOutcome]:
    """Separate the LLM's prose from its trailing JSON verdict. On any parse failure,
    keep the step open (safe default — the engine re-asks)."""
    import json

    prose, blob = raw, None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        prose = raw[: fence.start()].strip()
        blob = fence.group(1)
    elif "{" in raw and "}" in raw:
        blob = raw[raw.find("{") : raw.rfind("}") + 1]
        prose = raw[: raw.find("{")].strip()
    if not blob:
        return raw, StageOutcome(stage_complete=False)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return prose or raw, StageOutcome(stage_complete=False)

    units = []
    for u in data.get("units") or []:
        try:
            units.append((str(u[0]), Phase(u[1]), []))
        except (ValueError, IndexError, TypeError):
            continue
    reqs = []
    for r in data.get("requirements") or []:
        try:
            reqs.append((str(r[0]), str(r[1]), str(r[2]) if len(r) > 2 else aidlc.REQ_READY))
        except (IndexError, TypeError):
            continue
    return prose or raw, StageOutcome(
        stage_complete=bool(data.get("stage_complete")),
        mode=data.get("mode") or None,
        requirements=reqs,
        units=units,
        warranted=data.get("warranted"),
    )


# -- the engine ------------------------------------------------------------

class PlannerEngine:
    """Drives one operator turn: sequence the walk (greenfield stages or the brownfield
    criteria loop), compose steering, launch the reverse-engineering sim, persist, and
    stream the planner's reply tokens."""

    def __init__(
        self,
        store: PlanStore,
        *,
        brain: Optional[PlannerBrain] = None,
        sim_runner: Optional[SimRunner] = None,
    ) -> None:
        self._store = store
        self._brain: PlannerBrain = brain or StubPlannerBrain()
        self._sim = sim_runner

    def run_turn(self, plan_id: str, operator_content: str) -> Iterator[EngineEvent]:
        """A sync generator of :class:`TokenEvent` / :class:`StageEvent` /
        :class:`DoneEvent`. Persists the operator turn, works the current step, records
        the results, and persists + streams the planner reply."""
        if self._store.get_plan(plan_id) is None:
            raise KeyError(plan_id)
        self._store.append_turn(plan_id, ROLE_OPERATOR, operator_content)
        yield from self._reply_flow(plan_id, operator_content)

    def reply_for_latest(self, plan_id: str) -> Iterator[EngineEvent]:
        """Generate + stream the planner's reply to the latest operator turn WITHOUT
        appending an operator turn (it is already recorded). Used by the streaming UI,
        where the operator turn is posted first and the reply streams over a separate
        SSE connection. A no-op when the last turn is already the planner's reply — so a
        reconnect never double-drives the turn."""
        if self._store.get_plan(plan_id) is None:
            raise KeyError(plan_id)
        turns = self._store.list_turns(plan_id)
        if not turns or turns[-1].role != ROLE_OPERATOR:
            return  # nothing pending to answer
        yield from self._reply_flow(plan_id, turns[-1].content)

    def _reply_flow(self, plan_id: str, operator_content: str) -> Iterator[EngineEvent]:
        """Dispatch the pending operator turn to the mode-appropriate handler."""
        plan = self._store.get_plan(plan_id)
        completed = [u.title for u in self._store.list_units(plan_id)
                     if u.phase == Phase.INCEPTION.value]
        wd_title = aidlc.INCEPTION_STAGE_BY_KEY["workspace_detection"].title
        if wd_title not in completed:
            yield from self._workspace_detection(plan, plan_id, operator_content, completed)
        elif plan.mode == aidlc.MODE_BROWNFIELD:
            yield from self._brownfield_turn(plan, plan_id, operator_content)
        else:
            yield from self._greenfield_turn(plan, plan_id, operator_content, completed)

    # -- workspace detection (shared entry) --------------------------------

    def _workspace_detection(self, plan, plan_id, operator_content, completed):
        stage = aidlc.INCEPTION_STAGE_BY_KEY["workspace_detection"]
        detected = aidlc.MODE_BROWNFIELD if detect_existing_code(plan.target) else None
        ctx = self._ctx(plan, operator_content, stage=stage, detected_mode=detected)
        parts: list[str] = []
        outcome = yield from self._stream_brain(ctx, parts)

        if not outcome.stage_complete:
            yield from self._stream_text("\n\n" + aidlc.format_questions(stage), parts)
            yield from self._finish(plan_id, parts)
            return

        mode = detected or outcome.mode or plan.mode
        self._lay_down(plan_id, stage.title, Phase.INCEPTION)
        if mode != plan.mode:
            self._store.set_mode(plan_id, mode)
        self._store.set_stage(plan_id, stage.key)

        if mode == aidlc.MODE_BROWNFIELD:
            note = self._reverse_engineer(plan_id, plan)
            yield from self._stream_text(note, parts)
            report = self._report(plan_id, mode)
            nxt = _first_unmet(report)
            if nxt is None:
                self._store.set_status(plan_id, STATUS_READY)
                yield from self._stream_text("\n\n" + self._summary(plan_id), parts)
            else:
                yield from self._stream_text("\n\n" + aidlc.format_criterion(nxt), parts)
        else:  # greenfield → ask the requirements stage next
            nxt = aidlc.next_inception_stage(completed + [stage.title],
                                             user_stories_warranted=False)
            yield from self._stream_text("\n\n" + aidlc.format_questions(nxt), parts)

        yield from self._finish(plan_id, parts, StageEvent(stage.title, "in_place"))

    def _reverse_engineer(self, plan_id: str, plan: PlanRow) -> str:
        """Reverse-engineer the target via a real read-only sim (the existing launch
        path), folding the summary back into requirements and laying down the
        Reverse Engineering stage. Returns a one-line note for the reply."""
        self._lay_down(plan_id, aidlc.REVERSE_ENGINEERING_TITLE, Phase.INCEPTION)
        if not (self._sim and plan.target and Path(plan.target).is_dir()):
            self._store.upsert_requirement(
                plan_id, aidlc.REQ_KEY_RE_SUMMARY,
                value="(no target available to reverse-engineer)", state=aidlc.REQ_READY)
            return "No target repository was available to reverse-engineer. "
        result = self._sim.run_sim(target=plan.target, prompt=_RE_PROMPT)
        self._store.upsert_requirement(
            plan_id, aidlc.REQ_KEY_RE_SUMMARY, value=result.summary, state=aidlc.REQ_READY)
        self._store.upsert_requirement(
            plan_id, aidlc.REQ_KEY_RE_RUN, value=result.run_id, state=aidlc.REQ_READY)
        return f"Reverse-engineered the codebase via sim run {result.run_id}. "

    # -- greenfield stage walk ---------------------------------------------

    def _greenfield_turn(self, plan, plan_id, operator_content, completed):
        reqs = self._store.list_requirements(plan_id)
        stage = aidlc.next_inception_stage(completed, user_stories_warranted=_warranted(reqs))
        if stage is None:
            yield from self._closing(plan_id)
            return

        ctx = self._ctx(plan, operator_content, stage=stage)
        parts: list[str] = []
        outcome = yield from self._stream_brain(ctx, parts)
        ask_stage, stage_event = self._apply_greenfield(plan_id, stage, outcome, completed)

        appended = ("\n\n" + aidlc.format_questions(ask_stage)) if ask_stage is not None \
            else "\n\n" + self._summary(plan_id)
        yield from self._stream_text(appended, parts)
        yield from self._finish(plan_id, parts, stage_event)

    def _apply_greenfield(self, plan_id, stage, outcome, completed):
        if not outcome.stage_complete:
            return stage, None  # re-ask the same stage

        self._lay_down(plan_id, stage.title, Phase.INCEPTION)
        for key, value, state in outcome.requirements:
            self._store.upsert_requirement(plan_id, key, value=value, state=state)
        # Capture the CONSTRUCTION work-list the FIRST time the brain emits it (a real
        # LLM may volunteer `units` at whichever stage it decides the plan is concrete —
        # not always units-generation). Guarding on "already have units" dedupes so it
        # is captured exactly once, never zero-because-wrong-stage, never duplicated.
        if outcome.units and not self._has_construction_units(plan_id):
            self._append_units(plan_id, outcome.units)
        self._store.set_stage(plan_id, stage.key)

        warranted = outcome.warranted if outcome.warranted is not None \
            else _warranted(self._store.list_requirements(plan_id))
        next_stage = aidlc.next_inception_stage(
            completed + [stage.title], user_stories_warranted=warranted)
        if next_stage is None:
            self._store.set_status(plan_id, STATUS_READY)
            return None, StageEvent(stage.title, STATUS_READY)
        return next_stage, StageEvent(stage.title, "in_place")

    # -- brownfield criteria loop ------------------------------------------

    def _brownfield_turn(self, plan, plan_id, operator_content):
        current = _first_unmet(self._report(plan_id, aidlc.MODE_BROWNFIELD))
        if current is None:
            yield from self._closing(plan_id)
            return

        ctx = self._ctx(plan, operator_content, criterion=current)
        parts: list[str] = []
        outcome = yield from self._stream_brain(ctx, parts)

        stage_event = None
        if outcome.stage_complete:
            for key, value, state in outcome.requirements:
                self._store.upsert_requirement(plan_id, key, value=value, state=state)
            # Capture the work-list once, whenever the brain first emits it (dedupe on
            # "already have units") — robust to the LLM volunteering units early/late.
            if outcome.units and not self._has_construction_units(plan_id):
                self._append_units(plan_id, outcome.units)
            self._store.set_stage(plan_id, f"brownfield:{current.key}")

        nxt = _first_unmet(self._report(plan_id, aidlc.MODE_BROWNFIELD))
        if nxt is None:
            self._store.set_status(plan_id, STATUS_READY)
            yield from self._stream_text("\n\n" + self._summary(plan_id), parts)
            stage_event = StageEvent(current.label, STATUS_READY)
        else:
            ask = nxt if outcome.stage_complete else current
            yield from self._stream_text("\n\n" + aidlc.format_criterion(ask), parts)
            if outcome.stage_complete:
                stage_event = StageEvent(current.label, "in_place")
        yield from self._finish(plan_id, parts, stage_event)

    # -- shared plumbing ---------------------------------------------------

    def _ctx(self, plan, operator_content, *, stage=None, criterion=None,
             detected_mode=None) -> StageContext:
        return StageContext(
            plan=plan, operator_content=operator_content,
            steering_text=self._steering(plan), cloud_target=plan.cloud_target,
            transcript=self._store.list_turns(plan.id),
            stage=stage, criterion=criterion, detected_mode=detected_mode,
        )

    def _stream_brain(self, ctx, parts):
        """Drive the brain, yielding its tokens; return its :class:`StageOutcome`."""
        gen = self._brain.advance(ctx)
        outcome = StageOutcome(stage_complete=False)
        while True:
            try:
                tok = next(gen)
            except StopIteration as stop:
                return stop.value or outcome
            parts.append(tok)
            yield TokenEvent(tok)

    def _stream_text(self, text, parts):
        for tok in tokenize(text):
            parts.append(tok)
            yield TokenEvent(tok)

    def _finish(self, plan_id, parts, stage_event=None):
        turn = self._store.append_turn(plan_id, ROLE_PLANNER, "".join(parts))
        if stage_event is not None:
            yield stage_event
        yield DoneEvent(turn=turn, plan=self._store.get_plan(plan_id))

    def _closing(self, plan_id: str):
        yield from self._closing_turn(
            plan_id, "Planning is complete — the plan is ready to finalize.")

    def _closing_turn(self, plan_id: str, text: str):
        parts: list[str] = []
        yield from self._stream_text(text, parts)
        yield from self._finish(plan_id, parts)

    def _lay_down(self, plan_id: str, title: str, phase: Phase) -> None:
        """Record a completed stage as an INCEPTION unit ('in place')."""
        self._store.upsert_unit(plan_id, self._next_seq(plan_id), title=title, phase=phase)

    def _has_construction_units(self, plan_id: str) -> bool:
        """Whether the plan already has a CONSTRUCTION work-list (dedupe guard so the
        brain's units are captured exactly once)."""
        return any(u.phase == Phase.CONSTRUCTION.value
                   for u in self._store.list_units(plan_id))

    def _append_units(self, plan_id: str, units) -> None:
        # Chain a work-list into a sequence by default: each unit depends on the prior
        # one unless it declares its own deps. This gives the builder a real dependency
        # order (units apply one after another, not in a conflicting parallel burst).
        seq = self._next_seq(plan_id)
        prev = None
        for title, phase, deps in units:
            chained = list(deps) if deps else ([prev] if prev is not None else [])
            self._store.upsert_unit(plan_id, seq, title=title, phase=phase, depends_on=chained)
            prev = seq
            seq += 1

    def _report(self, plan_id: str, mode: str):
        units = self._store.list_units(plan_id)
        reqs = self._store.list_requirements(plan_id)
        inception = [u.title for u in units if u.phase == Phase.INCEPTION.value]
        return aidlc.readiness_report(
            mode, inception_stages=inception, requirements=reqs, units=units)

    def _steering(self, plan: PlanRow) -> str:
        """Compose the planner's system prompt from the target's AI-DLC install (if
        any), else the default steering — always under AI-DLC (the 'So default')."""
        steering = None
        if plan.target:
            root = Path(plan.target)
            if root.is_dir():
                steering = aidlc.probe(root)  # read-only probe
        if steering is None:
            steering = aidlc.default_steering()
        return aidlc.compose_system_prompt(_PLANNER_SYSTEM, steering)

    def _summary(self, plan_id: str) -> str:
        burns = [u for u in self._store.list_units(plan_id)
                 if u.phase == Phase.CONSTRUCTION.value]
        return (
            "Planning is complete. The INCEPTION work is in place and the CONSTRUCTION "
            f"work-list has {len(burns)} unit(s). The plan is ready to finalize."
        )

    def _next_seq(self, plan_id: str) -> int:
        return max((u.seq for u in self._store.list_units(plan_id)), default=-1) + 1


def _first_unmet(report) -> Optional[BrownfieldCriterion]:
    """The first unmet criterion in a brownfield report → its :class:`BrownfieldCriterion`
    (for the next question), or ``None`` when the gate is green."""
    for c in report:
        if not c.met:
            return aidlc.BROWNFIELD_CRITERION_BY_KEY.get(c.key)
    return None


def _warranted(requirements) -> bool:
    """User stories are warranted iff a personas requirement has been captured."""
    return any(r.key == WARRANT_REQUIREMENT_KEY for r in requirements)
