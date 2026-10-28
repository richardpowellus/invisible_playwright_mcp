"""Python-only, browser-lifetime masking; nothing is marked in the page."""
from __future__ import annotations

import asyncio
import html
import json
from urllib.parse import quote, quote_plus

from pydantic import BaseModel

from .clean import MASKED_PASSWORD

SCREENSHOT_REFUSED = "screenshot refused: a masked value is on the page; nothing was captured"


class MaskRefusal(ValueError):
    """A fixed, value-free refusal that must reach the caller verbatim."""


def validate(text: str, expect_origin: str | None) -> None:
    if not expect_origin:
        raise MaskRefusal("mask_value refused: requires expect_origin; nothing was written")
    if text and len(text) < 8:
        raise MaskRefusal(
            "mask_value refused: text must be at least 8 characters; nothing was written")


def _windows(text: str) -> set[str]:
    return {text[i:i + 8] for i in range(len(text) - 7)}


#: What a page inserts or removes when it formats a value for display: a card
#: number shown in groups of four, a phone number, an IBAN. Separators only;
#: a page that rewrites the characters themselves is not matched.
SEPARATORS = frozenset("-.\u2010\u2011\u2012\u2013\u2014\u2015\u2212")

#: Marks a window of a value with its separators removed, so it travels in the
#: same set as the exact windows. Nine characters, and never a whole: no text
#: slice is compared against it.
_BARE = "\ue000"


def _is_separator(char: str) -> bool:
    return char.isspace() or char in SEPARATORS


def _bare_windows(value: str) -> set[str]:
    """⛔ A MASK THAT MATCHED ONLY THE TYPED FORM LEAKED A CARD NUMBER.

    Helcim's card field (2026-10-26) took 16 digits typed with mask_value and
    showed them as four groups of four. No 8-character window of the typed
    digits occurs in "NNNN NNNN NNNN NNNN", so the next snapshot printed the
    whole number and the screenshot guard saw nothing on the page. These
    windows match the value with separators ignored on both sides."""
    bare = "".join(char for char in value if not _is_separator(char))
    return {_BARE + window for window in _windows(bare)}


def _forms(value: str) -> tuple[str, ...]:
    return (value, html.escape(value, quote=True), html.escape(value, quote=False),
            json.dumps(value, ensure_ascii=True)[1:-1],
            json.dumps(value, ensure_ascii=False)[1:-1],
            quote(value, safe=""), quote_plus(value))


class MaskedValues:
    def __init__(self) -> None:
        self.raw: set[str] = set()
        self.forms: set[str] = set()

    def register(self, value: str) -> None:
        if not value:
            return
        self.raw.update(_windows(value))
        for form in _forms(value):
            self.forms.update(_windows(form))
        bare = _bare_windows(value)
        self.raw.update(bare)
        self.forms.update(bare)

    def register_exact(self, value: str) -> None:
        """Mask a value (16+ characters) only where it appears whole.

        A typed secret is masked by every 8-character window, so a fragment
        cannot leak. An exported cookie is not typed by anyone and is often
        not random - a return URL, a locale list - and its windows then mask
        ordinary text: a cookie holding an encoded QBO URL masked the page's
        own origin. Entries longer than a window are matched whole.
        """
        if len(value) <= 8:
            return
        self.raw.add(value)
        self.forms.update(form for form in _forms(value) if len(form) > 8)

    def clear(self) -> None:
        self.raw.clear()
        self.forms.clear()

    def redact(self, text: str) -> str:
        return redact_text(text, self.forms)


def registry(session) -> MaskedValues:
    current = getattr(session, "_masked_values", None)
    if current is None:
        current = session._masked_values = MaskedValues()
    return current


def forget(session) -> None:
    current = getattr(session, "_masked_values", None)
    if current is not None:
        current.clear()


def result_windows(work) -> set[str]:
    return set().union(*(registry(s).forms for s in work._open.values()))


def redact_text(text: str, windows: set[str]) -> str:
    if not windows:
        return text
    return _redact_bare(_redact_exact(text, windows), windows)


