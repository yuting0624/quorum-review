<div align="center">

# 🗳️ quorum-review

**Two models review your pull request. Neither one sees the other's work.**

Where they agree independently, that is the result. Where only one found
something, the other is asked to judge it.

**Gemini and Claude both run on Gemini Enterprise Agent Platform** — Google
Cloud's platform, formerly Vertex AI — off **one credential**. One federation
to set up instead of two, no API keys, one invoice.

[![CI](https://github.com/yuting0624/quorum-review/actions/workflows/ci.yml/badge.svg)](https://github.com/yuting0624/quorum-review/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
![Gemini on Gemini Enterprise Agent Platform](https://img.shields.io/badge/Gemini-Agent%20Platform-4285F4?logo=googlegemini&logoColor=white)
![Claude on Gemini Enterprise Agent Platform](https://img.shields.io/badge/Claude-Agent%20Platform-D97757?logo=anthropic&logoColor=white)

</div>

---

![hero image](hero.png)

> ### 📐 Built to be read as well as run
>
> This started as a worked example of running Gemini and Claude together on
> Gemini Enterprise Agent Platform, and it still reads like one: the
> arrangement is the interesting part, and
> [`providers/vertex.py`](quorum_review/providers/vertex.py) is written to be
> understood rather than skimmed.
>
> It is also in use, on this repository, on every pull request — including the
> ones that change it. **Issues and pull requests are welcome and get answered.**
> One person maintains it, so it is not a vendor: expect a considered reply, not
> a support contract.

## ⚡ What it looks like

One summary comment, edited in place on every run, plus an inline comment per
confirmed finding. From a real run:

> ## Quorum review
>
> `gemini-3.6-flash`, `claude-sonnet-5` each read the diff without seeing the
> other's output (`gemini-3.6-flash` 9 and `claude-sonnet-5` 11), which merged to
> **11** distinct finding(s). **9** of those were reported by both models
> independently.
> The remaining findings were each judged by the model that did *not* report
> them: **2** confirmed, **0** uncertain, **0** refuted.
>
> ### Confirmed — 3 critical, 5 high, 3 medium
>
> | Severity | Category | Location | Finding | Evidence |
> |---|---|---|---|---|
> | 🔴 critical | security | `app/search.py:18` | SQL injection via f-string interpolation | both models, independently |
> | 🟠 high | security | `app/fetcher.py:29` | SSRF: user URL fetched with no allowlist | both models, independently |
> | 🟡 medium | reliability | `app/export.py:23` | TOCTOU between existence check and open() | `gemini-3.6-flash`, agreed by `claude-sonnet-5` |
>
> <sub>Reviewed `03a1883` · models `gemini-3.6-flash`, `claude-sonnet-5` · 82s</sub>

The **Evidence** column is the point. *Both models, independently* means two
models that could not see each other's output reached the same conclusion —
better than either one's self-reported confidence, and it costs nothing extra.

**And here is where that stops working.** Independent agreement is evidence
about what both models could see. It says nothing about context neither of them
has, because the thing they are missing is the same thing — the independence is
in the reasoning, not in the evidence.

A concrete case, from someone adopting this: a workflow passes
`pr-number: ${{ github.event.inputs.pr }}`, which is empty on every trigger
except `workflow_dispatch`. That is correct — the action unsets an empty value
and derives the number from the event payload — but the code that makes it
correct lives in *this* repository, outside the checkout a reviewer's tools can
read. Both models flagged it as a bug, independently, so it arrived in the
confirmed table with the strongest evidence this project can produce. Four
model reviews in total, all agreeing, all wrong, all for the same reason.

Two scans buy you insurance against one model's blind spot. They do not buy
insurance against a blind spot in what you handed both of them.

## 💡 Why

Claude is available on **Gemini Enterprise Agent Platform** alongside Gemini.
That is the whole basis of this project: two vendors' models, one platform, one
credential, one invoice.

Cross-model consensus, incremental review, and second-opinion verification all
exist elsewhere. What did not exist is running both models on **one cloud
credential**.

| | Two vendors, separately | quorum-review |
|---|---|---|
| **Trust to configure** | two federations, or two long-lived keys | **one** — the Google Cloud one you already have |
| **Secrets in the repo** | none, if both vendors support federation | **none** |
| **Billing** | two invoices, two contracts | one, against existing Google Cloud commitments |
| **Where the code goes** | two vendors' infrastructure | one platform, in your own project — [pin the region](docs/security.md#where-the-code-goes) and it stays there |
| **Model governance** | per-vendor, ad hoc | one org policy covers both |

> **This table used to say "two long-lived keys" against "none", and that was
> the whole argument.** It stopped being true in June 2026, when Anthropic
> shipped [workload identity federation](https://platform.claude.com/docs/en/manage-claude/workload-identity-federation):
> a GitHub Actions OIDC token can be exchanged for a short-lived Anthropic
> credential, so the Claude half no longer needs a stored key either. The
> Gemini half of a two-vendor setup still does, because the AI Studio API takes
> an API key — but "two stored secrets" is now "one".
>
> What survives is narrower and, I think, still worth having: **one trust
> relationship instead of two.** One issuer to register, one set of claim
> conditions to get right, one place where a misconfiguration lets the wrong
> workflow mint a token. Plus the invoice and the governance surface. If you
> only care about "no keys in the repository", federate both vendors and you
> are done — this project is about the number of things you have to get right,
> not the number of secrets.

The whole trick is what these two lines *don't* contain:

```python
gemini = genai.Client(vertexai=True, project=project, location="global")
claude = AsyncAnthropicVertex(project_id=project, region="global")
```

No API key in either. Both resolve the same Application Default Credentials that
`google-github-actions/auth` just wrote. [`providers/direct.py`](quorum_review/providers/direct.py)
implements the two-API-key version so the comparison is concrete rather than
rhetorical; [`providers/vertex.py`](quorum_review/providers/vertex.py) is the file worth reading.

## 🔀 How a review runs

![review architecture](architecture.png)

Both models also get three read-only tools over the checkout — `read_file`,
`search`, `list_files` — each on its own budget. That is what lets a reviewer
answer *"is this already handled somewhere else?"*: the question a diff cannot
answer, and the one behind most false positives and most misses. Nothing writes,
nothing executes, nothing leaves the runner.

### Two findings from measuring this

**A second opinion cannot raise recall.** It only ever sees findings that were
already reported, so a bug the scanner missed is never put in front of it.
`gemini-3.6-flash` missed the same seeded TOCTOU bug in **three runs out of
three**; a verifier recovered nothing, because it was never asked. Whichever
model scans sets a hard ceiling on what the review can find.

This is the finding that changed the design. The original plan had one model
scan and the other check — the arrangement everyone reaches for, because it
sounds like two opinions. It is one opinion and an audit of it, and the audit
cannot see what the first model walked past.

**So both models scan, and the second opinion is what is left over.** Where they
agree independently, that agreement is the result and costs no further call.
Where only one reported something, the other judges it. Scanning is one call
regardless of diff size; verification is one call *per finding*, so paying for
two scans and only the disagreements is usually less work than one scan and a
judgement on everything.

| Configuration | Calls | Recall |
|---|---|---|
| one scan, no verification | 1 | that model's ceiling |
| one scan, verify each finding | 1 + N | that model's ceiling |
| **both scan, verify disagreements** | **2 + d** | **the union** |

## 📊 Measured

A fixed pull request with ten seeded bugs, three decoys that look dangerous but
are correct, and two bugs that **cannot be decided from the diff** — their
correctness depends on a validator and a permission registry the pull request
never touches. Three runs per configuration. Full method, answer key and raw
findings: [`benchmark/seeded-bugs/`](benchmark/seeded-bugs/README.md).

**Two models scanning independently find more than either one does:**

| Scanning | Seeded bugs | All real bugs *(18 in the fixture)* |
|---|---|---|
| `gemini-3.6-flash` alone | 8.3 / 10 | 8.7 |
| `claude-sonnet-5` alone | 7.7 / 10 | 8.0 |
| **both, independently** *(default)* | **9.3 / 10** | **10.0** |

The union beats both, and it beats them **in both directions** — this is not one
model carrying the other:

| | Found by the other model only |
|---|---|
| `claude-sonnet-5` missed | a TOCTOU between an existence check and `open()`; a share link that never expires |
| `gemini-3.6-flash` missed | a permission check whose correctness depends on a registry the diff never touches |

Neither model is a subset of the other, and neither knew what the other was
looking at. That is the whole argument for two scans, and it is the reason the
second opinion is spent on disagreements rather than on everything.

**The disagreement is also where a false positive dies.** In one of the three
runs a scan reported the decoy — a raw path join whose guard is one file away.
The other model was asked, went and read the caller, and refuted it. It never
reached the pull request.

**Reading past the diff decides whether it can be right about them:**

| | Seeded bugs | Diff-undecidable bugs | False positive on the decoy |
|---|---|---|---|
| diff only | 8.7 / 10 | **0 / 2**, every run | **3 / 3 runs** |
| **repository readable** *(default)* | 8.7 / 10 | 2 / 2 in two runs of three | **0 / 3 runs** |

Same models, same fixture, same three runs each. The only difference is whether
the tools were available.

The decoy is a path join with nothing guarding it on the changed lines, whose
every caller validates first — in a file the pull request does not touch. The
diff-only reviewer reports it **every time**, and is wrong every time; it is
behaving correctly given what it can see. With the repository readable, two runs
never raise it and the third has it **removed by the second opinion**, which
went and read the caller.

The two diff-undecidable bugs go the other way: unreachable without the tools,
found with them. Recall and precision move together here, which is unusual and
is the argument for the tools.

Re-measured after the answer key turned out to be reachable a **third** time —
[the harness itself carried a copy](benchmark/seeded-bugs/README.md#three-contamination-sources-all-found-by-giving-the-models-access).
Every number on this page is from a run made after that was closed; the earlier
ones were discarded, as the two before them were.

The fixture also contains real bugs nobody planted — written by accident, found
by the models, and credited to the answer key afterwards. Unplanted bugs are
better evidence than planted ones, because nobody chose them. One of them cannot
be reached from a diff at all: a registry mapping every export format to a
module under `app.formatters`, where no such package exists. That is a fact
about the filesystem, not about any line of code.

Live from Actions: **0% re-report rate** on an unchanged pull request.

<details>
<summary><b>It found two real security bugs in the commit that gave it repository access</b></summary>

Neither was visible in the diff. Both required reading files the change did not
touch, which is the capability being tested.

**The fork guard admitted forks.** The workflow condition `head.repo.fork != true`
reads a field that is `null` on `issue_comment` events — that payload has no
`pull_request` object at all — and `null != true` is true. So `@quorum /review`
on a fork's pull request satisfied a condition written to exclude forks. The
reviewer had to read `.github/workflows/review.yml` against `review.py` to see
it.

**One policy file, read at two different refs.** `.quorumignore` is read at the
base branch for an untrusted head, so a fork cannot exclude its own files from
review — except that the file tools read it *again* from the checkout, which is
the head. The base's copy governed the diff while the head's copy governed the
tools. That needed `github_client.py` read against `workspace.py`.

Both are fixed, with regression tests naming the mechanism. The relevant point
is not that the tool is clever — it is that a diff-only reviewer had already
looked at these same lines and said nothing, because neither bug is *in* a line.

</details>

> **Read this narrowly.** One fixture, written by the same person who wrote the
> reviewer. Three rounds of results have been thrown away because the models
> turned out to be reading the answer key — [every source is documented rather
> than quietly fixed](benchmark/seeded-bugs/README.md#three-contamination-sources-all-found-by-giving-the-models-access),
> one of them was found by the reviewer itself, and the third was found in a
> tool log while measuring for this table. Enough to justify the design; not
> enough to rank the models.

## 🚀 Install

```yaml
# .github/workflows/review.yml
permissions:
  contents: read
  id-token: write          # required for OIDC federation
  pull-requests: write

steps:
  # Not optional. This is the tree the models read when a finding depends on
  # code the diff does not contain — the difference measured above. Without
  # it the review silently falls back to diff-only and says so in the summary.
  - uses: actions/checkout@v4
    with:
      ref: refs/pull/${{ github.event.pull_request.number }}/merge
      fetch-depth: 2

  - uses: google-github-actions/auth@v2
    with:
      workload_identity_provider: ${{ secrets.WIF_PROVIDER }}
      project_id: ${{ secrets.GOOGLE_CLOUD_PROJECT }}

  - uses: yuting0624/quorum-review@v1
    with:
      google-cloud-project: ${{ secrets.GOOGLE_CLOUD_PROJECT }}
```

**Prerequisites:** a Google Cloud project with **Gemini Enterprise Agent
Platform** (formerly Vertex AI) enabled, and **Claude enabled in Model
Garden** — skip that and every Claude call returns 404 while the Gemini half
keeps working, which is a confusing way to find out.
Full setup, including Workload Identity Federation: **[docs/setup-vertex.md](docs/setup-vertex.md)**.
How to read the output, what it costs, and what to do when it breaks:
**[docs/operations.md](docs/operations.md)**.

Complete workflows: [`review-vertex.yml`](examples/review-vertex.yml) ·
[`review-vertex-app.yml`](examples/review-vertex-app.yml) (GitHub App token) ·
[`review-apikey.yml`](examples/review-apikey.yml) (no Google Cloud) ·
[`review-fork.yml`](examples/review-fork.yml) (fork pull requests).

## 💬 Talking to it

| you write | it does |
|---|---|
| `@quorum /review` | re-review the pull request |
| `@quorum <question>` *(reply in a thread)* | the model that reported the finding answers — and concedes when you are right |
| `@quorum wontfix — <why>` *(reply in a thread)* | retires that finding; the reason is kept |
| `@quorum /criteria` | turns accumulated dismissals into a proposed edit to your `SKILL.md` |

Dismissals are proposals into the criteria, never applied automatically:
the reasons arrive in pull request comments, and writing those straight into the
review criteria would let someone argue the reviewer out of a whole category of
finding.

## ⚙️ Configuration

| Input | Default | Notes |
|---|---|---|
| `mode` | `vertex` | `direct` uses `GEMINI_API_KEY` + `ANTHROPIC_API_KEY` |
| `skill` | `security-review` | a built-in name, or a path to criteria in your own repository; several combine |
| `primary-model` | `gemini-3.8-flash` | **confirm against your project** — `python -m quorum_review.review --list-models` |
| `verifier-model` | `claude-sonnet-5` | both models scan; the names only decide which runs alone under `scan: single` |
| `scan` | `both` | `single` is cheaper and caps recall at one model's |
| `verification` | `on` | `off` skips the second opinion on findings only one model raised |
| `repo-access` | `on` | read-only tools over the checkout, so a finding that turns on code outside the diff can be settled instead of guessed. Needs `actions/checkout` |
| `fail-on` | `never` | `critical`/`high`/`medium`/`low` — exit non-zero so the action can be a required check |
| `fail-on-degraded` | `false` | also fail when the reviewer could not run properly. See below |
| `max-diff-characters` | `400000` | whole-diff budget; files that do not fit are named, never quietly skipped |
| `max-tokens` | `0` (none) | ceiling on what one review may spend, across both models |
| `fork-label` | `quorum: review` | label that authorises reviewing a pull request from a fork |
| `sarif-file` | — | write findings as SARIF for code scanning |
| `incremental` | `on` | re-reviews only what changed since the last review |
| `exclude` | — | extra paths to skip, on top of the built-in defaults |
| `inline-severity` | `low` | lowest severity that gets its own comment in the diff view |
| `max-inline-comments` | `25` | how many do, worst first; the rest stay in the summary |
| `review-language` | English | e.g. `Japanese` — affects finding prose only. [One caveat](docs/operations.md#reviewing-in-a-language-other-than-english) |
| `github-token` | `GITHUB_TOKEN` | pass an App token to post under a name you chose |
| `vertex-region` | `global` | pin both models to one region for data residency — see [security.md](docs/security.md#where-the-code-goes) |
| `claude-vertex-region` | inherits | override just Claude; try `us-east5` if your entitlement is region-scoped |
| `gemini-location` | inherits | override just Gemini |

**On the default pair.** `gemini-3.8-flash` and `claude-sonnet-5` are the
defaults. The benchmark below established that the previous Gemini default,
`gemini-3.6-flash`, complemented Claude in both directions. A model upgrade can
change those blind spots, so re-run `benchmark/measure.py` before treating the
same complementarity as measured for the new pair. Swapping either model is one
input.

**Effort tiers**, roughly, per review:

| Setting | Model calls | Trade |
|---|---|---|
| `scan: single`, `verification: off` | 1 | fewest calls; one model's recall, unfiltered |
| `scan: single`, `verification: on` | 1 + N | filters false positives, recall still capped |
| **`scan: both`, `verification: on`** *(default)* | **2 + disagreements** | the union of both models, and fewer calls than the row above |

The default is not the most expensive option — easy to assume, and wrong.
Verification is one call *per finding*; agreement between two independent scans
costs none.

`max-tokens` puts a ceiling on one review, which is the question a platform team
asks before enabling something on two hundred repositories. In tokens rather
than money, for the same reason the summary reports tokens: prices differ by
model, platform and contract, so a currency figure computed here would be a
guess wearing the costume of a fact.

It binds where cost scales with *findings* rather than with the diff — a scan is
one call sized by the diff, verification is one call each. A review that reaches
the ceiling stops verifying and says so in the summary; findings are demoted to
advisory, never dropped.

### 🚦 Making it a required check

`fail-on` turns the reviewer into something a branch protection rule can depend
on. Findings the reviewer declined to stand behind — advisory, refuted — never
gate; blocking on those would teach people the verdicts mean nothing.

```yaml
  - uses: yuting0624/quorum-review@v1
    id: review
    with:
      google-cloud-project: ${{ secrets.GOOGLE_CLOUD_PROJECT }}
      fail-on: critical

  - if: steps.review.outputs.degraded == 'true'
    run: echo "reviewed with less than full context — do not read a clean result as clean"
```

Outputs: `findings`, `critical`, `high`, `medium`, `low`, `advisory`, `refuted`,
`resolved`, `degraded`, `repo-access`. The review also lands in the Actions job
summary, so it is readable even when posting to the pull request fails.

`fail-on-degraded` is separate, and off by default. Both defaults are judgement
calls worth stating: a required check that passes because the reviewer was
broken is worse than no check, but one that blocks every merge in the
organisation because a Vertex region is having a bad afternoon is its own
outage. Two policies, two switches.

### 📋 Your criteria, not mine

The built-in criteria are a starting point, not a standard. Point `skill` at a
file in **your** repository and the review asks what your security review asks:

```yaml
    with:
      skill: security-review, .github/quorum/backend.md
```

A bare name is a built-in; anything with a slash or ending in `.md` is read from
the repository being reviewed. Up to four, concatenated. Criteria are
instructions to the model, so for a pull request from a fork they come from the
base branch — a branch does not get to choose the standard it is judged against.

### 🔎 In the Security tab, not just the pull request

A pull request comment is read once by whoever is looking at that pull request.
It is not a queue, it has no owner, and nothing counts it. `sarif-file` writes
the findings for `github/codeql-action/upload-sarif`, and they then appear in
code scanning — deduplicated across runs by content, tracked open-to-fixed, and
routed through the triage process you already have.

```yaml
    with:
      sarif-file: quorum.sarif
```
```yaml
  - if: always() && hashFiles('quorum.sarif') != ''
    uses: github/codeql-action/upload-sarif@v3
    with: { sarif_file: quorum.sarif, category: quorum-review }
```

Only findings the reviewer stands behind are uploaded. Advisory and refuted ones
stay in the comment: the Security tab is a queue someone is expected to empty,
and a queue full of maybes is not emptied.

Two things that are easy to get wrong, both learnt by having the upload
rejected:

- The SARIF carries the **whole open state from the ledger**, not what this run
  newly reported. Code scanning treats an upload as a replacement, so a
  re-review that finds nothing new — the common case, by design — would
  otherwise mark every earlier alert fixed.
- The upload needs `security-events: write` **and** `actions: read`, and code
  scanning has to be enabled on the repository at all: free on public ones, part
  of GitHub Advanced Security on private ones. The example marks the step
  `continue-on-error` for exactly that reason — a red check every run for a
  feature you have not bought is how the whole workflow gets deleted.

### 🏢 Rolling it out across an organisation

Copying the workflow into fifty repositories means copying the trigger
conditions too — and this project has already had to fix two security bugs in
those conditions. Neither fix would have reached a repository that copied the
file in January.

So they live in one place. Fork this repository, then each repository needs:

```yaml
# .github/workflows/review.yml
name: quorum-review
on:
  pull_request:
    types: [opened, reopened, synchronize]
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]
jobs:
  review:
    uses: your-org/quorum-review/.github/workflows/reusable.yml@v1
    secrets: inherit
    with:
      fail-on: critical      # optional
```

`on:` has to be in the caller — GitHub resolves triggers from the calling
workflow, so a reusable one cannot decide when it runs. Everything else,
including `action-ref` for pinning to a commit SHA and `runs-on` for a
self-hosted runner, is an input.

`synchronize` is worth adding once you have `incremental: on`: a re-review reads
the range since the last one and the ledger suppresses what was already
reported, so the marginal cost of a push is about the size of that push.

### 🍴 Pull requests from forks

Supported, gated on a label, in a separate workflow:
[`review-fork.yml`](examples/review-fork.yml). Read the comments at the top
before copying it — `pull_request_target` is the trigger people get compromised
by, and the reason this use is safe is that **nothing from the fork is ever
executed**. Add a dependency-install step and that stops being true.

Two conditions, both re-checked by the action rather than trusted to the
workflow's `if:` — the label, and write access for whoever applied it.

## 🏷️ Post as your own GitHub App

```bash
python scripts/create_app.py
```

Reads [`app-manifest.yml`](app-manifest.yml), walks GitHub's manifest flow, and
prints the secrets to set. It buys you one thing: comments arrive under a name
and avatar you chose, instead of `github-actions[bot]`.

It does **not** make resolved threads collapse. That was the original reason for
it and the reason turned out to be wrong — see
[Resolved threads stay open](docs/operations.md#resolved-threads-stay-open).

The App receives no webhooks and runs nowhere; Actions already delivers the
events, so it exists purely to mint short-lived tokens. It cannot write to your
repository. A single shared App is not distributable — its private key would
have to come with it — which is why you make your own.

---

<details>
<summary><b>🧠 How the pieces fit</b></summary>

**Independence is enforced, not assumed.** Scans run concurrently with identical
prompts and never see each other. A finding only one model raised is sent to the
other with the location, the code, and a one-line claim — not the argument, not
the severity, not who reported it. Give a model someone else's reasoning and it
agrees with it; the stage then filters nothing.
`test_verify_prompt_withholds_the_reporters_reasoning` exists to stop a
well-meaning refactor eroding that.

**Findings are identified by content, not by line.** Line-based identity breaks
the moment someone adds an import. Identity is
`sha256(path + normalised code)` with no line number in it, plus positional and
wording matching across models and across runs
([`matching.py`](quorum_review/matching.py)). Getting this wrong is what made the
first live re-run post eight duplicate comments.

**State lives in the summary comment**, as a hidden base64 marker, gzipped past
4 KB. No database, no configuration, and the state cannot drift away from the
pull request it describes.

**Everything degrades rather than failing.** A scanning model that dies leaves the
other's findings; a verifier that dies makes findings advisory; a token that
cannot collapse threads still posts the reply. In every case the summary says
which happened — a reviewer running at half strength must not look like a clean
one.

Full write-up: [docs/architecture.md](docs/architecture.md).

</details>

<details>
<summary><b>🔒 Security model</b></summary>

The reviewer reads attacker-controlled input by design: the diff, the title, the
body, and every comment. So the question is not whether hostile input arrives —
it always does — but what it can accomplish.

- **Untrusted input is labelled as data.** Everything goes inside `<untrusted_*>`
  tags, and the system prompt tells the model to report embedded instructions as
  a finding rather than follow them.
- **Output is schema-constrained** and re-validated on our side; entries with an
  out-of-enum severity are discarded rather than repaired.
- **Two independent scans are the strongest control.** A diff that talks one
  model out of reporting something has to fool a second model too, with no way
  to know whether it worked the first time.
- **No credential is ever in reach of a model.** They see the diff; they do not
  execute code or make network calls.
- **The reviewer cannot write to your repository.** `contents: read`, and no code
  path that pushes.
- **Forks are excluded**, because `pull_request_target` would mean write access
  while reviewing untrusted code.

Details, including the operational guidance: [docs/security.md](docs/security.md).

</details>

<details>
<summary><b>🚧 Known limits</b></summary>

- **No sandbox.** The intended primary stage is an agent that can check out the
  branch, install dependencies, and run tests. That API is pre-GA and not
  licensed for production, so the scan is a plain model call over the diff. The
  `ReviewProvider` protocol exists so that swap costs one new file.
- **Diff-only context.** Findings that need to compare a change against code the
  diff does not touch — a constant documented in one file and changed in
  another — are out of reach.
- **A file renamed *and* edited beyond recognition yields new findings.** Renames themselves are followed; git's own similarity detection decides what counts as one.
- **Two instances of one pattern, described identically, collapse into one.**
- **Deleting the summary comment loses some state.** Findings and dismissals
  are recovered from the comments still on the pull request; severity, which
  model raised each one, and dismissal reasons are not.
- **"No longer reported" does not mean fixed** — it can also mean the scan did
  not raise it this time. The summary says only what is known.
- **`issue_comment` workflows run from the default branch.** Editing the workflow
  on a branch does not change how `@quorum` behaves on that pull request. A
  GitHub Actions rule, not something this can work around.

</details>

<details>
<summary><b>📦 What's inside · development</b></summary>

```
action.yml                   composite action
quorum_review/
  review.py                  orchestrator: scan → merge → verify → post
  providers/vertex.py        Gemini + Claude on one credential  ← the point
  providers/direct.py        the two-API-key control case
  consensus.py               merging independent scans
  matching.py                is this the same defect? (across models, across runs)
  ledger.py                  finding identity and state, in a hidden comment marker
  conversation.py            answering questions in a thread
  dismissal.py               retiring a false positive
  learning.py                dismissals → proposed criteria change
  github_client.py           REST + the GraphQL needed to resolve threads
  workspace.py               the read-only tools the models use on the checkout
  criteria.py                built-in and repository-supplied review criteria
  redaction.py               keeping a finding from republishing the secret
  forks.py                   who may have their fork reviewed, and on whose say-so
  actions.py                 exit code, outputs, job summary
  sarif.py                   findings for the Security tab
  budget.py                  a ceiling on what one review may spend
  skills/                    built-in criteria, shipped inside the package
benchmark/                   seeded-bug fixture + repeated-measurement harness
  runs/                      raw findings behind the recorded numbers
scripts/create_app.py        GitHub App manifest flow
```

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check .
```

Measure a configuration against the fixture:

```bash
python -m benchmark.measure --pr 1 --runs 3 \
  --primary gemini-3.8-flash --verifier claude-sonnet-5
```

</details>

---

## 🔗 Also by the same author

**[antigravity-for-claude-code](https://github.com/yuting0624/antigravity-for-claude-code)** —
a Claude Code plugin that runs the Antigravity CLI (Gemini) as a collaborating
sub-agent, with model routing across the SDLC.

Same idea from the other end. That one puts Gemini and Claude together in the
editor, while you are writing the code; this one puts them together in CI, after
you have pushed it. If you want a second model reading over your shoulder rather
than reviewing the result, start there.

---

## ⚠️ Disclaimer

Personal project, Apache-2.0. **Not affiliated with, endorsed by, or supported
by Google or Anthropic.** Gemini, Antigravity, and Google Cloud are trademarks of
Google LLC; Claude is a trademark of Anthropic PBC; GitHub is a trademark of
GitHub, Inc. Those names appear only to identify the services this
interoperates with. You are responsible for your own cloud costs, credentials,
and data-sharing choices. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
