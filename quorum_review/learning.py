"""Turning dismissed findings back into review criteria.

Every ``@quorum wontfix`` is a small, specific statement about what this
repository does not consider a problem — and today each one is silenced
individually. The same mistake gets made on the next pull request, dismissed
again, and the reviewer never gets better.

This closes that loop by summarising the accumulated reasons into a proposed
edit to the skill in use.

**It proposes; a human applies.** The reasons come from pull request comments,
which are attacker-controlled input on any repository that takes contributions.
Writing them into the criteria automatically would let someone talk the
reviewer out of a whole category of finding by dismissing it convincingly a few
times. A pull request that a maintainer reads is the correct amount of
friction.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import Ledger, LedgerEntry
from .report import flatten

#: Below this there is not enough signal to distinguish a pattern from a
#: one-off, and proposing an edit from two dismissals is noise.
MIN_DISMISSALS = 3


@dataclass
class Proposal:
    """A suggested change to the review criteria."""

    skill: str
    dismissals: list[LedgerEntry]
    body: str

    @property
    def worth_showing(self) -> bool:
        return len(self.dismissals) >= MIN_DISMISSALS


def dismissed(ledger: Ledger) -> list[LedgerEntry]:
    """Findings a human explicitly retired, with a stated reason."""
    return [
        entry
        for entry in ledger.entries.values()
        if entry.status == "wontfix" and (entry.wontfix_reason or "").strip()
    ]


def render_prompt(skill_name: str, skill_body: str, entries: list[LedgerEntry]) -> str:
    """Ask a model to turn dismissals into a concrete criteria edit."""
    cases = "\n\n".join(
        f"- **{flatten(entry.title)}** (`{entry.file_path}`, {entry.severity} "
        f"{entry.category})\n  Dismissed because: {entry.wontfix_reason}"
        for entry in entries
    )

    return f"""\
These findings were reported by a code reviewer and then explicitly dismissed
by a maintainer of the repository. Each dismissal is a statement about what
this codebase does not consider a problem.

<dismissed_findings>
{cases}
</dismissed_findings>

Here are the review criteria that produced them:

<current_criteria name="{skill_name}">
{skill_body}
</current_criteria>

Propose a change to the criteria that would stop these specific findings being
reported, without blinding the reviewer to the real problems the criteria
exist to catch.

- If the dismissals share a cause, say what it is in one sentence.
- Give the edit as a short Markdown snippet ready to paste into the criteria,
  usually an addition to a "Do not report" section.
- If a dismissal looks like a one-off rather than a pattern, say so and leave
  it out. Narrowing the criteria for a single case costs more than it saves.
- If the criteria are fine and the model simply misapplied them, say that
  instead of inventing an edit.

Reply as plain Markdown. Be brief.
"""


def render_comment(proposal: Proposal) -> str:
    """The comment a maintainer reads, containing the proposal."""
    return f"""\
## Review criteria: suggested change

{len(proposal.dismissals)} findings have been dismissed on this pull request. \
Rather than silence each one separately, here is a proposed edit to \
`quorum_review/skills/{proposal.skill}/SKILL.md` so the same findings stop being raised.

{proposal.body}

<details>
<summary>The dismissals this is based on</summary>

{chr(10).join(
    f"- **{flatten(entry.title)}** — {flatten(entry.wontfix_reason or '', 300)}"
    for entry in proposal.dismissals
)}

</details>

<sub>Nothing has been changed. This is a suggestion to read and apply by hand:
dismissal reasons come from pull request comments, and writing those into the
review criteria automatically would let someone argue the reviewer out of a
whole category of finding.</sub>
"""
