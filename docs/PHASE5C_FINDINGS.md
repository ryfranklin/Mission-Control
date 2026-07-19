# Phase 5C findings — Slack surface, end-to-end

**What ran.** The full 5c flow against the **real** service + **real** bridge, two
profiles standing in for two workspaces:

| Profile (workspace) | Channel | Approver allowlist |
|---|---|---|
| `acme-eng` (A) | `#eng` | `U_ALICE` |
| `acme-ops` (B) | `#ops` | `U_BOB` |

Config comes from a registry JSON (`MC_SLACK_REGISTRY`) + env token **var names**;
tokens resolve from the environment. Harness: `scripts/e2e_5c.py` (reproducible,
deterministic across runs).

**Fidelity boundary (read this first).** Real in the harness: the FastAPI service +
RunManager + graph + StubWorker, the **durable Postgres notification outbox** (global
`IDENTITY` seq), the bridge's poll→route→cursor loop, the durable cursor file,
per-profile authorization + the one-shot gate (relayed to the *same* `/runs/{id}/approve|reject|cancel`
endpoints the UI/CLI use), and alert emission. The bridge's `ServiceClient` talks to the
app over an in-process ASGI transport (real HTTP). **Faked: only the Slack egress** — the
bot Web client and Socket Mode handler are recording stand-ins, because no org-approved
Slack app / real tokens exist in this environment. So this validates *everything up to
the WebSocket*; real Socket Mode connect/reconnect/latency is asserted by construction,
not measured against Slack (see graduation backlog).

---

## 1. Outbox + global cursor: at-least-once, no dupes, across restarts

Yes. The durable outbox (`notifications`, global monotonic `seq`) + the bridge's
file-backed global cursor deliver at-least-once without duplication across both restart
classes:

- **Bridge restart mid-flight** (run holding at the gate, cursor=`4`): a fresh bridge
  over the same cursor + store **re-opened both profile connections** and its first poll
  produced **0 new posts / 0 reposts** — nothing already-handled re-fired.
- **Service restart** (new service process over the same Postgres): outbox total survived
  (`11 → 14` rows as a new post-restart run appended); the bridge **caught up exactly the
  3 new rows** (`run_launched, run_terminal, cost_threshold`) and re-posted none of the 11
  pre-restart rows.
- Idempotency is layered: the cursor advances only after a note is handled (post *or*
  deliberate skip), and an in-memory high-water seeded from the durable cursor drops any
  re-presented `seq`. Dedupe on `seq` holds even when a service replays the whole tail.

## 2. Per-run routing: correct profile, isolation, null = silent

Holds exactly.

- **Opt-in default**: a run launched with **no profile** produced **0 posts to A and 0 to
  B** — silent. (Its milestones *are* written to the outbox with `slack_profile=null`, as
  designed; the bridge filters them out at routing, and cost-alert emission is skipped at
  source for null-profile runs.)
- **Routing**: A's run posted `run_launched → gate_awaiting → run_terminal → cost_threshold
  → regression` to `#eng` only; **B saw 0 messages** during A's entire flow, and A's
  recorder was **untouched** by B's flow. Per-run messages thread under the launch message,
  within that one workspace.
- Digest is per-profile and scoped: `A_runs=1`, `B_runs=1`, the null-profile run in
  neither.

## 3. Socket Mode is the right no-public-endpoint fit — per profile

Each active profile opens its **own** outbound Socket Mode connection (no request URL, no
public endpoint) and registers **its own** `mc_go`/`mc_nogo` buttons + `/mc` slash command
on its own Bolt app. In the harness both handlers connected; a bridge restart re-opened
both. This is the correct posture for a fleet where each box holds a different subset of
secrets.

