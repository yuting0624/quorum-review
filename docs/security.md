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
5. Steer the model's file reads — at somewhere outside the checkout, at a
   secret, or at enough of the repository to exhaust the run.

**What is out of scope**

- A model that is simply wrong. Bad findings are a quality problem, and that is
  what `benchmark/` measures.
- Compromise of the Google Cloud project or the GitHub organisation itself.
- Denial of service through very large pull requests, beyond the caps below.

## Controls

### Untrusted input is labelled as data

Every attacker-controlled field is wrapped in an `<untrusted_*>` tag, and the
system prompt — which comes first — states that content inside those tags is
data to review, never instructions.

**The tag has to be un-forgeable from inside**, and for a long time it was not:
the content was interpolated raw, so a pull request title reading
`</untrusted_pr_title>` closed the block and everything after it sat beside the
instructions as a peer. That was true of every prompt this project sends. One
helper, `prompts.untrusted`, now builds all of them and neutralises the
delimiter — visibly rather than by escaping, because the model is asked to
report an injection attempt as a finding and cannot do that if it cannot see
what was attempted. The model is additionally told to report
embedded instructions as a `security` finding rather than act on them.

See `BASE_INSTRUCTIONS` in [`quorum_review/schema.py`](../quorum_review/schema.py).

Every prompt the project sends is built in
[`prompts.py`](../quorum_review/prompts.py) and every one of them starts with
those instructions. That was not true for a while: the `@quorum /criteria` path
built its own system prompt, one line long, and put finding titles — which
quote code from the diff — beside its instructions as peers. It is the worst
place to have left open, because the output is an edit to the review criteria
offered to a human to paste in. A successful injection there is not one wrong
comment; it is a permanent blind spot, installed by someone who thought they
were tidying up false positives. That path is also now told never to propose
removing a whole category.

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

### Two models read the diff independently

This is the strongest control against attempt #1, and it comes for free with the
design. Both models scan the same diff in separate sessions, neither seeing the
other's output, so **a finding is only suppressed if both models were fooled
independently.** Text crafted against one model's phrasing has to work twice, on
two different models, with no way to confirm it worked the first time.

That property is why both models scan rather than one scanning and one checking.
A single scanner is a single point of failure for injection as well as for
recall: manipulate it and the finding is never raised, so there is nothing for a
second model to be asked about.

The same asymmetry works against attempt #2. A finding invented through
injection was raised by one model only, which means it is exactly the kind that
gets sent to the other model — the one that was asked to refute it.

### The models can read the checkout, and can do nothing else with it

A reviewer that only sees the diff has to guess whether the validator a changed
line calls actually validates, so the models are given three read-only tools —
`read_file`, `search`, `list_files` — over the checked-out repository. That is
an expansion of what attacker-controlled text can steer, and it is bounded on
four sides:

- **There is no write tool and no execute tool.** Not disabled, not
  permission-gated — never implemented. The most a fully successful injection
  buys is reads of files the same workflow already checked out onto the same
  runner.
- **Paths are resolved before they are checked.** Anything landing outside the
  checkout root is refused, including a symlink inside the repository that
  points out of it.
- **`.git`, `.env`, and private keys are unreadable**, on top of the ordinary
  review exclusions, regardless of what the repository's own configuration
  says.
- **Everything is budgeted** — calls, bytes returned, and conversation turns —
  so a diff crafted to send the reviewer on a long walk runs out rather than
  running up a bill.

The tools read the workspace, so the workspace has to be the right one. A
workflow triggered by `issue_comment` checks out the default branch rather than
the pull request; when the checkout does not contain the commit under review the
tools are withdrawn for that run and the summary says the review was diff-only.

Turn the whole thing off with `repo-access: off` if reading beyond the diff is
not acceptable in your environment. Findings will get worse; that is the trade,
and it is measured in `benchmark/seeded-bugs/README.md` rather than asserted.

### Credentials are never in reach of a model

The models never receive a credential. Federation produces a short-lived token
used by the runner, never placed in a prompt. Nothing the models can call makes
an outbound network request.

**Nothing long-lived reaches Google Cloud.** That property comes from Workload
Identity Federation, not from anything this code does: no key to leak from a
workflow log, none to find in the git history later.

The GitHub side is not the same. If you use a GitHub App token — the only way
to collapse a resolved thread, since GitHub forbids `resolveReviewThread` for
the Actions app — then `APP_PRIVATE_KEY` is a long-lived secret stored in the
repository. It never leaves GitHub and it is scoped to that App's installation,
and the review still works without it, but it is a stored key and belongs in
whatever inventory you keep of those.

### The reviewer does not republish the secret it found

A finding about a hardcoded credential quotes the credential — that is what
makes it legible. It also means the reviewer takes a value out of a diff and
puts it in a pull request comment, which is more visible, harder to remove, and
on a public repository indexed. The comment also outlives its source: force-push
the branch and the diff is gone while the comment stays.

So credential shapes are removed from every finding before it renders, records,
or posts — [`redaction.py`](../quorum_review/redaction.py). Three details:

