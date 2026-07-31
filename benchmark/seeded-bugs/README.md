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

Open a pull request from `benchmark/seeded-bugs-v1` into `main` and run the
reviewer against it.

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
| B8 | `app/sharing.py` | correctness | medium | Mutable default argument `scopes: list = []` is shared across calls |
| B9 | `app/documents.py` | reliability | medium | `limit` is no longer capped, so `MAX_PAGE_SIZE` can be bypassed and the whole table loaded |
| B10 | `app/admin.py` | correctness | high | `except Exception: pass` swallows an authorization failure, so a denied check reads as success |

## Decoys (suspicious-looking but correct)

Included so false positives can be measured. **Flagging any of these counts as a
false positive.**

| # | File | Code | Why it is correct |
|---|---|---|---|
| D1 | `app/indexer.py` | `subprocess.run` call | `shell=False`, the argv is a literal list, and user input is passed as a separate path argument |
| D2 | `app/plugins.py` | `importlib.import_module` | The module name comes from a hardcoded allowlist dict; user input is only a key lookup |
| D3 | `app/fetcher.py` | `random.uniform` for retry jitter | Not a cryptographic use — it only spreads out retry delays, so `secrets` is unnecessary |

## Results

| Date | Configuration | Detected (/10) | False positives | Notes |
|---|---|---|---|---|
| _pending_ | Primary only (Gemini) | | | |
| _pending_ | Primary + verifier (Gemini → Claude) | | | |
| _pending_ | Primary + verifier (Claude → Gemini) | | | |
| _pending_ | Second review (after fixing B1/B4/B6) | | | Measures the re-report rate |
| _pending_ | Reference: CodeRabbit | | | |
| _pending_ | Reference: Copilot Code Review | | | |
