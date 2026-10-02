"""Проверка: не добавила ли модель фактов, которых не было в источниках.

Достаём регулярками суммы и числа, даты, время, e-mail и URL из ответа модели
и ищем их в источниках — тексте входящего письма и текстах пользователя.
Чего нет ни там, ни там — показываем человеку. Отправку это не блокирует:
решает человек.

Модуль чистый — без сети и базы, чтобы его можно было покрыть тестами.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
# «мая» и «марта» начинаются одинаково — длинные основы проверяем первыми.
_MONTH_RE = (
    r"(январ[яь]|феврал[яь]|марта?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|августа?|"
    r"сентябр[яь]|октябр[яь]|ноябр[яь]|декабр[яь])"
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()«»\"']+", re.IGNORECASE)
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# Без года месяц обязан быть двузначным: иначе «1.5 млн» превратится в 1 мая.
_DATE_NUM = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2}|\d{4})\b|\b(\d{1,2})[./](\d{2})\b(?![.,]\d)")
_DATE_TEXT = re.compile(
    r"\b(\d{1,2})(?:-?го)?\s+" + _MONTH_RE + r"(?:\s+(\d{4})(?:\s*г(?:ода|\.)?)?)?",
    re.IGNORECASE,
)
_TIME = re.compile(
    r"\b([01]?\d|2[0-3]):([0-5]\d)\b"
    r"|\b([01]?\d|2[0-3])\.([0-5]\d)\s*ч"
    r"|\bв\s+([01]?\d|2[0-3])\s*(?:ч\b|час)",
    re.IGNORECASE,
)
# Относительные даты: «завтра», «в среду», «до конца недели». Ключ — смысл,
# а не форма: «в пятницу» и «до пятницы» — одна и та же пятница.
_RELATIVE: list[tuple[str, re.Pattern[str]]] = [
    (key, re.compile(rx, re.IGNORECASE))
    for key, rx in [
        ("послезавтра", r"\bпослезавтра(?:шн\w*)?\b"),
        ("завтра", r"\bзавтра(?:шн\w*)?\b"),
        ("сегодня", r"\bсегодня(?:шн\w*)?\b"),
        ("понедельник", r"\bпонедельник\w*"),
        ("вторник", r"\bвторник\w*"),
        # «среди», «средства» — не среда: только падежные окончания
        ("среда", r"\bсред(?:а|ы|у|е|ой|ам|ами|ах)\b"),
        ("четверг", r"\bчетверг\w*"),
        ("пятница", r"\bпятниц\w*"),
        ("суббота", r"\bсуббот\w*"),
        ("воскресенье", r"\bвоскресень\w*"),
        ("конец недели", r"\b(?:до|к)\s+конц[ау]\s+(?:(?:этой|текущей)\s+)?недели\b"),
        ("конец месяца", r"\b(?:до|к)\s+конц[ау]\s+(?:(?:этого|текущего)\s+)?месяца\b"),
        ("эта неделя", r"\bна\s+(?:этой|текущей)\s+неделе\b|\bна\s+эту\s+неделю\b"),
        ("следующая неделя", r"\bна\s+(?:следующей|будущей)\s+неделе\b|\bна\s+(?:следующую|будущую)\s+неделю\b"),
    ]
]

_MULT = {"тыс": 1_000, "млн": 1_000_000, "млрд": 1_000_000_000}
_NUMBER = re.compile(
    r"(?<![\w.,])(\d{1,3}(?: \d{3})+|\d+)(?:[.,](\d+))?(?![\w])"
    r"(?:\s*(тыс|млн|млрд)\.?)?",
    re.IGNORECASE,
)
# «1. Пункт» и «2) пункт» в начале строки — нумерация, а не факт.
_LIST_MARKER = re.compile(r"(?m)^\s*\d{1,2}[.)]\s")

_SPACES = re.compile(r"[    ]")


@dataclass(frozen=True)
class Fact:
    kind: str                 # money | date | time | email | url | relative
    key: tuple                # нормализованное значение для сравнения
    surface: str              # как написано в тексте — для показа человеку


def _norm_text(text: str) -> str:
    return _SPACES.sub(" ", text or "")


def _mask(text: str, m: re.Match[str]) -> str:
    """Закрыть найденное пробелами, сохранив позиции остального текста."""
    return text[: m.start()] + " " * (m.end() - m.start()) + text[m.end():]


def _year(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    y = int(raw)
    return y + 2000 if y < 100 else y


def extract(text: str) -> list[Fact]:
    """Все факты из текста. Порядок важен: что нашли раньше — закрываем,
    чтобы дата 12.03.2026 не всплыла заодно числами 12, 03 и 2026."""
    t = _norm_text(text)
    facts: list[Fact] = []

    def take(pattern: re.Pattern[str], build) -> None:
        nonlocal t
        for m in list(pattern.finditer(t)):
            fact = build(m)
            if fact is not None:
                facts.append(fact)
                t = _mask(t, m)

    for key, pattern in _RELATIVE:
        take(pattern, lambda m, key=key: Fact("relative", (key,), m.group(0).strip()))
    take(_EMAIL, lambda m: Fact("email", (m.group(0).lower().rstrip("."),), m.group(0)))
    take(_URL, lambda m: Fact("url", (m.group(0).lower().rstrip(".,;:!?"),), m.group(0).rstrip(".,;:!?")))
    take(_DATE_ISO, lambda m: _date(m.group(3), m.group(2), m.group(1), m.group(0)))
    take(_DATE_TEXT, lambda m: _date(m.group(1), _month(m.group(2)), m.group(3), m.group(0)))
    take(_DATE_NUM, lambda m: (
        _date(m.group(1), m.group(2), m.group(3), m.group(0)) if m.group(1)
        else _date(m.group(4), m.group(5), None, m.group(0))
    ))
    take(_TIME, _time)
    t = _LIST_MARKER.sub(lambda m: " " * len(m.group(0)), t)
    take(_NUMBER, _money)
    return facts


def _month(word: str) -> int:
    w = word.lower()
    for stem, n in sorted(_MONTHS.items(), key=lambda kv: -len(kv[0])):
        if w.startswith(stem):
            return n
    return 0


def _date(day, month, year, surface: str) -> Optional[Fact]:
    d, mth = int(day), int(month)
    if not (1 <= d <= 31 and 1 <= mth <= 12):
        return None
    return Fact("date", (d, mth, _year(year)), surface.strip())


def _time(m: re.Match[str]) -> Fact:
    if m.group(1) is not None:
        h, mi = int(m.group(1)), int(m.group(2))
    elif m.group(3) is not None:
        h, mi = int(m.group(3)), int(m.group(4))
    else:
        h, mi = int(m.group(5)), 0
    return Fact("time", (h, mi), m.group(0).strip())


def _money(m: re.Match[str]) -> Optional[Fact]:
    whole = m.group(1).replace(" ", "")
    frac = m.group(2) or ""
    try:
        value = Decimal(f"{whole}.{frac}" if frac else whole)
    except InvalidOperation:
        return None
    mult = (m.group(3) or "").lower()
    if mult:
        value *= _MULT[mult]
    return Fact("money", (value.normalize(),), m.group(0).strip())


def added(output: str, sources: Iterable[str]) -> list[str]:
    """Факты из output, которых нет ни в одном источнике. Для показа — как
    написано в output, без повторов."""
    known: list[Fact] = []
    for src in sources:
        if src:
            known.extend(extract(src))

    known_keys = {(f.kind, f.key) for f in known if f.kind != "date"}
    known_dates = [f.key for f in known if f.kind == "date"]
    # «в 2026 году» в ответе, если в письме была дата 12.03.2026, — не новое
    known_years = {Decimal(y) for _, _, y in known_dates if y}

    result: list[str] = []
    for f in extract(output):
        if f.kind == "date":
            d, m, y = f.key
            ok = any(
                kd == d and km == m and (y is None or ky is None or ky == y)
                for kd, km, ky in known_dates
            )
        elif f.kind == "money":
            ok = (f.kind, f.key) in known_keys or f.key[0] in known_years
        else:
            ok = (f.kind, f.key) in known_keys
        if not ok and f.surface not in result:
            result.append(f.surface)
    return result


def classify(output: str, sources: Iterable[str], history: str = "") -> tuple[list[str], list[str]]:
    """(добавлено моделью, найдено только в прошлой переписке).

    Факт из истории — не выдумка, но срок или сумма могли с тех пор смениться,
    поэтому показываем его мягко («ℹ️»), отдельно от «⚠️».
    """
    sources = list(sources)
    not_in_current = added(output, sources)
    if not history:
        return not_in_current, []
    invented = added(output, sources + [history])
    from_history = [x for x in not_in_current if x not in invented]
    return invented, from_history
