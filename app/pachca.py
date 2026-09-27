"""Тонкий клиент REST API Пачки: сообщения, кнопки, треды."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)

Button = dict[str, str]
ButtonRows = list[list[Button]]


class Pachca:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.pachca_base_url,
            headers={"Authorization": f"Bearer {settings.pachca_token}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        """Запрос с учётом 429: Пачка отдаёт Retry-After, её и слушаем."""
        for attempt in range(4):
            r = await self._client.request(method, path, **kw)
            if r.status_code == 429:
                delay = _retry_after(r, fallback=2 ** attempt)
                log.warning("Пачка 429, ждём %.1f с", delay)
                await asyncio.sleep(delay)
                continue
            if r.status_code >= 500 and attempt < 3:
                await asyncio.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        raise RuntimeError(f"Пачка: не удалось выполнить {method} {path}")

    async def send_message(
        self,
        *,
        entity_id: int,
        content: str,
        entity_type: str = "discussion",
        buttons: ButtonRows | None = None,
        parent_message_id: int | None = None,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {"entity_id": entity_id, "entity_type": entity_type, "content": content}
        if buttons:
            msg["buttons"] = buttons
        if parent_message_id:
            msg["parent_message_id"] = parent_message_id
        data = await self._request("POST", "/messages", json={"message": msg})
        return data.get("data", {})

    async def edit_message(
        self, message_id: int, *, content: str, buttons: ButtonRows | None = None
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {"content": content}
        if buttons is not None:
            msg["buttons"] = buttons          # [] убирает все кнопки
        data = await self._request("PUT", f"/messages/{message_id}", json={"message": msg})
        return data.get("data", {})

    async def drop_buttons(self, message_id: int, *, content: str) -> None:
        """Погасить кнопки, чтобы их нельзя было нажать повторно."""
        try:
            await self.edit_message(message_id, content=content, buttons=[])
        except Exception:  # noqa: BLE001 - косметика, не повод падать
            log.warning("не удалось убрать кнопки у %s", message_id, exc_info=True)

    async def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Журнал событий бота — замена исходящему вебхуку, когда нет публичного URL.

        Журнал не чистится сам: прочитанные события надо удалять, иначе
        придут снова.
        """
        data = await self._request("GET", "/webhooks/events", params={"limit": limit})
        items = data.get("data") or []
        return [x for x in items if isinstance(x, dict)]

    async def delete_event(self, event_id: str) -> None:
        await self._request("DELETE", f"/webhooks/events/{event_id}")

    async def create_thread(self, message_id: int) -> dict[str, Any]:
        data = await self._request("POST", f"/messages/{message_id}/thread")
        return data.get("data", {})

    async def send_to_thread(
        self, thread_id: int, content: str, buttons: ButtonRows | None = None
    ) -> dict[str, Any]:
        """Сообщение в тред.

        entity_id здесь — id треда (то, что вернул POST /messages/{id}/thread),
        а не chat_id треда: с chat_id Пачка отвечает 404. В этом же виде
        приходят и входящие сообщения треда, по нему их и сопоставляем.
        """
        return await self.send_message(
            entity_id=thread_id, entity_type="thread", content=content, buttons=buttons
        )


def _retry_after(r: httpx.Response, *, fallback: float) -> float:
    raw = r.headers.get("Retry-After")
    try:
        return max(float(raw), 0.5) if raw else fallback
    except ValueError:
        return fallback


pachca = Pachca()
