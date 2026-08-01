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

## Context-dependent cases

These were added later, after the first measurements showed the fixture was
flattering a diff-only reviewer: **every original bug was visible inside the
diff**, which is an artefact of who wrote it rather than a property of real
pull requests.

All three live in `app/reports.py`. Their correctness depends on files the pull
request does not touch, so a reviewer that reads only the diff cannot settle
them — it can only guess.

| # | Type | Depends on | Description |
|---|---|---|---|
| C1 | **decoy** | `app/validators.py` | `os.path.join` on user input looks like traversal, but `safe_export_name` has already reduced it to one path segment. **Flagging it is a false positive.** |
| C2 | **bug** (security, high) | `app/permissions.py` | The scope check reads correctly and passes for everyone: `"document.report"` is absent from `REQUIRED_SCOPES`, and `has_scope` returns True for unlisted actions — as its docstring warns |
| C3 | **bug** (correctness, medium) | `app/audit.py` | `audit.record(user, "document.report", doc_id)` — the signature is `(action, user_id, doc_id)`, so this writes a useless row rather than raising |

C1 is the one that matters most. "Is this input validated upstream?" is the
commonest cause of false positives in code review, and it is exactly the
question a diff cannot answer.

### What they measured

| Configuration | Seeded, scanned | Seeded, survived | C2 + C3 | C1 flagged (false positive) |
|---|---|---|---|---|
| diff only | 10.0 / 10 | 10.0 / 10 | **0 / 2, in every run** | never |
| repository readable | 10.0 / 10 | 9.7 / 10 | **2 / 2, in every run** | never |

Three runs each, both scanning models, second opinion on, same pull request,
same commit. Raw findings in [`../runs/`](../runs).

**Re-measured at `v1.0.0`**, forty commits later, because a number attached to
code that has since changed is a number about nothing. Three runs, repository
readable: **10.0 / 10 scanned, 10.0 / 10 survived, C2 and C3 in every run, no
decoy flagged.** Raw findings in
[`../runs/v1.0.0-repo-access.json`](../runs/v1.0.0-repo-access.json).

