# Coding Standards

These are the craft standards for code you write in any Codeband swarm. They are
deliberately **language-agnostic** — Codeband runs against arbitrary repositories.
When this guide and the target repo disagree, **the target repo wins**: match the
conventions, idioms, and tooling that already exist in the code you are editing.
This guide governs only where the repo is silent.

## The one rule that overrides everything

**Read the surrounding code before you write any.** The single most common failure
of an autonomous coder is writing code that is locally correct but foreign to the
codebase — a different error-handling style, a different naming scheme, a hand-rolled
helper that already exists three files over. Before adding code:

- Read at least one neighbouring file in the same module or package.
- Find how this codebase already does the thing you are about to do (logging, config,
  HTTP calls, DB access, error types) and do it that way.
- Prefer extending an existing abstraction over introducing a new one.

## Code quality principles

- **Smallest change that fully solves the task.** Do not refactor unrelated code,
  rename things you weren't asked to rename, or "tidy while you're in there." Scope
  creep is the enemy of a clean review and a clean merge.
- **Clarity over cleverness.** Code is read far more than it is written. A longer,
  obvious implementation beats a terse, subtle one.
- **No dead code.** Don't leave commented-out blocks, unused variables, unreachable
  branches, or speculative "might need this later" helpers. Delete it; git remembers.
- **No debugging leftovers.** No stray prints, console logs, `TODO: remove`, or
  temporary instrumentation in the code you submit.
- **Single responsibility.** A function should do one thing. If you can't describe it
  without "and", consider splitting it.

## Naming and structure

- Names describe intent, not implementation. `retry_count`, not `n`. `is_eligible`,
  not `flag`.
- Match the casing/convention of the file you're in (snake_case, camelCase, etc.) —
  do not introduce a second style.
- Keep functions short enough to see whole. Deep nesting is a smell; prefer early
  returns / guard clauses over arrowhead `if` pyramids.
- Put new code where a reader would look for it. Don't create a new top-level module
  for something that belongs in an existing one.

## Logging, not stdout

Use the codebase's logging facility, never raw stdout/stderr writes, for diagnostic
output that ships.

- **Don't** emit `print(...)` / `console.log(...)` / `fmt.Println(...)` as a logging
  mechanism in production code paths.
- **Do** use the project's logger at an appropriate level (`debug` for developer
  detail, `info` for normal lifecycle, `warning`/`error` for problems).
- Never log secrets, credentials, tokens, full request bodies with PII, or anything
  you wouldn't want in a shared log aggregator. See `security.md`.

## Error handling

Handle errors at boundaries; don't swallow them.

- **Fail loudly at system boundaries** — network calls, file I/O, parsing untrusted
  input, subprocess calls. These *will* fail in production; handle the failure
  explicitly with a clear message and the original cause attached.
- **Don't catch-all-and-ignore.** A bare `except: pass` (or `catch {}`) hides the bug
  you'll be paged for. If you must catch broadly, log the cause and re-raise or return
  a typed error.
- **Don't catch what you can't handle.** Catching an exception only to re-raise an
  identical one adds noise. Either add context or let it propagate.
- **Preserve the cause.** When wrapping an error, chain the original (`raise X from e`,
  `fmt.Errorf("...: %w", err)`, `throw new X({cause: e})`) so the stack trace survives.
- **Error messages are for the person debugging at 3am.** Include what was being
  attempted and the relevant identifiers, not just "operation failed".

## Common traps to avoid

- **Mutable default arguments / shared mutable state** captured across calls.
- **Off-by-one and boundary errors** in ranges, slices, and pagination.
- **Silent type coercion** — comparing across types, truthiness of `0`/`""`/`None`.
- **Resource leaks** — files, sockets, DB connections, locks not released on the error
  path. Use the language's scoped-cleanup construct (`with`, `defer`, `try/finally`,
  RAII).
- **Time and timezones** — naive timestamps, assuming local time, DST.
- **Floating point for money** — use integers/decimals for currency.
- **Concurrency** — shared state without synchronization, assuming ordering, races on
  check-then-act. See `testing.md` for what to test here.
- **Trusting external input** — anything from the network, a file, an env var, or a
  user is untrusted until validated. See `security.md`.

## What good code looks like

- A new reader can follow it without you explaining it.
- It fits the file it lives in — same patterns, same error style, same naming.
- The happy path is obvious; the error paths are explicit, not implied.
- It has tests that would fail if the behaviour regressed (see `testing.md`).
- The diff is minimal: every changed line is there for a reason tied to the task.

## Production hardening

For anything that runs in production, before you call it done:

- **Inputs validated** at the boundary, with clear rejection of bad input.
- **External calls** have timeouts and a defined behaviour on failure (retry with
  backoff, fail fast, or degrade) — never an unbounded hang.
- **Idempotency** considered for anything that can be retried (webhooks, queue
  consumers, payment-adjacent flows).
- **Observability** — the code emits enough logging/metrics that an operator can tell
  what happened when it misbehaves.
- **No new secrets in code or config committed to the repo.**

## Self-review checklist (run before you open the PR)

Before reporting completion, read your own diff top to bottom and confirm:

- [ ] Every changed line is necessary for the task — no scope creep, no drive-by edits.
- [ ] It matches the surrounding code's style, naming, and error handling.
- [ ] No debug prints, commented-out code, or dead branches.
- [ ] Errors at every system boundary are handled explicitly.
- [ ] No secret, token, or credential is hardcoded or logged.
- [ ] There are tests that would fail if this behaviour broke (see `testing.md`).
- [ ] You ran the verification command from the plan and it passed.
- [ ] You could explain every line if a reviewer asked.