- **Once, at the source.** Five places put a finding's text in front of a
  reader, and one of them is the ledger, which lives inside the summary
  comment. Redacting per rendering site works until someone adds the sixth.
- **A suggestion is dropped, not redacted.** It is applied verbatim by a click,
  so a redacted one would write the placeholder into the file.
- **The comment says what was removed**, by kind, and tells the author to
  rotate it. A finding that quotes `[redacted]` with no explanation reads like
  a bug in the reviewer.

The pattern list is short by design. Every entry matches a token format issued
by a service, where the full value is worth nothing to a reader. Anything that
would require guessing whether a string is sensitive is left alone: a reviewer
that mangles ordinary code gets switched off, and then it protects nothing.

This is not a secret scanner. It reduces what the reviewer itself spreads; it
does not tell you whether your repository has secrets in it.

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

### Forks are reviewed only after a maintainer authorises it

`GITHUB_TOKEN` is read-only on pull requests from forks, so an ordinary
`pull_request` run cannot post and cannot reach Vertex — it fails noisily on
every outside contribution. `pull_request_target` fixes that by running in the
base repository's context, with write access and secrets, while the code under
review belongs to someone else.

Every published attack on that trigger has the same shape: **the workflow checks
out the fork's code and then executes something from it** — `npm ci`, a build
step, a test run, a linter that loads a config file from the tree. This action
never does. It is installed from its own published copy, and the fork's code is
read as text by tools that cannot write or execute.

Two conditions authorise a run, and both are enforced in
[`forks.py`](../quorum_review/forks.py) rather than only in the workflow's `if:`
— a YAML condition is one careless edit away from being wrong, and nothing tests
it:

1. The pull request carries the review label.
2. Whoever applied it has write access, checked against the API. Labelling is
   available to triage collaborators, so the label alone is not authorisation,
   for the same reason `author_association` is not.

A fork's `.quorumignore` is also ignored in favour of the base branch's. It can
only remove files from review, which makes it one commit from an empty review
that still reports success.

If none of that is acceptable in your environment, do not deploy
`examples/review-fork.yml`. Same-repository pull requests do not need it.

### Bounded input and cost

- 20,000 characters per file, and binary patches skipped. Truncated files are
  named in the summary, so a partial review never passes as a complete one.
- Verification runs on at most the top 20 findings by severity, so cost cannot
  scale with a hostile model's willingness to report.
- Repository reads are capped per caller: 24 tool calls and 400 KB for a scan,
  6 calls for a verification, 8 conversation turns either way. Budgets are
  per-caller, so exhausting one does not silence the other model.
- A 20-minute ceiling on the whole run.

## Where the code goes

The diff, and whatever the models read from the checkout, are sent to Vertex AI
in your own Google Cloud project. Nothing is sent anywhere else: there is no
service operated by this project, no telemetry, and no second vendor. The
findings are written back to GitHub as comments and SARIF, which is the only
other place the content lands.

**The default is not a residency guarantee.** Both models default to the
`global` endpoint, which routes to whichever region has capacity. That is the
right default for getting started — it is the endpoint most likely to have the
model available — and it is the wrong one if you have to state a jurisdiction
in writing.

Pin it:

```yaml
with:
  vertex-region: europe-west4     # both models
  # claude-vertex-region: us-east5  # override one, if the entitlement demands it
```

Precedence, highest first:

1. `claude-vertex-region` / `gemini-location` — the per-model input
2. `vertex-region` — the shared pin
3. `CLAUDE_VERTEX_REGION` / `GOOGLE_CLOUD_LOCATION` in the environment
4. `global`

The pin sits above the environment variables deliberately. An ambient
`GOOGLE_CLOUD_LOCATION` — from a runner image, an organisation-level `env:`, a
devcontainer — must not be able to quietly undo a region someone wrote into the
workflow. A pin that anything inherited can override is not a pin.

Check that both models are actually served in the region before you rely on it.
A region that does not offer one of them returns 404 rather than falling back —
which is the behaviour you want, but it means the review degrades to one model
rather than failing loudly. The summary names the model that dropped out.

Every summary comment records where each model that ran was called, so the
answer to "which region processed this pull request" is in the pull request:

> Reviewed `abc1234` · quorum-review `1.5.0` · models `gemini-3.6-flash`,
> `claude-opus-5` · `claude-opus-5` in `europe-west4`, `gemini-3.6-flash` in
> `europe-west4` · 118s

A value that is not shaped like a region is refused at startup rather than
turned into a hostname, because that failure otherwise arrives as a connection
error and reads like the network.

Two things this does *not* control, and which belong in the same assessment:

- **Prompt caching** is on for the system prompt. It is Vertex-side, within the
  same project and endpoint, and it holds the criteria rather than your diff.
- **Model training.** Whether prompts sent to Vertex are used for training is
  governed by your Google Cloud terms, not by this action. Check them; the
  answer differs between the Google-published and third-party models.

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
