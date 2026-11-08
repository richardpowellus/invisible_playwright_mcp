"""Redact text shaped like an authenticator secret from every tool result.

⛔ A SNAPSHOT PRINTED A LIVE TOTP SEED INTO THE CONVERSATION (2026-11-08).
Ramp's authenticator-app setup drawer shows the base32 setup key in a
``<code>`` element and puts the whole key in its copy button's accessible
name, ``aria-label="Copy JBSW...``. ``browser_snapshot`` lists that button
with its name, so the 32-character seed reached the agent's transcript. The
masking in ``masked.py`` cannot see it: it only knows values this server was
asked to type. A secret the PAGE shows has never been registered.

So this module recognises secrets by their shape, wherever they appear in a
result - an accessible name, a label, text, an attribute, a JSON reply of
``browser_evaluate``:

  * ``otpauth://`` and ``otpauth-migration://`` URIs, whole, in their plain,
    percent-encoded and JSON-escaped spellings;
  * a ``secret=`` URI parameter;
  * a base32 run of 16 or more characters (``A-Z2-7``, one case, optional
    ``=`` padding) standing as a word on its own, and the same split into
    groups of four by spaces or dashes, as authenticator setup pages print it.
    It must also contain one of ``2-7``, or be 24+ characters long, so an
    ordinary long word is never taken for a key (beside a label, below, the
    digit is not needed);
  * any code-like token within a short distance after a label that names a
    secret - "secret key", "setup key", "recovery code", "backup codes", ...

It is applied at the one result boundary (``masked.redact_result``), to every
caller EXCEPT a call made with a valid browser fill handle: the 1Password
filler reads a setup key from the page precisely to move it into a vault
without it entering a transcript, and its own result guard keeps it out of
the conversation. Redaction there would break the only safe path.

What it cannot do: recognise a secret that has no secret's shape and no
label beside it. It narrows the leak; it does not make reading a secret
page safe.
"""
from __future__ import annotations

import re

#: What replaces a redacted secret. Says why, so a caller that needed the
#: text knows to use the credential tools rather than retry the read.
REDACTED = "[secret redacted]"

_SLASH = r"(?:/|\\/|%2[Ff])"
_OTPAUTH = re.compile(
    r"otpauth(?:-migration)?(?::|%3[Aa])" + _SLASH + _SLASH +
    r"[^\s\"'<>\\`]*(?:\\/[^\s\"'<>\\`]*)*", re.IGNORECASE)

_SECRET_PARAM = re.compile(
    r"((?:^|[?&;#]|&amp;|\b)secret(?:=|%3[Dd]))[A-Za-z0-9%=]+", re.IGNORECASE)

# Not preceded or followed by a letter or a digit: a key is a word on its own,
# never the middle of a longer identifier.
_EDGE_BEFORE = r"(?<![A-Za-z0-9])"
_EDGE_AFTER = r"(?![A-Za-z0-9])"

_RUN_UPPER = re.compile(_EDGE_BEFORE + r"[A-Z2-7]{16,}=*" + _EDGE_AFTER)
_RUN_LOWER = re.compile(_EDGE_BEFORE + r"[a-z2-7]{16,}=*" + _EDGE_AFTER)
_GROUPED = re.compile(
    _EDGE_BEFORE + r"(?:[A-Z2-7]{4}(?:[ \-][A-Z2-7]{4}){3,}|[a-z2-7]{4}(?:[ \-][a-z2-7]{4}){3,})"
    + _EDGE_AFTER)

#: Words a page puts beside a secret it shows. Matched case-insensitively.
_LABEL = re.compile(
    r"(?i)\b(?:"
    r"(?:secret|setup|manual(?:[ -]entry)?|authenticator(?:[ -]app)?|2fa|mfa|totp|otp|"
    r"two[ -]factor|verification|security)[ -](?:key|secret|seed)"
    r"|(?:recovery|backup|emergency|scratch)[ -](?:codes?|keys?)"
    r"|secret(?:[ -]code)?"
    r"|private[ -]key|api[ -]secret|client[ -]secret|shared[ -]secret"
    r")\b")

#: How far after a label its secret may stand: the same accessible name, the
#: next few lines of a snapshot.
_LABEL_REACH = 240

#: A code-like token: groups of letters and digits joined by single spaces or
#: dashes. Judged by _code_like, not by this pattern alone.
_TOKEN = re.compile(_EDGE_BEFORE + r"[A-Za-z0-9]{2,}(?:[ \-][A-Za-z0-9]{2,})*" + _EDGE_AFTER)


def _has_key_digit(text: str) -> bool:
    return any(char in "234567" for char in text)


def _code_like(token: str) -> bool:
    """A recovery code or a setup key, not a sentence that follows a label."""
    alnum = "".join(char for char in token if char.isalnum())
    if len(alnum) < 6:
        return False
    parts = re.split(r"[ \-]", token)
    has_digit = any(char.isdigit() for char in alnum)
    has_alpha = any(char.isalpha() for char in alnum)
    # Words separated by spaces are prose unless every part carries a digit
    # ("a1b2 c3d4") or every part is a four-character group ("abcd efgh ...").
    if len(parts) > 1 and " " in token:
        if all(any(c.isdigit() for c in p) for p in parts):
            return True
        return len(parts) >= 4 and all(len(p) == 4 for p in parts)
    if has_digit:
        return True
    # A single letters-only token of a key's length: an unspaced seed whose
    # random draw had no digit. Only upper-case, as base32 is shown.
    return has_alpha and len(alnum) >= 16 and alnum.isupper()


def _redact_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, copied = [], 0
    for start, end in merged:
        out.extend((text[copied:start], REDACTED))
        copied = end
    out.append(text[copied:])
    return "".join(out)


def redact_secret_shapes(text: str) -> str:
    """Replace every secret-shaped span of ``text`` with REDACTED."""
    if not text or len(text) < 6:
        return text
    text = _OTPAUTH.sub(REDACTED, text)
    text = _SECRET_PARAM.sub(lambda m: m.group(1) + REDACTED, text)
    spans: list[tuple[int, int]] = []
    # An all-letter upper-case run of a key's length is an English word in
    # capitals until it is longer than any such word; beside a label it is
    # caught below whatever its length.
    spans.extend(m.span() for m in _RUN_UPPER.finditer(text)
                 if _has_key_digit(m.group()) or len(m.group().rstrip("=")) >= 24)
    spans.extend(m.span() for m in _RUN_LOWER.finditer(text) if _has_key_digit(m.group()))
    spans.extend(m.span() for m in _GROUPED.finditer(text) if _has_key_digit(m.group()))
    for label in _LABEL.finditer(text):
        reach_end = min(len(text), label.end() + _LABEL_REACH)
        # Grouped keys of letters only are accepted here, beside their label.
        for m in _GROUPED.finditer(text, label.end(), reach_end):
            spans.append(m.span())
        for m in _TOKEN.finditer(text, label.end(), reach_end):
            if _code_like(m.group()):
                spans.append(m.span())
            else:
                # A prose token may still hold a code among its words.
                for part in re.finditer(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", m.group()):
                    if _code_like(part.group()):
                        spans.append((m.start() + part.start(), m.start() + part.end()))
    return _redact_spans(text, spans)
