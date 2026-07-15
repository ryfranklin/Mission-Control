"""Compose a worker's system-prompt steering for ONE AI-DLC v2 stage.

Mission Control runs v2 as **content only**: a stage-unit worker is steered by that
stage's own protocol (the Markdown body of its stage file) plus the stage's
``lead_agent`` definition and that agent's ``knowledge/`` — and nothing more. It is
NOT the whole methodology, and NOT the generic worker prompt.

The vendored stage/agent/knowledge text was written for v2's own runtime, so it tells
the model to run ``aidlc-*.ts`` tools, ``bun .claude/tools/*``, and hooks, and to spawn
subagents. Those are intentionally ABSENT here (we vendored no hooks/tools; the worker
runs with ``setting_sources=[]``). So the composed steering ends with a loud
Mission-Control override that disables all of that: MC owns orchestration, state, audit
logging, and the go/no-go gate.
"""

from __future__ import annotations

from pathlib import Path

from .catalog import StageSpec


def _strip_frontmatter(text: str) -> str:
    """Drop a leading ``---`` YAML frontmatter block, returning just the Markdown body."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            return text[nl + 1:].lstrip("\n") if nl != -1 else ""
    return text


def _read_body(path: Path) -> str:
    """Read a vendored Markdown file's body (frontmatter stripped), or '' if absent."""
    if not path.is_file():
        return ""
    return _strip_frontmatter(path.read_text(encoding="utf-8", errors="ignore")).strip()


def _agent_section(catalog_root: Path, lead_agent: str | None) -> str:
    """The lead agent's definition + all of its ``knowledge/<agent>/*.md``. Empty when
    the stage has no file-backed agent (e.g. ``orchestrator``)."""
    if not lead_agent:
        return ""
    agent_body = _read_body(catalog_root / "agents" / f"{lead_agent}.md")
    if not agent_body:
        return ""  # not a file-backed v2 agent (e.g. the orchestrator) → no steering
    parts = [f"### Lead agent: {lead_agent}", "", agent_body]

    knowledge_dir = catalog_root / "knowledge" / lead_agent
    files = sorted(knowledge_dir.glob("*.md")) if knowledge_dir.is_dir() else []
    if files:
        parts += ["", f"### Agent knowledge ({lead_agent})"]
        for f in files:
            body = _read_body(f)
            if body:
                parts += ["", f"--- knowledge/{lead_agent}/{f.name} ---", "", body]
    return "\n".join(parts)


def compose_stage_prompt(stage: StageSpec, catalog_root: Path) -> str:
    """Build the worker system-prompt steering for executing exactly ``stage``.

    Sources (and ONLY these): the stage file's Markdown body (its protocol steps), the
    ``lead_agent`` definition, and that agent's ``knowledge/`` files. Ends with the
    Mission-Control override that forbids v2's ``.ts`` tools / hooks / subagents.

    The output rule is keyed on ``stage.kind``: a ``sim`` stage writes only its
    ``produces:`` docs under ``aidlc-docs/`` (read-only design/analysis), while a
    ``burn`` stage's PRIMARY deliverable is real source/config/tests in the working
    directory — the ``produces:`` are the accompanying plan/summary docs. (Without this
    split a burn like ``code-generation`` produces design docs, not code.)
    """
    catalog_root = Path(catalog_root)
    stage_body = _read_body(stage.path)
    agent_section = _agent_section(catalog_root, stage.lead_agent)

    produces = ", ".join(stage.produces) if stage.produces else "(none declared)"
    consumes = ", ".join(c.artifact for c in stage.consumes) if stage.consumes \
        else "(none — do not read prior artifacts)"

    # The output constraint depends on the stage KIND. A `sim` stage is read-only
    # analysis/design → it only writes its `produces:` docs under aidlc-docs/. A `burn`
    # stage MUTATES the target → its primary deliverable is real code/config/tests in
    # the project tree; the `produces:` are the accompanying plan/summary docs. (The old
    # "produce EXACTLY … under aidlc-docs/ and produce nothing else" caged burn stages
    # into writing docs instead of code — that is this bug's fix.)
    if stage.kind == "burn":
        output_rules = [
            "- This is a CODE/CHANGE stage: your PRIMARY deliverable is working output "
            "in the project itself — write real application source, infrastructure-as-"
            "code, CI config, and tests to their proper paths in the working directory "
            "(follow the stage's protocol and the project's existing conventions). Do "
            "NOT stop at documentation, and do NOT confine your output to `aidlc-docs/`.",
            f"- ALSO record this stage's declared artifacts ({produces}) — the plan / "
            "summary docs — under `aidlc-docs/` (or the location the protocol names).",
            "- NEVER write real or realistic secrets/credentials into any file — no "
            "passwords, API keys, tokens, or `scheme://user:password@host` connection "
            "strings, not even as examples. Use obvious placeholders "
            "(`<DB_PASSWORD>`, `${API_KEY}`, `changeme`) and reference secrets by name "
            "only. Mission Control's egress guard BLOCKS any commit that contains a "
            "secret-shaped value, which fails the whole stage.",
        ]
    else:  # sim (and any read-only stage): analysis/design only, never mutate code
        output_rules = [
            f"- This is a READ-ONLY analysis stage: produce EXACTLY these artifacts as "
            f"Markdown under `aidlc-docs/` (or the location the protocol names): "
            f"{produces}. Inspect and document only — do NOT modify project source.",
        ]

    parts = [
        f"## AI-DLC v2 stage: {stage.title} ({stage.slug})",
        "",
        "You are executing EXACTLY ONE AI-DLC v2 stage in this run. Mission Control owns "
        "orchestration, workflow state, audit logging, and the go/no-go approval gate — "
        "you do not. Work only this one stage, then stop and report.",
        "",
        "### Stage protocol",
        "",
        stage_body or "(no protocol body found for this stage)",
    ]
    if agent_section:
        parts += ["", agent_section]

    parts += [
        "",
        "### Operating constraints (Mission Control — these OVERRIDE the material above)",
        "",
        "- Follow this stage's protocol to produce its output, but treat ALL "
        "state-tracking, audit logging, gate/approval, and orchestration mechanics as "
        "Mission Control's responsibility, not yours.",
        *output_rules,
        f"- Read ONLY these input artifacts: {consumes}. Do not go looking for others.",
        "- Do NOT invoke any `aidlc-*.ts` tool, any `bun .claude/tools/*` command, or any "
        "v2 hook — they are intentionally absent from this environment. IGNORE every "
        "instruction in the material above that tells you to run one; Mission Control "
        "performs that bookkeeping itself.",
        "- Do NOT spawn subagents and do NOT use a Task/sub-agent tool. You are the "
        "single delegated worker for this stage.",
        # The stage frontmatter's `reviewer` / `reviewer_max_iterations` is NOT a v2
        # subagent in Mission Control: review collapses into MC's go/no-go gate — a human
        # GO is the approval, a NO-GO with feedback is "request changes". So we forbid a
        # reviewer subagent here rather than honoring the frontmatter's review loop.
        "- Any `reviewer` this stage names is handled by Mission Control's go/no-go gate "
        "(a human GO approves; a NO-GO with feedback requests changes) — do NOT run a "
        "review sub-agent yourself.",
    ]
    return "\n".join(parts)
