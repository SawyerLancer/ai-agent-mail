import asyncio
import datetime as dt

import pytest

from app import mail as mail_mod
from app.mail import MailClient, MailToolError, forwarded_subject


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


def _sent_client(monkeypatch, emails):
    c = MailClient()
    calls = []

    async def call(tool, args, retry=True):
        calls.append((tool, args))
        if tool == "list_mailboxes":
            return [{"name": "Sent", "flags": ["\\Sent"]}, {"name": "INBOX", "flags": []}]
        return {"emails": emails}

    monkeypatch.setattr(c, "call", call)
    return c, calls


SINCE = dt.datetime(2026, 10, 2, 17, 0, tzinfo=dt.timezone.utc)


def test_find_sent_matches_recipient_subject_and_time(monkeypatch):
    c, calls = _sent_client(monkeypatch, [
        {"subject": "Re:  Счёт", "date": "2026-10-02T17:01:00Z", "recipients": ["Иван <A@B.ru>"]},
    ])
    assert asyncio.run(c.find_sent(to="a@b.ru", since=SINCE, subject="re: счёт"))
    assert not asyncio.run(c.find_sent(to="a@b.ru", since=SINCE, subject="Re: Другое"))
    args = calls[1][1]
    # Серверные to_address/since на Яндексе не работают — не передаём (skill mail-mcp).
    assert args["mailbox"] == "Sent" and "to_address" not in args and "since" not in args
    assert args["order"] == "desc"


def test_find_sent_other_recipient_not_found(monkeypatch):
    c, _ = _sent_client(monkeypatch, [
        {"subject": "Re: Счёт", "date": "2026-10-02T17:01:00Z", "recipients": ["c@d.ru"]},
    ])
    assert not asyncio.run(c.find_sent(to="a@b.ru", since=SINCE, subject="Re: Счёт"))


def test_find_sent_ignores_same_subject_sent_earlier_today(monkeypatch):
    # Второй ответ в цепочке: тот же адресат и «Re: …», но утром — не наш.
    c, _ = _sent_client(monkeypatch, [{"subject": "Re: Счёт", "date": "2026-10-02T09:15:00+00:00", "recipients": ["a@b.ru"]}])
    assert not asyncio.run(c.find_sent(to="a@b.ru", since=SINCE, subject="Re: Счёт"))


def test_find_sent_without_date_is_not_found(monkeypatch):
    c, _ = _sent_client(monkeypatch, [{"subject": "Re: Счёт", "recipients": ["a@b.ru"]}, {"subject": "Re: Счёт", "date": "мусор", "recipients": ["a@b.ru"]}])
    assert not asyncio.run(c.find_sent(to="a@b.ru", since=SINCE, subject="Re: Счёт"))


def test_find_sent_naive_date_is_utc(monkeypatch):
    c, _ = _sent_client(monkeypatch, [{"subject": "Re: Счёт", "date": "2026-10-02T17:05:00", "recipients": ["a@b.ru"]}])
    assert asyncio.run(c.find_sent(to="a@b.ru", since=SINCE, subject="Re: Счёт"))


def test_forward_with_other_subject_not_found(monkeypatch):
    c, _ = _sent_client(monkeypatch, [{"subject": "Fwd: Другое письмо", "date": "2026-10-02T17:01:00+00:00", "recipients": ["a@b.ru"]}])
    assert not asyncio.run(c.find_sent(to="a@b.ru", since=SINCE, subject=forwarded_subject("Счёт")))


def test_forwarded_subject_like_server():
    assert forwarded_subject("Счёт") == "Fwd: Счёт"
    assert forwarded_subject("FWD: Счёт") == "FWD: Счёт"
    assert forwarded_subject("Re: Счёт") == "Fwd: Re: Счёт"