The one movement is B8, which survived 3/3 this time against 2/3 before — the
finding the fixture is [known to be ambiguous about](#caveat-on-b8), so the
change is in what the verifier decided about a debatable case rather than in
what was found. Everything intervening — the binary-detection fix, rename
following, the diff budget, retries, table escaping — could have moved these
and did not. The binary bug in particular could only have: none of the fixture
files mention its markers, which was checked rather than assumed.

**The gap is the whole result.** Both context-dependent bugs went from never
found to always found. Neither is subtle once you can see the other file —
`"document.report"` is missing from `REQUIRED_SCOPES`, and `audit.record` takes
`(action, user_id, doc_id)` while the call passes `(user, action, doc_id)`. They
are simply undecidable from the diff, and the diff-only reviewer was correct to
stay quiet about them. It just could not do the job.

**One more real bug appeared, and only with access.** `export_document` performs
no `document.export` scope check. Reported in 2 of 3 runs with the repository
readable, and in **0 of 3** without it — the same shape of finding as C2, and
one nobody had planted.

**The false-positive half of the hypothesis was not tested.** C1 was designed to
catch a reviewer guessing that an unvalidated-looking `os.path.join` is
traversal. Neither configuration ever flagged it, so the decoy discriminated
nothing. The likely reason is that the call reads
`validators.safe_export_name(filename)`, and a name that says `safe_` is already
most of the answer. A stronger version of this case would give the validator a
neutral name — `normalise`, say — so that reading it is the only way to know.
Until then, "no false positives" here is a weaker claim than it looks.

**Verification became slightly more willing to subtract.** Survived dropped to
9.7 because B8 was refuted in one run, which is the finding the fixture is
[known to be ambiguous about](#caveat-on-b8). Three tool calls into the
repository is three more chances to build an argument, and the argument against
B8 is a good one. This is the stage doing its job in a case where its job is
debatable.

### Two contamination sources, both found by giving the models access

Neither was visible while the reviewer could only read the diff, and both
invalidated every measurement taken before they were fixed.

1. **The answer key was readable.** This file lists every bug by file, and
   `tests/test_fixture_integrity.py` asserts each one. Both live on `main`
   specifically so they stay out of PR #1's diff — sufficient right up until
   the models could open them, which they did, in several runs. Now excluded
   via [`.quorumignore`](../../.quorumignore), with a test.

2. **The pull request body announced the answer.** It said the branch contained
   "ten deliberately seeded defects and three constructs that look dangerous but
   are correct", and linked to this file. Every measurement in this document's
   history was therefore taken by a reviewer that had been told how many bugs to
   find and how many decoys to avoid. The body now says only "do not merge".

   The model found this one itself, in run 1 of the first clean measurement:
   *"PR description contains reviewer-directed instructions (prompt injection)."*
   Which is exactly right, and not a false positive.

The numbers above are from after both fixes. Earlier figures in this file were
taken under (2) and are marked where they appear.

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
`security-review`, models `gemini-3.6-flash` and `claude-opus-5`. Three runs per
configuration, via `python -m benchmark.measure`.

> ⚠️ **Everything in this section predates the contamination fixes described
> above.** These runs were made while the pull request body told the reviewer
> there were ten bugs and three decoys. The seeded-bug counts and the
> false-positive count are both flattered by that, and the comparisons between
> *configurations* are the part still worth reading — every row was contaminated
> identically. Re-running the single-scanner rows cleanly is on the list below.

| Scanning | Second opinion | Seeded found, mean of 3 | Per-bug stability | Unseeded real | False positives |
|---|---|---|---|---|---|
| `gemini-3.6-flash` alone | `claude-opus-5` | 9.0 / 10 | B7 **0/3** — never found | — | 0 |
| `claude-opus-5` alone | `gemini-3.6-flash` | 10.0 / 10 | all 3/3 | U1, U2 | 0 |
| **both, independently** | on disagreements only | **10.0 / 10** | **all 3/3** | U1, U2, +3 more | **0** |

PRD targets were ≥6 of 10 detected and ≤3 false positives. Every configuration
clears both, and **no configuration was fooled by any of the three decoys in any
run.**

Earlier single runs, kept for the record — these used the previous
single-scanning design and `gemini-3.1-pro-preview`:

| Primary | Verifier | Found | Survived | Notes |
|---|---|---|---|---|
| `gemini-3.1-pro-preview` | `claude-opus-5` | 9 | 9 | missed B8 |
| `claude-opus-5` | `gemini-3.1-pro-preview` | 10 | 9 | B8 refuted |

### The result that changed the design

`gemini-3.6-flash` missed B7 in **three runs out of three** — not occasionally,
always. Pairing it with `claude-opus-5` as a verifier did not recover it, and
could not have: **a second opinion is only ever shown findings that were already
reported.** Claude was never asked about B7, because nothing had raised it.

That is a recall ceiling set by whichever model scans, and no verifier can lift
it. The fix was to have both models scan independently and merge, which took the
same pair from 9.0 to a stable 10.0 with no loss of precision. Findings both
models reported without seeing each other's output are treated as agreed and
skip verification, so the change also removed most of the per-finding calls.

The dual-scan configuration also surfaced three real bugs the fixture never
seeded — an unescaped `LIKE` prefix in `suggest()`, a share purge that deletes
orphaned rows for every user rather than the target, and an admin delete reached
through a handler that only checks authentication. Those are wins, but note the
direction of the bias: they were found because a second model was looking, which
is the same reason the seeded recall improved.

### What the numbers say

**Which single model scans decides what the review can find.**
`claude-opus-5` scanning alone hit 10/10 in every run; `gemini-3.6-flash`
scanning alone hit 9/10 in every run and missed the same bug each time. The
misses are not random noise to be averaged away — they are stable blind spots,
and the earlier single-run data shows the two Gemini models had *different*
ones (`gemini-3.1-pro-preview` missed B8, `gemini-3.6-flash` missed B7).

**Which is exactly why two scanners beat picking the right one.** Running both
gave a stable 10/10 without having to know in advance which model is stronger on
a given codebase. On another repository the ranking could invert; the union does
not care.

**The second opinion did far less than expected.** Across six runs of the
single-scanner configurations it refuted **nothing at all**. It removed a
finding in only two runs out of twelve total, and in both cases the finding was
B8 — the one the fixture is known to be ambiguous about. On this fixture the
first model simply did not produce plausible-but-wrong findings for the second
to cut.

**What it did contribute was severity.** In runs with no refutations the
measurable effect was re-ranking: SQL injection and the swallowed authorization
check promoted to `critical`, TOCTOU and the timing issue demoted to `low`, all
decided without having seen the original ratings.

**Verification can still remove a true positive.** Two models refuted B8 with
near-identical reasoning — the append is idempotent, so nothing accumulates —
while a third confirmed it. Given the caveat above the refutations are
defensible on impact and wrong on the latent defect, but the point holds
regardless of who is right: the stage subtracts, and it can subtract something
real.

**Do not over-read any of this.** One fixture, three runs, and it was written by
the same person who wrote the reviewer. `claude-opus-5` reported 11, 12, and 12
findings on identical input, so run-to-run variance is real even where the
seeded-bug numbers look clean. This is enough to justify the dual-scan change
and to answer the role-ordering question for this fixture. It is not enough to
rank the models in general.

### Open questions this raises

- The verification stage barely fired. Every removal concerned the one
  deliberately ambiguous finding. A fixture that seeds *plausible-but-wrong*
  findings — not just correct-looking decoys — would show whether the stage
  earns its cost on the inputs it was designed for.
- B7 and B8 are `correctness` defects being hunted by a `security-review` skill.
  Running `code-quality-review` would separate model capability from prompt
  scope as the cause of the single-scanner misses.
- Dual scanning found three real bugs the fixture never seeded. That is a good
  sign and an uncontrolled one: nobody looked for them in the single-scanner
  runs either, so it is not yet evidence that dual scanning finds *more*
  unseeded bugs, only that it found some.

## Live run from GitHub Actions

The measurements above drive the provider directly. This section records the
real thing: the workflow running on `ubuntu-latest`, authenticating through
Workload Identity Federation, and posting to PR #1.

| | Result |
|---|---|
| Single Google Cloud credential drives both models from Actions | **yes** — this is the Phase 0 completion condition |
| Findings posted | 13 inline + 1 summary carrying the ledger |
| Seeded bugs found | 10 / 10 |
| False positives | 0 — no decoy flagged |
| Independent agreement | 9 of 11 findings reported by both models without seeing each other |
| **Re-report rate on an unchanged PR** | **0%** (0 of 13) — target was ≤ 5% |
| Summary comments accumulated | 1 — edited in place, not appended |

### Getting to 0% took two fixes, both found by running it

The first live re-run posted **eight of eleven findings a second time**.
Suppression keyed on `finding_id`, which hashes the code the model quoted — and
a model re-quotes the same bug with a different span between runs, so the same
defect arrived with a new ID and read as new. Matching on position instead cut
that to two.

The two that survived were the same defect anchored at different lines: the
unenforced share expiry reported at the storage, the lookup, and the check
across sixteen lines, and the TOCTOU reported at the write and at the `open()`.
Widening the line window was not available as a fix — `delete_document` and
`purge_user` sit nine lines apart in the same file and are different bugs. A
title-overlap rule closed it, with the thresholds tuned against exactly these
cases and pinned in `tests/test_matching.py`.

Neither failure was visible in the dry-run measurements, which never write a
ledger. They only appeared once the thing actually posted twice.

## Still to measure

- **The single-scanner rows, cleanly.** They were taken before the pull request
  body stopped announcing the answer. The dual-scan-versus-single comparison
  probably survives — the contamination applied equally to both — but the
  absolute numbers do not.
- **A decoy that actually discriminates.** C1 was never flagged by either
  configuration, so it measured nothing. Rename `safe_export_name` to something
  neutral and the case starts asking the question it was written to ask.
- Re-review after genuinely fixing some seeded bugs. The 0% above proves nothing
  is re-posted on an unchanged PR; it does not prove a real fix is recognised as
  one. Note also that one finding appeared under "no longer reported" purely
  because the second scan did not re-raise it — the summary is careful not to
  call that a fix.
- Reference comparison against CodeRabbit and Copilot Code Review on the same PR.
