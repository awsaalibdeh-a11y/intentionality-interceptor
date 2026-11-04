"""
The Intentionality Interceptor -- AI gatekeeper response contract.
===================================================================

This module is the single source of truth for how the LLM gatekeeper is
prompted and how its answer is interpreted.  It is deliberately import-only:
no side effects, no I/O, no printing, stdlib only.

Wire contract
-------------
The app sends two chat messages::

    system : CLAUDE_SYSTEM_PROMPT      (this module)
    user   : build_user_message(app, reason, minutes_today)

and the model must answer in exactly ONE of these two shapes::

    APPROVE: <minutes>          e.g.  "APPROVE: 15"
    DENY                        e.g.  "DENY"

There is no third shape.  An approval always carries a positive whole number
of minutes (no units, no prose, no markdown).  A denial carries nothing.

Interpretation is handled by :func:`parse_verdict`, which is intentionally
unforgiving because it gates access to distracting applications:

* Matching is case-insensitive; surrounding whitespace, backticks, asterisks,
  quotes, separating punctuation are tolerated (see ``parse_verdict`` docs for
  the exact accepted grammar).
* Minutes are always clamped to ``max_minutes`` (default
  :data:`MAX_GRANT_MINUTES`), so the model can never widen the window.
* Anything ambiguous, contradictory, empty, or non-numeric fails closed to
  ``DENY`` with ``minutes == 0``.
* Minutes are only ever taken from digits the model emitted -- never inferred
  from free text such as "about an hour".

Invariants guaranteed by :func:`parse_verdict`:

    result.approved is False  =>  result.minutes == 0
    result.approved is True   =>  0 < result.minutes <= max_minutes
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "MAX_GRANT_MINUTES",
    "MIN_GRANT_MINUTES",
    "CLAUDE_SYSTEM_PROMPT",
    "Verdict",
    "build_user_message",
    "parse_verdict",
]

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: Absolute ceiling on a single granted window.  Enforced in the prompt AND
#: re-enforced by :func:`parse_verdict` (the app-side clamp).  The model cannot
#: exceed it because the parser silently clamps before returning a verdict.
MAX_GRANT_MINUTES: Final[int] = 60

#: Smallest grantable window.  A grant must be a positive integer, so 1 is the
#: floor; anything lower is treated as a denial.
MIN_GRANT_MINUTES: Final[int] = 1


# --------------------------------------------------------------------------- #
# System prompt
# --------------------------------------------------------------------------- #

CLAUDE_SYSTEM_PROMPT: str = f"""\
You are the Intentionality Interceptor, the gatekeeper between a person and a \
distracting application (games, social apps, streaming, chat launchers).

Your job is to protect the user's attention -- not to punish them. You decide \
whether they may open the app right now, and for how many minutes.

INPUT YOU WILL RECEIVE
The user message contains:
  - the application they are trying to open,
  - their stated reason, in their own words,
  - "Today's Usage: [X] minutes" -- how long they have already spent on this \
app today.

DECISION RULES
1. Be strict but fair.
   - APPROVE only reasons that are SPECIFIC, CONCRETE and TIME-BOXED or \
task-linked. Examples: "finish my history essay for tomorrow", "20-minute \
call with my friend about the group project", "check one message from my \
coach".
   - DENY vague, impulse, or open-ended reasons. Examples: "bored", "just 5 \
minutes", "I want to", "everyone else is on", "I'll only look for a second", \
"I deserve a break", or no reason at all.
   - When in doubt, DENY. Failing closed is always correct.
2. Scale the grant to the request.
   - Give the SMALLEST window that honestly covers the stated task.
   - A quick check is 5-10 minutes. A focused task is 15-30 minutes. A long \
block is at most 45-60 minutes.
3. Respect today's usage.
   - If "Today's Usage" is already high (roughly 45 minutes or more), grant \
substantially less, or DENY entirely.
   - If it is very high (over 120 minutes), DENY unless the reason is \
exceptional and clearly task-critical.
4. Hard ceiling.
   - Never grant more than {MAX_GRANT_MINUTES} minutes. This is a hard \