def _redact_bare(text: str, windows: set[str]) -> str:
    """Mask any run whose characters, separators removed, are a registered
    value's bare window; the separators inside the run are masked with it."""
    index = [i for i, char in enumerate(text) if not _is_separator(char)]
    bare = "".join(text[i] for i in index)
    spans: list[list[int]] = []
    for j in range(len(bare) - 7):
        if _BARE + bare[j:j + 8] not in windows:
            continue
        start, end = index[j], index[j + 7] + 1
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    if not spans:
        return text
    out = []
    copied = 0
    for start, end in spans:
        out.extend((text[copied:start], MASKED_PASSWORD))
        copied = end
    out.append(text[copied:])
    return "".join(out)


def _redact_exact(text: str, windows: set[str]) -> str:
    # Whole values (register_exact) first, longest first, so one containing
    # another is masked as a unit; then the 8-character windows.
    wholes = (w for w in windows if len(w) > 8 and not w.startswith(_BARE))
    for whole in sorted(wholes, key=len, reverse=True):
        text = text.replace(whole, MASKED_PASSWORD)
    out = []
    copied = 0
    start = end = -1
    for i in range(len(text) - 7):
        if text[i:i + 8] not in windows:
            continue
        if start >= 0 and i > end:
            out.extend((text[copied:start], MASKED_PASSWORD))
            copied = end
            start = -1
        if start < 0:
            start = i
        end = i + 8
    if start < 0:
        return text
    out.extend((text[copied:start], MASKED_PASSWORD, text[end:]))
    return "".join(out)


def redact_result(result, registries: list[set[str]]):
    """The only result scrubber, after SDK normalization of successes/errors."""
    windows = set().union(*registries)

    def walk(value):
        if isinstance(value, str):
            return redact_text(value, windows)
        if isinstance(value, BaseModel):
            return value.model_copy(update={
                key: item if key in ("data", "blob") else walk(item)
                for key, item in value.__dict__.items()})
        if isinstance(value, dict):
            return {walk(key): walk(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(walk(item) for item in value)
        return value

    return walk(result)


# eval_on_selector runs in the pinned engine's utility world, unlike evaluate.
# Only DOM reads cross the wire; no value, fragment or registry enters JavaScript.
PRESENCE_JS = """(element) => {
    const values = [];
    function read(root) {
        if (root.nodeType === 9) {
            if (!root.body) throw new Error('No readable body');
            values.push(root.body.innerText);
        }
        for (const el of root.querySelectorAll('*')) {
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') values.push(el.value);
            if (root.nodeType === 11 && typeof el.innerText === 'string') values.push(el.innerText);
            if (el.shadowRoot) read(el.shadowRoot);
            if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
                const doc = el.contentDocument;
                if (doc) {
                    read(doc);
                } else {
                    try {
                        void el.contentWindow.location.href;
                    } catch (error) {
                        if (error.name === 'SecurityError') continue;
                        throw error;
                    }
                    throw new Error('No readable same-origin frame');
                }
            }
        }
    }
    read(element.ownerDocument);
    return values;
}"""


async def guard_pixels(session) -> None:
    values = registry(session)
    if not values.raw:
        return
    try:
        # ⛔ EVERY FRAME, READ IN ITS OWN DOCUMENT. PRESENCE_JS descends into a
        # same-origin iframe from the parent, but a cross-origin one answers
        # SecurityError and is skipped - so a value masked inside Plaid Link's
        # iframe (cdn.plaid.com, inside app.squareup.com) was on the page and
        # the guard read nothing. The engine reaches each frame directly;
        # nested same-origin frames are then read twice, which only costs time.
        page = session.page()
        frames = list(getattr(page, "frames", None) or [page])
        answers = await asyncio.wait_for(asyncio.gather(
            *(frame.eval_on_selector("html", PRESENCE_JS) for frame in frames)), 5)
        fields = []
        for answer in answers:
            if not isinstance(answer, list):
                raise MaskRefusal(SCREENSHOT_REFUSED)
            fields.extend(answer)
        if not all(isinstance(v, str) for v in fields):
            raise MaskRefusal(SCREENSHOT_REFUSED)
        # The same matcher as the result boundary, so a value the page shows
        # reformatted (spaced card digits) is present here too.
        if any(redact_text(field, values.raw) != field for field in fields):
            raise MaskRefusal(SCREENSHOT_REFUSED)
    except Exception:
        # A failed presence read is never permission to capture, and its
        # diagnostics may themselves contain the value.
        raise MaskRefusal(SCREENSHOT_REFUSED) from None
