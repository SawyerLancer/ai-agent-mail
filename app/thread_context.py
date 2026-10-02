"""История переписки для черновика: какие прошлые письма показать модели.

Модуль чистый — без сети и базы: на входе записи, на выходе список писем
истории и готовый блок промпта. Сбор записей из БД — в handlers.

Источники и правила — skill thread-context.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field

from .promptsafe import attr, escape

log = logging.getLogger(__name__)

# Публичные почтовые домены: совпадение домена тут ничего не говорит о том,
# что это одна компания, — только точный адрес.
# Список неполный — дополняется PUBLIC_DOMAINS_EXTRA в .env.
PUBLIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "yandex.ua", "yandex.com.tr", "ya.ru",
    "mail.ru", "bk.ru", "list.ru", "inbox.ru", "internet.ru",
    "rambler.ru", "ro.ru", "lenta.ru", "autorambler.ru", "myrambler.ru",
    "ukr.net", "i.ua", "meta.ua",
    "outlook.com", "outlook.ru", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "me.com",
    "yahoo.com", "yahoo.co.uk", "ymail.com", "aol.com",
    "gmx.com", "gmx.de", "gmx.net", "web.de",
    "proton.me", "protonmail.com", "pm.me", "tutanota.com", "tuta.io",
    "zoho.com", "fastmail.com",
})

# Общие темы, по которым склеивать нельзя: у одного отправителя «Счёт» в марте
# и «Счёт» в апреле — разные истории.
STOP_SUBJECTS = frozenset({
    "без темы", "(без темы)", "вопрос", "документы", "документ", "счёт", "счет",
    "отчёт", "отчет", "информация", "привет", "добрый день", "запрос", "письмо",
    "договор", "напоминание", "no subject",
})
MIN_SUBJECT_CHARS = 5
MIN_TRUNCATED_CHARS = 200   # меньше — письмо не обрезаем, а выкидываем

_PREFIX = re.compile(r"^\s*(?:re|fwd?|отв|пересл|ответ)\s*(?:\[\d+\]|\(\d+\))?\s*:\s*", re.IGNORECASE)
_REPLY_PREFIX = re.compile(r"^\s*(?:re|отв|ответ)\s*(?:\[\d+\]|\(\d+\))?\s*:", re.IGNORECASE)

# Начало цитаты. Вживую (skill mail-mcp) видели шапку «…пишет:/wrote:» и строки
# «>»; остальное — распространённые форматы, вживую не проверены. Принцип:
# сомневаешься — не режь (лишняя цитата безобиднее отрезанного текста).
_QUOTE_HEADER = re.compile(r"(?im)^.{0,200}\b(?:пишет|написал\(а\)|написала?|wrote)\s*:\s*$")
# В шапке цитаты всегда есть дата, время или адрес — «Вот что бухгалтер пишет:» не шапка.
# Год сам по себе не признак («с 2019 года пишет:») — только время, полная
# дата цифрами, день рядом с месяцем (в любом порядке) или адрес.
_MONTH_WORD = r"(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-zа-яё]*\.?"
_DATE_HINT = re.compile(
    r"\b\d{1,2}:\d{2}\b|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b|"
    r"\b\d{1,2}\s+" + _MONTH_WORD + r"|\b" + _MONTH_WORD + r"\s+\d{1,2}\b",
    re.IGNORECASE,
)


def _hint(line: str) -> bool:
    return "@" in line or bool(_DATE_HINT.search(line))
_QUOTE_START = [
    re.compile(r"(?im)^-{2,}\s*(?:original message|исходное сообщение|пересылаемое сообщение|forwarded message)\b.*$"),
    re.compile(r"(?ims)^\s*(?:from|от)\s*:[^\n]*\n(?:[^\n]*\n){0,4}?\s*(?:sent|date|отправлено|дата)\s*:"),
]
_MIN_QUOTED_LINES = 2       # одиночная «>= 10 шт» — не цитата
_MAX_TAIL_LINES = 5         # после цитаты допустима только короткая подпись


@dataclass
class Incoming:
    """Входящее письмо из TrackedEmail — только то, что нужно для склейки."""
    pk: int
    message_id: str
    in_reply_to: str
    references: str
    sender: str
    sender_addr: str
    norm_subject: str
    subject: str
    body: str
    date: dt.datetime


@dataclass
class SentReply:
    """Наш отправленный ответ (Draft status=sent, kind=reply)."""
    email_pk: int
    body: str
    date: dt.datetime


@dataclass
class HistoryItem:
    direction: str            # in | out
    date: dt.datetime
    sender: str
    text: str


@dataclass
class History:
    items: list[HistoryItem] = field(default_factory=list)
    cut_current_quote: bool = False

    @property
    def count(self) -> int:
        return len(self.items)


# --- нормализация ---

def norm_subject(subject: str | None) -> str:
    """Тема без Re:/Fwd:/Fw:/Отв: (в том числе повторных), регистра и лишних пробелов."""
    s = subject or ""
    while True:
        new = _PREFIX.sub("", s, count=1)
        if new == s:
            break
        s = new
    return " ".join(s.split()).casefold().replace("ё", "е")


def address_of(sender: str | None) -> str:
    s = (sender or "").strip()
    if "<" in s and ">" in s:
        s = s[s.index("<") + 1 : s.index(">")]
    return s.strip().lower() if "@" in s else ""


def message_ids(*headers: str | None) -> list[str]:
    """Message-ID из In-Reply-To / References: «<a@b> <c@d>» → ['a@b', 'c@d']."""
    out: list[str] = []
    for h in headers:
        for raw in re.findall(r"<([^<>\s]+)>|(\S+@\S+)", h or ""):
            mid = (raw[0] or raw[1]).strip("<>")
            if mid and mid not in out:
                out.append(mid)
    return out


def split_quote(body: str | None) -> tuple[str, str]:
    """(собственный текст, цитата). Нет надёжных маркеров — цитата пустая."""
    text = body or ""
    starts = [m.start() for rx in _QUOTE_START for m in [rx.search(text)] if m]
    starts += [_header_start(text, m) for m in _QUOTE_HEADER.finditer(text) if _header_ok(text, m)]
    gt = _gt_block_start(text)
    if gt is not None:
        starts.append(gt)
    # Цитата с самого начала — это ответ снизу (bottom-posting), а не «нет своего
    # текста»: резать нечего, иначе потеряем ответ.
    starts = [i for i in starts if text[:i].strip()]
    if not starts:
        return text.strip(), ""
    cut = min(starts)
    return text[:cut].rstrip(), text[cut:].strip()


def _prev_line(text: str, m: re.Match[str]) -> tuple[int, str]:
    end = m.start() - 1                      # «\n» перед строкой шапки
    if end < 0:
        return -1, ""
    start = text.rfind("\n", 0, end) + 1
    return start, text[start:end]


def _wrapped(prev: str, line: str) -> bool:
    """Шапка перенесена на две строки (Gmail: «On Mon, Oct 2, 2026 at 2:05 PM John <»
    / «a@b.com> wrote:»): в строке с «wrote:» даты нет, в предыдущей — есть."""
    return (
        bool(prev.strip())
        and not prev.rstrip().endswith((".", "!", "?", ":"))
        and bool(_DATE_HINT.search(prev))
        and not _DATE_HINT.search(line)
    )


def _header_ok(text: str, m: re.Match[str]) -> bool:
    _, prev = _prev_line(text, m)
    return _hint(m.group(0)) or _wrapped(prev, m.group(0))


def _header_start(text: str, m: re.Match[str]) -> int:
    """Разрез — по первой строке шапки, даже если клиент перенёс её на две."""
    start, prev = _prev_line(text, m)
    return start if start >= 0 and _wrapped(prev, m.group(0)) else m.start()


def _gt_block_start(text: str) -> int | None:
    """Начало блока «>»-строк, который идёт до конца письма: внутри — только
    «>» и пустые строки, не меньше двух «>», после — до 5 строк подписи."""
    lines = text.split("\n")
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith(">"):
            i += 1
            continue
        j, quoted = i, 0
        while j < len(lines) and (lines[j].lstrip().startswith(">") or not lines[j].strip()):
            quoted += lines[j].lstrip().startswith(">")
            j += 1
        tail = [ln for ln in lines[j:] if ln.strip()]
        if (
            quoted >= _MIN_QUOTED_LINES
            and len(tail) <= _MAX_TAIL_LINES
            and not any(ln.lstrip().startswith(">") for ln in tail)
        ):
            return offsets[i]
        i = j
    return None


def looks_like_reply(subject: str | None, body: str | None) -> bool:
    return bool(_REPLY_PREFIX.match(subject or "")) or bool(split_quote(body)[1])


def subject_linkable(norm: str) -> bool:
    return len(norm) >= MIN_SUBJECT_CHARS and norm not in STOP_SUBJECTS


# --- сборка ---

def collect(
    current: Incoming,
    candidates: list[Incoming],
    sent: list[SentReply],
    *,
    subject_window_days: int,
    max_chars: int,
    max_messages: int,
    same_domain: bool = True,
    public_domains: frozenset[str] | set[str] = PUBLIC_DOMAINS,
) -> History:
    """Цепочка для текущего письма: заголовки → (запасной вариант) тема +
    точный адрес собеседника; плюс наши отправленные ответы; затем бюджет."""
    earlier = [c for c in candidates if c.pk != current.pk and c.date <= current.date]
    chain = _by_headers(current, earlier, same_domain=same_domain, public_domains=public_domains)
    if not chain:
        chain = _by_subject(current, earlier, subject_window_days)

    chain_pks = {c.pk for c in chain} | {current.pk}
    items = [
        HistoryItem("in", c.date, c.sender, split_quote(c.body)[0])
        for c in chain
    ]
    items += [HistoryItem("out", r.date, "мы", r.body) for r in sent if r.email_pk in chain_pks]
    items = [i for i in items if i.text.strip()]

    # Цитату текущего режем, только если его In-Reply-To — письмо из базы:
    # наши ответы мимо бота (телефон, веб) в базу не попадают, и цитата —
    # единственное, что закрывает такие дыры.
    reply_to = set(message_ids(current.in_reply_to))
    cut = bool(reply_to) and any(c.message_id in reply_to for c in chain)
    return History(items=fit_budget(items, max_chars=max_chars, max_messages=max_messages), cut_current_quote=cut)


def same_party(
    a: str,
    b: str,
    *,
    same_domain: bool = True,
    public_domains: frozenset[str] | set[str] = PUBLIC_DOMAINS,
) -> bool:
    """Тот же собеседник: точный адрес, либо (если same_domain) один домен,
    которого нет среди публичных. Адреса сравниваются без регистра."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    if not same_domain:
        return False
    da, db = a.rsplit("@", 1)[-1], b.rsplit("@", 1)[-1]
    return da == db and da not in public_domains