**Latency, honestly.** Delivery is a **pull** (poll `GET /notifications?after=<cursor>`),
not a Socket-Mode push. In-process the seam round-trip is ~4 ms, but real-world freshness
is bounded by the poll interval (**default 3 s**). That is well inside the "operator not
watching" use case; if seconds matter, the S1-reserved `GET /notifications/stream`
(Last-Event-ID) is the drop-in upgrade. Socket Mode is used for the *inbound* privileged
path (button/command receipt), not outbound delivery.

## 4. Per-profile authz + one-shot gate: unauthorized, cross-profile, race

All three failure modes held, with **no state change** on refusal:

| Click | Result | Run status after |
|---|---|---|
| Non-allowlisted user (`U_RANDO`) GO on A-run, A conn | **denied** (ephemeral, no service call) | `awaiting_gate` (unchanged) |
| B's approver (`U_BOB`) GO on an **A-run**, B conn | **denied** — cross-workspace relay refused | `awaiting_gate` (unchanged) |
| Allowlisted `U_ALICE` GO, A conn | **resolved** → `applied` | terminal |
| Second click (NO-GO) after resolution | **conflict** — seam one-shot ("run is not awaiting a gate…"); message updated | terminal |
| A's approver (`U_ALICE`) GO on a **B-run**, B conn | **denied** — B's allowlist governs B | (B resolved only by `U_BOB`) |

The identity gate checks the approver allowlist first (so an unauthorized click makes
**zero** service calls), then confirms the run's `slack_profile` matches the connection's
profile before relaying. The gate is one-shot across all surfaces/profiles because the
resolve is the same seam endpoint that already rejects a double-submit (`RunConflict` →
409); the bridge renders that as "already resolved" and disables the buttons.

## 5. Metadata-only, by construction

Audited **all 11 outbox payloads** on the wire: **0 fields outside the metadata whitelist**
(`target, task_type, status, cost_usd, node, timestamps` + alert metadata
`reason/threshold/budget/window*/axes/k/n`), regressed axes limited to numeric/bool fields,
and the planted eval-content sentinel (`EVAL-CONTENT-must-not-leak`) **did not appear**.
The payload types simply have no field for prompt/code/diff/target contents, so this is a
construction guarantee, not a filter. Gate cost is shown as "not yet reconciled" — never
`$0`.

---

## Demo vs. prod — honest line

What's demonstrated here is the **runtime + seam**, not a live Slack integration:

- **Org Slack app still pending.** No real bot/app tokens, so outbound `chat_postMessage`
  and inbound Socket Mode receipt are faked. Everything up to the WebSocket is real and
  tested; the last hop is not exercised against Slack.
- **Approver lists are solo/static.** Authz reads a flat per-profile allowlist from the
  registry — correct and enforced, but it's hand-maintained IDs, not a directory/group
  (SCIM/IdP) with rotation.
- **No auth at the service.** The seam is still localhost, no-auth (v1). The Slack surface
  has an identity gate; the HTTP API underneath does not. A bridge (or anything on the
  host) can still call `/runs/{id}/approve` directly.
- **Delivery is pull, ~3 s.** Fine for alerts/milestones; not a push.

### Hardening / graduation backlog

1. **Stand up the real Slack app** (Socket Mode, `chat:write`, `commands`, interactivity);
   replace the faked clients and measure real connect/reconnect + push latency. Handle
   token invalidation at connect (currently a profile is validated only when its socket
   opens; add an `auth.test` at resolve so a bad token is a clear startup skip).
2. **Service-side auth** so a decision requires an authenticated principal even off the
   Slack surface — the Slack identity gate should not be the *only* gate.
3. **Directory-backed approvers** (group/role lookup, rotation) instead of static IDs.
4. **Optional push feed** (`GET /notifications/stream`, Last-Event-ID) if sub-second
   freshness is needed; keep the pull cursor as the durable fallback.
5. **Alert-storm dampening** beyond once-per-run (per-window rate limits on cost/regression
   so a bad target can't flood a channel).
6. **Digest scheduler** — the bridge has `post_digests()`; wire a real cron/interval and a
   window policy (daily / end-of-window) rather than an on-demand call.
