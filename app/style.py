"""Память стиля: короткие правила о том, как владелец пишет письма.

Учимся только на текстах самого пользователя (правки в треде, «Свой текст»,
его исправления черновика ИИ). Тело входящего письма сюда не попадает —
иначе письмо могло бы «научить» бота чему угодно (skill email-actions-safety).

owner_id передаётся явно во все функции: профиль привязан к владельцу,
глобального «текущего профиля» нет.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import factcheck, llm, proofdiff
from .config import settings
from .db import SessionLocal, StyleRule

log = logging.getLogger(__name__)

MAX_RULE_CHARS = 120
_EDIT_FRAGMENT_CHARS = 80
_MAX_EDITS = 10

# Заглавные, которые не имена: вежливое «Вы» и его формы.
_ALLOWED_CAPS = {"Вы", "Вас", "Вам", "Ваш", "Ваша", "Ваше", "Ваши", "Вами", "Вашего", "Вашей", "Вашим"}
_CAP_WORD = re.compile(r"\b[А-ЯЁA-Z][а-яёa-z]+")


# --- фильтр: что нельзя хранить ---

def rejected_reason(text: str) -> str | None:
    """Почему правило нельзя сохранить; None — можно.

    Проверка кодом, а не только промптом: модель могла не послушаться,
    а в профиле не должно оказаться содержания переписки.
    """
    t = (text or "").strip()
    if not t:
        return "пустое правило"
    if len(t) > MAX_RULE_CHARS:
        return f"длиннее {MAX_RULE_CHARS} символов"
    if factcheck.extract(t):
        return "содержит сумму, дату, время, адрес или ссылку"
    if _has_name(t):
        return "похоже на имя — храним только форму обращения"
    return None


def _has_name(text: str) -> bool:
    for m in _CAP_WORD.finditer(text):
        before = text[: m.start()].rstrip()
        # начало предложения или цитаты — заглавная по правилам письма
        if not before or before[-1] in ".!?:;«\"(—-":
            continue
        if m.group(0) in _ALLOWED_CAPS:
            continue
        return True
    return False


def normalize(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").lower()).strip(" .;!")
    return t.replace("ё", "е")[:255]


# --- выборка ---

def active_rules(s: Session, *, owner_id: str, recipient: str) -> list[str]:
    """Активные правила для промпта: общие + для этого адресата."""
    rows = s.scalars(
        select(StyleRule)
        .where(StyleRule.owner_id == owner_id, StyleRule.status == "active")
        .where(
            (StyleRule.scope == "global")
            | ((StyleRule.scope == "recipient") & (StyleRule.recipient == recipient.lower()))
        )
        .order_by(StyleRule.scope, StyleRule.id)
    ).all()
    return [r.text if r.scope == "global" else f"(для этого адресата) {r.text}" for r in rows]


def listed_rules(s: Session, *, owner_id: str) -> list[StyleRule]:
    """Активные правила в том порядке, в каком их видит человек.

    Порядок детерминированный: «/стиль удали 3» должен попасть в то же
    правило, что было третьим в показанном списке.
    """
    return list(
        s.scalars(
            select(StyleRule)
            .where(StyleRule.owner_id == owner_id, StyleRule.status == "active")
            .order_by(StyleRule.scope, StyleRule.recipient, StyleRule.id)
        ).all()
    )


def _active_count(s: Session, owner_id: str, scope: str, recipient: str) -> int:
    q = select(func.count()).select_from(StyleRule).where(
        StyleRule.owner_id == owner_id, StyleRule.status == "active", StyleRule.scope == scope
    )
    if scope == "recipient":
        q = q.where(StyleRule.recipient == recipient)
    return int(s.scalar(q) or 0)


def _cap(scope: str) -> int:
    return settings.style_max_global if scope == "global" else settings.style_max_per_recipient


# --- изменение профиля ---

def _hit(s: Session, rule: StyleRule) -> None:
    rule.hits += 1
    if (
        rule.status == "candidate"
        and rule.hits >= settings.style_activate_hits
        and _active_count(s, rule.owner_id, rule.scope, rule.recipient) < _cap(rule.scope)
    ):
        rule.status = "active"


def apply_ops(
    s: Session, *, owner_id: str, recipient: str, ops: list[dict[str, Any]], known_ids: set[int]
) -> int:
    """Применить операции модели. Возвращает число изменений."""
    recipient = recipient.lower()
    changed = 0
    # Одно обучение — не больше одного подтверждения на правило: модель могла
    # повторить один и тот же hit или new, а порог активации — это число писем.
    confirmed: set[int] = set()
    for op in ops:
        if op.get("op") == "hit":
            rid = op.get("id")
            if isinstance(rid, int) and rid in known_ids and rid not in confirmed:
                rule = s.get(StyleRule, rid)
                if rule is not None and rule.owner_id == owner_id:
                    _hit(s, rule)
                    confirmed.add(rule.id)
                    changed += 1
        elif op.get("op") == "new":
            scope = "recipient" if op.get("scope") == "recipient" else "global"
            text = str(op.get("text") or "").strip()
            reason = rejected_reason(text)
            if reason:
                log.info("правило стиля отклонено (%s)", reason)
                continue
            rcpt = recipient if scope == "recipient" else ""
            if scope == "recipient" and not rcpt:
                continue
            norm = normalize(text)
            same = s.scalar(
                select(StyleRule).where(
                    StyleRule.owner_id == owner_id,
                    StyleRule.scope == scope,
                    StyleRule.recipient == rcpt,
                    StyleRule.norm_text == norm,
                )
            )
            if same is not None:
                if same.id in confirmed:
                    continue
                _hit(s, same)
                confirmed.add(same.id)
            else:
                rule = StyleRule(
                    owner_id=owner_id, scope=scope, recipient=rcpt, text=text,
                    norm_text=norm, hits=0, status="candidate",
                )
                s.add(rule)
                s.flush()
                _hit(s, rule)     # hits=1; при пороге 1 сразу станет active
                confirmed.add(rule.id)
            changed += 1
    s.flush()
    _prune(s, owner_id, "global", "")
    if recipient:
        _prune(s, owner_id, "recipient", recipient)
    return changed


def _prune(s: Session, owner_id: str, scope: str, recipient: str) -> None:
    """Кандидатов держим не больше двух лимитов: слабые и старые — вон."""
    candidates = s.scalars(
        select(StyleRule)
        .where(
            StyleRule.owner_id == owner_id,
            StyleRule.status == "candidate",
            StyleRule.scope == scope,
            StyleRule.recipient == recipient,
        )
        .order_by(StyleRule.hits.desc(), StyleRule.updated_at.desc())
    ).all()
    for extra in candidates[2 * _cap(scope):]:
        s.delete(extra)


def parse_ops(raw: str) -> list[dict[str, Any]]:
    """Достать JSON-массив из ответа модели; мусор — пустой список."""
    if not raw:
        return []
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def edits_between(first_ai_body: str, sent_body: str) -> list[tuple[str, str]]:
    """Как человек поправил черновик ИИ: короткие пары «было → стало»."""
    if not first_ai_body or first_ai_body == sent_body:
        return []
    pairs = proofdiff.compare(first_ai_body, sent_body).changes
    short = [
        (a[:_EDIT_FRAGMENT_CHARS], b[:_EDIT_FRAGMENT_CHARS]) for a, b in pairs if a or b
    ]
    return short[:_MAX_EDITS]


async def learn(
    *,
    owner_id: str,
    recipient: str,
    user_texts: list[str],
    first_ai_body: str,
    sent_body: str,
    human_final: bool,
) -> None:
    """Обучение после отправки. Фоновая задача: любые ошибки только в лог.

    human_final — итоговый текст написал человек («Свой текст», «Без правок»).
    Только тогда разница с черновиком ИИ — его стиль; после llm.revise это
    переформулировки модели, их не учим. Указания (user_texts) учим всегда.
    """
    try:
        edits = edits_between(first_ai_body, sent_body) if human_final else []
        if not user_texts and not edits:
            return          # человек ничего не писал сам — учиться не на чем
        with SessionLocal() as s:
            known = s.scalars(
                select(StyleRule).where(
                    StyleRule.owner_id == owner_id,
                    (StyleRule.scope == "global")
                    | (StyleRule.recipient == recipient.lower()),
                )
            ).all()
            existing = [(r.id, r.scope, r.text) for r in known]
        raw = await llm.extract_style(user_texts=user_texts, edits=edits, existing=existing)
        ops = parse_ops(raw)
        with SessionLocal() as s:
            n = apply_ops(
                s, owner_id=owner_id, recipient=recipient, ops=ops,
                known_ids={i for i, _, _ in existing},
            )
            s.commit()
        if n:
            log.info("память стиля: изменений %d", n)
    except Exception:  # noqa: BLE001 - обучение не должно мешать работе с почтой
        log.warning("не удалось обновить память стиля", exc_info=True)


# --- команды из чата ---

_CMD = re.compile(r"^/стиль\b(.*)$", re.IGNORECASE | re.DOTALL)
_CMD_DELETE = re.compile(r"^удали\s+(\d+)$", re.IGNORECASE)
_CMD_REMEMBER = re.compile(r"^запомни(?:\s+для\s+(\S+@\S+?))?\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)

HELP = (
    "Команды памяти стиля:\n"
    "/стиль — показать правила\n"
    "/стиль удали 3 — удалить правило №3\n"
    "/стиль запомни: обращаться на «вы» — добавить общее правило\n"
    "/стиль запомни для a@b.ru: на «ты» — правило для адресата"
)


def is_command(text: str) -> bool:
    return bool(_CMD.match((text or "").strip()))


def handle_command(text: str, *, owner_id: str) -> str:
    """Выполнить «/стиль …» и вернуть ответ для чата."""
    m = _CMD.match((text or "").strip())
    arg = (m.group(1) if m else "").strip()
    with SessionLocal() as s:
        if not arg:
            return _render(listed_rules(s, owner_id=owner_id), s, owner_id)

        dm = _CMD_DELETE.match(arg)
        if dm:
            rules = listed_rules(s, owner_id=owner_id)
            n = int(dm.group(1))
            if not 1 <= n <= len(rules):
                return f"Правила №{n} нет. Список — /стиль."
            rule = rules[n - 1]
            s.delete(rule)
            s.commit()
            return f"Удалил правило №{n}: {rule.text}"

        rm = _CMD_REMEMBER.match(arg)
        if rm:
            recipient = (rm.group(1) or "").strip("<>").lower()
            text = rm.group(2).strip()
            reason = rejected_reason(text)
            if reason:
                return f"Не запомнил: {reason}."
            scope = "recipient" if recipient else "global"
            if _active_count(s, owner_id, scope, recipient) >= _cap(scope):
                return (
                    f"Лимит правил ({_cap(scope)}) исчерпан — удалите ненужное: /стиль."
                )
            norm = normalize(text)
            rule = s.scalar(
                select(StyleRule).where(
                    StyleRule.owner_id == owner_id, StyleRule.scope == scope,
                    StyleRule.recipient == recipient, StyleRule.norm_text == norm,
                )
            )
            if rule is None:
                rule = StyleRule(
                    owner_id=owner_id, scope=scope, recipient=recipient,
                    text=text, norm_text=norm, hits=0,
                )
                s.add(rule)
            rule.status = "active"
            rule.hits = max(rule.hits, settings.style_activate_hits)
            s.commit()
            where = f" для {recipient}" if recipient else ""
            return f"Запомнил{where}: {text}"

    return HELP


def _render(rules: list[StyleRule], s: Session, owner_id: str) -> str:
    candidates = int(
        s.scalar(
            select(func.count()).select_from(StyleRule).where(
                StyleRule.owner_id == owner_id, StyleRule.status == "candidate"
            )
        )
        or 0
    )
    if not rules:
        head = "Активных правил стиля пока нет."
    else:
        lines = []
        for i, r in enumerate(rules, 1):
            where = "" if r.scope == "global" else f" _(для {r.recipient})_"
            lines.append(f"{i}. {r.text}{where}")
        head = "**Мой стиль**\n" + "\n".join(lines)
    tail = f"\n\nЕщё присматриваюсь: {candidates}." if candidates else ""
    return f"{head}{tail}\n\n{HELP}"
