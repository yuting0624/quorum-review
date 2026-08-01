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


def untrusted(tag: str, content: str) -> str:
    """Wrap attacker-controlled text so it cannot climb out of its own block.

    The whole labelling scheme rests on the model being able to tell where the
    untrusted text ends, and interpolating raw content into
    ``<untrusted_x>...</untrusted_x>`` does not achieve that: a pull request
    title reading ``</untrusted_pr_title>`` closes the block, and everything
    after it sits beside the instructions as a peer.

    That was live in every prompt this project sends. Neutralising the closing
    delimiter is the fix, and it has to happen in one place — the failure is
    invisible at each call site, because each one looks like correct XML.

    The replacement is deliberately readable rather than escaped: a model that
    sees ``<!untrusted_pr_title>`` understands what was attempted, and the base
    instructions tell it to report exactly that as a finding.
    """
    safe = (content or "").replace("</untrusted", "</!untrusted")
    safe = safe.replace("<untrusted", "<!untrusted")
    return "\n".join([f"<untrusted_{tag}>", safe, f"</untrusted_{tag}>"])


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

    Every attacker-controlled field goes through ``untrusted()``; see
    ``BASE_INSTRUCTIONS`` for how the model is told to treat them, and
    ``untrusted()`` for why interpolating them directly did not work.
    """
    return (
        f"Repository: {ctx.owner}/{ctx.repo}\n"
        f"Pull request: #{ctx.number}\n\n"
        + untrusted("pr_title", ctx.title)
        + "\n\n"
        + untrusted("pr_body", ctx.body)
        + "\n\n"
        + untrusted("diff", ctx.diff)
        + "\n"
    )


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

    return (
        claim
        + "\n\n"
        + untrusted("diff", file_diff)
        + "\n\n"
        + untrusted("conversation", conversation)
        + "\n\nReply to the most recent message.\n"
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

    return (
        f"<claim>\nFile: {finding.file_path}\nLine: {finding.line}\n"
        f"Claim: {finding.title}\n</claim>\n\n"
        # The snippet is the model's own quotation of attacker-controlled code,
        # so it is labelled too — it was not, and a claim is a shorter route to
        # the instructions than a diff.
        + untrusted("code_under_review", finding.code_snippet)
        + "\n\n"
        + untrusted("diff", file_diff)
        + "\n\nDoes the claim hold?\n"
    )


def criteria_system(language: str = "") -> str:
    """System prompt for turning dismissals into a proposed criteria change.

    This path was the one place attacker-controlled text reached a model
    without the base instructions in front of it, and it is the worst place for
    that: its output is a proposed edit to the criteria, offered to a human to
    paste in. A successful injection here does not produce one wrong comment —
    it produces a permanent hole, applied by someone who thought they were
    tidying up false positives.

    The finding titles quote code from the diff. The dismissal reasons come
    from someone with write access, which the handler checks, so they are the
    trusted half — but they arrive in the same block and are labelled with it.
    """
    return (
        BASE_INSTRUCTIONS
        + language_directive(language)
        + """
## Your task

Findings were reported by a code reviewer and then dismissed by a maintainer of
the repository. Each dismissal is a statement about what this codebase does not
consider a problem. Propose a change to the criteria that would stop these
specific findings being reported, without blinding the reviewer to the real
problems the criteria exist to catch.

- If the dismissals share a cause, say what it is in one sentence.
- Give the edit as a short Markdown snippet ready to paste into the criteria,
  usually an addition to a "Do not report" section.
- If a dismissal looks like a one-off rather than a pattern, say so and leave
  it out. Narrowing the criteria for a single case costs more than it saves.
- If the criteria are fine and the model simply misapplied them, say that
  instead of inventing an edit.
- **Never propose removing a whole category** — dropping "injection" or
  "access control" is not a narrowing, it is a blind spot. If that is what the
  dismissals would imply, say so and propose nothing.

Reply as plain Markdown. Be brief.
"""
    )
