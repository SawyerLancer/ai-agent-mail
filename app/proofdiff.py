"""Что изменила вычитка: список «было → стало» по словам.

Считаем сами через difflib, а не спрашиваем модель: так список нельзя
приукрасить, и видно, если модель переписала больше, чем ошибки.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

REWRITE_SHARE = 0.3        # больше этой доли слов изменено — не вычитка
MIN_REWRITE_WORDS = 3

_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


@dataclass
class ProofDiff:
    changes: list[tuple[str, str]] = field(default_factory=list)   # (было, стало)
    changed_share: float = 0.0     # доля слов оригинала, которых коснулась правка
    touched_words: int = 0

    @property
    def looks_rewritten(self) -> bool:
        """Похоже на переписывание, а не на вычитку. Нужны оба условия: в
        коротком тексте одна опечатка — это уже треть слов."""
        return self.changed_share > REWRITE_SHARE and self.touched_words >= MIN_REWRITE_WORDS

    @property
    def unchanged(self) -> bool:
        return not self.changes


def compare(original: str, fixed: str) -> ProofDiff:
    a = list(_TOKEN.finditer(original))
    b = list(_TOKEN.finditer(fixed))
    sm = difflib.SequenceMatcher(a=[m.group(0) for m in a], b=[m.group(0) for m in b], autojunk=False)

    changes: list[tuple[str, str]] = []
    touched_words = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        touched_words += sum(1 for m in a[i1:i2] if m.group(0)[0].isalnum() or m.group(0)[0] == "_")
        # Голая запятая ничего не скажет — показываем её вместе со словом перед ней.
        if _only_punct(a[i1:i2]) and _only_punct(b[j1:j2]) and i1 > 0 and j1 > 0:
            i1, j1 = i1 - 1, j1 - 1
        changes.append((_span(original, a, i1, i2), _span(fixed, b, j1, j2)))

    words = sum(1 for m in a if m.group(0)[0].isalnum()) or 1
    return ProofDiff(changes=changes, changed_share=touched_words / words, touched_words=touched_words)


def _only_punct(tokens: list[re.Match[str]]) -> bool:
    return all(not t.group(0)[0].isalnum() for t in tokens)


def _span(text: str, tokens: list[re.Match[str]], i1: int, i2: int) -> str:
    if i1 >= i2:
        return ""
    return text[tokens[i1].start() : tokens[i2 - 1].end()]
