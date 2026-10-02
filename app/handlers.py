"""Обработка событий Пачки: нажатия кнопок и правки в тредах."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from . import cards, factcheck, llm, proofdiff, style, thread_context
from .config import settings
from .db import Draft, SessionLocal, TrackedEmail
from .mail import MailToolError, forwarded_subject, mail
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
    elif action == cards.BTN_OWN:
        await _ask_own_text(obj_id, message_id)
    elif action == cards.BTN_RAW:
        await _use_raw(obj_id, message_id)
    elif action == cards.BTN_CANCEL:
        await _cancel(obj_id, chat_id, message_id)
    else:
        log.warning("неизвестное действие: %s", action)


async def handle_message(event: dict[str, Any]) -> None:
    """Сообщение пользователя. «/стиль …» — команда памяти стиля (в треде и
    в чате); любое другое сообщение в треде письма — правка черновика."""
    user_id = int(event.get("user_id") or 0)
    if not user_allowed(user_id):
        return
    text = str(event.get("content") or "").strip()
    if not text:
        return

    entity_type = str(event.get("entity_type") or "")
    entity_id = int(event.get("entity_id") or 0)
    if style.is_command(text):
        reply = style.handle_command(text, owner_id=_owner_id())
        if entity_type == "thread":
            await pachca.send_to_thread(entity_id, reply)
        else:
            await pachca.send_message(entity_id=entity_id, entity_type=entity_type or "discussion", content=reply)
        return

    if entity_type != "thread":
        return
    thread_id = entity_id   # entity_id треда = его id

    with SessionLocal() as s:
        draft = s.scalar(
            select(Draft)
            .where(Draft.thread_id == thread_id, Draft.status.in_(_OPEN))
            .order_by(Draft.id.desc())
        )
        if draft is None:
            return
        email = s.get(TrackedEmail, draft.email_pk)
        if email is None:
            return
        draft_id, kind, status = draft.id, draft.kind, draft.status
        current, subject, sender = draft.body, email.subject, email.sender
        history = draft.history_text
        old_preview = draft.preview_message_id

    # «Свой текст»: модель исправляет только ошибки, без стиля и подписи.
    if status == "awaiting_text":
        await _proofread_own(draft_id, thread_id, str(event.get("content") or "").strip("\n"), old_preview)
        return

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

    recipient = _address_of(sender) if kind == "reply" else ""
    try:
        revised = await llm.revise(
            current=current, instruction=text, sender=sender, subject=subject,
            style_rules=_style_rules(recipient), history=history,
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
        _add_user_text(draft, text)
        _set_body(s, draft, revised)
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
    history, history_count, prompt_body = await _history_for(email, body)
    try:
        text = await llm.draft_reply(
            sender=email.sender, subject=email.subject, body=prompt_body,
            style_rules=_style_rules(_address_of(email.sender)), history=history,
        )
    except Exception:  # noqa: BLE001
        log.error("LLM не смог составить черновик", exc_info=True)
        await pachca.send_to_thread(
            thread_id, "Модель недоступна — черновик не составлен. Нажмите «Ответить» снова."
        )
        return

    with SessionLocal() as s:
        draft = Draft(
            email_pk=email_pk, thread_id=thread_id, kind="reply",
            source_text=body[: settings.body_chars_for_llm], first_ai_body=text,
            history_text=history, history_count=history_count,
        )
        s.add(draft)
        _set_body(s, draft, text)
        s.commit()
        card, buttons = cards.draft_card(draft), cards.draft_buttons(draft.id)
        draft_id = draft.id

    msg = await pachca.send_to_thread(thread_id, card, buttons)
    _remember_preview(draft_id, msg.get("id"))


async def _start_forward(email_pk: int, chat_id: int) -> None:
    thread_id, email = await _thread_for(email_pk)
    # Как у ответа: источник для проверки, не добавила ли модель фактов в комментарий.
    body = await _body_of(email)
    with SessionLocal() as s:
        draft = Draft(
            email_pk=email_pk,
            thread_id=thread_id,
            kind="forward",
            body="(без комментария)",
            source_text=body[: settings.body_chars_for_llm],
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
        if draft is not None and draft.status == "sending":
            await pachca.send_to_thread(draft.thread_id, "Уже отправляю — подождите.")
            return
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

    if kind == "forward":
        if not recipients:
            await pachca.send_to_thread(thread_id, "Сначала укажите адрес получателя в этом треде.")
            return
        to_list = _split(recipients)
        done = f"↪️ Переслано: {recipients}"
    else:
        to = _address_of(sender)
        if not to:
            await pachca.send_to_thread(
                thread_id, f"Не разобрал адрес отправителя ({sender}). Отправка отменена."
            )
            return
        to_list = [to]
        done = f"📨 Ответ отправлен на {to}"

    # editing → sending одной условной записью: второе нажатие (или повторная
    # доставка события) сюда уже не пройдёт.
    with SessionLocal() as s:
        taken = s.execute(
            update(Draft)
            .where(Draft.id == draft_id, Draft.status == "editing")
            .values(status="sending")
        ).rowcount
        s.commit()
    if not taken:
        await pachca.send_to_thread(thread_id, "Уже отправляю — подождите.")
        return
    await pachca.drop_buttons(message_id, content=f"📨 Отправляю…\n\n{body}")

    started = dt.datetime.now(dt.timezone.utc) - _CLOCK_SKEW
    reply_subject = _re_subject(subject)
    sent_subject = forwarded_subject(subject or "") if kind == "forward" else reply_subject
    try:
        if kind == "forward":
            await mail.forward(uid=uid, to=to_list, note=body)
        else:
            await mail.send(
                to=to_list,
                subject=reply_subject,
                body=body,
                in_reply_to=rfc_id or None,
                references=rfc_id or None,
            )
    except MailToolError as exc:
        # Сервер сам сказал «нет» — письмо точно не ушло.
        log.error("отправка не удалась", exc_info=True)
        await _back_to_editing(draft_id, f"Отправить не удалось: {exc}")
        return
    except Exception:  # noqa: BLE001 - таймаут или обрыв: результат неизвестен
        log.error("отправка без ответа сервера, проверяю «Отправленные»", exc_info=True)
        if not await _found_in_sent(to=to_list[0], since=started, subject=sent_subject):
            await _back_to_editing(draft_id, NOT_SURE)
            return
        log.info("письмо нашлось в «Отправленных», считаю отправленным")

    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        assert draft is not None
        draft.status = "sent"
        draft.sent_at = dt.datetime.now(dt.timezone.utc)
        s.commit()
        user_texts, first_ai = _user_texts(draft), draft.first_ai_body
        human_final = draft.body_source == "human"
    await pachca.drop_buttons(message_id, content=f"{done}\n\n{body}")

    # Память стиля — в фоне: ответ в чате не ждёт модель, ошибка не мешает.
    # Пересылки не учим: комментарий к ним слишком короткий.
    if kind == "reply":
        _background(style.learn(
            owner_id=_owner_id(), recipient=_address_of(sender),
            user_texts=user_texts, first_ai_body=first_ai, sent_body=body,
            human_final=human_final,
        ))


async def recover_sending() -> None:
    """После рестарта: черновики, застрявшие в sending, — бот упал посреди
    отправки. Ищем письмо в «Отправленных»; нет — возвращаем к правке."""
    with SessionLocal() as s:
        stuck = [
            (d.id, d.kind, d.recipients, d.body, d.preview_message_id, d.updated_at, e.subject, e.sender)
            for d, e in s.execute(
                select(Draft, TrackedEmail)
                .join(TrackedEmail, TrackedEmail.id == Draft.email_pk)
                .where(Draft.status == "sending")
            ).all()
        ]
    for draft_id, kind, recipients, body, preview, updated_at, subject, sender in stuck:
        since = _aware(updated_at) - _CLOCK_SKEW
        if kind == "forward":
            to, sent_subject = (_split(recipients) or [""])[0], forwarded_subject(subject or "")
            done = f"↪️ Переслано: {recipients}"
        else:
            to, sent_subject = _address_of(sender), _re_subject(subject)
            done = f"📨 Ответ отправлен на {to}"
        try:
            found = bool(to) and await _found_in_sent(to=to, since=since, subject=sent_subject)
            if found:
                with SessionLocal() as s:
                    d = s.get(Draft, draft_id)
                    if d is not None:
                        d.status = "sent"
                        # точного момента не знаем — начало отправки ближе всего
                        d.sent_at = _aware(updated_at)
                        s.commit()
                if preview:
                    await pachca.drop_buttons(preview, content=f"{done}\n\n{body}")
            else:
                await _back_to_editing(draft_id, NOT_SURE)
            log.info("черновик %s после рестарта: %s", draft_id, "sent" if found else "editing")
        except Exception:  # noqa: BLE001 - один черновик не должен мешать остальным
            log.error("не удалось восстановить черновик %s", draft_id, exc_info=True)


def _aware(value: dt.datetime) -> dt.datetime:
    """SQLite отдаёт время без зоны; пишем мы его в UTC."""
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


async def _found_in_sent(*, to: str, since: dt.datetime, subject: str) -> bool:
    try:
        return await mail.find_sent(to=to, since=since, subject=subject)
    except Exception:  # noqa: BLE001 - не смогли проверить = не уверены
        log.warning("не удалось проверить «Отправленные»", exc_info=True)
        return False


async def _back_to_editing(draft_id: int, note: str) -> None:
    """Вернуть черновик к правке и показать его заново с кнопками."""
    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        assert draft is not None
        draft.status = "editing"
        s.commit()
        thread_id = draft.thread_id
        raw = bool(draft.original_text) and draft.original_text != draft.body
        card, buttons = cards.draft_card(draft), cards.draft_buttons(draft_id, raw=raw)
    await pachca.send_to_thread(thread_id, note)
    msg = await pachca.send_to_thread(thread_id, card, buttons)
    _remember_preview(draft_id, msg.get("id"))


async def _regen_draft(draft_id: int, chat_id: int, message_id: int) -> None:
    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        if draft is None or draft.status != "editing":
            return
        email = s.get(TrackedEmail, draft.email_pk)
        if email is None:
            return
        thread_id, source = draft.thread_id, draft.source_text

    # Тело уже сохранено при «Ответить»; заново из ящика — только у старых черновиков.
    body = source or await _body_of(email)
    history, history_count, prompt_body = await _history_for(email, body)
    try:
        text = await llm.draft_reply(
            sender=email.sender,
            subject=email.subject,
            body=prompt_body,
            instruction="Предложи другой вариант: иная структура и формулировки.",
            style_rules=_style_rules(_address_of(email.sender)),
            history=history,
        )
    except Exception:  # noqa: BLE001
        log.error("LLM не смог перегенерировать черновик", exc_info=True)
        await pachca.send_to_thread(thread_id, "Модель недоступна, черновик оставлен как был.")
        return

    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        assert draft is not None
        if not draft.source_text:
            draft.source_text = body[: settings.body_chars_for_llm]
        draft.first_ai_body = text      # новая точка отсчёта для правок человека
        draft.history_text, draft.history_count = history, history_count
        _set_body(s, draft, text)
        s.commit()
        card, buttons = cards.draft_card(draft), cards.draft_buttons(draft_id)

    await pachca.drop_buttons(message_id, content="_Новый вариант ниже._")
    msg = await pachca.send_to_thread(thread_id, card, buttons)
    _remember_preview(draft_id, msg.get("id"))


async def _ask_own_text(draft_id: int, message_id: int) -> None:
    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        if draft is None or draft.status != "editing":
            await pachca.drop_buttons(message_id, content="_Черновик уже неактуален._")
            return
        draft.status = "awaiting_text"
        s.commit()
        thread_id = draft.thread_id

    text, buttons = cards.own_text_prompt(draft_id)
    await pachca.drop_buttons(message_id, content="_Жду ваш текст ниже._")
    msg = await pachca.send_to_thread(thread_id, text, buttons)
    _remember_preview(draft_id, msg.get("id"))


async def _proofread_own(draft_id: int, thread_id: int, original: str, old_preview: int | None) -> None:
    """«Свой текст» → вычитка. Профиль стиля и подпись не применяются."""
    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        assert draft is not None
        draft.original_text = original
        _add_user_text(draft, original)
        s.commit()
    if old_preview:
        await pachca.drop_buttons(old_preview, content="_Текст получил, проверяю ошибки…_")

    failed = False
    try:
        fixed = await llm.proofread(original)
    except Exception:  # noqa: BLE001 - без вычитки текст всё равно можно отправить
        log.error("LLM не смог вычитать текст", exc_info=True)
        fixed, failed = original, True

    diff = proofdiff.compare(original, fixed)
    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        assert draft is not None
        _set_body(s, draft, fixed, source="human")   # вычитка: слова — человека
        draft.original_text = original    # _set_body сбрасывает — здесь он нужен
        draft.status = "editing"
        s.commit()
        card = cards.draft_card(draft, proof=None if failed else diff, proof_failed=failed)
        buttons = cards.draft_buttons(draft_id, raw=fixed != original)

    msg = await pachca.send_to_thread(thread_id, card, buttons)
    _remember_preview(draft_id, msg.get("id"))


async def _use_raw(draft_id: int, message_id: int) -> None:
    """«Без правок»: вернуть дословный текст пользователя."""
    with SessionLocal() as s:
        draft = s.get(Draft, draft_id)
        if draft is None or draft.status != "editing" or not draft.original_text:
            await pachca.drop_buttons(message_id, content="_Черновик уже неактуален._")
            return
        original = draft.original_text
        _set_body(s, draft, original, source="human")
        draft.original_text = original
        s.commit()
        thread_id = draft.thread_id
        card = cards.draft_card(draft)

    await pachca.drop_buttons(message_id, content="_Вернул ваш текст без правок — ниже._")
    msg = await pachca.send_to_thread(thread_id, card, cards.draft_buttons(draft_id))
    _remember_preview(draft_id, msg.get("id"))


def purge_old_bodies(now: dt.datetime | None = None) -> int:
    """Срок хранения: тела писем и история закрытых черновиков старше
    BODY_RETENTION_DAYS — стираем (skill email-actions-safety)."""
    border = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=settings.body_retention_days)
    with SessionLocal() as s:
        n = s.execute(
            update(TrackedEmail)
            .where(TrackedEmail.created_at < border, TrackedEmail.body_text != "")
            .values(body_text="")
        ).rowcount
        n += s.execute(
            update(Draft)
            .where(
                Draft.status.not_in(_EXPIRABLE),
                Draft.updated_at < border,
                (Draft.body != "") | (Draft.source_text != "") | (Draft.history_text != "")
                | (Draft.user_texts != "[]") | (Draft.first_ai_body != ""),
            )
            .values(
                body="", first_ai_body="", source_text="", history_text="",
                user_texts="[]", original_text="", added_facts="[]", history_facts="[]",
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        s.commit()
    return n


async def expire_drafts() -> int:
    """Погасить черновики, которые не трогали дольше DRAFT_TTL_HOURS.

    Иначе старая кнопка «Отправить» живёт вечно, а строки копятся в базе.
    """
    border = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=settings.draft_ttl_hours)
    with SessionLocal() as s:
        stale = s.scalars(
            select(Draft).where(Draft.status.in_(_EXPIRABLE), Draft.updated_at < border)
        ).all()
        previews = [(d.preview_message_id, d.status) for d in stale if d.preview_message_id]
        for d in stale:
            d.status = "expired"
        s.commit()
    for message_id, was in previews:
        # Застрявший sending — не «устарел»: письмо могло уйти.
        text = NOT_SURE if was == "sending" else "_Черновик устарел — нажмите «Ответить» заново._"
        await pachca.drop_buttons(message_id, content=text)
    return len(stale)


async def _cancel(draft_id: int, chat_id: int, message_id: int) -> None:
    if draft_id:
        with SessionLocal() as s:
            draft = s.get(Draft, draft_id)
            if draft is not None:
                draft.status = "cancelled"
                s.commit()
    await pachca.drop_buttons(message_id, content="_Отменено._")


# --- вспомогательное ---

# Черновики, с которыми ещё работают: их ищут правки и гасит срок жизни.
_OPEN = ("editing", "awaiting_text")
_CLOCK_SKEW = dt.timedelta(minutes=2)   # запас на расхождение часов с почтовым сервером
NOT_SURE = (
    "Не уверен, ушло ли письмо — проверьте «Отправленные». "
    "Если его там нет, отправьте ещё раз."
)
# «sending» навсегда остаётся, только если бот упал посреди отправки — гасим сроком.
_EXPIRABLE = (*_OPEN, "sending")

_bg_tasks: set[asyncio.Task[None]] = set()   # держим ссылки, иначе GC снимет задачу


def _background(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _owner_id() -> str:
    """Владелец профиля стиля. Пока бот у одного человека — адрес ящика."""
    return (settings.owner_email or settings.email_account).strip().lower()


def _style_rules(recipient: str) -> list[str]:
    try:
        with SessionLocal() as s:
            return style.active_rules(s, owner_id=_owner_id(), recipient=recipient)
    except Exception:  # noqa: BLE001 - без стиля черновик всё равно нужен
        log.warning("не удалось прочитать профиль стиля", exc_info=True)
        return []


def _user_texts(draft: Draft) -> list[str]:
    try:
        items = json.loads(draft.user_texts or "[]")
    except json.JSONDecodeError:
        return []
    return [str(x) for x in items] if isinstance(items, list) else []


def _add_user_text(draft: Draft, text: str) -> None:
    draft.user_texts = json.dumps(_user_texts(draft) + [text], ensure_ascii=False)


def _set_body(s: Session, draft: Draft, body: str, *, source: str = "ai") -> None:
    """Новое тело черновика + проверка, не добавила ли модель фактов.

    Источники — письмо и тексты пользователя, а не прошлый черновик: иначе
    выдуманная однажды сумма после правки перестала бы помечаться.
    """
    draft.body = body
    draft.body_source = source
    draft.original_text = ""      # «Без правок» имеет смысл только сразу после вычитки
    email = s.get(TrackedEmail, draft.email_pk)
    sources = [draft.source_text, *_user_texts(draft), settings.signature]
    if email is not None:
        sources += [email.subject, email.sender]
    invented, from_history = factcheck.classify(
        body, sources, thread_context.plain(draft.history_text)
    )
    draft.added_facts = json.dumps(invented, ensure_ascii=False)
    draft.history_facts = json.dumps(from_history, ensure_ascii=False)


async def _history_for(email: TrackedEmail, body: str) -> tuple[str, int, str]:
    """(блок истории для промпта, сколько писем учтено, тело для промпта).

    Синхронная работа с БД — в отдельном потоке, чтобы не держать цикл событий.
    Ошибка сбора — не повод остаться без черновика: пишем как раньше, без истории.
    """
    try:
        return await asyncio.to_thread(_collect_history, email, body)
    except Exception:  # noqa: BLE001
        log.warning("не удалось собрать историю переписки", exc_info=True)
        return "", 0, body


# Сколько раз дочитывать письма по новым Message-ID из найденных (транзитивность).
_HISTORY_REF_ROUNDS = 3
_HISTORY_MAX_CANDIDATES = 200


def _collect_history(email: TrackedEmail, body: str) -> tuple[str, int, str]:
    current = _incoming(email, body)
    days = max(settings.history_subject_window_days, settings.body_retention_days)
    border = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    base = select(TrackedEmail).where(
        TrackedEmail.account == email.account,     # только этот ящик
        TrackedEmail.id != email.id,
        TrackedEmail.body_text != "",
        TrackedEmail.created_at >= border,
    )
    with SessionLocal() as s:
        # Не весь ящик: та же тема, тот же собеседник или письма из ссылок.
        ids = set(thread_context.message_ids(email.in_reply_to, email.references))
        found: dict[int, TrackedEmail] = {}
        conds = [TrackedEmail.norm_subject == current.norm_subject] if current.norm_subject else []
        if current.sender_addr:
            conds.append(TrackedEmail.sender_addr == current.sender_addr)
        if conds:
            for row in s.scalars(base.where(or_(*conds)).limit(_HISTORY_MAX_CANDIDATES)):
                found[row.id] = row
        seen_ids: set[str] = set()
        for _ in range(_HISTORY_REF_ROUNDS):
            new = ids - seen_ids
            if not new:
                break
            seen_ids |= new
            wanted = {f"<{i}>" for i in new} | new
            for row in s.scalars(base.where(TrackedEmail.rfc_message_id.in_(wanted)).limit(_HISTORY_MAX_CANDIDATES)):
                if row.id not in found:
                    found[row.id] = row
                    ids |= set(thread_context.message_ids(row.in_reply_to, row.references))
        candidates = [_incoming(r, r.body_text) for r in found.values()]
        chain_pks = [c.pk for c in candidates] + [email.id]
        sent = [
            thread_context.SentReply(d.email_pk, d.body, _aware(d.sent_at or d.updated_at))
            for d in s.scalars(
                select(Draft).where(
                    Draft.status == "sent", Draft.kind == "reply",
                    Draft.email_pk.in_(chain_pks), Draft.body != "",
                )
            )
        ]
    history = thread_context.collect(
        current, candidates, sent,
        subject_window_days=settings.history_subject_window_days,
        max_chars=settings.history_chars,
        max_messages=settings.history_max_messages,
    )
    prompt_body = thread_context.split_quote(body)[0] if history.cut_current_quote else body
    return thread_context.render(history), history.count, prompt_body


def _incoming(row: TrackedEmail, body: str) -> thread_context.Incoming:
    return thread_context.Incoming(
        pk=row.id,
        message_id=(row.rfc_message_id or "").strip("<>"),
        in_reply_to=row.in_reply_to or "",
        references=row.references or "",
        sender=row.sender or "",
        sender_addr=row.sender_addr or thread_context.address_of(row.sender),
        norm_subject=row.norm_subject or thread_context.norm_subject(row.subject),
        subject=row.subject or "",
        body=body or "",
        date=_aware(row.mail_date or row.created_at),
    )

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
