"""Обработка событий Пачки: нажатия кнопок и правки в тредах."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from . import cards, llm
from .config import settings
from .db import Draft, SessionLocal, TrackedEmail
from .mail import mail
from .pachca import pachca

log = logging.getLogger(__name__)


def user_allowed(user_id: int) -> bool:
    return not settings.allowed_user_ids or user_id in settings.allowed_user_ids


async def handle_button(event: dict[str, Any]) -> None:
    data = str(event.get("data") or "")
    user_id = int(event.get("user_id") or 0)
    message_id = int(event.get("message_id") or 0)
    chat_id = int(event.get("chat_id") or 0)

    if not user_allowed(user_id):
        await pachca.send_message(
            entity_id=chat_id, content="У вас нет доступа к управлению почтой."
        )
        return

    action, _, raw_id = data.rpartition(":")
    try:
        obj_id = int(raw_id)
    except ValueError:
        log.warning("непонятная кнопка: %s", data)
        return

    if action in _CARD_ACTIONS and message_id:
        _backfill_card_id(obj_id, message_id)

    if action == cards.BTN_REPLY:
        await _start_reply(obj_id, chat_id)
    elif action == cards.BTN_FORWARD:
        await _start_forward(obj_id, chat_id)
    elif action == cards.BTN_FULL:
        await _show_full(obj_id, chat_id)
    elif action == cards.BTN_ARCHIVE:
        await _archive(obj_id, chat_id, message_id)
    elif action == cards.BTN_DELETE:
        await _ask_delete(obj_id, chat_id)
    elif action == cards.BTN_DELETE_OK:
        await _do_delete(obj_id, chat_id, message_id)
    elif action == cards.BTN_SEND:
        await _send_draft(obj_id, chat_id, message_id)
    elif action == cards.BTN_REGEN:
        await _regen_draft(obj_id, chat_id, message_id)
    elif action == cards.BTN_CANCEL:
        await _cancel(obj_id, chat_id, message_id)
    else:
        log.warning("неизвестное действие: %s", action)


async def handle_message(event: dict[str, Any]) -> None:
    """Сообщение пользователя. В треде письма = правка черновика."""
    if event.get("entity_type") != "thread":
        return
    user_id = int(event.get("user_id") or 0)
    if not user_allowed(user_id):
        return

    thread_id = int(event.get("entity_id") or 0)   # entity_id треда = его id
    text = str(event.get("content") or "").strip()
    if not text:
        return

    with SessionLocal() as s:
        draft = s.scalar(
            select(Draft)
            .where(Draft.thread_id == thread_id, Draft.status == "editing")
            .order_by(Draft.id.desc())
        )
        if draft is None:
            return
        email = s.get(TrackedEmail, draft.email_pk)
        if email is None:
            return
        draft_id, kind = draft.id, draft.kind
        current, subject, sender = draft.body, email.subject, email.sender
        old_preview = draft.preview_message_id

    # Для пересылки первая реплика в треде — это адрес получателя.
    if kind == "forward" and _looks_like_email(text):
        with SessionLocal() as s:
            draft = s.get(Draft, draft_id)
            assert draft is not None
            draft.recipients = _clean_recipients(text)
            s.commit()
            new_text, buttons = cards.draft_card(draft), cards.draft_buttons(draft_id)
        if old_preview:
            await pachca.drop_buttons(old_preview, content="_Черновик обновлён ниже._")
        msg = await pachca.send_to_thread(thread_id, new_text, buttons)
        _remember_preview(draft_id, msg.get("id"))
        return

    try:
        revised = await llm.revise(
            current=current, instruction=text, sender=sender, subject=subject
        )
    except Exception:  # noqa: BLE001 - ошибку модели показываем человеку
        log.error("LLM не смог переписать черновик", exc_info=True)
        await pachca.send_to_thread(
            thread_id, "Не удалось переписать черновик — модель недоступна. Попробуйте ещё раз."
        )
        return

    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        assert draft is not None
        draft.body = revised
        s.commit()
        new_text, buttons = cards.draft_card(draft), cards.draft_buttons(draft_id)

    if old_preview:
        await pachca.drop_buttons(old_preview, content="_Черновик обновлён ниже._")
    msg = await pachca.send_to_thread(thread_id, new_text, buttons)
    _remember_preview(draft_id, msg.get("id"))


# --- действия по письму ---

async def _thread_for(email_pk: int) -> tuple[int, TrackedEmail]:
    """Найти или создать тред под карточкой письма. Возвращает id треда."""
    with SessionLocal() as s:
        email = s.get(TrackedEmail, email_pk)
        if email is None:
            raise LookupError(f"письмо {email_pk} не найдено")
        if email.thread_id:
            return email.thread_id, email
        if not email.pachca_message_id:
            raise LookupError(f"у письма {email_pk} нет карточки в Пачке")
        thread = await pachca.create_thread(email.pachca_message_id)
        email.thread_id = thread.get("id")
        email.thread_chat_id = thread.get("chat_id")
        s.commit()
        return int(email.thread_id), email


async def _start_reply(email_pk: int, chat_id: int) -> None:
    thread_id, email = await _thread_for(email_pk)
    await pachca.send_to_thread(thread_id, "Готовлю черновик ответа…")

    body = await _body_of(email)
    try:
        text = await llm.draft_reply(sender=email.sender, subject=email.subject, body=body)
    except Exception:  # noqa: BLE001
        log.error("LLM не смог составить черновик", exc_info=True)
        await pachca.send_to_thread(
            thread_id, "Модель недоступна — черновик не составлен. Нажмите «Ответить» снова."
        )
        return

    with SessionLocal() as s:
        draft = Draft(
            email_pk=email_pk, thread_id=thread_id, kind="reply", body=text
        )
        s.add(draft)
        s.commit()
        card, buttons = cards.draft_card(draft), cards.draft_buttons(draft.id)
        draft_id = draft.id

    msg = await pachca.send_to_thread(thread_id, card, buttons)
    _remember_preview(draft_id, msg.get("id"))


async def _start_forward(email_pk: int, chat_id: int) -> None:
    thread_id, email = await _thread_for(email_pk)
    with SessionLocal() as s:
        draft = Draft(
            email_pk=email_pk,
            thread_id=thread_id,
            kind="forward",
            body="(без комментария)",
        )
        s.add(draft)
        s.commit()
        draft_id = draft.id

    msg = await pachca.send_to_thread(
        thread_id,
        "Кому переслать письмо? Напишите адрес в этот тред "
        "(можно несколько через запятую). Следующим сообщением можно добавить "
        "комментарий к пересылке.",
        cards.draft_buttons(draft_id),
    )
    _remember_preview(draft_id, msg.get("id"))


async def _show_full(email_pk: int, chat_id: int) -> None:
    thread_id, email = await _thread_for(email_pk)
    body = await _body_of(email)
    if not body:
        await pachca.send_to_thread(thread_id, "Не удалось получить текст письма.")
        return
    # Сообщения Пачки не бесконечные — режем длинное тело на части.
    chunks = [body[i : i + 3500] for i in range(0, min(len(body), 14000), 3500)]
    for i, chunk in enumerate(chunks, 1):
        prefix = f"**Полный текст ({i}/{len(chunks)})**\n\n" if len(chunks) > 1 else ""
        await pachca.send_to_thread(thread_id, prefix + chunk)


async def _archive(email_pk: int, chat_id: int, message_id: int) -> None:
    with SessionLocal() as s:
        email = s.get(TrackedEmail, email_pk)
        if email is None:
            return
        uid, subject = email.uid, email.subject
    try:
        await mail.archive(uid)
    except Exception:  # noqa: BLE001
        log.error("не удалось заархивировать uid=%s", uid, exc_info=True)
        await pachca.send_message(entity_id=chat_id, content="Не удалось заархивировать письмо.")
        return
    await pachca.drop_buttons(
        message_id, content=f"📥 Письмо «{subject or '(без темы)'}» в архиве."
    )


async def _ask_delete(email_pk: int, chat_id: int) -> None:
    with SessionLocal() as s:
        email = s.get(TrackedEmail, email_pk)
        if email is None:
            return
        text, buttons = cards.delete_confirm(email)
    await pachca.send_message(entity_id=chat_id, content=text, buttons=buttons)


async def _do_delete(email_pk: int, chat_id: int, message_id: int) -> None:
    with SessionLocal() as s:
        email = s.get(TrackedEmail, email_pk)
        if email is None:
            return
        uid, subject, card_id = email.uid, email.subject, email.pachca_message_id
    try:
        await mail.delete(uid)
    except Exception:  # noqa: BLE001
        log.error("не удалось удалить uid=%s", uid, exc_info=True)
        await pachca.drop_buttons(message_id, content="Не удалось удалить письмо.")
        return
    await pachca.drop_buttons(message_id, content=f"🗑 Письмо «{subject}» перемещено в «Удалённые».")
    if card_id:
        await pachca.drop_buttons(card_id, content=f"🗑 _Удалено:_ {subject or '(без темы)'}")


# --- действия по черновику ---

async def _send_draft(draft_id: int, chat_id: int, message_id: int) -> None:
    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        if draft is None or draft.status != "editing":
            await pachca.drop_buttons(message_id, content="_Черновик уже неактуален._")
            return
        email = s.get(TrackedEmail, draft.email_pk)
        if email is None:
            return
        kind, body, recipients = draft.kind, draft.body, draft.recipients
        thread_id = draft.thread_id
        uid, subject, sender = email.uid, email.subject, email.sender
        rfc_id = email.rfc_message_id

    try:
        if kind == "forward":
            if not recipients:
                await pachca.send_to_thread(
                    thread_id, "Сначала укажите адрес получателя в этом треде."
                )
                return
            await mail.forward(uid=uid, to=_split(recipients), note=body)
            done = f"↪️ Переслано: {recipients}"
        else:
            to = _address_of(sender)
            if not to:
                await pachca.send_to_thread(
                    thread_id, f"Не разобрал адрес отправителя ({sender}). Отправка отменена."
                )
                return
            await mail.send(
                to=[to],
                subject=_re_subject(subject),
                body=body,
                in_reply_to=rfc_id or None,
                references=rfc_id or None,
            )
            done = f"📨 Ответ отправлен на {to}"
    except Exception as exc:  # noqa: BLE001 - причину показываем человеку
        log.error("отправка не удалась", exc_info=True)
        await pachca.send_to_thread(thread_id, f"Отправить не удалось: {exc}")
        return

    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        if draft is not None:
            draft.status = "sent"
            s.commit()
    await pachca.drop_buttons(message_id, content=f"{done}\n\n{body}")


async def _regen_draft(draft_id: int, chat_id: int, message_id: int) -> None:
    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        if draft is None or draft.status != "editing":
            return
        email = s.get(TrackedEmail, draft.email_pk)
        if email is None:
            return
        thread_id = draft.thread_id

    body = await _body_of(email)
    try:
        text = await llm.draft_reply(
            sender=email.sender,
            subject=email.subject,
            body=body,
            instruction="Предложи другой вариант: иная структура и формулировки.",
        )
    except Exception:  # noqa: BLE001
        log.error("LLM не смог перегенерировать черновик", exc_info=True)
        await pachca.send_to_thread(thread_id, "Модель недоступна, черновик оставлен как был.")
        return

    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        assert draft is not None
        draft.body = text
        s.commit()
        card, buttons = cards.draft_card(draft), cards.draft_buttons(draft_id)

    await pachca.drop_buttons(message_id, content="_Новый вариант ниже._")
    msg = await pachca.send_to_thread(thread_id, card, buttons)
    _remember_preview(draft_id, msg.get("id"))


async def _cancel(draft_id: int, chat_id: int, message_id: int) -> None:
    if draft_id:
        with SessionLocal() as s:
            draft = s.get(Draft, draft_id)
            if draft is not None:
                draft.status = "cancelled"
                s.commit()
    await pachca.drop_buttons(message_id, content="_Отменено._")


# --- вспомогательное ---

# Кнопки, которые живут на самой карточке письма (не на подтверждении удаления).
_CARD_ACTIONS = {
    cards.BTN_REPLY, cards.BTN_FORWARD, cards.BTN_FULL, cards.BTN_ARCHIVE, cards.BTN_DELETE,
}


def _backfill_card_id(email_pk: int, message_id: int) -> None:
    """Дописать id карточки, если поллер упал между отправкой и commit.

    Карточка тогда уже в чате, а запись без pachca_message_id: тред под ней
    не создать. Нажатая кнопка карточки сама говорит, где она.
    """
    with SessionLocal() as s:
        email = s.get(TrackedEmail, email_pk)
        if email is not None and not email.pachca_message_id:
            email.pachca_message_id = message_id
            s.commit()
            log.info("письму %s восстановлен id карточки %s", email_pk, message_id)

async def _body_of(email: TrackedEmail) -> str:
    try:
        content = await mail.get_body(email.uid)
        return str(content.get("body") or content.get("content") or "")
    except Exception:  # noqa: BLE001
        log.warning("не удалось получить тело письма uid=%s", email.uid, exc_info=True)
        return ""


def _remember_preview(draft_id: int, message_id: Any) -> None:
    if not message_id:
        return
    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        if draft is not None:
            draft.preview_message_id = int(message_id)
            s.commit()


def _looks_like_email(text: str) -> bool:
    first = text.split(",")[0].strip()
    return "@" in first and " " not in first.strip("<>")


def _clean_recipients(text: str) -> str:
    return ", ".join(_split(text))


def _split(text: str) -> list[str]:
    return [p.strip().strip("<>") for p in text.replace(";", ",").split(",") if p.strip()]


def _address_of(sender: str) -> str:
    """Вытащить адрес из строки вида 'Имя <a@b.ru>'."""
    if "<" in sender and ">" in sender:
        return sender[sender.index("<") + 1 : sender.index(">")].strip()
    sender = sender.strip()
    return sender if "@" in sender else ""


def _re_subject(subject: str) -> str:
    subject = subject or "(без темы)"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"
