"""Text normalization for OCR output. Source-language text is preserved for display;
normalized forms are used only for comparison and deduplication."""

from __future__ import annotations

import difflib
import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_SPACES = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"(\.{2,}|…+|⋯+)\s*$")


def normalize(text: str | None) -> str:
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    value = _SPACES.sub(" ", value).strip()
    return value.casefold()


def normalize_name(text: str | None) -> str:
    # Names: drop all whitespace; OCR frequently inserts or drops spaces in CJK names.
    return normalize(text).replace(" ", "")


def is_truncated(text: str | None) -> bool:
    return bool(text) and bool(_ELLIPSIS.search(text.strip()))


def strip_ellipsis(text: str) -> str:
    return _ELLIPSIS.sub("", text).rstrip()


def similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


_DIGITS = re.compile(r"\d+")


def parse_badge(text: str | None) -> int | None:
    """Parse an unread badge numeral. ``99+`` style values return the numeric floor."""
    if not text:
        return None
    value = normalize(text).replace("o", "0").replace("l", "1").replace("i", "1")
    match = _DIGITS.search(value)
    if not match:
        return None
    number = int(match.group(0))
    return number if 0 < number < 10000 else None
