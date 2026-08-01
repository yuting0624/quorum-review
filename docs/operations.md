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

**Someone deleted the summary comment.** That comment carries the review's
entire state in a hidden marker, so deleting it would normally mean every open
finding is posted again and every `wontfix` is undone. It does not: findings
and dismissals are recovered from the inline comments still on the pull
request, and the next summary says how many. Severity, which model raised each
finding, and the reason behind a dismissal are gone — they were only in the
marker.

**A review was cancelled halfway through.** `cancel-in-progress` is on by
default, so a second push during a review kills the first. If it had already
posted some comments but not yet saved the record of them, the next review
reconciles: anything on the pull request that the record does not know about is
taken back in rather than posted again, and the summary says how many.

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

## GitHub Enterprise Server

Nothing needs configuring: Actions sets `GITHUB_API_URL` on the runner, and
both REST and GraphQL are derived from it. GraphQL is derived rather than
joined because it does not live under the REST root — on GHES the REST API is
at `https://host/api/v3` and GraphQL is at `https://host/api/graphql`, a
sibling of `v3` rather than a child. Getting that wrong fails silently: thread
resolution is the only GraphQL caller and it already degrades quietly, so the
symptom is threads that never collapse and a run that reports success.

Two things are worth checking before you roll it out:

- **The Agent Platform has to be reachable from the runner.** A self-hosted runner
  inside a VPC usually is not. Private Google Access or a proxy is the fix; the
  action does not tunnel anything itself.
- **The `codeql-action/upload-sarif` step needs GitHub Advanced Security.**
  Without it, the upload is skipped and the findings stay in the pull request
  comments — which is a degraded mode, not a failure, and the summary says so.

## Behind a TLS-intercepting proxy

`HTTPS_PROXY` and `NO_PROXY` work as they do everywhere else. The corporate CA
is the part that needs saying: `httpx` verifies against `certifi` and does not
read the environment for anything else, so on a network that terminates TLS at
a proxy every call fails at the handshake with an error that reads like the
host is unreachable.

Set one of these to a PEM bundle and the GitHub client will use it:

| Variable | Note |
|---|---|
| `QUORUM_CA_BUNDLE` | This project's own; wins over the rest |
| `REQUESTS_CA_BUNDLE` | Already set on most runners behind a proxy |
| `SSL_CERT_FILE` | OpenSSL's convention |
| `CURL_CA_BUNDLE` | |

A path that does not exist is ignored rather than fatal — a stale value left
over from an earlier image should not break a setup that works.

The Google and Anthropic clients are separate stacks with their own TLS
handling. `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` cover them in practice; if
Vertex calls fail at the handshake while GitHub calls succeed, that is the
difference.

## Versions

`v1` is a moving alias for the latest `v1.x.y`, which is the GitHub Actions
convention and what the examples use. `v1.x.y` tags are immutable; pin to one,
or to a commit SHA, in a regulated environment.

The alias is moved by [`release.yml`](../.github/workflows/release.yml) when a
version tag is pushed, not by hand. That is a reaction to getting it wrong:
`v1` once sat 38 commits behind `main` while the README told everyone to use
it, so the documented install path shipped without the repository-access
feature the README measures and without two security fixes to the workflow
trigger conditions. Nothing failed, no check went red, and the one repository
that had adopted it simply never received them.

The usual warning about tags is that one can be moved to point at *new* code.
A tag left pointing at old code is the same hazard with the opposite face, and
it is quieter.

To cut a release:

```bash
# bump __version__ in quorum_review/__init__.py and version in pyproject.toml
git tag -a v0.3.0 -m "..." && git push origin v0.3.0
```

The workflow refuses a tag that disagrees with `__version__`. A release whose
artefacts misreport their own version is worse than no release: every finding
it produces, in a comment and in the Security tab, is stamped with a number
that does not identify the code that made it.
