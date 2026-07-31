<div align="center">

# 🗳️ quorum-review

**Two models review your pull request. Neither one sees the other's work.**

Where they agree independently, that is the result. Where only one found
something, the other is asked to judge it. Both run on **a single Google Cloud
credential** — no API keys, no long-lived secret in your repository.

[![CI](https://github.com/yuting0624/quorum-review/actions/workflows/ci.yml/badge.svg)](https://github.com/yuting0624/quorum-review/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)
![Gemini on Vertex AI](https://img.shields.io/badge/Gemini-Vertex%20AI-4285F4?logo=googlegemini&logoColor=white)
![Claude on Vertex AI](https://img.shields.io/badge/Claude-Vertex%20AI-D97757?logo=anthropic&logoColor=white)

</div>

---

> ### 📐 A reference implementation, not a product
>
> This is a worked example of running Gemini and Claude together on Vertex AI.
> Pull request review is the subject matter, chosen because it makes the example
> concrete and testable — not an attempt to compete with the review tools that
> already exist. Read it expecting to learn the arrangement. No support channel,
> no compatibility promise.

## ⚡ What it looks like

One summary comment, edited in place on every run, plus an inline comment per
confirmed finding. From a real run:

> ## Quorum review
>
> `gemini-3.6-flash`, `claude-opus-5` each read the diff without seeing the
> other's output (`gemini-3.6-flash` 9 and `claude-opus-5` 11), which merged to
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
> | 🟡 medium | reliability | `app/export.py:23` | TOCTOU between existence check and open() | `claude-opus-5`, agreed by `gemini-3.6-flash` |
>
> <sub>Reviewed `03a1883` · models `gemini-3.6-flash`, `claude-opus-5` · 82s</sub>

The **Evidence** column is the point. *Both models, independently* means two
models that could not see each other's output reached the same conclusion —
better than either one's self-reported confidence, and it costs nothing extra.

## 💡 Why

Cross-model consensus, incremental review, and second-opinion verification all
exist elsewhere. What did not exist is running the models on **one cloud
credential**: every implementation we found stacks API keys from separate
vendors.

| | Two vendor API keys | quorum-review |
|---|---|---|
| **Secrets in the repo** | two long-lived keys | **none** — OIDC federation, short-lived credentials |
| **Billing** | two invoices, two contracts | one, against existing Google Cloud commitments |
| **Where the code goes** | two vendors' infrastructure | stays inside your Vertex AI region |
| **Model governance** | per-vendor, ad hoc | one `vertexai.allowedModels` org policy covers both |

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

```
GitHub event ──▶ google-github-actions/auth ──▶ short-lived GCP credentials
                                                          │
                        ┌─────────────────────────────────┴───────────────┐
                        │                                                 │
                  scan: Gemini                                      scan: Claude
                  (whole diff)                                      (whole diff)
                        │        neither sees the other's output          │
                        └────────────────────┬────────────────────────────┘
                                             ▼
                                     merge and compare
                                             │
                    ┌────────────────────────┴────────────────────────┐
                    ▼                                                 ▼
        both reported it                                   only one reported it
        → agreed, no further call                          → the other one judges it
                    │                                                 │
                    └────────────────────────┬────────────────────────┘
                                             ▼
                    confirmed → inline · uncertain → advisory · refuted → dropped
```

### Two findings from measuring this

**A second opinion cannot raise recall.** It only ever sees findings that were
already reported, so a bug the scanner missed is never put in front of it.
`gemini-3.6-flash` missed the same seeded TOCTOU bug in **three runs out of
three**; pairing it with `claude-opus-5` recovered nothing, because Claude was
never asked. Whichever model scans sets a hard ceiling on what the review can
find — which is why both models scan.

**Agreement is free, and it makes the whole thing cheaper.** Scanning is one call
regardless of diff size; verification is one call *per finding*. Letting
agreement stand without a second call means you pay for two scans plus the
disagreements — usually less than one scan plus a judgement on everything.

| Configuration | Calls | Recall |
|---|---|---|
| one scan, no verification | 1 | that model's ceiling |
| one scan, verify each finding | 1 + N | that model's ceiling |
| **both scan, verify disagreements** | **2 + d** | **the union** |

## 📊 Measured

Ten seeded bugs and three decoys that look dangerous but are correct. Three runs
per configuration. Full method and answer key:
[`benchmark/seeded-bugs/`](benchmark/seeded-bugs/README.md).

| Scanning | Found (mean of 3) | Stability | False positives |
|---|---|---|---|
| `gemini-3.6-flash` alone | 9.0 / 10 | one bug missed **3/3 times** | 0 |
| `claude-opus-5` alone | 10.0 / 10 | all 3/3 | 0 |
| **both, independently** | **10.0 / 10** | **all 3/3** | **0** |

No configuration was fooled by any decoy in any run. Live from Actions: 13
findings posted, **0% re-report rate** on an unchanged pull request.

> **Read this narrowly.** One fixture, three runs, written by the same person who
> wrote the reviewer. Enough to justify the dual-scan design; not enough to rank
> the models.

## 🚀 Install

```yaml
# .github/workflows/review.yml
permissions:
  contents: read
  id-token: write          # required for OIDC federation
  pull-requests: write

steps:
  - uses: google-github-actions/auth@v2
    with:
      workload_identity_provider: ${{ secrets.WIF_PROVIDER }}
      project_id: ${{ secrets.GOOGLE_CLOUD_PROJECT }}

  - uses: yuting0624/quorum-review@v1
    with:
      google-cloud-project: ${{ secrets.GOOGLE_CLOUD_PROJECT }}
```

**Prerequisites:** a Google Cloud project with Vertex AI enabled, and **Claude
enabled in Vertex AI Model Garden** — skip that and every Claude call returns 404
while the Gemini half keeps working, which is a confusing way to find out.
Full setup, including Workload Identity Federation: **[docs/setup-vertex.md](docs/setup-vertex.md)**.

Complete workflows: [`review-vertex.yml`](examples/review-vertex.yml) ·
[`review-vertex-app.yml`](examples/review-vertex-app.yml) (GitHub App token) ·
[`review-apikey.yml`](examples/review-apikey.yml) (no Google Cloud).

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
| `skill` | `security-review` | also ships `code-quality-review`; add your own under `skills/` |
| `primary-model` | a Gemini model | **confirm against your project** — `python -m quorum_review.review --list-models` |
| `verifier-model` | `claude-opus-5` | both models scan; the names only decide which runs alone under `scan: single` |
| `scan` | `both` | `single` is cheaper and caps recall at one model's |
| `verification` | `on` | `off` skips the second opinion on findings only one model raised |
| `incremental` | `on` | re-reviews only what changed since the last review |
| `exclude` | — | extra paths to skip, on top of the built-in defaults |
| `inline-severity` | `low` | lowest severity that gets its own comment in the diff view |
| `review-language` | English | e.g. `Japanese` — affects finding prose only |
| `github-token` | `GITHUB_TOKEN` | pass an App token to collapse resolved threads |
| `claude-vertex-region` | `global` | try `us-east5` if your entitlement is region-scoped |

**Cost tiers**, roughly, per review:

| Setting | Model calls | Trade |
|---|---|---|
| `scan: single`, `verification: off` | 1 | cheapest; one model's recall, unfiltered |
| `scan: single`, `verification: on` | 1 + N | filters false positives, recall still capped |
| **`scan: both`, `verification: on`** *(default)* | **2 + disagreements** | best recall, usually cheaper than the row above |

The default is not the most expensive option — easy to assume, and wrong.

## 🏷️ Post as your own GitHub App

```bash
python scripts/create_app.py
```

Reads [`app-manifest.yml`](app-manifest.yml), walks GitHub's manifest flow, and
prints the secrets to set. Worth doing for two reasons: resolved threads only
collapse with an App token — GitHub does not let the Actions app call
`resolveReviewThread`, whatever `permissions:` says — and comments arrive under
a name and avatar you chose.

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
- **Forks are unsupported.** They get a read-only token and no secrets.
- **A renamed file yields new findings.** Identity derives from the path.
- **Two instances of one pattern, described identically, collapse into one.**
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
skills/                      review criteria, pluggable
benchmark/                   seeded-bug fixture + repeated-measurement harness
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
  --primary gemini-3.6-flash --verifier claude-opus-5
```

</details>

---

## ⚠️ Disclaimer

Personal project, Apache-2.0. **Not affiliated with, endorsed by, or supported
by Google or Anthropic.** Gemini, Antigravity, and Google Cloud are trademarks of
Google LLC; Claude is a trademark of Anthropic PBC; GitHub is a trademark of
GitHub, Inc. Those names appear only to identify the services this
interoperates with. You are responsible for your own cloud costs, credentials,
and data-sharing choices. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
