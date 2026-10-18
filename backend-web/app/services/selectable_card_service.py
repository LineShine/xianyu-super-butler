"""商品自有卡券的分页选择服务。"""
from typing import Any, Dict, List, Optional
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from common.models.card import Card

class SelectableCardService:
    _COLUMNS = (Card.id, Card.name, Card.type, Card.is_multi_spec, Card.spec_name, Card.spec_value, Card.enabled)
    def __init__(self, session: AsyncSession):
        self.session = session
    def conditions(self, user_id, search):
        filters = [] if user_id is None else [Card.user_id == user_id]
        if search:
            q = f"%{search}%"
            filters.append(or_(Card.name.ilike(q), Card.description.ilike(q), Card.type.ilike(q), cast(Card.id, String).ilike(q)))
        return filters
    def item(self, card):
        return {"id": card.id, "name": card.name, "type": card.type, "source": "own",
                "unique_key": f"own_{card.id}", "enabled": card.enabled,
                "is_multi_spec": card.is_multi_spec, "spec_name": card.spec_name, "spec_value": card.spec_value}
    async def get_selectable_cards_paginated(self, item_id: str, user_id: Optional[int], page=1, page_size=50, search="") -> Dict[str, Any]:
        filters = self.conditions(user_id, search)
        total = (await self.session.execute(select(func.count(Card.id)).where(*filters))).scalar() or 0
        rows = (await self.session.execute(select(Card).where(*filters).options(load_only(*self._COLUMNS)).order_by(Card.id.desc()).offset((page-1)*page_size).limit(page_size))).scalars().all()
        return {"list": [self.item(c) for c in rows], "total": total, "page": page, "page_size": page_size, "total_pages": (total+page_size-1)//page_size}
    async def get_all_selectable_card_keys(self, user_id: Optional[int], search="") -> List[Dict[str, Any]]:
        rows = (await self.session.execute(select(Card).where(*self.conditions(user_id, search)).options(load_only(*self._COLUMNS)).order_by(Card.id.desc()))).scalars().all()
        return [self.item(c) for c in rows]
