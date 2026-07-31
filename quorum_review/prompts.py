"""Prompt construction, shared by every provider.

Both stages are built here so that the ``vertex`` and ``direct`` providers put
identical text in front of their models. If the prompts diverged per provider,
comparing the two configurations would measure the prompts rather than the
models.
"""

from __future__ import annotations

from . import diffs
from .schema import (
    BASE_INSTRUCTIONS,
    Discussion,
    Finding,
    PRContext,
    Skill,
    language_directive,
)

#: Closes an exploration loop. The model has been reading the repository with
#: tools; this turn asks for the schema-shaped answer and nothing else. It is a
#: separate turn because Gemini cannot be given tools and a response schema in
#: the same request.
FINALISE = """\
Stop investigating and give your final answer now, as JSON matching the schema \
you were given. Base it on the diff and on whatever you read. Do not call any \
more tools.\
"""

#: The same, for a turn that produces prose rather than a schema — answering a
#: question in a review thread.
FINALISE_PROSE = """\
Stop investigating and answer now, in Markdown, using the diff and whatever you \
read. Do not call any more tools.\
"""

#: Appended to a scan or verification when the repository is readable. The
#: instruction that earns its place is the last one: most false positives in
#: code review are a guard the reviewer could not see, and most misses are a
#: definition it did not open.
_TOOL_GUIDANCE = """
## Reading the repository

The diff is the change; it is not the codebase. You have three read-only tools —
`read_file`, `search`, `list_files` — over the full checkout, and you are
expected to use them before deciding anything that the diff alone cannot settle:

- A call to a function defined elsewhere. Open it. `validate_x(...)` is not
  evidence that anything was validated; the body is.
- A missing check. Search for it before reporting it, in the caller, in a
  decorator, in middleware. Absent from the diff is not absent from the code.
- A name in a registry, a constant, a config key. Confirm it is actually there.
- An argument list. Read the signature rather than inferring it from the name.

Your budget is a couple of dozen calls, so spend them on the claims that turn on
what they would show. Findings you can already establish from the diff need no
lookup. When the budget runs out, answer from what you have.
"""


def scan_system(skill: Skill, language: str = "", tools: bool = False) -> str:
    """System prompt for the primary scan. Optimised for recall."""
    return (
        BASE_INSTRUCTIONS
        + language_directive(language)
        + (_TOOL_GUIDANCE if tools else "")
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


def verify_system(language: str = "", tools: bool = False) -> str:
    """System prompt for verification.

    Constant across every finding in a run, which is what makes it worth a
    prompt-cache breakpoint: the verifier is called once per finding, so this
    text is re-sent N times.
    """
    settle = (
        """
Because you can read the repository, `uncertain` now means you looked and still
could not tell — not that the answer was somewhere you could not reach. If a
definition would settle it, open the definition.
"""
        if tools
        else ""
    )
    return (
        BASE_INSTRUCTIONS
        + language_directive(language)
        + (_TOOL_GUIDANCE if tools else "")
        + settle
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


def discuss_system(language: str = "", tools: bool = False) -> str:
    """System prompt for answering a question about a finding."""
    return (
        BASE_INSTRUCTIONS
        + language_directive(language)
        + (_TOOL_GUIDANCE if tools else "")
        + """
## Your task

Someone is asking about a finding in a code review. Answer them, in the thread,
as a reviewer who is accountable for what was reported.

- Answer the question that was asked. Do not restate the finding.
- If they have shown the finding is wrong, say so plainly. Being right matters
  more than the finding standing; tell them they can reply
  `@quorum wontfix — <reason>` to retire it.
- If they are asking whether some other case is affected, reason about that
  case specifically rather than repeating the general rule.
- If you cannot answer from what you can see, say what you would need. Look
  first, if looking is available to you.
- No preamble, no sign-off. A few sentences is usually right; use a short code
  block when it is clearer than prose.

Reply as plain Markdown. There is no JSON schema for this response.
"""
    )


def discuss_user(discussion: Discussion, ctx: PRContext) -> str:
    """User turn for a question in a review thread."""
    file_diff = diffs.for_file(ctx.diff, discussion.file_path)
    if not file_diff:
        file_diff = "(this file does not appear in the current diff)"

    claim = (
        f"<finding>\nFile: {discussion.file_path}:{discussion.line}\n"
        f"Claim: {discussion.title}\n\n{discussion.body}\n</finding>"
        if discussion.title
        else f"<finding>\nFile: {discussion.file_path}:{discussion.line}\n"
        f"(the original finding is no longer on record)\n</finding>"
    )

    conversation = "\n\n".join(
        f"{author}:\n{text}" for author, text in discussion.transcript
    )

    return f"""\
{claim}

<untrusted_diff file="{discussion.file_path}">
{file_diff}
</untrusted_diff>

<untrusted_conversation>
{conversation}
</untrusted_conversation>

Reply to the most recent message.
"""


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
