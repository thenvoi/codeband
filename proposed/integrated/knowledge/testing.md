# Testing Guide

Tests are how the swarm proves a change works without a human checking by hand. The
cross-model reviewer reads your tests as evidence — weak tests are treated as no tests.
This guide is **language-agnostic**; use your stack's test runner and idioms, but the
standard for *what makes a test worth writing* is the same everywhere.

## The standard: a test must be able to fail

A test that cannot fail proves nothing. Before you keep a test, ask: *"If the behaviour
I care about were broken, would this test go red?"* If not, it is decoration. The most
common worthless test asserts something the code can't violate (e.g. that a constructor
returns a non-null object) — delete or strengthen it.

## Test structure

- **One behaviour per test.** A test that asserts five unrelated things tells you little
  when it fails. Split them.
- **Arrange / act / assert.** Set up the world, perform the one action, assert the one
  outcome. Keep setup obvious; a reader should see what's being tested in seconds.
- **Descriptive names.** `test_login_rejects_expired_token`, not `test_login_2`.
- **Deterministic.** No reliance on wall-clock, network, random seeds, or test ordering.
  Inject clocks/randomness; stub the network. A flaky test is worse than no test.

## What to test

### 1. Happy path

Prove the feature does what the plan's acceptance criteria say, end to end, with
realistic inputs. This is necessary but **not sufficient** — a passing happy path is
where testing starts, not ends.

### 2. Then try to break it

After the happy path passes, deliberately attack your own code. This is the mindset that
separates real tests from rubber stamps. For the change you made, ask:

### 3. Input validation

- Empty input, missing fields, `null`/`None`, wrong type.
- Boundary values: 0, 1, -1, max, max+1, empty collection, single-element collection.
- Oversized input (huge strings/lists) where it matters.
- Malformed/garbage input from any untrusted boundary.

### 4. Edge cases — probe these systematically

- Off-by-one at the start and end of ranges, slices, and pages.
- Duplicate entries, already-exists, not-found.
- Unicode, whitespace-only, and very long strings in text fields.
- The empty case for every collection the code iterates.

### 5. Concurrency and races (if the code touches shared state)

- Two operations interleaving on the same resource.
- Check-then-act gaps (the value changed between the check and the use).
- Idempotency: does running the same operation twice corrupt state?

### 6. Resource lifecycle

- Files/connections/locks are released even on the error path.
- Cleanup runs when the operation fails partway.

### 7. Error paths

- The dependency raises/returns an error — does your code handle it, and does the test
  assert the *handling* (right error surfaced, right cleanup, right log), not just that
  it threw?
- Timeouts and unavailable external services.

### 8. State transitions

- Invalid transitions are rejected.
- The system ends in the state you claim after a sequence of operations.

### 9. End-to-end integration (at least one)

At least one test should exercise the new behaviour through the real seams it ships
with — not every collaborator mocked into meaninglessness. A suite of fully-mocked unit
tests can pass while the wired-together system is broken.

## Test quality standards

- **Assert on behaviour, not implementation.** Test the observable outcome, not internal
  call counts, so a refactor that preserves behaviour doesn't break the test.
- **No vacuous assertions.** `assert result is not None` after a call that can't return
  `None` tests nothing. Assert the actual value/shape/effect.
- **Don't assert on log strings or error message wording** unless that wording is the
  contract — those are brittle.
- **Mock at boundaries, not internals.** Stub the network/clock/filesystem; don't mock
  the function under test's own helpers, or you test the mock.
- **A test's failure message should point at the cause.** Prefer specific assertions
  over one giant equality check on a blob.

## What NOT to test

- Third-party libraries and the language standard library — assume they work.
- Trivial getters/setters and pure pass-throughs with no logic.
- Generated code.
- Exact wording of human-facing copy (unless it's a contract).

Don't pad coverage with tests that can't fail. Coverage percentage is not the goal;
*the ability to catch a regression* is the goal.

## When a test reveals an implementation bug

If writing the test surfaces a real bug in the code, fix the code — that's the test
doing its job. But if the bug is outside the scope of your task (pre-existing, in code
you weren't asked to touch), don't silently widen scope: note it and raise it rather
than quietly patching unrelated production code. See `coding-standards.md` on scope.

## Regression coverage

When you fix a bug, add a test that fails before your fix and passes after. That test is
the proof the bug is gone and the guard that keeps it gone.

## Reporting test results

When you report completion, state exactly what you ran and the outcome — the verbatim
command and the pass/fail counts (e.g. `pytest tests/test_auth.py -v — 7 passed`). If a
test fails for a reason outside your change (pre-existing on the base branch, an
environmental flake you have evidence for), say so explicitly with the evidence; never
report a green status you didn't actually observe.

## Hanging tests

If a test hangs, it usually means a missing timeout, an unawaited async operation, an
open resource, or a real deadlock in the code — investigate the cause rather than just
raising the test timeout. A test that only passes with a 5-minute timeout is hiding a
bug.
