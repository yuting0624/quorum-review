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

Measured against PR #1 at `cc4b946`, project `data-agent-bq`, Vertex `global`,
skill `security-review`. One run per configuration.

| Date | Configuration | Seeded found (/10) | Unseeded real | False positives | Notes |
|---|---|---|---|---|---|
| 2026-08-01 | Primary only — `gemini-3.1-pro-preview` | 9 | 1 (U1) | 0 | Missed B8 |
| 2026-08-01 | `gemini-3.1-pro-preview` → `claude-opus-5` | 9 | 1 (U1) | 0 | Verifier confirmed all 10, refuted none |
| 2026-08-01 | Primary only — `claude-opus-5` | 10 | 2 (U1, U2) | 0 | Found B8 |
| 2026-08-01 | `claude-opus-5` → `gemini-3.1-pro-preview` | 9 | 2 (U1, U2) | 0 | Verifier refuted B8 (see caveat) |

Targets from the PRD were ≥6 of 10 detected and ≤3 false positives per review.
Both are met in every configuration.

### What the numbers say

**Claude is the stronger primary on this fixture.** It reported 12 findings to
Gemini's 10, caught B8 where Gemini did not, and found U2 as well. Neither model
was fooled by any of the three decoys.

**The verification stage did not remove anything in the forward direction.** With
Gemini scanning, Claude confirmed all ten findings and refuted none. On this
fixture the second stage bought no precision, because the first stage produced
no plausible-but-wrong findings for it to cut. Its measurable contribution here
was re-scoring severity — it promoted the SQL injection and the swallowed
authorization check to `critical` and demoted the TOCTOU and timing issues to
`low`, all without having seen the original ratings.

**In the reverse direction the verifier removed a true positive.** Gemini
refuted B8, reasoning that the idempotent append means the default list cannot
accumulate. Given the caveat above, that reasoning is defensible on impact and
still wrong on the latent defect. This is the clearest evidence so far that the
verifier is not free: it can cost recall, and which model does the verifying
matters.

**Do not over-read any of this.** One fixture, one run per configuration, no
repetition, and the fixture was written by the same person who wrote the
reviewer. It is enough to answer "does the arrangement work end to end" and to
give the role-ordering question a first data point. It is not enough to rank the
models.

### Open questions this raises

- The forward configuration produced no refutations. Is that a property of the
  fixture, or does the verification stage rarely fire in practice? A fixture with
  deliberately plausible-but-wrong findings would separate the two.
- B8 was found only by the `security-review` skill's weaker half. It is a
  `correctness` defect; running `code-quality-review` should be a fairer test of
  whether it is reachable at all.
- Verification cost roughly one model call per finding for zero removals in the
  forward direction. Worth measuring against the value of the severity re-scoring.

### Still to measure

- Re-review after fixing some bugs, to confirm resolved findings are recognised
  and open ones are not re-reported. Requires a run that actually posts, since
  the ledger lives in the summary comment.
- Reference comparison against CodeRabbit and Copilot Code Review on the same PR.
