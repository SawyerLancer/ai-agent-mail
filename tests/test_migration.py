import sqlite3

from app import db


def test_old_draft_table_gets_new_columns():
    path = db.settings.db_path
    db._engine.dispose()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE IF EXISTS draft")
    con.execute(
        "CREATE TABLE draft (id INTEGER PRIMARY KEY, email_pk INTEGER, thread_id BIGINT, "
        "kind VARCHAR(16), status VARCHAR(16), body TEXT, recipients TEXT, "
        "preview_message_id BIGINT, updated_at DATETIME)"
    )
    con.execute("INSERT INTO draft (email_pk, thread_id, kind, status, body, recipients) "
                "VALUES (1, 2, 'reply', 'editing', 'старый', '')")
    con.commit()
    con.close()

    db.init_db()
    db.init_db()   # повторный запуск ничего не ломает

    with db.SessionLocal() as s:
        d = s.get(db.Draft, 1)
        assert d.body == "старый"
        assert d.user_texts == "[]" and d.added_facts == "[]" and d.original_text == ""


def test_old_tracked_email_gets_thread_columns():
    db._engine.dispose()
    con = sqlite3.connect(db.settings.db_path)
    con.execute("DROP TABLE IF EXISTS tracked_email")
    con.execute(
        "CREATE TABLE tracked_email (id INTEGER PRIMARY KEY, account VARCHAR(255), mailbox VARCHAR(255), "
        "uid BIGINT, rfc_message_id VARCHAR(998), subject TEXT, sender TEXT, pachca_message_id BIGINT, "
        "thread_id BIGINT, thread_chat_id BIGINT, created_at DATETIME)"
    )
    con.execute("INSERT INTO tracked_email (account, mailbox, uid, rfc_message_id, subject, sender) "
                "VALUES ('test', 'INBOX', 1, '<a@x>', 'RE: Поставка', 'Иван <Ivan@X.ru>')")
    con.commit()
    con.close()

    db.init_db()
    with db.SessionLocal() as s:
        e = s.query(db.TrackedEmail).one()
        # Тела и заголовков у старых писем нет и не будет — в историю они не попадут.
        assert e.body_text == "" and e.references == "" and e.norm_subject == ""
