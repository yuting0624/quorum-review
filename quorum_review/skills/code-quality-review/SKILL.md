# code-quality-review

Correctness and maintainability rather than security. Select this with
`REVIEW_SKILL=code-quality-review`.

## Report

**Logic errors.** Off-by-one bounds, inverted conditions, a branch that cannot
be reached, a loop that cannot terminate. An early return that skips required
cleanup.

**Language traps.** A mutable default argument. A closure capturing a loop
variable by reference. Integer division where a float was meant. Truthiness
checks that treat `0` or `""` the same as absent.

**State and lifetime.** Use after close or free. A resource opened without a
`with` or a `finally`. A cache that grows without bound. Global mutable state
written from more than one place.

**Error handling.** An exception caught too broadly to act on. An error path
that returns a value indistinguishable from success. A retry that repeats a
non-idempotent operation.

**API contract.** A public signature changed without updating its callers. A
return type that varies by branch. A function documented to do one thing that
also does another.

**Performance with a real cost.** A query inside a loop. An O(n²) scan over data
that is not bounded. A synchronous call on a hot path that blocks.

## Do not report

- Formatting, naming, and import order — a linter handles those.
- Preferences with no behavioural difference.
- Missing tests, unless the change removes an existing one.
- Refactoring opportunities in code the diff does not touch.

## Severity

- **critical** — data loss or corruption, or a crash on a common path.
- **high** — incorrect results, or a failure under a plausible input.
- **medium** — correct today but fragile, or a real performance problem.
- **low** — maintainability. Worth mentioning, not worth blocking on.
