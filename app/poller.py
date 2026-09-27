"""Поллинг почты: раз в N секунд спрашиваем MCP про новые письма."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import cards
from .config import settings
from .db import SessionLocal, TrackedEmail, get_poll_state
from .llm import summarize
from .mail import mail
from .pachca import pachca

log = logging.getLogger(__name__)

_lock = asyncio.Lock()   # страховка от наложения циклов, если IMAP подвис


async def poll_once() -> int:
    """Один проход. Возвращает число новых писем, отправленных в чат."""
    if _lock.locked():
        log.warning("предыдущий проход ещё идёт, пропускаю")
        return 0

    async with _lock:
        with SessionLocal() as s:
            state = get_poll_state(s, settings.email_account, settings.mailbox)
            last_uid = state.last_uid
            first_run = last_uid == 0

        try:
            fresh, top_uid = await mail.list_new(since_uid=last_uid)
        except Exception:  # noqa: BLE001 - сеть/IMAP отвалились, попробуем в следующий раз
            log.error("не удалось получить список писем", exc_info=True)
            return 0

        if not fresh:
            # UID меньше сохранённого = папку пересоздали (сменился UIDVALIDITY).
            # Сдвигаем точку отсчёта на текущий максимум, ничего не рассылая.
            if top_uid is not None and top_uid < last_uid:
                log.warning(
                    "UID обнулились (было %s, стало %s): папку пересоздали, сбрасываю точку",
                    last_uid, top_uid,
                )
                _reset_uid(top_uid)
            return 0

        # Первый запуск: не заливаем канал историей, просто фиксируем точку отсчёта.
        if first_run:
            top = max(x["_uid"] for x in fresh)
            _save_uid(top)
            log.info("первый запуск: базовая точка UID=%s, %d писем пропущено", top, len(fresh))
            return 0

        sent = 0
        for meta in fresh:
            try:
                if await _publish(meta):
                    sent += 1
            except Exception:  # noqa: BLE001 - одно плохое письмо не должно ломать проход
                log.error("ошибка при публикации письма uid=%s", meta.get("_uid"), exc_info=True)
                break   # UID не двигаем — повторим на следующем проходе
            _save_uid(meta["_uid"])
            await asyncio.sleep(0.3)   # мягкий троттлинг под лимит ~4 rps на чат
        return sent


async def _publish(meta: dict[str, Any]) -> bool:
    """Завести запись о письме и отправить карточку с кнопками в канал."""
    uid: int = meta["_uid"]
    subject = str(meta.get("subject") or "")
    sender = _fmt_sender(meta)
    rfc_id = str(meta.get("message_id") or "")
    date = str(meta.get("date") or "")[:25]

    with SessionLocal() as s:
        if rfc_id:
            dup = s.scalar(
                select(TrackedEmail).where(
                    TrackedEmail.account == settings.email_account,
                    TrackedEmail.rfc_message_id == rfc_id,
                )
            )
            if dup is not None:
                log.info("письмо %s уже публиковалось, пропускаю", rfc_id)
                return False

        email = TrackedEmail(
            account=settings.email_account,
            mailbox=settings.mailbox,
            uid=uid,
            rfc_message_id=rfc_id,
            subject=subject,
            sender=sender,
        )
        s.add(email)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            log.info("письмо uid=%s уже в базе, пропускаю", uid)
            return False
        pk = email.id

    # Строка в базе — это уже заявка на публикацию: она держит дедупликацию,
    # пока карточка готовится. Если опубликовать не удалось, заявку снимаем,
    # иначе дедупликация навсегда спрячет письмо, которого никто не видел.
    try:
        # Тело тянем лениво — только чтобы показать превью и выжимку.
        body = ""
        try:
            content = await mail.get_body(uid)
            body = str(content.get("body") or content.get("content") or "")
        except Exception:  # noqa: BLE001 - карточка полезна и без тела
            log.warning("не удалось получить тело письма uid=%s", uid, exc_info=True)

        summary = await summarize(sender=sender, subject=subject, body=body) if body else ""
        preview = _preview(body)

        with SessionLocal() as s:
            email = s.get(TrackedEmail, pk)
            assert email is not None
            text = cards.email_card(email, preview, summary, date)
            msg = await pachca.send_message(
                entity_id=settings.pachca_channel_id,
                content=text,
                buttons=cards.email_buttons(pk),
            )
            email.pachca_message_id = msg.get("id")
            s.commit()
    except Exception:
        _drop_claim(pk)
        raise
    return True


def _drop_claim(pk: int) -> None:
    """Убрать незавершённую заявку на публикацию, чтобы письмо повторилось."""
    with SessionLocal() as s:
        email = s.get(TrackedEmail, pk)
        if email is not None and email.pachca_message_id is None:
            s.delete(email)
            s.commit()


def _reset_uid(uid: int) -> None:
    """Принудительно выставить точку отсчёта (после пересоздания папки)."""
    with SessionLocal() as s:
        state = get_poll_state(s, settings.email_account, settings.mailbox)
        state.last_uid = uid
        s.commit()


def _save_uid(uid: int) -> None:
    with SessionLocal() as s:
        state = get_poll_state(s, settings.email_account, settings.mailbox)
        if uid > state.last_uid:
            state.last_uid = uid
            s.commit()


def _preview(body: str) -> str:
    text = " ".join((body or "").split())
    if not text:
        return ""
    if len(text) <= settings.preview_chars:
        return text
    return text[: settings.preview_chars] + " …"


def _fmt_sender(meta: dict[str, Any]) -> str:
    raw = meta.get("sender") or meta.get("from") or meta.get("from_address") or ""
    if isinstance(raw, dict):
        name, addr = raw.get("name"), raw.get("address") or raw.get("email")
        return f"{name} <{addr}>" if name and addr else str(addr or name or "")
    if isinstance(raw, list) and raw:
        return _fmt_sender({"sender": raw[0]})
    return str(raw)
