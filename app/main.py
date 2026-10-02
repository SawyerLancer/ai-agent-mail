"""FastAPI-приложение: приём вебхуков Пачки + поллинг почты по расписанию."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response

from . import events as events_log
from . import handlers
from .config import settings
from .db import SessionLocal, already_seen, init_db
from .mail import mail
from .pachca import pachca
from .poller import poll_once

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Чужие библиотеки на INFO заливают журнал так, что своих строк не видно.
for _noisy in ("httpx", "apscheduler.executors.default"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("bot")

scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await mail.start()
    scheduler.add_job(
        _poll_job,
        "interval",
        seconds=settings.poll_interval,
        max_instances=1,          # наложение циклов = дубли писем
        coalesce=True,
        id="poll_mail",
    )
    scheduler.add_job(
        _expire_job,
        "interval",
        minutes=10,               # точность срока жизни черновика — 10 минут
        max_instances=1,
        coalesce=True,
        id="expire_drafts",
    )
    if settings.events_mode == "polling":
        scheduler.add_job(
            _events_job,
            "interval",
            seconds=settings.events_poll_interval,
            max_instances=1,
            coalesce=True,
            id="poll_events",
        )
    scheduler.start()
    # В фоне: проверка «Отправленных» на большом ящике — секунды, старт не ждёт.
    recovery = asyncio.create_task(handlers.recover_sending(), name="recover-sending")
    log.info(
        "бот запущен: почта каждые %d с, события — %s",
        settings.poll_interval,
        "журнал бота" if settings.events_mode == "polling" else "исходящий вебхук",
    )
    try:
        yield
    finally:
        recovery.cancel()
        scheduler.shutdown(wait=False)
        await mail.stop()
        await pachca.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


async def _poll_job() -> None:
    try:
        n = await poll_once()
        if n:
            log.info("новых писем: %d", n)
    except Exception:  # noqa: BLE001 - джоба не должна умирать насовсем
        log.error("сбой в проходе поллинга", exc_info=True)


async def _expire_job() -> None:
    try:
        n = await handlers.expire_drafts()
        if n:
            log.info("устаревших черновиков погашено: %d", n)
    except Exception:  # noqa: BLE001 - джоба не должна умирать насовсем
        log.error("сбой при уборке черновиков", exc_info=True)


async def _events_job() -> None:
    try:
        await events_log.drain_once()
    except Exception:  # noqa: BLE001 - джоба не должна умирать насовсем
        log.error("сбой при чтении журнала событий", exc_info=True)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "provider": settings.llm_provider}


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    if settings.events_mode != "webhook":
        return Response("webhook mode disabled", status_code=404)

    raw = await request.body()

    # 1. Подпись: HMAC-SHA256 от сырого тела.
    expected = hmac.new(
        settings.pachca_signing_secret.encode(), raw, hashlib.sha256
    ).hexdigest()
    got = request.headers.get("Pachca-Signature", "")
    if not hmac.compare_digest(expected, got):
        log.warning("неверная подпись вебхука")
        return Response("invalid signature", status_code=401)

    # 2. IP отправителя — опционально: за прокси здесь будет адрес прокси.
    if settings.check_source_ip:
        client_ip = request.client.host if request.client else ""
        if client_ip != settings.pachca_allowed_ip:
            log.warning("вебхук с чужого адреса: %s", client_ip)
            return Response("forbidden", status_code=403)

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return Response("bad json", status_code=200)   # повторы бессмысленны

    # 3. Защита от повторной отправки (replay).
    ts = event.get("webhook_timestamp")
    if isinstance(ts, (int, float)) and abs(time.time() - ts) > 60:
        log.warning("просроченное событие, отклоняю")
        return Response("expired", status_code=401)

    # 4. Дедупликация: доставка at-least-once.
    key = _event_key(event)
    with SessionLocal() as s:
        if already_seen(s, key):
            return Response("duplicate", status_code=200)

    # Отвечаем сразу, обработку уносим в фон: 2xx нужен быстро.
    asyncio.create_task(_dispatch(event))
    return Response("ok", status_code=200)


async def _dispatch(event: dict[str, Any]) -> None:
    try:
        etype = event.get("type")
        if etype == "button" and event.get("event") == "click":
            await handlers.handle_button(event)
        elif etype == "message" and event.get("event") == "new":
            await handlers.handle_message(event)
    except Exception:  # noqa: BLE001 - фоновая задача не должна ронять процесс
        log.error("ошибка обработки события %s", event.get("type"), exc_info=True)


def _event_key(event: dict[str, Any]) -> str:
    parts = [
        str(event.get("type")),
        str(event.get("event")),
        str(event.get("id") or event.get("message_id") or ""),
        str(event.get("webhook_timestamp") or ""),
        str(event.get("data") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:48]
