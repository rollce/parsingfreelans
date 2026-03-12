from __future__ import annotations

import re

CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")
LATIN_RE = re.compile(r"[a-zA-Z]")


def detect_language(text: str) -> str:
    if not text.strip():
        return "unknown"
    has_ru = bool(CYRILLIC_RE.search(text))
    has_en = bool(LATIN_RE.search(text))
    if has_ru and not has_en:
        return "ru"
    if has_en and not has_ru:
        return "en"
    if has_ru and has_en:
        return "mixed"
    return "unknown"


def compact(text: str, limit: int = 900) -> str:
    s = " ".join(text.split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"