def _by_headers(
    current: Incoming,
    earlier: list[Incoming],
    *,
    same_domain: bool = True,
    public_domains: frozenset[str] | set[str] = PUBLIC_DOMAINS,
) -> list[Incoming]:
    """Транзитивно по In-Reply-To/References, но только через письма того же
    собеседника (same_party): иначе тот, кто однажды был в копии, подделав
    References, получил бы в черновик ответа ему нашу переписку с другим.
    Отсеянное письмо цепочку дальше не продолжает."""
    refs = {c.pk: set(message_ids(c.in_reply_to, c.references)) for c in earlier}
    ids = set(message_ids(current.in_reply_to, current.references))
    if current.message_id:
        ids.add(current.message_id)
    found: dict[int, Incoming] = {}
    rejected: set[int] = set()
    changed = True
    while changed:
        changed = False
        for c in earlier:
            if c.pk in found or c.pk in rejected:
                continue
            if not ((c.message_id and c.message_id in ids) or (refs[c.pk] & ids)):
                continue
            if not same_party(c.sender_addr, current.sender_addr,
                              same_domain=same_domain, public_domains=public_domains):
                rejected.add(c.pk)
                log.info("письмо %s связано заголовками, но от другого собеседника — не в истории", c.pk)
                continue
            found[c.pk] = c
            ids |= refs[c.pk]
            if c.message_id:
                ids.add(c.message_id)
            changed = True
    return list(found.values())


