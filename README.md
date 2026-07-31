# quorum-review

**Cross-model pull request review on Google Cloud.**

Two models review your pull request. One scans the diff broadly; the other
re-examines each finding independently and throws out the ones that do not hold.
Only what survives gets posted.

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
       ├─ primary scan     Gemini on Vertex — cast a wide net, favour recall
       ├─ verification     Claude on Vertex — one call per finding, favour precision
       └─ post             confirmed inline · uncertain as advisory · refuted discarded
```

Both models authenticate off the same Application Default Credentials. That is
the entire point of the repository.

### Three things worth knowing about the two stages

**The verifier never sees the reporter's reasoning.** It gets the location, the
code, and the one-line claim — not the argument, not the severity rating, not
which model produced it. Hand a model someone else's rationale and it agrees
with it; the stage stops filtering anything. See `prompts.verify_user`.

**Verification raises precision and can never raise recall.** The verifier only
judges findings the primary already reported, so a bug the primary missed is
never put in front of it. **The primary model sets a hard ceiling on what the
pipeline can find.** If you are missing bugs, change the primary; a stronger
verifier cannot help. Conversely, verification can remove a true positive — in
our measurements it did.

**Verification also blunts prompt injection.** A diff crafted to talk the primary
model out of reporting something still has to get past a second model that never
saw the manipulated session.

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
      service_account: ${{ secrets.WIF_SERVICE_ACCOUNT }}
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
| `verifier-model` | `claude-opus-5` | |
| `verification` | `on` | `off` runs the primary model only — half the cost, more false positives |
| `review-language` | English | e.g. `Japanese` — affects finding prose only |
| `claude-vertex-region` | `global` | Try `us-east5` if your entitlement is region-scoped |
| `max-verified-findings` | `20` | One model call per finding; ignored when verification is off |

**Start with one model if cost matters.** `verification: off` posts what the
primary model finds, and the summary says plainly that nothing was
double-checked. Turn it on when the false-positive rate starts costing more
attention than the second model costs money.

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
