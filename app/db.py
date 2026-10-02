"""Состояние: точка поллинга, карта письмо↔сообщение, черновики, дедупликация."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    BigInteger, DateTime, Integer, String, Text, UniqueConstraint, create_engine, inspect,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PollState(Base):
    """Одна строка на (аккаунт, папка): докуда дочитали."""
    __tablename__ = "poll_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(255))
    mailbox: Mapped[str] = mapped_column(String(255))
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    # отпечаток ящика: если провайдер пересоздал папку, UID обнулятся
    uid_fingerprint: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("account", "mailbox", name="uq_poll_state"),)


class TrackedEmail(Base):
    """Письмо, о котором уже сообщили в Пачку."""
    __tablename__ = "tracked_email"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(255))
    mailbox: Mapped[str] = mapped_column(String(255))
    uid: Mapped[int] = mapped_column(BigInteger)
    rfc_message_id: Mapped[str] = mapped_column(String(998), default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    sender: Mapped[str] = mapped_column(Text, default="")
    # сообщение-карточка в Пачке и тред под ней
    pachca_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    thread_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    thread_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("account", "mailbox", "uid", name="uq_tracked_uid"),
        UniqueConstraint("account", "rfc_message_id", name="uq_tracked_rfc"),
    )


class Draft(Base):
    """Черновик ответа или пересылки, ожидающий подтверждения в треде."""
    __tablename__ = "draft"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_pk: Mapped[int] = mapped_column(Integer, index=True)
    thread_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))       # reply | forward
    # editing | awaiting_text (ждём «свой текст») | sending (ушло в MCP, ждём ответа)
    # | sent | cancelled | expired
    status: Mapped[str] = mapped_column(String(16), default="editing")
    body: Mapped[str] = mapped_column(Text, default="")
    recipients: Mapped[str] = mapped_column(Text, default="")  # для forward, через запятую
    # последнее сообщение бота с превью — чтобы гасить у него кнопки
    preview_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # от последней правки, а не от создания, отсчитывается срок жизни черновика
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    # «Свой текст» дословно — для кнопки «Без правок»; пусто, если тело от модели
    original_text: Mapped[str] = mapped_column(Text, default="")
    # первый вариант ИИ: разница с отправленным — материал для памяти стиля
    first_ai_body: Mapped[str] = mapped_column(Text, default="")
    # тело входящего письма (обрезанное) — источник для проверки фактов.
    # В обучение стилю не передаётся никогда: это недоверенный текст.
    source_text: Mapped[str] = mapped_column(Text, default="")
    # JSON-список текстов пользователя: правки из треда и «Свой текст»
    user_texts: Mapped[str] = mapped_column(Text, default="[]")
    # кто автор текущего body: ai (draft_reply/revise) или human («Свой текст»,
    # «Без правок»). Правки черновика учим только у human (skill style-memory).
    body_source: Mapped[str] = mapped_column(String(8), default="ai")
    # JSON-список того, что модель добавила от себя (для «⚠️» в превью)
    added_facts: Mapped[str] = mapped_column(Text, default="[]")


class StyleRule(Base):
    """Правило стиля пользователя: как он пишет, а не о чём.

    owner_id — владелец профиля (сейчас адрес ящика). Везде передаётся явно,
    чтобы потом развести профили нескольких людей без переделки.
    """
    __tablename__ = "style_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(320), index=True)
    scope: Mapped[str] = mapped_column(String(16))              # global | recipient
    recipient: Mapped[str] = mapped_column(String(320), default="")
    text: Mapped[str] = mapped_column(Text)
    norm_text: Mapped[str] = mapped_column(String(255))         # для отсева точных дублей
    hits: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="candidate")  # candidate | active
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "scope", "recipient", "norm_text", name="uq_style_rule"),
    )


class SeenEvent(Base):
    """Дедупликация вебхуков: доставка at-least-once."""
    __tablename__ = "seen_event"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


_engine = create_engine(f"sqlite:///{settings.db_path}", future=True)
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(_engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Мини-миграция: create_all не добавляет колонки в существующие таблицы.

    Только добавление столбцов с простым значением по умолчанию — для
    переименований и смены типов этого мало, тогда пора брать Alembic.
    """
    insp = inspect(_engine)
    with _engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} " + col.type.compile(
                    dialect=_engine.dialect
                )
                default = col.default.arg if col.default is not None and col.default.is_scalar else None
                if isinstance(default, str):
                    ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                elif isinstance(default, int):
                    ddl += f" DEFAULT {default}"
                conn.exec_driver_sql(ddl)


def already_seen(s: Session, key: str) -> bool:
    """True, если событие с таким ключом уже обрабатывали."""
    if s.get(SeenEvent, key) is not None:
        return True
    s.add(SeenEvent(key=key))
    s.commit()
    return False


def get_poll_state(s: Session, account: str, mailbox: str) -> PollState:
    st = s.scalar(
        select(PollState).where(PollState.account == account, PollState.mailbox == mailbox)
    )
    if st is None:
        st = PollState(account=account, mailbox=mailbox, last_uid=0)
        s.add(st)
        s.commit()
    return st
