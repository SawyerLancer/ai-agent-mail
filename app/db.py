"""Состояние: точка поллинга, карта письмо↔сообщение, черновики, дедупликация."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    BigInteger, DateTime, Integer, String, Text, UniqueConstraint, create_engine, select,
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
    # editing | awaiting_text (ждём «свой текст») | sent | cancelled | expired
    status: Mapped[str] = mapped_column(String(16), default="editing")
    body: Mapped[str] = mapped_column(Text, default="")
    recipients: Mapped[str] = mapped_column(Text, default="")  # для forward, через запятую
    # последнее сообщение бота с превью — чтобы гасить у него кнопки
    preview_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # от последней правки, а не от создания, отсчитывается срок жизни черновика
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
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
