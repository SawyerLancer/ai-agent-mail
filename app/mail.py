"""Почта через MCP-сервер mcp-email-server (stdio).

Единственное место, где проект знает о почте. Своего IMAP/SMTP-кода нет:
все операции — вызовы инструментов MCP.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import settings

log = logging.getLogger(__name__)


class MailClient:
    """Долгоживущая MCP-сессия с автоматическим переподключением."""

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()   # stdio-транспорт не выдерживает параллельных вызовов

    async def start(self) -> None:
        async with self._lock:
            await self._connect()

    async def _connect(self) -> None:
        await self._close()
        stack = AsyncExitStack()
        params = StdioServerParameters(command=settings.mcp_command, args=list(settings.mcp_args))
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._stack, self._session = stack, session
        tools = await session.list_tools()
        log.info("MCP подключён, инструментов: %d", len(tools.tools))

    async def _close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001 - закрытие не должно мешать переподключению
                log.warning("ошибка при закрытии MCP-сессии", exc_info=True)
        self._stack = self._session = None

    async def stop(self) -> None:
        async with self._lock:
            await self._close()

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        """Вызвать инструмент MCP. При обрыве stdio — одна попытка переподключения."""
        async with self._lock:
            for attempt in (1, 2):
                if self._session is None:
                    await self._connect()
                try:
                    result = await self._session.call_tool(tool, args)  # type: ignore[union-attr]
                    break
                except Exception:  # noqa: BLE001 - транспорт мог умереть
                    if attempt == 2:
                        raise
                    log.warning("MCP-вызов %s упал, переподключаюсь", tool, exc_info=True)
                    await self._close()

        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP {tool}: {_as_text(result)}")

        structured = getattr(result, "structuredContent", None)
        if structured:
            return structured
        text = _as_text(result)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    # --- операции, которые нужны боту ---

    async def list_new(self, since_uid: int) -> list[dict[str, Any]]:
        """Метаданные писем в папке, новее указанного UID. Тела не тянем."""
        data = await self.call(
            "list_emails_metadata",
            {
                "account_name": settings.email_account,
                "mailbox": settings.mailbox,
                "page": 1,
                "page_size": settings.poll_page_size,
                "order": "desc",
            },
        )
        items = _extract_list(data, ("emails", "items", "results", "data"))
        fresh = []
        for it in items:
            uid = _to_int(it.get("email_id") or it.get("uid"))
            if uid is not None and uid > since_uid:
                it["_uid"] = uid
                fresh.append(it)
        fresh.sort(key=lambda x: x["_uid"])
        return fresh

    async def get_body(self, uid: int) -> dict[str, Any]:
        data = await self.call(
            "get_emails_content",
            {
                "account_name": settings.email_account,
                "mailbox": settings.mailbox,
                "email_ids": [uid],
                "max_body_length": max(settings.body_chars_for_llm, settings.preview_chars) + 500,
                "mark_as_read": False,
            },
        )
        items = _extract_list(data, ("emails", "items", "results", "data"))
        return items[0] if items else {}

    async def send(
        self,
        *,
        to: list[str],
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> Any:
        args: dict[str, Any] = {
            "account_name": settings.email_account,
            "recipients": to,
            "subject": subject,
            "body": body,
        }
        if in_reply_to:
            args["in_reply_to"] = in_reply_to
        if references:
            args["references"] = references
        return await self.call("send_email", args)

    async def forward(self, *, uid: int, to: list[str], note: str = "") -> Any:
        return await self.call(
            "forward_email",
            {
                "account_name": settings.email_account,
                "email_id": uid,
                "source_mailbox": settings.mailbox,
                "recipients": to,
                "body": note,
                "include_attachments": True,
            },
        )

    async def delete(self, uid: int) -> Any:
        return await self.call(
            "delete_emails",
            {
                "account_name": settings.email_account,
                "mailbox": settings.mailbox,
                "email_ids": [uid],
            },
        )

    async def archive(self, uid: int) -> Any:
        return await self.call(
            "archive_emails",
            {
                "account_name": settings.email_account,
                "source_mailbox": settings.mailbox,
                "email_ids": [uid],
            },
        )

    async def mark_read(self, uid: int) -> Any:
        return await self.call(
            "mark_emails_as_read",
            {
                "account_name": settings.email_account,
                "mailbox": settings.mailbox,
                "email_ids": [uid],
            },
        )


def _as_text(result: Any) -> str:
    parts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text"]
    return "\n".join(parts)


def _extract_list(data: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Достать список писем из ответа, не завязываясь на точное имя поля."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


mail = MailClient()
