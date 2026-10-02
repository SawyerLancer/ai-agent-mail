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
