"""_collect_history на настоящей SQLite: выборка кандидатов, лимиты, ссылки."""
import datetime as dt

import pytest
from sqlalchemy import delete

from app import handlers
from app.db import Draft, SessionLocal, TrackedEmail, init_db

NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture(autouse=True)
def clean():
    init_db()
    with SessionLocal() as s:
        s.execute(delete(Draft))
        s.execute(delete(TrackedEmail))
        s.commit()


_uid = iter(range(1, 10_000))


def add(*, mid, sender="Иван <ivan@corp.example>", subject="Поставка серверов", body="текст",
        irt="", refs="", minutes_ago=60, account="a"):
    from app.thread_context import address_of, norm_subject
    with SessionLocal() as s:
        row = TrackedEmail(
            account=account, mailbox="INBOX", uid=next(_uid), rfc_message_id=mid, subject=subject,
            sender=sender, sender_addr=address_of(sender), norm_subject=norm_subject(subject),
            body_text=body, in_reply_to=irt, references=refs,
            created_at=NOW - dt.timedelta(minutes=minutes_ago),
            mail_date=NOW - dt.timedelta(minutes=minutes_ago),
        )
        s.add(row)
        s.commit()
        return row


def history_of(row, body="Ответ"):
    block, count, _ = handlers._collect_history(row, body)
    return block, count


def test_newest_candidate_wins_over_limit():
    # 250 писем от одного отправителя; нужное — самое новое.
    for i in range(250):
        add(mid=f"<old{i}@x>", subject=f"Тема {i}", body=f"старое {i}", minutes_ago=10_000 - i)
    add(mid="<need@x>", subject="Поставка серверов", body="НУЖНОЕ", minutes_ago=5)
    cur = add(mid="<cur@x>", irt="<need@x>", refs="<need@x>", minutes_ago=1)
    block, count = history_of(cur)
    assert "НУЖНОЕ" in block


def test_message_id_brackets_both_ways():
    add(mid="<a@x>", body="Со скобками в базе", minutes_ago=30)
    cur = add(mid="<c@x>", irt="a@x", refs="a@x", minutes_ago=1)
    assert "Со скобками в базе" in history_of(cur)[0]

    with SessionLocal() as s:
        s.execute(delete(TrackedEmail))
        s.commit()
    add(mid="b@x", body="Без скобок в базе", minutes_ago=30)
    cur = add(mid="<d@x>", irt="<b@x>", refs="<b@x>", minutes_ago=1)
    assert "Без скобок в базе" in history_of(cur)[0]


def test_refs_of_first_query_extend_search():
    # У текущего — неполные References. Промежуточное найдено по отправителю
    # и ссылается на раннее письмо с другой темой и другим отправителем-коллегой.
    add(mid="<early@x>", sender="Пётр <petr@corp.example>", subject="Совсем другая тема",
        body="РАННЕЕ", minutes_ago=300)
    add(mid="<mid@x>", subject="Промежуточное", irt="<early@x>", refs="<early@x>", body="ПРОМЕЖУТОЧНОЕ",
        minutes_ago=200)
    cur = add(mid="<cur@x>", irt="<mid@x>", refs="<mid@x>", minutes_ago=1)
    block, _ = history_of(cur)
    assert "РАННЕЕ" in block and "ПРОМЕЖУТОЧНОЕ" in block


def test_foreign_refs_do_not_extend_search():
    # Письмо от чужого (gmail) с References на нашу переписку с Анной не ведёт к ней.
    add(mid="<anna@x>", sender="Анна <anna@gmail.com>", subject="Личное", body="ПЕРЕПИСКА С АННОЙ",
        minutes_ago=300)
    cur_sender = "Олег <oleg@gmail.com>"
    add(mid="<o1@x>", sender="Мошенник <evil@gmail.com>", subject="Поставка серверов",
        refs="<anna@x>", body="подделка", minutes_ago=100)
    cur = add(mid="<cur@x>", sender=cur_sender, subject="Re: Поставка серверов", minutes_ago=1)
    assert "ПЕРЕПИСКА С АННОЙ" not in history_of(cur)[0]


