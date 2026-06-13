# Verifier seat — reservation note (Stage-3)

> **Status: RESERVATION, not implementation.** This note reserves the design
> space for a dedicated *Verifier* role and records the shape we expect it to
> take. It is deliberately not built yet — the open questions below are real,
> and the design conversation happens *before* the build. Nothing in the
> codebase depends on this seat existing.

## Why a Verifier (and why now)

Stage-3 added evidence integrity (the hash-chained `transition_log` /
`audit_log` + `cb verify-log`), attribution (CLI invocation logging + role
markers), and peer-governance mechanicals (reconcile requires a grant, the
universal scope rule, the Conductor's verify-claims duty). Those are
*primitives and prompt duties*. What they lack is a **seat that owns running
them** — today the Conductor is asked to verify claims, but the Conductor is a
router, not an auditor, and finding 27 showed the Conductor's verification gap
is real. The Verifier is the role whose whole job is to not take anyone's word
for it.

## Dual mandate

The Verifier carries two distinct duties under one seat:

1. **Acceptance / QA verdict.** Like a Code Reviewer but for *acceptance*: does
   the change actually do what the task asked? This is a verdict leg in the
   verdict-list architecture (alongside `verify` and `review`) — a SHA-pinned
   passing record gated into a `to_state` the merge-eligibility check reads.

2. **Protocol-integrity duties.** The part no existing seat owns:
   - runs `cb verify-log` (both chains) on a cadence and on demand, and treats
     a break / head-regression as a blocking finding, not a note;
   - cross-checks agents' **claims** against store state (the same
     `cb status` discipline the Conductor's verify-claims clause now mandates,
     but as a primary duty rather than a side check) — "merged / abandoned /
     approved / blocked / resumed" claims are verified against the FSM + grants;
   - audits **room-record vs transition-log consistency**: every protocol
     effect announced in chat should have a corresponding gate/store fact, and
     every gated effect should have its expected chat trail. Divergence is a
     finding.

The integrity duties are detection, not prevention — consistent with the
Stage-3 posture (a process with DB write access can still recompute a chain).
The Verifier makes tampering and drift *visible and attributed*, fast.

## Opposite-model pairing rule

The Verifier's model must differ in vendor from the **coder's** vendor for the
work under verification — the same adversarial principle as the existing
Coder→Reviewer cross-model pairing (a Claude coder's PR routes to a Codex
reviewer, and vice versa). Rationale: a verifier sharing the coder's model
shares its blind spots and failure modes. Concretely, the pairing rule is
`verifier.vendor != coder.vendor`, resolved at allocation time the same way
`WorkerPool.pair_for_task` is intended to resolve the reviewer pairing.

## Implementation shape (under the verdict-list architecture)

The seat is deliberately small — one of each of the following, mirroring how
the reviewer leg is wired:

- **one config entry** — a `verifiers` pool under `agents` (`{framework:
  {count, model}}`), like `reviewers`; contributes a `verify_acceptance` (name
  TBD) verdict leg to the required-verdicts snapshot when enabled.
- **one leg** — a `cb-phase verify-acceptance` (name TBD) handoff leg that
  records the SHA-pinned acceptance verdict, plus a thin command (or a
  watchdog-style cadence) for the integrity sweep. Role-gated to `verifier`.
- **one prompt** — `prompts/verifier.md`, carrying the dual mandate, the scope
  rule (3b), and the verify-claims discipline (3c) as primary duties.
- **one pool slot** — a `verifier-{framework}-{index}` identity in the worker
  pool, allocated opposite-vendor to the coder.

## Open questions (genuinely open — resolve before building)

- **Verdict vs. auditor split.** Is the acceptance verdict and the
  integrity-audit duty really one seat, or two? One seat keeps the model count
  inside Band's free-tier cap; two cleanly separates "judge this change" from
  "audit the whole ledger." Leaning one seat with two modes — but open.
- **Integrity-sweep cadence.** On-demand only, per-merge, or a watchdog-like
  interval? The watchdog already has an incremental integrity rung (Stage-3
  PR1); does the Verifier *replace* that rung, *consume* its alerts, or run
  independently full-history? Overlap must be deliberate, not accidental.
- **Authority of an integrity finding.** Can the Verifier *block* a merge on a
  chain break, or only *report*? Blocking turns detection into a gate (a
  posture shift); reporting keeps Stage-3's detection-over-prevention line.
- **Free-tier budget.** The default `cb init` config is 8 agents under Band's
  10-agent cap. A verifier pool spends from that budget — is it on by default,
  or opt-in for users who want the seat?
- **Pairing under single-vendor configs.** If a user runs Claude-only, the
  opposite-vendor rule cannot be satisfied. Degrade to same-vendor-different-
  model? Warn (as `cb doctor` warns on reviewer capacity)? Disable the seat?
- **Room-vs-log audit scope.** "Every chat claim has a store fact" is
  expensive to evaluate exhaustively. What is the bounded, deterministic
  version that catches the row-5 / finding-25 class of drift without
  re-reading entire rooms each sweep?
