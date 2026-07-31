# quorum-review

**Cross-model pull request review on Google Cloud.**

Two models review your pull request. Each reads the diff without seeing the
other's output. Where they agree independently, that is the result. Where only
one of them found something, the other is asked to judge it. Only what survives
gets posted.

The interesting part is not the review. It is that both models run on **a single
Google Cloud credential**, federated from GitHub Actions, with no long-lived
secret stored in the repository.

> ### This is a reference implementation, not a product
>
> It is a worked example of running Gemini and Claude together on Vertex AI.
> Pull request review is the subject matter chosen to make the example concrete
> and testable — it is not an attempt to compete with the review tools that
> already exist. Read the code expecting to learn the arrangement, not to adopt
> a supported service. There is no support channel and no compatibility promise.

---

## Why this exists

Cross-model consensus, incremental review, and second-opinion verification are
all things other projects already do. What none of them do is run the models on
one cloud credential: every implementation we found stacks API keys from
separate vendors.

For a team already on Google Cloud, that difference is the whole story:

| | Two vendor API keys | This repository |
|---|---|---|
| Secrets in the repo | Two long-lived keys | **None** — OIDC federation issues short-lived credentials |
| Billing | Two invoices, two contracts | One, against existing Google Cloud commitments |
| Where the code goes | Two vendors' infrastructure | Stays inside your Vertex AI region |
| Model governance | Per-vendor, ad hoc | `vertexai.allowedModels` org policy covers both |

`src/providers/direct.py` implements the two-API-key version so the comparison is
concrete rather than rhetorical. `src/providers/vertex.py` is the file worth
reading.

## How a review runs

```
GitHub event (pull_request, or an @quorum /review comment)
  │
  ├─ google-github-actions/auth   OIDC token → short-lived Google Cloud credentials
  │
  └─ quorum-review
       ├─ read the PR diff, and the ledger from the previous run
       │
       ├─ scan   Gemini on Vertex  ─┐  independently, neither sees the other
       ├─ scan   Claude on Vertex  ─┤
       │                            ▼
       ├─ merge   both reported it → agreed, no further call
       │          one reported it  → the other model judges it
       │
       └─ post   confirmed inline · uncertain as advisory · refuted discarded
```

Both models authenticate off the same Application Default Credentials. That is
the entire point of the repository.

### Why both models scan

**A second opinion can only judge findings that were reported.** It is never
shown a bug the first model missed, so with one model scanning, that model's
blind spots are the whole reviewer's blind spots — no verifier, however strong,
can raise recall.

We measured exactly that. `gemini-3.6-flash` missed the same seeded TOCTOU bug
in three runs out of three; pairing it with `claude-opus-5` did not help,
because Claude was never asked about it. Having both models scan fixes it,
because recall becomes the union rather than one model's ceiling.

### Agreement is evidence, and it is free

When two models that could not see each other's output report the same defect,
that is a stronger signal than either one's self-reported confidence — and it
costs nothing extra to obtain. Those findings skip verification entirely.

Only the disagreements get a second call, which makes the arrangement *cheaper*
than verifying everything: on a diff where the models mostly agree, you pay for
two scans and a handful of judgements instead of one scan and one judgement per
finding.

### The judge never sees the reporter's reasoning

A finding that only one model raised is sent to the other with the location, the
code, and the one-line claim — not the argument, not the severity rating, not
who reported it. Hand a model someone else's rationale and it agrees with it;
the stage stops filtering anything. See `prompts.verify_user`.

This also blunts prompt injection: a diff crafted to talk one model out of
reporting something still has to get past a second model that never saw the
manipulated session — and that second model is now scanning too, not just
reacting.

### Living with it

**Dismiss a false positive** by replying `@quorum wontfix — <why>` to the review
comment. It is not reported again, and the reason is kept.

**Resolved findings close themselves.** When a finding stops being reported, its
thread gets a reply and is collapsed. A wall of open comments for problems that
are gone is how a reviewer teaches people to ignore it.

**Fixes arrive as suggestions when they are safe.** Only when the model can
replace the anchored lines completely — a partial fix someone clicks apply on is
worse than no fix.

**Generated files are skipped.** Lockfiles, vendored code, build output, and
generated sources are excluded by default; add your own with `exclude` or a
`.quorumignore`. Whatever is skipped is named in the summary.

**Re-reviews read only the new commits**, using the sha in the ledger. Findings
in files those commits do not touch are carried over rather than re-derived —
and, importantly, are not mistaken for fixed.

### Findings are tracked by content, not by line

The failure mode of automated review is not wrong findings — it is the *same*
finding re-posted on every push. Findings are keyed on
`sha256(file_path + normalised_code)`, with **no line number in the hash**, so
adding an import above a finding does not resurrect it. State lives in a hidden
marker inside the summary comment: no external storage, nothing to configure,
and the state travels with the pull request. See `src/ledger.py`.

## Setup

Vertex mode (recommended) — see **[docs/setup-vertex.md](docs/setup-vertex.md)**
for Workload Identity Federation and, easy to miss, enabling Claude in Vertex AI
Model Garden.

