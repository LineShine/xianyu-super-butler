"""卡券模型"""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship
from common.db.base_class import Base

class Card(Base):
    """卡券表"""
    __tablename__ = 'ytx_cards'
    __table_args__ = (Index('idx_cards_user_id_desc', 'user_id', 'id'), Index('idx_cards_user_enabled', 'user_id', 'enabled'))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment='卡券ID')
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment='所属用户ID')
    item_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True, comment='关联商品ID')
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment='卡券名称')
    type: Mapped[str] = mapped_column(String(50), nullable=False, comment='卡券类型(api/text/data/image)')
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment='卡券描述')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment='是否启用')
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0, comment='延迟秒数')
    use_no_logistics_form: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='0', comment='是否通过无需邮寄表单发货')
    delivery_count: Mapped[int] = mapped_column(Integer, default=0, comment='发货次数')
    is_multi_spec: Mapped[bool] = mapped_column(Boolean, default=False, comment='是否多规格')
    spec_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment='规格名称')
    spec_value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment='规格值')
    api_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment='API配置(JSON)')
    text_content: Mapped[Optional[str]] = mapped_column(LONGTEXT, nullable=True, comment='文本卡券内容')
    data_content: Mapped[Optional[str]] = mapped_column(LONGTEXT, nullable=True, comment='批量数据（卡密）内容')
    image_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, comment='图片URL')
    image_urls: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment='多图片URL列表(JSON数组，最多3张)')
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), comment='更新时间')
    user = relationship('User', primaryjoin='Card.user_id == User.id', foreign_keys='[Card.user_id]', back_populates='cards', viewonly=True)
    item_relations = relationship('CardItemRelation', primaryjoin='Card.id == CardItemRelation.card_id', foreign_keys='[CardItemRelation.card_id]', viewonly=True, lazy='select')
