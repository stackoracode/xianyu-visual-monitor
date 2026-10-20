"""Minimal JSON-catalog i18n. English is the default and the fallback language.

Only interface strings are translated; observed customer text (names, previews, message
bodies) is always shown exactly as captured in its source language.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
SUPPORTED = ("en", "zh")
DEFAULT = "en"


@cache
def catalog(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_lang(lang: str | None) -> str:
    if not lang:
        return DEFAULT
    lang = lang.lower().replace("_", "-")
    if lang.startswith("zh"):
        return "zh"
    if lang.startswith("en"):
        return "en"
    return DEFAULT


def translate(key: str, lang: str | None = DEFAULT, **params: object) -> str:
    lang = normalize_lang(lang)
    text = catalog(lang).get(key) or catalog(DEFAULT).get(key)
    if text is None:
        # Dynamic keys (e.g. "reason.screen_not_ready:blank") fall back to their raw code.
        return key.split(".", 1)[-1]
    if params:
        try:
            text = text.format(**params)
        except (KeyError, IndexError, ValueError):
            pass
    return text


class Translator:
    def __init__(self, lang: str | None):
        self.lang = normalize_lang(lang)

    def __call__(self, key: str, **params: object) -> str:
        return translate(key, self.lang, **params)
