"""Caller-input validation primitives shared by the pipeline's entry nodes.

Three guarantees, applied to every value that arrives from a caller:

1. **Numbers are finite and bounded.** ``float("nan")`` and ``float("inf")``
   both survive a plain ``isinstance(value, (int, float))`` check, and every
   comparison against NaN evaluates False — so an unchecked NaN does not raise,
   it silently answers "within tolerance" to the exact question this agent
   exists to decide. Numbers are therefore parsed through
   :func:`finite_in_range`, which fails closed and names the field.
   ``json.loads`` additionally accepts the bare literals ``NaN``, ``Infinity``
   and ``-Infinity``, so the batch is decoded with
   :func:`reject_json_constants` wired into ``parse_constant``.

2. **Strings that reach the report are inert.** A label identifier is echoed
   back in the rendered report, so it is locked to a short identifier alphabet
   rather than accepted as free text. Rule names, rule details and the model's
   own assessment are not caller-controlled and keep their full character set,
   Japanese included.

3. **Credential shapes are refused at the boundary, using the framework's own
   detector.** A label identifier is an inert-looking alphabet — letters,
   digits, dashes — and an API key fits inside it exactly. Left to the output
   gate, such a value is caught only after the whole batch has been processed,
   and the caller gets a withheld report instead of a usable answer about the
   field they got wrong. Screening here calls the same detector the framework's
   gate calls, so what is refused at the door and what is blocked at the exit
   are one set by construction.

4. **Free text is screened for prompt injection before and after sanitizing.**
   Stripping markup is not refusal: the tag stripper removes
   ``<|im_start|>`` as though it were an HTML tag and forwards the directive
   that followed it as ordinary prose, converting a detectable attack into an
   undetectable one. Screening the raw text catches control tokens before they
   are erased; screening the sanitized text catches directives that were split
   by markup (``ig<b>nore all previous instructions``) and only become
   contiguous once the markup is gone.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping

from framework.security.credential_detector import detect_credentials_in_value

# Identifier alphabet for caller strings that render into the report. Retail
# label and article codes are ASCII in every source system this agent reads
# from; anything outside this set is data, not an identifier.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

# Chat-template control tokens, screened as a class rather than as a list of
# known phrases. The delimiters themselves are the signal: no legitimate price
# label or promotional claim contains them.
_CONTROL_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<\|[^|>\n]{0,64}\|>"),  # <|im_start|>, <|endoftext|>, ...
    re.compile(r"\[/?INST\]"),  # [INST] / [/INST]
    re.compile(r"<</?SYS>>"),  # <<SYS>> / <</SYS>>
    re.compile(r"(?i)<\|?(?:im_start|im_end|endoftext)\|?>"),
)

# Directive phrases, anchored to a verb plus an explicit reference to earlier
# instructions. Anchoring matters in both directions: an unanchored verb match
# would refuse legitimate copy, and this agent's whole purpose is to accept
# real promotional wording.
_DIRECTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)?"
        r"(?:previous|prior|preceding|above|earlier|foregoing)\s+"
        r"(?:instruction|instructions|rule|rules|prompt|prompts|direction|directions)\b"
    ),
    re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an|the)\b"),
    re.compile(r"(?i)\b(?:system|developer)\s+prompt\b"),
    re.compile(r"(?i)\bact\s+as\s+(?:a|an|the)\s+(?:system|assistant|admin|administrator)\b"),
    re.compile(r"(?:これまで|以前|上記|前)の(?:指示|命令|規則|ルール)を(?:無視|忘れ)"),
    re.compile(r"システム\s*プロンプト"),
)

# Markup stripper. Deliberately conservative: it removes tag-shaped runs so the
# model never receives markup, and it runs AFTER the raw screen so nothing it
# erases can pass unnoticed.
_MARKUP_RE = re.compile(r"<[^>]*>")


class CallerInputError(ValueError):
    """A caller-supplied value failed validation. Names the field, never the value.

    Every message raised in this module is composed of this module's own
    constant phrases plus a positional field path (``labels[3].sale_price``) and
    bounds that are module or operator constants. No caller value, no decoded
    literal and no third-party string is ever part of one — which is what makes
    it safe for a node to carry the message into ``error_log``. Pinned by test:
    a recognisable value driven through every refusal path appears in no
    message.
    """


def reject_json_constants(literal: str) -> float:
    """``parse_constant`` hook that refuses the non-finite JSON literals.

    ``json.loads`` accepts bare ``NaN``, ``Infinity`` and ``-Infinity`` by
    default and returns real float objects for them, so a batch can carry a
    non-finite number without ever passing through a Python numeric literal.

    The literal is NOT echoed. Which of the three was sent is not information
    the caller lacks, and the argument arrives from the decoder rather than from
    this module — so interpolating it would be the one place in this module
    where a refusal message is composed of something other than its own
    constants and a field path. The invariant is worth more than the detail.
    """
    raise CallerInputError("a non-finite JSON literal is not accepted")


def finite_in_range(
    value: Any,
    *,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    """Return ``value`` as a finite number within ``[minimum, maximum]``.

    Fails closed on: booleans (a ``bool`` is an ``int`` subclass and would
    otherwise be read as 0 or 1), non-numeric types, NaN, positive and negative
    infinity, and out-of-range magnitudes. The error names the field only — the
    rejected value is never echoed back.

    An integer is returned as an integer. Prices in this domain are whole
    currency units, and the report is read to decide whether a displayed price
    is wrong — so an amount submitted as ``980`` must be rendered as ``980``,
    not as ``980.0``. Widening every amount to a float would not corrupt the
    value, but it would stop the report showing the caller their own figure.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CallerInputError(f"{field} must be a number")
    number: float = value
    if not math.isfinite(number):
        raise CallerInputError(f"{field} must be a finite number")
    if not (minimum <= number <= maximum):
        raise CallerInputError(f"{field} must be between {minimum} and {maximum}")
    return number


