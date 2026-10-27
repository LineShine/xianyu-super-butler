"""Durable conversation state and entitlement ledger, independent of reply-log retention."""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base_class import Base


class CodeSelfServiceSession(Base):
    __tablename__ = "ytx_code_self_service_sessions"
    __table_args__ = (Index("uk_code_session_account_chat", "account_pk", "chat_id", unique=True),
                      {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"})

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    account_pk: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[str] = mapped_column(String(128))
    buyer_id: Mapped[str] = mapped_column(String(64))
    item_id: Mapped[str] = mapped_column(String(64))
    enabled_at_ms: Mapped[int] = mapped_column(BigInteger)
    started_ms: Mapped[int] = mapped_column(BigInteger)
    expires_ms: Mapped[int] = mapped_column(BigInteger)
    cooldown_until_ms: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(20), default="prompting")
    reminded: Mapped[bool] = mapped_column(Boolean, default=False)
    active_attempt_id: Mapped[str | None] = mapped_column(String(64))
    busy_until_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class CodeSelfServiceAttempt(Base):
    __tablename__ = "ytx_code_self_service_attempts"
    __table_args__ = (
        Index("idx_code_quota", "account_pk", "order_no", "quota_day", "quota_state"),
        Index("idx_code_echo", "account_pk", "chat_id", "marker_at_ms"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    # SHA-256(account PK, chat, source message ID/fingerprint); survives log deletion.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(32))
    owner_id: Mapped[int] = mapped_column(BigInteger)
    account_pk: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[str] = mapped_column(String(128))
    buyer_id: Mapped[str] = mapped_column(String(64))
    item_id: Mapped[str | None] = mapped_column(String(64))
    source_message_id: Mapped[str] = mapped_column(String(128))
    source_timestamp_ms: Mapped[int] = mapped_column(BigInteger)
    log_id: Mapped[int | None] = mapped_column(BigInteger)
    order_no: Mapped[str | None] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(32), default="received")
    reason: Mapped[str | None] = mapped_column(String(64))
    quota_day: Mapped[str | None] = mapped_column(String(10))
    # none/reserved/success/unknown/released. Only the middle three consume quota.
    quota_state: Mapped[str] = mapped_column(String(16), default="none")
    quota_used: Mapped[int | None] = mapped_column(BigInteger)
    marker_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    sent_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    finished_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_ms: Mapped[int] = mapped_column(BigInteger)
    busy_until_ms: Mapped[int] = mapped_column(BigInteger, default=0)
