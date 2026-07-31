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
       ├─ github_client     PR metadata, diff, ledger from the previous run
       │
       ├─ provider.scan(A)  ─┐ concurrent, independent — neither model
       ├─ provider.scan(B)  ─┤ sees the other's output
       │                     ▼
       ├─ consensus.merge    both reported → agreed, no further call
       │                     one reported  → unresolved
       │
       ├─ provider.verify    one call per unresolved finding, always by a
       │                     model that did not report it
       │
       └─ github_client      inline comments + summary carrying the new ledger
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

## Why both models scan

The design originally had one model scan and the other verify. Measurement
killed that, and the reason is structural rather than a matter of which model is
better.

**A verifier can only judge findings that were reported.** A bug the scanning
model missed is not a bug the verifier evaluates and gets wrong — it is a bug
the verifier is never shown. Whichever model scans sets a hard ceiling on the
review's recall, and no second opinion, however strong, can lift it.

That is not theoretical. `gemini-3.6-flash` missed the seeded TOCTOU defect in
three runs out of three. Pairing it with `claude-opus-5` recovered nothing,
because Claude was never asked. Having both models scan took the same pair from
9.0/10 to a stable 10.0/10 — see
[`benchmark/seeded-bugs/README.md`](../benchmark/seeded-bugs/README.md).

The union also removes a decision nobody can make reliably in advance: which
model is stronger on *this* codebase. The two Gemini models tested had different
blind spots from each other. Picking one is a bet; running both is not.

### Independent agreement is evidence, and it is free

Two models that could not see each other's output reaching the same conclusion
is a better signal than either model's self-reported confidence — and it arrives
as a by-product of scanning twice. Those findings are marked agreed and skip
verification.

Only disagreements cost a second call, which makes the arrangement cheaper than
verifying everything:

| Configuration | Calls | Recall |
|---|---|---|
| One scan, no verification | 1 | That model's ceiling |
| One scan, verify each finding | 1 + N | That model's ceiling |
| **Both scan, verify disagreements** | **2 + d** | **The union** |

`d` is the number of findings only one model raised. On a diff where the models
largely agree, `d` is far smaller than `N` — so the default configuration is
usually cheaper than the middle row as well as more accurate.

Verification is still capped at the top 20 unresolved findings by severity
(`max-verified-findings`); anything past the cap is demoted to advisory, never
silently dropped.

### Each stage is tuned for one thing

| | Scan | Second opinion |
|---|---|---|
| Scope | The whole diff, one call | One finding, one call |
| Optimise for | Recall | Precision |
| Effort | `high` | `low` |
| Told to | Cast a wide net; do not self-filter | Default to refuting unless you can show the failure |

### Verification subtracts, so it can subtract something real

The stage can only remove findings. If a refutation is wrong, a true positive
goes with it — which happened in the measurements, on a finding two models
refuted and a third confirmed.

That is why it is a switch (`verification: off`) rather than an architectural
given, and why agreement bypasses it rather than being re-checked.

### Independence is enforced, not assumed

Two rules keep the models genuinely independent, and both are load-bearing:

**Scans never see each other.** They run concurrently over the same diff with
identical prompts. If one model's output leaked into the other's prompt, the
agreement signal would be worthless — a model shown another's findings tends to
endorse them.

**The judge never sees the reporter's reasoning.** A finding only one model
raised is sent to the other with the file, the line, the code, and a one-line
claim. Not the argument, not the severity rating, not who reported it. Give a
model someone else's reasoning and it agrees; the stage then confirms everything
and filters nothing.
`test_verify_prompt_withholds_the_reporters_reasoning` in
[`tests/test_review.py`](../tests/test_review.py) exists to stop a well-meaning
refactor from eroding this.

Keeping the judge ignorant also gives it something the reporter cannot provide:
a **severity score decided from scratch**. Never having seen the original
rating, its rating is independent evidence rather than an echo, and it wins.

### As an injection defence

A diff that talks one model out of reporting something has to get past a second
model that never saw the manipulated session — and that second model is now
scanning independently, not merely reacting to what the first produced. See
[security.md](security.md).

### Degrading instead of failing

Every failure mode narrows the review rather than ending it, and the summary
always says which happened:

- A scanning model fails → the review proceeds on the other's findings, with the
  failure named. A reviewer running at half strength must not look like a clean
  one.
- A verifier fails → affected findings become advisory rather than disappearing.
- Only one model is configured → every finding is unresolved by definition, and
  the pipeline collapses gracefully into the older scan-then-verify shape.

## Cross-model identity

Merging two scans needs a way to tell "these two reports are the same defect",
and finding IDs cannot do it: they hash the quoted snippet, and two models
almost never quote a bug identically.

`consensus.looks_like_same` matches positionally instead — same file, and within
two lines, widening to fifteen when both models quote overlapping code. The
widening is capped deliberately. Identical snippets are not proof of identity: a
file can contain the same `except Exception: pass` twice, and merging those
would discard a real finding outright. A test caught exactly that during
development.

## Any model can play any part

`provider.scan(model, ...)` and `provider.verify(model, ...)` take the model as
an argument; the engine is resolved by model-ID prefix rather than by stage:

```python
def _engine(self, model: str) -> _Engine:
    if model.startswith("claude"):
        return _ClaudeEngine(model, self._project, self._claude_region)
    return _GeminiEngine(model, self._project, self._gemini_location)
```

That is what lets the same two models scan *and* judge each other, and it is
what made the role-ordering experiment cheap enough to actually run — the
question was settled by measurement rather than argument.

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
