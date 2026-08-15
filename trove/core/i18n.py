"""Language detection and zh/en string selection.

Answers, trajectory labels, and prompts follow the user's language:
CJK presence → Chinese, otherwise English.
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[一-鿿]")


def detect_language(text: str) -> str:
    """Return "zh" when the text contains Chinese characters, else "en"."""
    return "zh" if _CJK_RE.search(text or "") else "en"


def L(lang: str, zh: str, en: str) -> str:
    """Pick the string matching the language."""
    return zh if lang == "zh" else en
