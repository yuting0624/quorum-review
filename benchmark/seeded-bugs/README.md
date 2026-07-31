# seeded-bugs — baseline fixture for review quality

A small document-sharing service used as a fixed target for measuring
quorum-review's own detection rate and false-positive count.

**This code is not meant to run.** It only needs to be realistic enough to
review. Dependencies are limited to the standard library plus `requests`.

## Layout

```
main                        clean version, no seeded bugs
benchmark/seeded-bugs-v1    a feature PR; the added code carries 10 bugs and 3 decoys
```

Pull request [#1](https://github.com/yuting0624/quorum-review/pull/1) is the
fixed target. Do not merge it.

The answer key lives in this file, which is on `main` — deliberately, so it does
not appear in the pull request's diff. `tests/test_fixture_integrity.py` asserts
that every bug below is still present, so a later cleanup cannot silently repair
one and invalidate the recorded numbers.

## Seeded bugs (answer key)

| # | File | Category | Severity | Description |
|---|---|---|---|---|
| B1 | `app/search.py` | security | high | SQL injection — the query is assembled with an f-string |
| B2 | `app/export.py` | security | high | Path traversal — user input reaches `os.path.join` with no `..` check |
| B3 | `app/sharing.py` | security | medium | Share token compared with `==` instead of `hmac.compare_digest`, allowing a timing attack |
| B4 | `app/fetcher.py` | security | high | SSRF — a user-supplied URL is fetched without scheme or host validation |
| B5 | `app/config.py` | security | high | `os.getenv("QUORUM_DEMO_SECRET", "dev-secret-change-me")` — the fallback silently applies in production |
| B6 | `app/admin.py` | security | high | IDOR — the delete endpoint performs no owner or admin check |
| B7 | `app/export.py` | correctness | medium | TOCTOU — another process can intervene between the existence check and the write |
| B8 | `app/sharing.py` | correctness | medium | Mutable default argument `scopes: list = []` is shared across calls — **see the caveat below** |
| B9 | `app/documents.py` | reliability | medium | `limit` is no longer capped, so `MAX_PAGE_SIZE` can be bypassed and the whole table loaded |
| B10 | `app/admin.py` | correctness | high | `except Exception: pass` swallows an authorization failure, so a denied check reads as success |

### Caveat on B8

B8 is weaker than intended and the fixture, not the reviewer, is at fault. The
only mutation of the default list is `scopes.append("read")` guarded by
`if "read" not in scopes`, which is idempotent: the default list reaches
`["read"]` after the first call and never changes again. The defect is real but
**latent** — it becomes observable only if a future caller appends something
per-call.

A model that reports B8 is right, and a model that refutes it on impact grounds
is not obviously wrong. Both outcomes are recorded below rather than scored as
a straightforward hit or miss. A future revision of the fixture should make the
mutation non-idempotent; it has been left alone for now so the runs below stay
comparable.

## Unseeded bugs that turned out to be real

Found during the first measurement runs. These were written by accident, not
planted, but they are genuine defects — so **reporting them is a true positive,
not a false one.**

| # | File | Description | Found by |
|---|---|---|---|
| U1 | `app/sharing.py` | `expires_in` is stored on the share row and never checked, so share links never expire | both configurations |
| U2 | `app/sharing.py` | `resolve_share_link` ignores the share's `scopes` and returns the entire document row | Claude only |

## Decoys (suspicious-looking but correct)

Included so false positives can be measured. **Flagging any of these counts as a
false positive.**

| # | File | Code | Why it is correct |
|---|---|---|---|
| D1 | `app/indexer.py` | `subprocess.run` call | `shell=False`, the argv is a literal list, and the path is confined with `realpath` + `commonpath` before use |
| D2 | `app/plugins.py` | `importlib.import_module` | The module name comes from a hardcoded allowlist dict; user input is only a key lookup |
| D3 | `app/fetcher.py` | `random.uniform` for retry jitter | Not a cryptographic use — it only spreads out retry delays, so `secrets` is unnecessary |

## Results

Measured against PR #1, project `data-agent-bq`, Vertex `global`, skill
`security-review`. One run per configuration — see the caution below.

| Date | Primary | Verifier | Primary found (/10) | Survived verification | Unseeded real | False positives |
|---|---|---|---|---|---|---|
| 2026-08-01 | `gemini-3.1-pro-preview` | `claude-opus-5` | 9 (missed B8) | 9 | U1 | 0 |
| 2026-08-01 | `gemini-3.6-flash` | `claude-opus-5` | 9 (missed B7) | 8 (B8 refuted) | U1 | 0 |
| 2026-08-01 | `claude-opus-5` | `gemini-3.1-pro-preview` | 10 | 9 (B8 refuted) | U1, U2 | 0 |
| 2026-08-01 | **`claude-opus-5`** | **`gemini-3.6-flash`** | **10** | **10** | U1 | **0** |

PRD targets were ≥6 of 10 detected and ≤3 false positives. Every configuration
clears both, and **no configuration was fooled by any of the three decoys.**

### What the numbers say

**Claude is the stronger primary on this fixture, and the margin is not
marginal.** As primary it found all ten seeded bugs in both runs. Neither Gemini
model managed better than nine, and they missed different ones —
`gemini-3.1-pro-preview` missed B8, `gemini-3.6-flash` missed B7 while catching
B8. Two nines covering different ground is a recall problem, not a tie.

**Reversing the roles is also the cheaper shape.** Verification is the stage
that scales with the number of findings: one model call each. Scanning is a
single call regardless of diff size. Putting the expensive model on the O(1)
scan and a Flash model on the O(N) verification inverts the intuition the design
started from — and here it was both more accurate and cheaper.

**Verification is not free, and B8 is where that shows.** Three of the four runs
turned on it. `claude-opus-5` and `gemini-3.1-pro-preview` both refuted it, with
near-identical reasoning: the append is idempotent, so nothing accumulates.
`gemini-3.6-flash` confirmed it. Given the caveat above, the refutations are
defensible on impact and wrong on the latent defect — but the point stands
independently of who is right: **the second stage can and does remove true
positives.** Which model verifies changes the answer.

**When the verifier removed nothing, it still re-scored severity.** In the runs
with zero refutations the measurable contribution was re-ranking: the SQL
injection and the swallowed authorization check were promoted to `critical`, the
TOCTOU and timing issues demoted to `low`, all without the verifier having seen
the original ratings.

**Do not over-read any of this.** One fixture, one run per configuration, no
repetition, and the fixture was written by the same person who wrote the
reviewer. `claude-opus-5` also reported 12 findings in one run and 11 in
another, so single runs are visibly noisy. This is enough to answer "does the
arrangement work end to end" and to give the role-ordering question a first data
point. It is not enough to rank the models.

### Open questions this raises

- Three of four runs produced at most one refutation, and all of them concerned
  the one finding the fixture is known to be ambiguous about. A fixture with
  deliberately plausible-but-wrong findings would show whether the verification
  stage does anything on inputs it was designed for.
- B7 and B8 are both `correctness` defects being hunted by a `security-review`
  skill. Running `code-quality-review` would be a fairer test of whether the
  misses are model capability or prompt scope.
- Every configuration cost roughly one model call per finding for at most one
  removal. That is worth pricing against the severity re-scoring, which is real
  but harder to value.

### Still to measure

- Re-review after fixing some bugs, to confirm resolved findings are recognised
  and open ones are not re-reported. Requires a run that actually posts, since
  the ledger lives in the summary comment.
- Reference comparison against CodeRabbit and Copilot Code Review on the same PR.