def _by_subject(current: Incoming, earlier: list[Incoming], window_days: int) -> list[Incoming]:
    if not looks_like_reply(current.subject, current.body):
        return []
    if not subject_linkable(current.norm_subject) or not current.sender_addr:
        return []
    border = current.date - dt.timedelta(days=window_days)
    return [
        c for c in earlier
        if c.norm_subject == current.norm_subject
        and c.sender_addr == current.sender_addr
        and c.date >= border
    ]


def fit_budget(items: list[HistoryItem], *, max_chars: int, max_messages: int) -> list[HistoryItem]:
    """От новых к старым: что не влезло — обрезаем, всё старше — выкидываем.
    Возвращаем в хронологическом порядке."""
    kept: list[HistoryItem] = []
    left = max_chars
    for item in sorted(items, key=lambda i: i.date, reverse=True):
        if len(kept) >= max_messages or left <= 0:
            break
        text = item.text
        if len(text) > left:
            if left < MIN_TRUNCATED_CHARS:
                break           # обрывок «a …[обрезано]» модели бесполезен
            text = text[:left].rstrip() + " …[обрезано]"
            left = 0
        else:
            left -= len(text)
        kept.append(HistoryItem(item.direction, item.date, item.sender, text))
    return sorted(kept, key=lambda i: i.date)


def render(history: History) -> str:
    """Блок промпта. Пустая история — пустая строка."""
    if not history.items:
        return ""
    blocks = []
    for i in history.items:
        direction = "наш ответ" if i.direction == "out" else "от собеседника"
        blocks.append(
            f'<письмо_истории направление="{direction}" дата="{i.date:%Y-%m-%d %H:%M}" '
            f'от="{attr(i.sender)}">\n{escape(i.text)}\n</письмо_истории>'
        )
    return (
        "<история_переписки>\n"
        "Предыдущие письма этой переписки, от старых к новым. История может быть "
        "неполной: часть ответов могла уйти мимо бота.\n"
        + "\n".join(blocks)
        + "\n</история_переписки>\n"
    )


def plain(history_block: str) -> str:
    """Текст писем истории без служебных тегов — источник для factcheck
    (даты в атрибутах фактами переписки не считаются)."""
    return _OWN_TAGS.sub(" ", history_block or "")


_OWN_TAGS = re.compile(r"<\s*/?\s*(?:письмо_истории|история_переписки)\b[^<>]*>", re.IGNORECASE)
