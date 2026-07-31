# Architecture

Why the pipeline is shaped the way it is. If you only read one source file, read
[`src/providers/vertex.py`](../src/providers/vertex.py).

## The claim being demonstrated

Cross-model review is not new. Adversarial consensus, incremental re-review, and
second-opinion verification all exist in other projects. What did not exist when
this was written is a worked example of **two models from different vendors
running on a single cloud credential**.

Everything else here is in service of making that arrangement concrete enough to
run, measure, and criticise.

## Request path

```
GitHub event
  │
  ├─ google-github-actions/auth
  │    OIDC token → short-lived Google Cloud credentials → ADC
  │
  └─ src/review.py
       ├─ github_client   PR metadata, diff, ledger from the previous run
       ├─ provider.scan   one call, whole diff        → candidate findings
       ├─ provider.verify one call per finding        → confirmed | refuted | uncertain
       └─ github_client   inline comments + summary comment carrying the new ledger
```

There is no server. GitHub delivers events to Actions directly, and state lives
in a comment on the pull request. Nothing to host, nothing to operate.

## One credential, two vendors

`google-github-actions/auth` writes Application Default Credentials into the
runner. Both clients then pick them up without being told anything about
authentication:

```python
genai.Client(vertexai=True, project=project, location="global")
AsyncAnthropicVertex(project_id=project, region=region)
```

Note what is missing: no API key, in either constructor. `vertexai=True` is what
routes the Gemini client at Vertex instead of AI Studio, and the Anthropic SDK's
Vertex client resolves the same ADC that the Gemini client just used.

The consequences are the reason a platform team cares:

- No long-lived secret in the repository. Nothing to rotate or leak.
- Both models are Vertex models, so `vertexai.allowedModels` governs both.
- Inference stays in the Vertex region; code is not sent to a second vendor.
- One invoice, drawing on existing Google Cloud commitments.

[`src/providers/direct.py`](../src/providers/direct.py) implements the same
review against two vendor API keys. It exists so the comparison can be read
rather than argued.

## Why two stages

A single model tuned for recall reports too much; tuned for precision it stays
quiet about real bugs. Splitting the two lets each stage be tuned for one thing:

| | Primary | Verifier |
|---|---|---|
| Scope | The whole diff, one call | One finding, one call |
| Optimise for | Recall | Precision |
| Effort | `high` | `low` |
| Told to | Cast a wide net; do not self-filter | Default to refuting unless you can show the failure |

Verification costs one model call per finding, which is why it is capped at the
top 20 by severity (`max-verified-findings`). Findings past the cap are demoted
to advisory, never silently dropped.

### Verification raises precision. It can never raise recall.

This is the property most easily misread, and the measurements made it concrete.

The verifier only ever sees findings the primary model already reported. A bug
the primary missed is not a bug the verifier evaluates and gets wrong — it is a
bug the verifier is never shown. **The primary model's recall is a hard ceiling
on the pipeline's recall**, and no verifier, however strong, can raise it.

So when `gemini-3.1-pro-preview` missed the mutable-default-argument defect,
pairing it with `claude-opus-5` did not help: Claude was never asked. Swapping
the roles so that Claude scanned found the bug immediately — because the fix for
a recall problem is a better *primary*, not a better verifier.

The corollary is the direction the arrow can move. Verification can only remove
things, so it trades recall for precision:

- Something confirmed was seen by two models that never spoke to each other.
- Something refuted is gone, and if the refutation was wrong, a true positive
  went with it. In the measurements this happened — see
  [`benchmark/seeded-bugs/README.md`](../benchmark/seeded-bugs/README.md).

Which is why verification is a switch (`verification: off`) and not an
architectural given. A repository that would rather read three extra false
positives than miss one real bug should turn it off, and pay for one model
instead of two.

### Cost is shaped by which stage scales

Scanning is one call regardless of how large the diff is. Verification is one
call **per finding**. The stage that scales with N is the second one, so on cost
grounds the cheaper model belongs there:

| | Calls | Put here |
|---|---|---|
| Primary scan | 1 | The model with the best recall — it sets the ceiling |
| Verification | N | The cheaper model — it only has to judge one claim at a time |

That inverts the arrangement this project started with, and the measurements
support the inversion on accuracy as well as cost.

### The verifier is deliberately kept ignorant

It receives the file, the line, the code, and a one-line claim. It does not
receive the primary model's argument, its severity rating, or its identity.

This is the single most important decision in the codebase. Give a model
someone else's reasoning and it tends to agree; the second stage then confirms
everything and filters nothing. `test_verify_prompt_withholds_the_reporters_reasoning`
in [`tests/test_review.py`](../tests/test_review.py) exists to keep this from
being eroded by a well-meaning refactor.

It also gives the verifier something to do that the primary cannot: **re-score
severity from scratch**. Because it never saw the original rating, its rating is
independent evidence rather than an echo, and its score is the one that wins.

### Verification as an injection defence

A diff that talks the primary model out of reporting something still has to get
past a second model that never saw the manipulated session. That is a real
security property, not a side effect — see [security.md](security.md).

### Degrading instead of failing

If the verifier cannot run — no entitlement, wrong region, exhausted quota — the
review continues with primary findings only, and the summary comment says so in
as many words. A reviewer that goes silent is worse than one that says it is
running at half strength.

## The roles are configuration

`PRIMARY_MODEL` and `VERIFIER_MODEL` are resolved to an engine by model-ID
prefix, not hardcoded per stage:

```python
def _engine(self, model: str) -> _Engine:
    if model.startswith("claude"):
        return _ClaudeEngine(model, self._project, self._claude_region)
    return _GeminiEngine(model, self._project, self._gemini_location)
```

So running the experiment in reverse — Claude scanning, Gemini verifying — is
two environment variables. Whether Gemini-then-Claude actually beats
Claude-then-Gemini is unknown; `benchmark/` exists to find out rather than
assume.

## Findings are identified by content

Line-based identity breaks constantly: add an import and every finding below it
looks new. Identity is therefore
`sha256(file_path + normalised_code_snippet)[:16]`, with **no line number in the
input**. Normalisation strips comments and collapses whitespace, so reformatting
does not mint a new ID either.

The trade: a renamed file produces new IDs, and the old findings read as
resolved. Accepted for v1.

State is a base64 payload in a hidden HTML marker inside the summary comment
(`<!-- quorum-state: ... -->`), gzipped past 4 KB. No database, no configuration
for the adopter, and the state cannot drift away from the pull request it
describes. When the comment approaches GitHub's 64 KB limit, resolved findings
are dropped first — a fixed issue that reappears is a regression worth
reporting again anyway.

## What is deliberately absent

- **Managed Agents.** The intended primary stage is an agent in a persistent
  sandbox that can check out the branch, install dependencies, and run tests.
  That API is pre-GA and not licensed for production use, so the primary stage
  is a plain Gemini call over the diff. The `ReviewProvider` protocol exists so
  that swap costs one new file.
- **A framework.** The dependencies are two model SDKs and an HTTP client. A
  reader should be able to follow the path from event to comment without
  stepping through an abstraction layer.
- **`synchronize` triggers by default.** Reviewing every commit is expensive and
  the bot's own comments can re-trigger the workflow.
- **Anything that writes to the repository.** See [security.md](security.md).
