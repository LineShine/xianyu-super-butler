"""
Token缓存模型

功能：
1. 存储IM Token和Device ID的缓存
2. 支持按user_id查询和更新
3. 记录过期时间，替代Redis缓存
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, event, inspect
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column

from common.db.base_class import Base, TimestampMixin
from common.security.vault import decrypt, encrypt_token, is_encrypted, token_context


class TokenCache(TimestampMixin, Base):
    """Token缓存表"""

    __tablename__ = "ytx_token_cache"
    __table_args__ = (
        Index("idx_token_cache_expiries", "expire_at", "renew_expire_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="主键ID")
    user_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, comment="用户ID（myid）")
    _token: Mapped[str] = mapped_column("token", Text, nullable=False, comment="加密IM Token")

    @hybrid_property
    def token(self):
        if not self._token:
            return self._token
        state = inspect(self)
        if not is_encrypted(self._token) and (state.transient or state.pending or state.attrs._token.history.has_changes()):
            return self._token
        return decrypt(self._token, token_context(self.user_id))

    @token.setter
    def token(self, value):
        self._token = value

    @token.expression
    def token(cls):
        return cls._token
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="设备ID")
    expire_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="过期时间")
    renew_expire_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="续期Token过期时间",
    )


event.listen(TokenCache, "before_insert", encrypt_token)
event.listen(TokenCache, "before_update", encrypt_token)
