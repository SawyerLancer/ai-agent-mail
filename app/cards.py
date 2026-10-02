"""Тексты сообщений и раскладка кнопок."""
from __future__ import annotations

import json

from .db import Draft, TrackedEmail
from .proofdiff import ProofDiff

# Схема data у кнопок компактная: лимит Пачки — 255 символов.
# mail:<действие>:<id письма>   draft:<действие>:<id черновика>
BTN_REPLY = "mail:reply"
BTN_FORWARD = "mail:fwd"
BTN_DELETE = "mail:del"
BTN_DELETE_OK = "mail:delok"
BTN_ARCHIVE = "mail:arch"
BTN_FULL = "mail:full"
BTN_SEND = "draft:send"
BTN_REGEN = "draft:regen"
BTN_CANCEL = "draft:cancel"
BTN_OWN = "draft:own"
BTN_RAW = "draft:raw"

_MAX_SHOWN_FIXES = 10


def email_card(email: TrackedEmail, preview: str, summary: str, date: str = "") -> str:
    lines = [
        f"**{email.subject or '(без темы)'}**",
        f"От: {email.sender}" + (f" · {date}" if date else ""),
    ]
    if summary:
        lines += ["", f"_{summary}_"]
    if preview:
        lines += ["", preview]
    return "\n".join(lines)


def email_buttons(pk: int) -> list[list[dict[str, str]]]:
    # Ряд делится поровну между своими кнопками, а подпись не переносится:
    # пять в ряду превращаются в «Уда…». Разбиваем по смыслу — работа с
    # письмом отдельно, удаление из ящика отдельно.
    return [
        [
            {"text": "✉️ Ответить", "data": f"{BTN_REPLY}:{pk}"},
            {"text": "↪️ Переслать", "data": f"{BTN_FORWARD}:{pk}"},
            {"text": "📄 Полностью", "data": f"{BTN_FULL}:{pk}"},
        ],
        [
            {"text": "📥 В архив", "data": f"{BTN_ARCHIVE}:{pk}"},
            {"text": "🗑 Удалить", "data": f"{BTN_DELETE}:{pk}"},
        ],
    ]


def draft_card(
    draft: Draft, *, proof: ProofDiff | None = None, proof_failed: bool = False
) -> str:
    if draft.kind == "forward":
        head = f"**Черновик пересылки** → {draft.recipients or '(получатель не указан)'}"
    elif proof is not None or proof_failed:
        head = "**Ваш текст**"
    else:
        head = "**Черновик ответа**"

    parts = [head, draft.body]
    if proof_failed:
        parts.append("_Проверить ошибки не удалось — модель недоступна. Текст как есть._")
    elif proof is not None:
        parts.append(_proof_note(proof))
    added = _added(draft)
    if added:
        parts.append("⚠️ Модель добавила: " + ", ".join(added))
    parts.append(
        "—\nНапишите в этот тред правку («короче», «добавь про сроки»), и я перепишу. "
        "«Свой текст» — пришлёте письмо целиком, исправлю только ошибки. "
        "Или нажмите «Отправить»."
    )
    return "\n\n".join(parts)


def _proof_note(proof: ProofDiff) -> str:
    if proof.unchanged:
        return "_Ошибок не нашёл._"
    lines = [f"Исправлено: {len(proof.changes)}"]
    for was, now in proof.changes[:_MAX_SHOWN_FIXES]:
        lines.append(f"• _{_plain(was) or '(пусто)'}_ → **{_plain(now) or '(убрано)'}**")
    if len(proof.changes) > _MAX_SHOWN_FIXES:
        lines.append(f"…и ещё {len(proof.changes) - _MAX_SHOWN_FIXES}")
    if proof.looks_rewritten:
        lines.append(
            "⚠️ Модель изменила больше, чем ошибки, — проверьте или нажмите «Без правок»."
        )
    return "\n".join(lines)


def _plain(fragment: str) -> str:
    """Убрать символы разметки, чтобы фрагмент не сломал жирный/курсив."""
    return fragment.replace("*", "").replace("_", "").replace("\n", " ").strip()


def _added(draft: Draft) -> list[str]:
    try:
        items = json.loads(draft.added_facts or "[]")
    except json.JSONDecodeError:
        return []
    return [str(x) for x in items] if isinstance(items, list) else []


def draft_buttons(draft_id: int, *, raw: bool = False) -> list[list[dict[str, str]]]:
    # Ряд делится поровну и не переносит подписи: не больше трёх в ряд.
    first = [{"text": "📨 Отправить", "data": f"{BTN_SEND}:{draft_id}"}]
    if raw:
        first.append({"text": "↩️ Без правок", "data": f"{BTN_RAW}:{draft_id}"})
    first.append({"text": "✍️ Свой текст", "data": f"{BTN_OWN}:{draft_id}"})
    return [
        first,
        [
            {"text": "🔄 Заново", "data": f"{BTN_REGEN}:{draft_id}"},
            {"text": "✖️ Отмена", "data": f"{BTN_CANCEL}:{draft_id}"},
        ],
    ]


def own_text_prompt(draft_id: int) -> tuple[str, list[list[dict[str, str]]]]:
    text = (
        "Пришлите текст письма целиком следующим сообщением. Исправлю только "
        "ошибки — орфографию, пунктуацию, опечатки; формулировки не трону, "
        "подпись не добавлю."
    )
    return text, [[{"text": "✖️ Отмена", "data": f"{BTN_CANCEL}:{draft_id}"}]]


def delete_confirm(email: TrackedEmail) -> tuple[str, list[list[dict[str, str]]]]:
    text = (
        f"Удалить письмо «{email.subject or '(без темы)'}» от {email.sender}?\n"
        "Письмо будет перемещено в «Удалённые» — оттуда его можно вернуть."
    )
    buttons = [[
        {"text": "🗑 Да, удалить", "data": f"{BTN_DELETE_OK}:{email.id}"},
        {"text": "Отмена", "data": f"{BTN_CANCEL}:0"},
    ]]
    return text, buttons
