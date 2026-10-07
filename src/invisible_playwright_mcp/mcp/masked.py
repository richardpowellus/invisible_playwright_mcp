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


class MaskedValues:
    def __init__(self) -> None:
        self.raw: set[str] = set()
        self.forms: set[str] = set()

    def register(self, value: str) -> None:
        if not value:
            return
        self.raw.update(_windows(value))
        for form in (value, html.escape(value, quote=True), html.escape(value, quote=False),
                     json.dumps(value, ensure_ascii=True)[1:-1],
                     json.dumps(value, ensure_ascii=False)[1:-1],
                     quote(value, safe=""), quote_plus(value)):
            self.forms.update(_windows(form))

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
        fields = await asyncio.wait_for(
            session.page().eval_on_selector("html", PRESENCE_JS), 5)
        if not isinstance(fields, list) or not all(isinstance(v, str) for v in fields):
            raise MaskRefusal(SCREENSHOT_REFUSED)
        if any(field[i:i + 8] in values.raw
               for field in fields for i in range(len(field) - 7)):
            raise MaskRefusal(SCREENSHOT_REFUSED)
    except Exception:
        # A failed presence read is never permission to capture, and its
        # diagnostics may themselves contain the value.
        raise MaskRefusal(SCREENSHOT_REFUSED) from None
