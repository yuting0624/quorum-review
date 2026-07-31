# Contributing

Issues and pull requests welcome. This is a personal project and a reference
implementation, so the bar is a little unusual — worth reading before you spend
time on a change.

## What this repository optimises for

**Readability over features.** It exists to be read by someone working out how to
run two models on one cloud credential. A feature that makes the arrangement
harder to follow is a net loss even if it works.

Before adding something, the question is: *does this help demonstrate the
cross-model arrangement on Vertex?* If not, it probably belongs in a fork.

**Measured over asserted.** Claims about review quality need numbers. The
fixture and harness are in [`benchmark/`](benchmark/seeded-bugs/README.md):

```bash
python -m benchmark.measure --pr 1 --runs 3
```

Single runs are noisy enough to mislead — the same model reported 11 and 12
findings on identical input. Three runs minimum.

**Few dependencies.** Two model SDKs and an HTTP client. A reader should be able
to follow the path from a GitHub event to a posted comment without stepping
through a framework.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest && ruff check .
```

Tests need no credentials — model calls are not made in the suite.

## Things that will get a change rejected

- **Weakening the independence of the two scans.** If one model's output can
  reach the other's prompt, the agreement signal is worthless. Same for showing
  the verifier the reporter's reasoning; there is a test guarding it.
- **Making a partial suggestion applicable.** Suggestions are applied by a click.
  Anything that is not a complete replacement of the anchored lines stays prose.
- **Granting `contents: write`.** Nothing here pushes a commit. Granting it
  raises the cost of a successful prompt injection from a wrong comment to a
  write against someone's repository.
- **Making the reviewer look confident when it is degraded.** Every failure path
  says what happened in the summary. A run with one model down must not read
  like a clean one.

## If you find a false positive worth fixing

The interesting ones are a signal about the criteria, not about the code.
`skills/*/SKILL.md` is where they get fixed — a "Do not report" entry is usually
better than a code change. `@quorum /criteria` on a pull request will draft one
from findings that were dismissed there.

## Security

Flaws in the approach are best discussed in the open — please open a normal
issue. There is no support commitment, so a private report would only be slower.
[docs/security.md](docs/security.md) has the threat model.
