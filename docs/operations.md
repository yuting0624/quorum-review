# Operating it

Setup is in [setup-vertex.md](setup-vertex.md). This is the part after that: how
to read what it tells you, what it costs, and what to do when it goes wrong.

## Reading a summary comment

The first paragraph is the one that matters, and it is not the findings list.

> `gemini-3.6-flash`, `claude-opus-5` each read the diff without seeing the
> other's output (`gemini-3.6-flash` 10 and `claude-opus-5` 15), which merged to
> **16** distinct finding(s). **3** of those were reported by both models
> independently.

Two models reporting the same defect without seeing each other is the strongest
signal this design produces, and it is free — an agreed finding costs no
verification call. The **Evidence** column carries it per finding:

| Evidence | What it means |
|---|---|
| both models, independently | Neither could have influenced the other. Weigh this highest. |
| `X`, agreed by `Y` | One reported it; the other was shown the code but not the reasoning, and agreed. |
| `X`, unchecked | Verification was off, or the cap was reached. One opinion. |

**Advisory** findings are ones the second opinion could not settle. They are not
weak findings; they are undecided ones, and they usually mean the answer depends
on something neither model could see.

## Signals that the review did less than it looks like

Any of these makes a clean result mean less than a clean result:

| In the summary | What happened |
|---|---|
| `⚠️ N file(s) were not reviewed` | The diff exceeded `max-diff-characters`. Nothing below is a statement about them. |
| `ℹ️ Diff only` | The models could not read the checkout. Anything depending on code elsewhere was judged from the call site. |
| `⚠️ one or more models failed` | Half a review. Named, never hidden. |
| `Reviewing only what changed since …` | An incremental run. Findings in untouched files were carried over, not re-checked. |

The `degraded` action output is `true` for the first, third, and a verifier
outage. Branch on it before treating a clean result as clean:

```yaml
  - if: steps.review.outputs.degraded == 'true'
    run: echo "::warning::reviewed with less than full context"
```

Trimmed files — shortened to the 20,000-character per-file limit — deliberately
do **not** set `degraded`. Nearly every repository has a file that long, and a
flag that is always true is not a flag.

## What it costs

The summary reports tokens per model, never money: prices differ by model, by
platform and by contract, so a currency figure computed here would be a guess
wearing the costume of a fact. Multiply by your own rate.

Rough shape of a review, with both models scanning and verification on:

| | Calls | Scales with |
|---|---|---|
| Scanning | 2 | diff size |
| Verification | one per finding only one model reported | number of findings |
| Tool lookups | up to 24 per scan, 6 per verification | how much the diff depends on code elsewhere |

The default is **not** the most expensive configuration, which is easy to assume
and wrong. Verifying every finding from a single scan costs `1 + N` calls;
scanning with both and verifying only the disagreements costs `2 + d`, and `d`
is usually well under `N`.

Levers, cheapest effect first:

1. `incremental: on` (the default) — a re-review reads the range since the last
   one. On a branch with twenty commits this is most of the saving available.
2. `exclude` / `.quorumignore` — generated code reviewed is money spent for
   nothing.
3. `max-verified-findings` — caps the part that scales with findings.
4. `repo-access: off` — cheaper and measurably blinder. See the
   [benchmark](../benchmark/seeded-bugs/README.md).
5. `scan: single` — halves scanning and caps recall at one model's.

`max-tokens` is a ceiling rather than a lever: a review that reaches it stops
verifying and says so, and findings are demoted to advisory rather than dropped.

## When something goes wrong

**Every Claude call returns 404, Gemini works.** Claude is not enabled in Vertex
AI Model Garden for the project. The single most common setup failure, and it
degrades rather than fails, so it can run for a while unnoticed — the summary
names it.

**`Unable to retrieve Identity Pool subject token`.** The runner could not reach
its own OIDC endpoint. Transient; it is retried. If it persists, check
`id-token: write` is in the workflow's `permissions`.

**Threads do not collapse when a finding is fixed.** GitHub does not let the
Actions app call `resolveReviewThread`, whatever `permissions:` says. Use a
GitHub App token — [`examples/review-vertex-app.yml`](../examples/review-vertex-app.yml),
created by `python scripts/create_app.py`. The reply is still posted either way.

**`@quorum /review` does nothing.** An `issue_comment` event runs the workflow
from the **default branch**, not the pull request. If the workflow was added on
a branch that is not merged, there is nothing to run.

**A finding is reported twice.** It should not be; the ledger matches on
position and title overlap, not just ID. Worth an issue with both comments.

**The SARIF upload fails.** Code scanning has to be enabled on the repository:
free on public ones, part of GitHub Advanced Security on private ones. The
example marks that step `continue-on-error` for this reason.

## Changing what it reports

A false positive worth arguing with is a signal about the criteria, not about
the code. In order of increasing weight:

1. **Reply in the thread.** `@quorum <question>` asks the model that made the
   claim to defend or withdraw it. It concedes when it is wrong.
2. **Retire it.** `@quorum wontfix — <reason>` records the dismissal, and it
   will not come back on this pull request.
3. **Change the criteria.** `@quorum /criteria` turns accumulated dismissals
   into a proposed edit. It is posted as a comment for a human to apply, never
   written automatically — the reasons arrive in pull request comments, and
   writing those straight into the criteria would let someone argue the reviewer
   out of a category of finding by dismissing it convincingly a few times.
4. **Point `skill` at your own file.** `skill: security-review,
   .github/quorum/backend.md`. This is the one that scales.

## Rolling it out

Start with `fail-on: never` — the default — and watch the outputs for a few
weeks before blocking anything. A reviewer that starts by failing builds is a
reviewer that gets removed before anyone has calibrated it.

Use the [reusable workflow](../.github/workflows/reusable.yml) rather than
copying YAML into every repository. The trigger conditions decide who may
trigger a review, and this project has already had to fix two security bugs in
them; neither fix would have reached a repository that copied the file first.

Pin `action-ref` to a commit SHA in a regulated environment. A tag can be moved
to point at new code, and the job holds a write-scoped token.
