import asyncio
import datetime as dt

import pytest

from app import mail as mail_mod
from app.mail import MailClient, MailToolError


class HangingSession:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, tool, args):
        self.calls += 1
        await asyncio.sleep(10)


def _client(monkeypatch, session):
    c = MailClient()
    monkeypatch.setattr(mail_mod.settings, "mcp_timeout", 0.05)

    async def connect():
        c._session = session

    async def close():
        c._session = None

    monkeypatch.setattr(c, "_connect", connect)
    monkeypatch.setattr(c, "_close", close)
    return c


def test_send_is_not_retried_after_timeout(monkeypatch):
    session = HangingSession()
    c = _client(monkeypatch, session)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(c.send(to=["a@b.ru"], subject="s", body="b"))
    assert session.calls == 1          # второй попытки — второго письма — нет


def test_reads_are_retried_after_timeout(monkeypatch):
    session = HangingSession()
    c = _client(monkeypatch, session)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(c.call("list_mailboxes", {}))
    assert session.calls == 2


def test_server_error_is_mail_tool_error(monkeypatch):
    class Failing:
        async def call_tool(self, tool, args):
            class R:
                isError = True
                content = []
            return R()

    c = _client(monkeypatch, Failing())
    with pytest.raises(MailToolError):
        asyncio.run(c.send(to=["a@b.ru"], subject="s", body="b"))


def test_find_sent_matches_subject(monkeypatch):
    c = MailClient()
    calls = []

    async def call(tool, args, retry=True):
        calls.append((tool, args))
        if tool == "list_mailboxes":
            return [{"name": "Sent", "flags": ["\\Sent"]}, {"name": "INBOX", "flags": []}]
        return {"emails": [{"subject": "Re:  Счёт"}]}

    monkeypatch.setattr(c, "call", call)
    since = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
    assert asyncio.run(c.find_sent(to="a@b.ru", since=since, subject="re: счёт"))
    assert not asyncio.run(c.find_sent(to="a@b.ru", since=since, subject="Re: Другое"))
    args = calls[1][1]
    assert args["mailbox"] == "Sent" and args["to_address"] == "a@b.ru"
    assert args["since"].startswith("2026-10-02")
