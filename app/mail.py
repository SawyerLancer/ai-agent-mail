"""Почта через MCP-сервер mcp-email-server (stdio).

Единственное место, где проект знает о почте. Своего IMAP/SMTP-кода нет:
все операции — вызовы инструментов MCP.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from contextlib import suppress
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from .config import settings

log = logging.getLogger(__name__)

class MailToolError(RuntimeError):
    """MCP-сервер сам ответил ошибкой: операция точно не выполнена.

    В отличие от таймаута и обрыва связи, где результат неизвестен.
    """


# Предохранитель от листания всего ящика: 20 × POLL_PAGE_SIZE писем за проход.
_MAX_POLL_PAGES = 20


class MailClient:
    """Долгоживущая MCP-сессия с автоматическим переподключением.

    Транспорт stdio живёт внутри одной выделенной задачи: anyio требует,
    чтобы вход и выход из его cancel scope произошли в одной и той же
    задаче. Планировщик и обработчики кнопок работают в своих задачах,
    поэтому открывать и закрывать сессию «по месту» нельзя — соединением
    владеет _runner, остальные только вызывают инструменты.
    """

    def __init__(self) -> None:
        self._session: ClientSession | None = None
        self._lock = asyncio.Lock()   # stdio-транспорт не выдерживает параллельных вызовов
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._special: dict[str, str] = {}   # флаг RFC 6154 → имя папки, ищется один раз

    async def start(self) -> None:
        async with self._lock:
            await self._connect()

    async def _connect(self) -> None:
        """Поднять задачу-владельца сессии и дождаться готовности."""
        await self._close()
        self._ready, self._stop, self._error = asyncio.Event(), asyncio.Event(), None
        self._task = asyncio.create_task(self._runner(), name="mcp-email")
        await self._ready.wait()
        if self._session is None:
            raise RuntimeError("не удалось подключиться к MCP") from self._error

    async def _runner(self) -> None:
        """Владеет транспортом: открывает, держит и закрывает его сам."""
        assert self._ready is not None and self._stop is not None
        try:
            params = StdioServerParameters(
                command=settings.mcp_command,
                args=list(settings.mcp_args),
                env=_server_env(),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    log.info("MCP подключён, инструментов: %d", len(tools.tools))
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - разбудить ожидающих и дать им ошибку
            self._error = exc
            log.error("MCP-сессия оборвалась", exc_info=True)
        finally:
            self._session = None
            self._ready.set()

    async def _close(self) -> None:
        task, self._task = self._task, None
        self._session = None
        if task is None or task.done():
            return
        if self._stop is not None:
            self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("MCP-сессия не закрылась за 10 с, снимаю задачу")
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        except Exception:  # noqa: BLE001 - закрытие не должно мешать переподключению
            log.warning("ошибка при закрытии MCP-сессии", exc_info=True)

    async def stop(self) -> None:
        async with self._lock:
            await self._close()

    async def call(self, tool: str, args: dict[str, Any], *, retry: bool = True) -> Any:
        """Вызвать инструмент MCP. При обрыве stdio — одна попытка переподключения.

        retry=False — для отправки: после таймаута письмо могло уже уйти, и
        повтор отправил бы его второй раз. Сессию всё равно переподнимем.
        """
        async with self._lock:
            result = None
            for attempt in ((1, 2) if retry else (2,)):
                if self._session is None:
                    await self._connect()
                try:
                    # Без таймаута подвисший IMAP держит блокировку вечно и
                    # останавливает всего бота: поллинг и кнопки ждут её же.
                    result = await asyncio.wait_for(
                        self._session.call_tool(tool, args),  # type: ignore[union-attr]
                        timeout=settings.mcp_timeout,
                    )
                    break
                except (TimeoutError, asyncio.TimeoutError):
                    log.warning("MCP-вызов %s не ответил за %d с", tool, settings.mcp_timeout)
                    await self._close()          # сессия подозрительная — поднимем заново
                    if attempt == 2:
                        raise
                except Exception:  # noqa: BLE001 - транспорт мог умереть
                    if attempt == 2:
                        raise
                    log.warning("MCP-вызов %s упал, переподключаюсь", tool, exc_info=True)
                    await self._close()

        if getattr(result, "isError", False):
            raise MailToolError(f"MCP {tool}: {_as_text(result)}")

        structured = getattr(result, "structuredContent", None)
        if structured:
            return structured
        text = _as_text(result)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    # --- операции, которые нужны боту ---

    async def list_new(self, since_uid: int) -> tuple[list[dict[str, Any]], int | None]:
        """Метаданные писем новее указанного UID и верхний UID в папке.

        Верхний UID возвращаем здесь же: он виден из того же ответа, а лишний
        запрос метаданных на большом ящике стоит секунды.

        Листаем страницы, пока на них только новые письма: иначе всплеск больше
        page_size терял бы старшие письма, а точка отсчёта уезжала бы за них.
        На первом запуске (since_uid=0) нужен лишь верхний UID — одна страница.
        """
        fresh, top = [], None
        for page in range(1, _MAX_POLL_PAGES + 1):
            data = await self.call(
                "list_emails_metadata",
                {
                    "account_name": settings.email_account,
                    "mailbox": settings.mailbox,
                    "page": page,
                    "page_size": settings.poll_page_size,
                    "order": "desc",
                },
            )
            items = _extract_list(data, ("emails", "items", "results", "data"))
            reached_old = False
            for it in items:
                uid = _to_int(it.get("email_id") or it.get("uid"))
                if uid is None:
                    continue
                top = uid if top is None else max(top, uid)
                if uid > since_uid:
                    it["_uid"] = uid
                    fresh.append(it)
                else:
                    reached_old = True
            if reached_old or since_uid == 0 or len(items) < settings.poll_page_size:
                break
        else:
            log.warning(
                "новых писем больше %d страниц: более старые из них пропущены",
                _MAX_POLL_PAGES,
            )
        fresh.sort(key=lambda x: x["_uid"])
        return fresh, top

    async def get_body(self, uid: int) -> dict[str, Any]:
        data = await self.call(
            "get_emails_content",
            {
                "account_name": settings.email_account,
                "mailbox": settings.mailbox,
                "email_ids": [_eid(uid)],
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
        return await self.call("send_email", args, retry=False)

    async def forward(self, *, uid: int, to: list[str], note: str = "") -> Any:
        return await self.call(
            "forward_email",
            {
                "account_name": settings.email_account,
                "email_id": _eid(uid),
                "source_mailbox": settings.mailbox,
                "recipients": to,
                "body": note,
                "include_attachments": True,
            },
            retry=False,
        )

    async def delete(self, uid: int) -> Any:
        """Переместить письмо в корзину ящика.

        Не delete_emails: тот делает UID EXPUNGE, и письмо пропадает мимо
        «Удалённых» безвозвратно.
        """
        return await self.call(
            "move_emails",
            {
                "account_name": settings.email_account,
                "email_ids": [_eid(uid)],
                "source_mailbox": settings.mailbox,
                "destination_mailbox": await self._special_mailbox("\\trash"),
            },
        )

    async def _special_mailbox(self, flag: str) -> str:
        """Папка по флагу RFC 6154 (\\Trash, \\Sent), а не по имени: у провайдеров
        оно разное и бывает локализованным. Нет флага — ошибка, а не догадка."""
        if flag not in self._special:
            data = await self.call("list_mailboxes", {"account_name": settings.email_account})
            boxes = _extract_list(data, ("mailboxes", "items", "results", "data"))
            for box in boxes:
                flags = {str(f).lower() for f in box.get("flags") or []}
                name = str(box.get("name") or "")
                if flag in flags and name:
                    self._special[flag] = name
                    break
            else:
                raise RuntimeError(f"в ящике не найдена папка с флагом {flag}")
        return self._special[flag]

    async def find_sent(self, *, to: str, since: dt.datetime, subject: str | None = None) -> bool:
        """Есть ли в «Отправленных» письмо этому адресату не раньше since.

        Свой Message-ID задать нельзя (send_email не принимает заголовки),
        поэтому ищем по адресату, времени и, для ответа, теме. Если сервер
        отправил письмо, но не успел сохранить копию, — не найдём.
        """
        data = await self.call(
            "list_emails_metadata",
            {
                "account_name": settings.email_account,
                "mailbox": await self._special_mailbox("\\sent"),
                "to_address": to,
                "since": since.isoformat(),
                "page": 1,
                "page_size": 10,
                "order": "desc",
            },
        )
        want = _norm_subject(subject) if subject is not None else None
        for it in _extract_list(data, ("emails", "items", "results", "data")):
            if want is None or _norm_subject(str(it.get("subject") or "")) == want:
                return True
        return False

    async def archive(self, uid: int) -> Any:
        return await self.call(
            "archive_emails",
            {
                "account_name": settings.email_account,
                "mailbox": settings.mailbox,
                "email_ids": [_eid(uid)],
            },
        )

    async def mark_read(self, uid: int) -> Any:
        return await self.call(
            "mark_emails_as_read",
            {
                "account_name": settings.email_account,
                "mailbox": settings.mailbox,
                "email_ids": [_eid(uid)],
            },
        )


def _server_env() -> dict[str, str]:
    """Окружение для дочернего процесса MCP.

    stdio_client по умолчанию пропускает только безопасный минимум (HOME, PATH),
    поэтому настройки ящика (MCP_EMAIL_SERVER_*) нужно передать явно — иначе
    сервер поднимается без аккаунта.
    """
    env = get_default_environment()
    env.update(
        {k: v for k, v in os.environ.items() if k.startswith("MCP_EMAIL_SERVER_")}
    )
    return env


def _norm_subject(subject: str) -> str:
    return " ".join(subject.split()).casefold()


def _eid(uid: int) -> str:
    """MCP ждёт email_id строкой (схема: pattern ^[1-9][0-9]*$)."""
    return str(uid)


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
