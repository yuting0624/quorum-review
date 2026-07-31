# Security

## Threat model

The reviewer reads attacker-controlled input by design. Anyone who can open a
pull request controls the diff, the title, the body, the commit messages, and
any instructions committed into the repository. All of it reaches a model.

So the question is not whether hostile input arrives — it always does. It is
what that input can accomplish.

**What an attacker might try**

1. Talk the model out of reporting a real vulnerability.
2. Talk the model into posting misleading or abusive comments.
3. Reach a credential through the model.
4. Get the model to write to the repository.

**What is out of scope**

- A model that is simply wrong. Bad findings are a quality problem, and that is
  what `benchmark/` measures.
- Compromise of the Google Cloud project or the GitHub organisation itself.
- Denial of service through very large pull requests, beyond the caps below.

## Controls

### Untrusted input is labelled as data

Every attacker-controlled field is wrapped in an `<untrusted_*>` tag, and the
system prompt — which comes first — states that content inside those tags is
data to review, never instructions. The model is additionally told to report
embedded instructions as a `security` finding rather than act on them.

See `BASE_INSTRUCTIONS` in [`src/schema.py`](../src/schema.py).

This is a mitigation, not a guarantee. Prompt injection is not a solved problem
and instruction-hierarchy prompting can be defeated. The controls below assume
it sometimes will be.

### Output is constrained by schema

Both stages are called with structured output bound to a JSON schema, and the
parsed result is validated again on our side: entries missing fields, or
carrying an out-of-enum `severity` or `category`, are discarded rather than
repaired. A model steered off-format produces nothing, not arbitrary text.

An unparseable response raises rather than returning "no findings" — a silent
empty review looks identical to a clean one, which is the more dangerous
failure.

### Verification is a second, independent look

This is the strongest control against attempt #1, and it comes for free with the
two-stage design. A diff that manipulates the primary model still faces a
verifier that never saw that session and never receives the primary's reasoning
— so a suppressed finding is only suppressed if *both* models were fooled
independently.

The same asymmetry works against attempt #2: a finding invented through
injection has to survive a model asked to refute it.

### Credentials are never in reach of a model

The models see the diff. They do not execute code, do not make network calls,
and do not receive any credential. Federation produces a short-lived token used
by the runner, never placed in a prompt.

There is no long-lived secret in the repository at all — that property comes
from Workload Identity Federation, not from anything this code does. Nothing to
leak from a workflow log, nothing to find in the git history later.

### The reviewer cannot write to the repository

The workflow requests `contents: read`. There is no code path that pushes a
commit, creates a branch, or edits a file. The blast radius of a fully
successful injection is a wrong or missing comment on a pull request.

If commit-writing is ever added, the design constraint is: **the model emits a
patch, the runner validates and applies it.** Never hand write access to the
model directly.

### Bot comments cannot re-trigger the workflow

The example workflows require `github.event.comment.user.type != 'Bot'`. Without
it, the summary comment this action posts satisfies the trigger condition and
starts another run — an unbounded loop that consumes Actions minutes and model
quota until someone notices.

Do not remove that condition.

### Forks are unsupported

`GITHUB_TOKEN` is read-only on pull requests from forks, so posting fails.
`pull_request_target` would fix that by running with write access in the context
of the base repository — while reviewing untrusted code. That is not a trade
worth making, so forks stay unsupported.

### Bounded input and cost

- 20,000 characters per file, and binary patches skipped. Truncated files are
  named in the summary, so a partial review never passes as a complete one.
- Verification runs on at most the top 20 findings by severity, so cost cannot
  scale with a hostile model's willingness to report.
- A 20-minute ceiling on the whole run.

## Operational guidance

**Grant the service account `roles/aiplatform.user` and nothing more.** Any
workflow run in the repository can assume this identity.

**Scope the federation binding to one repository.** Bind on
`attribute.repository/OWNER/REPO`, not on the pool. A pool-level binding lets
every repository in the organisation impersonate the account. See
[setup-vertex.md](setup-vertex.md).

**Be deliberate about enabling review on public repositories.** Any stranger can
then cause model spend. Consider restricting the trigger to
`author_association` values you trust.

**Untrusted contributor? Read the findings, not just the verdict.** A finding
whose text tries to instruct *you* is the injection surfacing one layer up.

## Reporting a vulnerability

This is a reference implementation with no support commitment. If you find a
flaw in the approach, please open a public issue — the discussion is more
valuable to readers than a private fix would be.
