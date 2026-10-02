"""Сквозной сценарий черновика на подставных Пачке, почте и модели."""
import asyncio
import json

import pytest
from sqlalchemy import delete

from app import handlers, llm, style
from app.db import Draft, SessionLocal, StyleRule, TrackedEmail, init_db

BODY = "Добрый день! Пришлите, пожалуйста, счёт на 15 000 ₽ до 12 марта."


class FakePachca:
    def __init__(self):
        self.out = []

    async def send_to_thread(self, thread_id, content, buttons=None):
        self.out.append({"content": content, "buttons": buttons})
        return {"id": 1000 + len(self.out)}

    async def send_message(self, **kw):
        self.out.append(kw)
        return {"id": 1}

    async def drop_buttons(self, message_id, content):
        self.out.append({"drop": message_id, "content": content})

    async def create_thread(self, message_id):
        return {"id": 77, "chat_id": 9}


class FakeMail:
    def __init__(self):
        self.sent = []

    async def get_body(self, uid):
        return {"body": BODY}

    async def send(self, **kw):
        self.sent.append(kw)


@pytest.fixture
def env(monkeypatch):
    init_db()
    with SessionLocal() as s:
        s.execute(delete(Draft))
        s.execute(delete(TrackedEmail))
        s.execute(delete(StyleRule))
        s.add(TrackedEmail(account="a", mailbox="INBOX", uid=5, rfc_message_id="<m@x>",
                           subject="Счёт", sender="Иван <ivan@x.ru>", pachca_message_id=50))
        s.commit()
        pk = s.query(TrackedEmail).one().id
    p, m = FakePachca(), FakeMail()
    monkeypatch.setattr(handlers, "pachca", p)
    monkeypatch.setattr(handlers, "mail", m)
    calls = {"proofread": [], "learn": [], "draft_reply": []}

    async def draft_reply(**kw):
        calls["draft_reply"].append(kw)
        return "Добрый день! Счёт на 15 000 ₽ пришлём до 12 марта, оплата до 20 марта."

    async def proofread(text):
        calls["proofread"].append(text)
        return text.replace("прийдёт", "придёт")

    async def learn(**kw):
        calls["learn"].append(kw)

    monkeypatch.setattr(llm, "draft_reply", draft_reply)
    monkeypatch.setattr(llm, "proofread", proofread)
    monkeypatch.setattr(style, "learn", learn)
    return pk, p, m, calls


def run(coro):
    return asyncio.run(coro)


def click(data, message_id=50):
    return handlers.handle_button({"data": data, "user_id": 1, "message_id": message_id, "chat_id": 1})


def say(text):
    return handlers.handle_message({"entity_type": "thread", "entity_id": 77, "user_id": 1, "content": text})


def test_reply_marks_added_facts(env):
    pk, p, _, _ = env
    run(click(f"mail:reply:{pk}"))
    card = p.out[-1]["content"]
    assert "⚠️ Модель добавила: 20 марта" in card
    assert "15 000" not in card.split("⚠️")[1]      # было в письме — не помечаем


def test_own_text_is_proofread_and_raw_restores(env):
    pk, p, m, calls = env
    run(click(f"mail:reply:{pk}"))
    run(click("draft:own:1", message_id=1001))
    run(say("Счёт прийдёт завтра"))
    card = p.out[-1]
    assert calls["proofread"] == ["Счёт прийдёт завтра"]
    assert "_прийдёт_ → **придёт**" in card["content"]
    assert [b["text"] for b in card["buttons"][0]] == ["📨 Отправить", "↩️ Без правок", "✍️ Свой текст"]
    assert "⚠️" not in card["content"]                 # вычитка ничего не добавила

    run(click("draft:raw:1", message_id=1003))
    with SessionLocal() as s:
        d = s.get(Draft, 1)
        assert d.body == "Счёт прийдёт завтра" and d.status == "editing"
    run(click("draft:send:1", message_id=1004))
    assert m.sent[-1]["body"] == "Счёт прийдёт завтра"    # дословно, без подписи


def test_no_errors_message_and_no_raw_button(env):
    pk, p, _, _ = env
    run(click(f"mail:reply:{pk}"))
    run(click("draft:own:1", message_id=1001))
    run(say("Всё верно, спасибо."))
    card = p.out[-1]
    assert "_Ошибок не нашёл._" in card["content"]
    assert "↩️ Без правок" not in [b["text"] for b in card["buttons"][0]]


def test_proofread_failure_keeps_text(env, monkeypatch):
    pk, p, m, _ = env

    async def broken(text):
        raise RuntimeError("down")

    monkeypatch.setattr(llm, "proofread", broken)
    run(click(f"mail:reply:{pk}"))
    run(click("draft:own:1", message_id=1001))
    run(say("Мой текст"))
    assert "Проверить ошибки не удалось" in p.out[-1]["content"]
    with SessionLocal() as s:
        assert s.get(Draft, 1).body == "Мой текст"


def test_send_triggers_learning_with_user_texts_only(env):
    pk, _, _, calls = env

    async def scenario():
        await click(f"mail:reply:{pk}")
        await click("draft:own:1", message_id=1001)
        await say("Иван, счёт прийдёт завтра.")
        await click("draft:send:1", message_id=1003)
        await asyncio.gather(*handlers._bg_tasks)

    run(scenario())
    kw = calls["learn"][-1]
    assert kw["recipient"] == "ivan@x.ru"
    assert kw["user_texts"] == ["Иван, счёт прийдёт завтра."]
    assert BODY not in json.dumps(kw, ensure_ascii=False)     # тело письма не учим


def test_style_rules_go_to_draft_prompt(env):
    pk, _, _, calls = env
    style.handle_command("/стиль запомни для ivan@x.ru: на «ты»", owner_id=handlers._owner_id())
    run(click(f"mail:reply:{pk}"))
    assert calls["draft_reply"][-1]["style_rules"] == ["(для этого адресата) на «ты»"]


def test_style_command_in_main_chat(env):
    _, p, _, _ = env
    run(handlers.handle_message({"entity_type": "discussion", "entity_id": 5, "user_id": 1, "content": "/стиль"}))
    assert p.out[-1]["entity_id"] == 5 and "Активных правил" in p.out[-1]["content"]
