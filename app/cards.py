"""Тексты сообщений и раскладка кнопок."""
from __future__ import annotations

from .db import Draft, TrackedEmail

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


def draft_card(draft: Draft) -> str:
    if draft.kind == "forward":
        head = f"**Черновик пересылки** → {draft.recipients or '(получатель не указан)'}"
    else:
        head = "**Черновик ответа**"
    return (
        f"{head}\n\n{draft.body}\n\n"
        "—\nНапишите в этот тред правку («короче», «добавь про сроки»), "
        "и я перепишу. Или нажмите «Отправить»."
    )


def draft_buttons(draft_id: int) -> list[list[dict[str, str]]]:
    return [[
        {"text": "📨 Отправить", "data": f"{BTN_SEND}:{draft_id}"},
        {"text": "🔄 Заново", "data": f"{BTN_REGEN}:{draft_id}"},
        {"text": "✖️ Отмена", "data": f"{BTN_CANCEL}:{draft_id}"},
    ]]


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
