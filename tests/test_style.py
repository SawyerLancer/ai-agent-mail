import pytest
from sqlalchemy import delete

from app import style
from app.db import SessionLocal, StyleRule, init_db

OWNER = "me@example.ru"


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    with SessionLocal() as s:
        s.execute(delete(StyleRule))
        s.commit()


# --- фильтр запрещённого ---

@pytest.mark.parametrize("text", [
    "Обращаться на «вы»",
    "Начинать с «Добрый день»",
    "Не использовать оборот «в рамках»",
])
def test_allowed_rules(text):
    assert style.rejected_reason(text) is None


def test_numbers_rejected_even_in_length_rules():
    # Известное ограничение: «3–5 строк» отсекается вместе с суммами и датами.
    assert style.rejected_reason("Писать коротко, 3–5 строк") is not None


@pytest.mark.parametrize("text", [
    "Обращаться к Ивану Петровичу по имени-отчеству",
    "Напоминать про оплату 15 000 ₽",
    "Встречи назначать на 12 марта",
    "Копию слать на boss@example.ru",
    "x" * 200,
])
def test_rejected_rules(text):
    assert style.rejected_reason(text) is not None


def test_polite_you_is_not_a_name():
    assert style.rejected_reason("Писать Вы с заглавной буквы") is None


def test_parse_ops_tolerates_garbage():
    assert style.parse_ops('Вот: [{"op":"new","scope":"global","text":"Коротко"}] ок') == [
        {"op": "new", "scope": "global", "text": "Коротко"}
    ]
    assert style.parse_ops("не json") == []
    assert style.parse_ops('{"op": "hit"}') == []


# --- профиль ---

def _new(text, scope="global"):
    return {"op": "new", "scope": scope, "text": text}


def test_candidate_becomes_active_on_second_hit():
    with SessionLocal() as s:
        style.apply_ops(s, owner_id=OWNER, recipient="a@b.ru", ops=[_new("Писать коротко")], known_ids=set())
        s.commit()
        rule = s.query(StyleRule).one()
        assert rule.status == "candidate" and rule.hits == 1
        style.apply_ops(s, owner_id=OWNER, recipient="a@b.ru", ops=[{"op": "hit", "id": rule.id}], known_ids={rule.id})
        s.commit()
        assert s.get(StyleRule, rule.id).status == "active"


def test_same_text_merges_instead_of_duplicating():
    with SessionLocal() as s:
        style.apply_ops(s, owner_id=OWNER, recipient="", ops=[_new("Писать коротко.")], known_ids=set())
        style.apply_ops(s, owner_id=OWNER, recipient="", ops=[_new("писать  коротко")], known_ids=set())
        s.commit()
        assert s.query(StyleRule).count() == 1
        assert s.query(StyleRule).one().status == "active"


def test_hit_on_foreign_id_ignored():
    with SessionLocal() as s:
        other = StyleRule(owner_id="other@x.ru", scope="global", text="t", norm_text="t")
        s.add(other)
        s.commit()
        n = style.apply_ops(s, owner_id=OWNER, recipient="", ops=[{"op": "hit", "id": other.id}], known_ids=set())
        assert n == 0 and s.get(StyleRule, other.id).hits == 1


def test_recipient_rule_needs_recipient():
    with SessionLocal() as s:
        style.apply_ops(s, owner_id=OWNER, recipient="", ops=[_new("На ты", "recipient")], known_ids=set())
        s.commit()
        assert s.query(StyleRule).count() == 0


def test_active_rules_global_plus_recipient():
    with SessionLocal() as s:
        s.add_all([
            StyleRule(owner_id=OWNER, scope="global", text="Коротко", norm_text="коротко", status="active"),
            StyleRule(owner_id=OWNER, scope="recipient", recipient="a@b.ru", text="На ты", norm_text="на ты", status="active"),
            StyleRule(owner_id=OWNER, scope="recipient", recipient="c@d.ru", text="На вы", norm_text="на вы", status="active"),
            StyleRule(owner_id="other@x.ru", scope="global", text="Чужое", norm_text="чужое", status="active"),
        ])
        s.commit()
        rules = style.active_rules(s, owner_id=OWNER, recipient="A@B.ru")
    assert rules == ["Коротко", "(для этого адресата) На ты"]


def test_commands_remember_list_delete():
    reply = style.handle_command("/стиль запомни: Обращаться на «вы»", owner_id=OWNER)
    assert reply.startswith("Запомнил")
    reply = style.handle_command("/стиль запомни для A@B.ru: на «ты»", owner_id=OWNER)
    assert "a@b.ru" in reply
    listing = style.handle_command("/стиль", owner_id=OWNER)
    assert "1. Обращаться на «вы»" in listing and "2. на «ты»" in listing
    assert style.handle_command("/стиль удали 1", owner_id=OWNER).startswith("Удалил правило №1")
    assert "Обращаться" not in style.handle_command("/стиль", owner_id=OWNER)
    assert "нет" in style.handle_command("/стиль удали 9", owner_id=OWNER)


def test_command_rejects_facts():
    assert style.handle_command("/стиль запомни: счёт 15 000 ₽", owner_id=OWNER).startswith("Не запомнил")


def test_cap_on_manual_rules(monkeypatch):
    monkeypatch.setattr(style.settings, "style_max_global", 1)
    style.handle_command("/стиль запомни: Коротко", owner_id=OWNER)
    assert "Лимит" in style.handle_command("/стиль запомни: Без воды", owner_id=OWNER)


def test_learn_skips_when_user_wrote_nothing(monkeypatch):
    called = []

    async def fake(**kw):
        called.append(kw)
        return "[]"

    monkeypatch.setattr(style.llm, "extract_style", fake)
    import asyncio
    asyncio.run(style.learn(owner_id=OWNER, recipient="a@b.ru", user_texts=[], first_ai_body="A", sent_body="A"))
    assert called == []


def test_learn_never_receives_incoming_body(monkeypatch):
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return '[{"op":"new","scope":"global","text":"Писать коротко"}]'

    monkeypatch.setattr(style.llm, "extract_style", fake)
    import asyncio
    asyncio.run(style.learn(
        owner_id=OWNER, recipient="a@b.ru", user_texts=["короче"],
        first_ai_body="Добрый день. Длинный ответ.", sent_body="Добрый день.",
    ))
    assert set(seen) == {"user_texts", "edits", "existing"}
    with SessionLocal() as s:
        assert s.query(StyleRule).one().text == "Писать коротко"