```yaml
# .github/workflows/review.yml
permissions:
  contents: read
  id-token: write        # required for OIDC federation
  pull-requests: write

steps:
  - uses: actions/checkout@v4
  - uses: google-github-actions/auth@v2
    with:
      workload_identity_provider: ${{ secrets.WIF_PROVIDER }}
      project_id: ${{ secrets.GOOGLE_CLOUD_PROJECT }}
  - uses: your-org/quorum-review@main
    with:
      google-cloud-project: ${{ secrets.GOOGLE_CLOUD_PROJECT }}
```

Full workflows: [`examples/review-vertex.yml`](examples/review-vertex.yml) and
[`examples/review-apikey.yml`](examples/review-apikey.yml).

## Configuration

| Input | Default | Notes |
|---|---|---|
| `mode` | `vertex` | `direct` uses `GEMINI_API_KEY` + `ANTHROPIC_API_KEY` instead |
| `skill` | `security-review` | Also ships `code-quality-review`; add your own under `skills/` |
| `primary-model` | a Gemini model | **Confirm this against your project** — see below |
| `verifier-model` | `claude-opus-5` | Both models scan; the names only decide which runs alone under `scan: single` |
| `scan` | `both` | `single` uses one model — cheaper, but caps recall at that model's |
| `verification` | `on` | `off` skips the second opinion on findings only one model raised |
| `incremental` | `on` | Re-reviews only what changed since the last review |
| `exclude` | — | Extra paths to skip, on top of the built-in defaults |
| `review-language` | English | e.g. `Japanese` — affects finding prose only |
| `claude-vertex-region` | `global` | Try `us-east5` if your entitlement is region-scoped |
| `max-verified-findings` | `20` | Cap on second opinions; ignored when verification is off |

**Cost tiers.** Roughly, per review:

| Setting | Model calls | Trade |
|---|---|---|
| `scan: single`, `verification: off` | 1 | Cheapest. One model's recall, unfiltered |
| `scan: single`, `verification: on` | 1 + N | Filters false positives, still capped recall |
| `scan: both`, `verification: on` (default) | 2 + disagreements | Best recall. Usually cheaper than the row above |

The default is not the most expensive option, which is easy to assume and wrong:
agreement between the two scans removes most of the per-finding calls.

**Swapping the roles is configuration, not code.** Set `primary-model` to a
Claude ID and `verifier-model` to a Gemini ID and the pipeline reverses —
engines are resolved by model ID, not hardcoded per stage. Which ordering
actually performs better is an open question; `benchmark/` exists to answer it.

**Confirm your Gemini model ID before the first run.** Availability varies by
project and release channel, and it is the single most common thing to get
wrong:

```bash
python -m src.review --list-models
```

## Known limitations

- **Managed Agents is not used.** The design this is modelled on calls for a
  persistent sandbox that can install packages and run tests. That API is
  pre-GA and not licensed for production, so the primary stage is a plain
  Gemini call over the diff instead. The `ReviewProvider` protocol
  (`src/providers/base.py`) exists so a sandboxed implementation can be dropped
  in later without touching the orchestrator.
- **Forks are unsupported.** `GITHUB_TOKEN` is read-only on pull requests from
  forks, so posting fails. `pull_request_target` would fix that by running
  untrusted code with write access, which is not a trade worth making.
- **A renamed file yields new findings.** Identity is derived from the path, so
  a rename reads as a new location.
- **Two instances of one pattern, described identically, collapse into one.**
  Findings are matched across models and across runs by position and wording
  (`src/matching.py`). If the same defect appears twice in a file and a model
  gives both occurrences the same title, they merge and the second is lost.
- **"No longer reported" does not mean fixed.** A finding drops off when the
  scan stops raising it, which can be a fix or can be non-determinism. The
  summary says only what is known, and such findings are un-suppressed so they
  reappear if detected again.
- **`issue_comment` workflows run from the default branch.** Editing the
  workflow on a PR branch does not change how `@quorum /review` behaves on that
  PR — a GitHub Actions rule, not something this project can work around.
- **Re-review on push is opt-in.** `synchronize` is deliberately absent from the
  example workflows: it burns a review on every commit, and bot comments can
  re-trigger the workflow.
- Phase 0 scope. Incremental re-review, conversational replies, false-positive
  dismissal, and suggested fixes are not implemented yet.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check .
```

`benchmark/seeded-bugs/` is a fixture with ten known bugs and three decoys that
look dangerous but are correct. It is how detection rate and false-positive
count get measured rather than asserted — including whether the verification
stage earns its cost. See [`benchmark/seeded-bugs/README.md`](benchmark/seeded-bugs/README.md).

## Documentation

- [docs/setup-vertex.md](docs/setup-vertex.md) — federation, IAM, Model Garden
- [docs/architecture.md](docs/architecture.md) — why the pipeline is shaped this way
- [docs/security.md](docs/security.md) — the threat model and what it does not cover

## License

Apache-2.0. See [LICENSE](LICENSE).

---

<sub>Gemini and Antigravity are trademarks of Google LLC. Claude is a trademark
of Anthropic PBC. This is not an official Google product and is not endorsed by
or affiliated with Google or Anthropic.</sub>
