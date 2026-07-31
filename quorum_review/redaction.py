"""Keeping the reviewer from being the thing that leaks the secret.

A finding about a hardcoded credential quotes the credential. That is what makes
the finding legible — "`API_KEY = "sk-live-..."` is committed" is a better
comment than "there is a secret on line 14". It also means the reviewer takes a
value that was sitting in a diff and republishes it in a pull request comment,
where it is more visible, harder to remove, and on a public repository, indexed.

Diffs are already visible to whoever can see the pull request, so this is not a
disclosure boundary being crossed. It is a blast radius being widened by the one
participant that should know better. The comment is also what survives: force-
push the branch and the diff is gone, while the comment stays.

So the shapes below are replaced before anything is posted. The list is
deliberately short. Every pattern here matches a token format that is issued by
a service and is worthless to a reader in full — nobody needs the last thirty
characters of an AWS key to act on the finding. Anything that would need a
guess about whether a string is sensitive is left alone, because a reviewer
that mangles ordinary code is a reviewer people turn off.
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "[redacted by quorum-review]"

#: Ordered, and the order is not arbitrary: the private-key block has to be
#: taken before anything inside it matches something narrower.
#:
#: Each entry is (name, pattern). A pattern must be specific enough that a
#: false match is close to impossible — the cost of over-redacting is a finding
#: nobody can read, which is worse than the leak this prevents in the cases
#: where it fires wrongly.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private key",
        re.compile(
            r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github app token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("stripe key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{20,}\b")),
    ("google service account key", re.compile(r'"private_key_id"\s*:\s*"[^"]+"')),
    (
        "connection string password",
        # postgres://user:secret@host — the password only, not the rest, so the
        # finding can still say which database it was talking about.
        re.compile(r"(?<=://)([^\s:/@]+):([^\s:/@]+)(?=@)"),
    ),
    (
        "authorization header",
        # The quoting varies by language — `Authorization: Bearer x`,
        # `"Authorization": "Bearer x"`, `authorization='Bearer x'` — so the
        # quotes are optional on both sides of the separator. The value is a
        # token character class rather than \S so that a trailing quote or
        # brace stays outside the match and the line still reads as code.
        re.compile(
            r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?)"
            r"(?:bearer\s+|token\s+)?[A-Za-z0-9._\-+/=]{16,}"
        ),
    ),
]


def redact(text: str) -> tuple[str, list[str]]:
    """Return the text with credential shapes replaced, and what was replaced.

    The names are returned rather than the values, for the obvious reason. They
    go in the comment so the reader knows something was removed — a finding
    that silently quotes ``[redacted]`` with no explanation reads like a bug in
    the reviewer.
    """
    if not text:
        return text, []

    found: list[str] = []
    for name, pattern in PATTERNS:
        text, count = pattern.subn(_replacement(name), text)
        if count:
            found.append(name)
    return text, found


def _replacement(name: str):
    def replace(match: re.Match[str]) -> str:
        # Connection strings keep the username: knowing the review is about
        # `postgres://app_writer:...@db` is most of the value of the finding.
        if name == "connection string password":
            return f"{match.group(1)}:{PLACEHOLDER}"
        if name == "authorization header":
            return f"{match.group(1)}{PLACEHOLDER}"
        return PLACEHOLDER

    return replace


def sanitise(finding: Any) -> list[str]:
    """Strip credential shapes from a finding, in place. Returns what was found.

    Called once, on every finding, before anything renders or records it. Doing
    it at each rendering site instead would work until someone adds a new one —
    and one of the existing ones is the ledger, which is stored inside the
    summary comment, so a leak there is just as public and lives longer.

    ``fix_replacement`` is not redacted but dropped. A suggestion is applied
    verbatim by a click, so a redacted one would write the placeholder into the
    file.
    """
    found: list[str] = []
    for field_name in ("title", "body", "code_snippet", "verifier_reason"):
        value = getattr(finding, field_name, "")
        if not value:
            continue
        cleaned, hits = redact(value)
        if hits:
            setattr(finding, field_name, cleaned)
            found += hits

    fix = getattr(finding, "fix_replacement", "")
    if fix:
        _, hits = redact(fix)
        if hits:
            finding.fix_replacement = ""
            finding.fix_end_line = 0
            found += hits
    return found


def note(found: list[str]) -> str:
    """A line for the comment explaining what was taken out, or "" for none."""
    if not found:
        return ""
    kinds = ", ".join(sorted(set(found)))
    return (
        f"<sub>🔒 A value matching a credential format ({kinds}) was removed "
        f"from this comment. It is still in the diff — rotate it, then remove "
        f"it from the branch.</sub>"
    )