def test_four_levels_with_three_rounds():
    # l1 ← l2 ← l3 ← l4 ← текущий, у всех разные темы, ссылки только на предыдущее.
    add(mid="<l1@x>", subject="Тема один", body="УРОВЕНЬ1", minutes_ago=400)
    add(mid="<l2@x>", subject="Тема два", irt="<l1@x>", refs="<l1@x>", body="УРОВЕНЬ2", minutes_ago=300)
    add(mid="<l3@x>", subject="Тема три", irt="<l2@x>", refs="<l2@x>", body="УРОВЕНЬ3", minutes_ago=200)
    add(mid="<l4@x>", subject="Тема четыре", irt="<l3@x>", refs="<l3@x>", body="УРОВЕНЬ4", minutes_ago=100)
    cur = add(mid="<cur@x>", sender="Другой <other@gmail.com>", subject="Тема пять",
              irt="<l4@x>", refs="<l4@x>", minutes_ago=1)
    # Отправитель текущего — другой (gmail), поэтому по собеседнику ничего не нашлось,
    # а ссылки чужих писем поиск не расширяют: найдено только то, на что ссылается текущий.
    block, _ = history_of(cur)
    assert "УРОВЕНЬ4" not in block          # и оно отсеяно same_party в collect

    with SessionLocal() as s:
        s.execute(delete(TrackedEmail).where(TrackedEmail.rfc_message_id == "<cur@x>"))
        s.commit()
    cur = add(mid="<cur2@x>", sender="Аноним <anon@corp2.example>", subject="Тема пять",
              irt="<l4@x>", refs="<l4@x>", minutes_ago=1)
    assert history_of(cur)[0] == ""


def test_four_levels_same_sender_found_by_sender_query():
    for n, ago in [(1, 400), (2, 300), (3, 200), (4, 100)]:
        prev = f"<l{n - 1}@x>" if n > 1 else ""
        add(mid=f"<l{n}@x>", subject=f"Тема {n}", irt=prev, refs=prev, body=f"УРОВЕНЬ{n}", minutes_ago=ago)
    cur = add(mid="<cur@x>", subject="Тема пять", irt="<l4@x>", refs="<l4@x>", minutes_ago=1)
    block, count = history_of(cur)
    # Все от того же собеседника — нашлись первым запросом, раунды не понадобились.
    assert all(f"УРОВЕНЬ{n}" in block for n in (1, 2, 3, 4)) and count == 4


def test_ref_rounds_limit_chain_through_colleagues():
    # Ссылки ведут через коллег (другие адреса того же домена) — первым запросом не найти,
    # только раундами по ссылкам. Раундов 3: уровни 4, 3, 2 находятся, уровень 1 — нет.
    for n, ago in [(1, 400), (2, 300), (3, 200), (4, 100)]:
        prev = f"<l{n - 1}@x>" if n > 1 else ""
        add(mid=f"<l{n}@x>", sender=f"Коллега{n} <c{n}@corp.example>", subject=f"Тема {n}",
            irt=prev, refs=prev, body=f"УРОВЕНЬ{n}", minutes_ago=ago)
    cur = add(mid="<cur@x>", subject="Тема пять", irt="<l4@x>", refs="<l4@x>", minutes_ago=1)
    block, _ = history_of(cur)
    assert all(f"УРОВЕНЬ{n}" in block for n in (2, 3, 4))
    assert "УРОВЕНЬ1" not in block


def test_same_domain_off_cuts_colleague(monkeypatch):
    monkeypatch.setattr(handlers.settings, "history_same_domain", False)
    add(mid="<a@x>", sender="Пётр <petr@corp.example>", body="ОТ КОЛЛЕГИ", minutes_ago=30)
    cur = add(mid="<c@x>", irt="<a@x>", refs="<a@x>", minutes_ago=1)
    assert "ОТ КОЛЛЕГИ" not in history_of(cur)[0]


def test_public_domains_extra(monkeypatch):
    monkeypatch.setattr(handlers.settings, "public_domains_extra", "corp.example")
    add(mid="<a@x>", sender="Пётр <petr@corp.example>", body="ОТ КОЛЛЕГИ", minutes_ago=30)
    cur = add(mid="<c@x>", irt="<a@x>", refs="<a@x>", minutes_ago=1)
    assert "ОТ КОЛЛЕГИ" not in history_of(cur)[0]


def test_purge_keeps_open_drafts_older_than_retention():
    cur = add(mid="<c@x>", minutes_ago=1)
    old = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    with SessionLocal() as s:
        for st in ("editing", "awaiting_text", "sending"):
            s.add(Draft(email_pk=cur.id, thread_id=1, kind="reply", status=st, body=f"ЖИВОЙ {st}",
                        source_text="s", history_text="h", updated_at=old))
        s.commit()
    handlers.purge_old_bodies(now=dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc))
    with SessionLocal() as s:
        for d in s.query(Draft).all():
            assert d.body.startswith("ЖИВОЙ") and d.source_text == "s" and d.history_text == "h"