def safe_identifier(value: Any, *, field: str) -> str:
    """Return ``value`` if it is an inert identifier, else fail closed.

    Applied to caller strings that are echoed into the rendered report. The
    alphabet is ASCII alphanumerics plus ``_``, ``.`` and ``-``, starting with
    an alphanumeric, at most 64 characters.
    """
    if not isinstance(value, str):
        raise CallerInputError(f"{field} must be a string")
    if not _SAFE_IDENTIFIER_RE.match(value):
        raise CallerInputError(
            f"{field} must be 1-64 characters of A-Z, a-z, 0-9, '_', '.' or '-', " "starting with a letter or digit"
        )
    return value


def screen_for_credentials(value: Any, *, field: str) -> None:
    """Refuse a credential-shaped value anywhere inside ``value``.

    Delegates to the framework detector, which recurses through mappings and
    lists on its own, so this refusal set and the framework gate's block set
    cannot drift apart. The field is named; the value never is.
    """
    if detect_credentials_in_value(value):
        raise CallerInputError(f"{field} contains a credential-shaped value")


def _iter_strings(value: Any) -> Iterable[str]:
    """Yield every string in a nested structure, keys included.

    Keys are walked as well as values: a directive placed in a field NAME
    reaches the same downstream text as one placed in a value, and escape
    sequences in the wire format are already resolved by the time the parsed
    object gets here, so a post-parse walk cannot be evaded by encoding.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _screen_one(text: str, *, field: str) -> None:
    for pattern in _CONTROL_TOKEN_PATTERNS:
        if pattern.search(text):
            raise CallerInputError(f"{field} contains a chat-template control token")
    for pattern in _DIRECTIVE_PATTERNS:
        if pattern.search(text):
            raise CallerInputError(f"{field} contains an instruction-override directive")


def screen_for_injection(value: Any, *, field: str) -> None:
    """Refuse prompt-injection payloads anywhere inside ``value``.

    Every string is screened twice — once as received and once with markup
    removed — because each pass catches what the other cannot. The raw pass
    sees control tokens that markup stripping would delete; the stripped pass
    sees directives that markup had split apart.
    """
    for text in _iter_strings(value):
        _screen_one(text, field=field)
        stripped = _MARKUP_RE.sub("", text)
        if stripped != text:
            _screen_one(stripped, field=field)


def strip_markup(text: str) -> str:
    """Remove tag-shaped runs from caller text bound for the model."""
    return _MARKUP_RE.sub("", text)
