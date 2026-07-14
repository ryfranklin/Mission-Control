# AI-DLC v2 (vendored methodology content)

This package holds the AWS **AI-DLC v2** methodology, vendored into the repo as
**content only** so the Mission Control runtime can read it as text.

## What this is

`methodology/` is a pinned copy of the AI-DLC v2 stage definitions, protocols, agent
definitions, and knowledge — the subtrees `aidlc-common/stages/`,
`aidlc-common/protocols/`, `agents/`, and `knowledge/` from the upstream
`dist/claude/.claude/` tree.

## The pin

Vendored via `scripts/vendor_aidlc_v2.py`, which shallow-clones the exact revision and
copies only the content subtrees. The exact source is recorded in
[`methodology/VENDOR.json`](methodology/VENDOR.json):

- repo: `https://github.com/awslabs/aidlc-workflows`
- ref: `v2`
- commit: `d4fc34dd2e548b43fb781ff6177662d6bf54e6f8`

Re-run the script to refresh; change the pin there to move to a new revision.

## Content-only rule (load-bearing)

Mission Control reads AI-DLC v2 as **content only and never runs its hooks or tools.**
The upstream `hooks/` and `tools/` (all TypeScript) are explicitly EXCLUDED from the
vendored tree. MC substitutes its own orchestration + go/no-go gate + state for
whatever v2's runtime would otherwise do — we consume the *methodology*, not the
*machinery*.
