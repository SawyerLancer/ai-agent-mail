"""Режим без публичного URL: читаем журнал событий бота вместо вебхуков.

Пачка хранит события бота, если в настройках включено «Сохранять историю
событий». Журнал сам не чистится — прочитанное обязательно удаляем, иначе
события придут снова.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import handlers
from .config import settings
from .db import SessionLocal, already_seen
from .pachca import pachca

log = logging.getLogger(__name__)

_lock = asyncio.Lock()

# event_type в журнале → (type, event) как в payload исходящего вебхука
_TYPES = {
    "message_new": ("message", "new"),
    "button": ("button", "click"),
    "button_click": ("button", "click"),
}


async def drain_once() -> int:
    """Вычитать и обработать порцию событий. Возвращает число обработанных."""
    if _lock.locked():
        return 0

    async with _lock:
        try:
            events = await pachca.list_events(limit=50)
        except Exception:  # noqa: BLE001 - сеть подвела, повторим через интервал
            log.error("не удалось прочитать журнал событий", exc_info=True)
            return 0

        handled = 0
        # Журнал отдаёт новое сверху; обрабатываем в порядке появления.
        for record in reversed(events):
            event_id = str(record.get("id") or "")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            event_type = str(record.get("event_type") or "")

            try:
                if await _process(event_id, event_type, payload):
                    handled += 1
            except Exception:  # noqa: BLE001 - плохое событие не должно вставать поперёк
                log.error("ошибка обработки события %s (%s)", event_id, event_type, exc_info=True)

            # Удаляем в любом случае: иначе событие вернётся и зациклит бота.
            if event_id:
                try:
                    await pachca.delete_event(event_id)
                except Exception:  # noqa: BLE001
                    log.warning("не удалось удалить событие %s", event_id, exc_info=True)
        return handled


async def _process(event_id: str, event_type: str, payload: dict[str, Any]) -> bool:
    mapped = _TYPES.get(event_type)
    if mapped is None:
        return False
    kind, action = mapped

    # Журнал доставляет at-least-once ровно как вебхук.
    key = f"log:{event_id}" if event_id else f"log:{kind}:{payload.get('message_id')}"
    with SessionLocal() as s:
        if already_seen(s, key):
            return False

    event = {**payload, "type": kind, "event": action}
    if kind == "button":
        await handlers.handle_button(event)
    else:
        if int(event.get("user_id") or 0) == settings.bot_user_id:
            return False   # своё же сообщение — не реагируем
        await handlers.handle_message(event)
    return True
