"""Prompt construction, shared by every provider.

Both stages are built here so that the ``vertex`` and ``direct`` providers put
identical text in front of their models. If the prompts diverged per provider,
comparing the two configurations would measure the prompts rather than the
models.
"""

from __future__ import annotations

from . import diffs
from .schema import BASE_INSTRUCTIONS, Finding, PRContext, Skill, language_directive


def scan_system(skill: Skill, language: str = "") -> str:
    """System prompt for the primary scan. Optimised for recall."""
    return (
        BASE_INSTRUCTIONS
        + language_directive(language)
        + f"""
## Your task

Find defects introduced by this pull request. Cast a wide net: it is better to
surface something a second reviewer later dismisses than to stay silent about a
real bug. A separate verification stage will filter your output, so do not
pre-filter for confidence yourself.

Report only on lines the diff adds or modifies. Pre-existing problems in
untouched code are out of scope.

## Proposing a fix

`fix_replacement` becomes a one-click suggestion the author can apply without
reading it closely. Treat that as the bar.

Fill it in only when all of these hold:

- The fix is a **complete replacement** of lines `line` through `fix_end_line`.
  Never a fragment, never a diff, never a `...` placeholder.
- It compiles and behaves correctly on its own, with the surrounding code
  unchanged. If it needs a new import, a new helper, or an edit elsewhere,
  there is no safe suggestion — leave it empty and describe the fix in `body`.
- You are confident. A plausible-looking fix that someone applies and ships is
  worse than no fix at all.

Preserve the original indentation exactly; the text is inserted verbatim.
Leave `fix_replacement` as an empty string and `fix_end_line` as 0 whenever the
bar above is not met, which will often be most findings.

## Review criteria

{skill.content}
"""
    )


def scan_user(ctx: PRContext) -> str:
    """User turn for the primary scan.

    Every attacker-controlled field is wrapped in an ``<untrusted_*>`` tag; see
    ``BASE_INSTRUCTIONS`` for how the model is told to treat them.
    """
    return f"""\
Repository: {ctx.owner}/{ctx.repo}
Pull request: #{ctx.number}

<untrusted_pr_title>
{ctx.title}
</untrusted_pr_title>

<untrusted_pr_body>
{ctx.body}
</untrusted_pr_body>

<untrusted_diff>
{ctx.diff}
</untrusted_diff>
"""


def verify_system(language: str = "") -> str:
    """System prompt for verification.

    Constant across every finding in a run, which is what makes it worth a
    prompt-cache breakpoint: the verifier is called once per finding, so this
    text is re-sent N times.
    """
    return (
        BASE_INSTRUCTIONS
        + language_directive(language)
        + """
## Your task

You are the second opinion. A claim has been made about one specific place in
this pull request. Read the code and decide, on your own, whether the claim
holds.

You are deliberately **not** told who made the claim, how confident they were,
or how severe they thought it was. Judge the code, not the report.

- `confirmed` — you can trace a concrete path from this code to wrong or unsafe
  behaviour. State that path in `reason`.
- `refuted` — the claim does not hold. Say what makes it wrong: a guard
  elsewhere, a misread of the language, a scenario that cannot occur.
- `uncertain` — the claim is plausible but the diff alone cannot settle it,
  typically because the answer depends on code that is not shown.

Default to `refuted` when the claim is merely plausible and you cannot show the
failure. Precision is your job; recall was someone else's.
"""
    )


def verify_user(finding: Finding, ctx: PRContext) -> str:
    """User turn for verifying one finding.

    Only the claim itself is passed through — the location, the snippet and the
    one-line title. The reporter's argument, severity rating and identity are
    all withheld on purpose (PRD §3.2): handing over the rationale turns the
    verifier into a rubber stamp and removes the only stage that can cut false
    positives.
    """
    file_diff = diffs.for_file(ctx.diff, finding.file_path)
    if not file_diff:
        file_diff = "(this file does not appear in the diff)"

    return f"""\
<claim>
File: {finding.file_path}
Line: {finding.line}
Claim: {finding.title}
</claim>

<code_under_review>
{finding.code_snippet}
</code_under_review>

<untrusted_diff file="{finding.file_path}">
{file_diff}
</untrusted_diff>

Does the claim hold?
"""