limit. The application also clamps your number to {MAX_GRANT_MINUTES}, so \
asking for more is pointless and will be silently reduced.
   - Never grant 0 or a negative number; that is not an approval.

OUTPUT FORMAT -- EXACTLY ONE LINE, NOTHING ELSE
- To approve:  APPROVE: <minutes>
  where <minutes> is a whole number from 1 to {MAX_GRANT_MINUTES}. Example: \
APPROVE: 15
- To deny: DENY
- Do not add prose, explanations, markdown, quotes, code fences, units, or a \
second line. Do not write "minutes" after the number.
- Your first characters must be "APPROVE:" or "DENY".
- If anything about the request is ambiguous, missing, or you are unsure: \
output DENY.
"""


# --------------------------------------------------------------------------- #
# Verdict model
# --------------------------------------------------------------------------- #


@dataclass
class Verdict:
    """Result of interpreting a raw gatekeeper reply.

    Attributes
    ----------
    approved:
        True only when the reply was an unambiguous approval with a positive
        whole number of minutes.
    minutes:
        Granted window in whole minutes.  Always 0 when ``approved`` is False;
        always in ``1..max_minutes`` when ``approved`` is True.
    raw:
        The original model text, stripped, for logging/UI.
    reason:
        Human-readable explanation suitable for the UI.
    """

    approved: bool
    minutes: int = 0
    raw: str = ""
    reason: str = ""


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

# Approval marker: the word APPROVE followed by an optional separator
# (":" or "=" or "for" or whitespace) and an optional bracketed integer.
# Examples matched: "APPROVE: 15", "APPROVE 15", "APPROVE: [15]",
# "APPROVE = 20", "APPROVE for 25".
_APPROVE_RE: Final[re.Pattern[str]] = re.compile(
    r"APPROVE"
    r"(?:\s*[:=]\s*|\s+FOR\s+|\s*)"
    # A whole integer token.  The negative lookahead rejects decimals such as
    # "APPROVE: 3.7" or "APPROVE: 15.0" instead of silently truncating them.
    r"(?:\[\s*([+-]?\d+)\s*\]|([+-]?\d+)(?![\d.]\d|\d))"
)

# Sentence-level approval token (the word APPROVE standing alone), used for
# contradiction detection even when no usable number is present.
_APPROVE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\bAPPROVE\b")

# Sentence-level denial token.
_DENY_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\bDENY\b")

# Characters stripped from the edges before matching.
_EDGE_JUNK: Final[str] = " \t\r\n`*\"'“”‘’.,!:;[](){}<>-_/\\|"

# Internal separator punctuation neutralised so "APPROVE\n15" or
# "APPROVE -- 15" behave like "APPROVE 15".
_INTERNAL_JUNK_RE: Final[re.Pattern[str]] = re.compile(r"[`*\"'“”‘’.,;!?(){}<>\[\]|\\]+")


def _normalise(raw: str) -> str:
    """Upper-case, collapse whitespace, remove decorative punctuation.

    The transformation is deliberately conservative: digits, ``:``, ``=``,
    ``+``, ``-`` and ``_`` survive so the grammar below still sees them.
    """
    text = raw.strip()
    # Strip a surrounding code fence, if present.
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3]
    text = _INTERNAL_JUNK_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(_EDGE_JUNK).strip().upper()


def parse_verdict(raw: str | None, max_minutes: int = MAX_GRANT_MINUTES) -> Verdict:
    """Interpret a raw gatekeeper reply as a :class:`Verdict`.

    Accepted approval formats (case-insensitive, decoration tolerated)::

        APPROVE: 15
        APPROVE:15
        APPROVE 15            (whitespace separator)
        APPROVE = 15          ("=" separator)
        APPROVE for 15        ("for" separator)
        APPROVE: [15]         (bracketed integer)

    Accepted denial format::

        DENY

    Tolerated decoration: leading/trailing whitespace, backticks, asterisks,
    single/double/curly quotes, and trailing sentence punctuation
    (``.``, ``,``, ``!``, ``:``, ``;``, brackets).  Markdown fences are stripped.

    Rules and fail-closed behaviour:

    * Minutes must be a whole integer written in digits.
    * ``APPROVE: 0``, negative numbers, or non-numeric garbage -> DENY.
    * A positive number larger than ``max_minutes`` is clamped, not denied.
    * A reply containing both an approval token and a denial token -> DENY
      (contradiction), regardless of ordering.
    * Empty/None/unparseable reply, or free text with no digits
      (e.g. ``"Approve for an hour"``) -> DENY.

    Invariants: ``minutes == 0`` whenever ``approved`` is False, and
    ``0 < minutes <= max_minutes`` whenever ``approved`` is True.
    """
    # Defensive: a non-positive ceiling makes any approval impossible.
    try:
        ceiling = int(max_minutes)
    except (TypeError, ValueError):
        ceiling = 0
    if ceiling < MIN_GRANT_MINUTES:
        ceiling = 0

    if raw is None:
        return Verdict(False, 0, "", "No response from the gatekeeper; denied.")

    original = raw.strip() if isinstance(raw, str) else str(raw).strip()
    if not original:
        return Verdict(False, 0, original, "Empty response; denied.")

    text = _normalise(original)
    if not text:
        return Verdict(False, 0, original, "Unparseable response; denied.")

    has_approve = _APPROVE_TOKEN_RE.search(text) is not None
    has_deny = _DENY_TOKEN_RE.search(text) is not None

    # Contradiction: both tokens present -> fail closed.
    if has_approve and has_deny:
        return Verdict(
            False, 0, original,
            "Contradictory response (contains both APPROVE and DENY); denied.",
        )

    if has_deny:
        return Verdict(False, 0, original, "Gatekeeper denied the request.")

    if not has_approve:
        return Verdict(False, 0, original, "No APPROVE/DENY token; denied.")

    # Approval token without a parseable integer -> DENY (never "0 minutes").
    match = _APPROVE_RE.search(text)
    if match is None:
        return Verdict(
            False, 0, original,
            "Approval without a numeric minute count; denied.",
        )

    # Guard against decimals.  _normalise() neutralises "." to a space, so
    # "APPROVE: 3.7" arrives as "APPROVE: 3 7" and would otherwise be read as
    # a 3-minute grant.  A second bare number immediately after the minute
    # token is exactly that signature -> fail closed.
    tail = text[match.end():].lstrip()
    if re.match(r"\d", tail):
        return Verdict(
            False, 0, original,
            "Non-integer minute count; denied.",
        )

    digits = match.group(1) if match.group(1) is not None else match.group(2)
    try:
        minutes = int(digits)
    except (TypeError, ValueError):
        return Verdict(
            False, 0, original,
            "Non-numeric minute count; denied.",
        )

    if minutes < MIN_GRANT_MINUTES:
        return Verdict(
            False, 0, original,
            f"Grant must be a positive minute count (got {minutes}); denied.",
        )

    if ceiling == 0:
        return Verdict(False, 0, original, "No grantable window configured; denied.")

    if minutes > ceiling:
        return Verdict(
            True, ceiling, original,
            f"Requested {minutes} min, clamped to the {ceiling}-minute ceiling.",
        )

    return Verdict(True, minutes, original, f"Granted {minutes} minute(s).")


# --------------------------------------------------------------------------- #
# User-message builder
# --------------------------------------------------------------------------- #


def build_user_message(app: str, reason: str, minutes_today: int) -> str:
    """Render the user-role message the app sends to the gatekeeper.

    The resulting text always contains the literal phrase
    ``Today's Usage: [X] minutes`` with the real number substituted, plus the
    application name and the user's stated reason.
    """
    app_text = (app or "an unknown application").strip()
    reason_text = (reason or "").strip()
    if not reason_text:
        reason_text = "(no reason given)"

    try:
        usage = max(0, int(minutes_today))
    except (TypeError, ValueError):
        usage = 0

    return (
        f"The user is asking to use: {app_text}\n"
        f"Their stated reason: \"{reason_text}\"\n"
        f"Today's Usage: {usage} minutes\n"
        "Decide. Reply with exactly one line: "
        f"APPROVE: <minutes 1-{MAX_GRANT_MINUTES}> or DENY."
    )
