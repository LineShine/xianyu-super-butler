"""
同意后发货 - 提货页发货落库服务

功能：
1. 读取订单快照并提供「同意后发货」所需字段（含 spec、同意状态等 compat 不返回的字段）
2. 挑选唯一匹配的自有卡券
3. 在同一事务内取卡内容（data 类型消费库存）并落库：写发货内容 + 同意标记 + 防重复标记 + 同步订单状态

说明：
- 确认发货 / 免拼发货由 websocket 账号实例的 handler 完成（需要在线实例与 cookie），
  本模块只负责「订单读取 + 选卡 + 取卡消费 + 落库」这类纯数据库操作。
- 取卡消费与订单落库放在同一事务：确认发货成功后再消费卡券，若落库失败则整体回滚，
  不会出现「卡券已消费但订单未记录」的错账。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.db.session import async_session_maker
from common.models.card import Card
from common.models.ytx_order import YtxOrder
from common.services.card_delivery_content import build_delivery_content
from common.services.delivery_utils import group_cards_by_source
from common.utils.time_utils import get_beijing_now_naive

# 同意后发货落库时「不覆盖为 shipped」的订单状态：终态 + 退款中。
# 这些状态代表平台侧更真实的最终结果，覆盖会丢失信息（与 order_service 的终态语义保持一致）。
_KEEP_STATUS_ON_DELIVER = {"shipped", "completed", "cancelled", "closed", "refunded", "refunding"}


async def read_order_snapshot(order_no: str) -> Optional[Dict[str, Any]]:
    """读取订单快照，返回同意后发货所需字段；订单不存在返回 None。

    Args:
        order_no: 闲鱼订单号
    Returns:
        含 id/item_id/buyer_id/is_bargain/quantity/spec_name/spec_value/
        buyer_fish_nick/account_id/status/agree_deliver_agreed/delivery_content 的字典
    """
    async with async_session_maker() as session:
        result = await session.execute(select(YtxOrder).where(YtxOrder.order_no == order_no))
        order = result.scalars().first()
        if not order:
            return None
        return {
            "id": order.id,
            "item_id": order.item_id,
            "buyer_id": order.buyer_id,
            "is_bargain": bool(order.is_bargain),
            "quantity": max(1, int(order.quantity or 1)),
            "spec_name": order.spec_name,
            "spec_value": order.spec_value,
            "buyer_fish_nick": order.buyer_fish_nick,
            "account_id": order.account_id,
            "status": order.status,
            "agree_deliver_agreed": bool(order.agree_deliver_agreed),
            "delivery_content": order.delivery_content,
        }


def pick_unique_own_card(cards: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """匹配到唯一自有卡券时返回，否则交由卖家检查关联和规格配置。"""
    cards = group_cards_by_source(cards).get("own", [])
    if len(cards) == 1:
        return cards[0], None
    if len(cards) > 1:
        logger.warning(f"【同意后发货】匹配到 {len(cards)} 张自有卡券，需要唯一匹配")
    return None, "暂无可用卡券，请联系卖家"


async def consume_card_and_record(
    order_no: str,
    card_id: int,
    quantity: int,
    context: Dict[str, str],
) -> Tuple[bool, str, Optional[str]]:
    """同一事务内取卡内容(消费库存)并落库；成功后订单标记为已同意+已处理+已发货。

    再次校验幂等：若订单已同意且已有发货内容，直接返回已有内容，不重复消费卡券。

    调用方（internal.py 的同意后发货接口）必须已经在平台侧「确认发货 / 免拼发货」成功，
    因此这里同步把本地状态更新为 shipped，避免本地与闲鱼平台状态不一致。

    Args:
        order_no: 闲鱼订单号
        card_id: 选定卡券主键
        quantity: 发货数量（>1 时取多份内容，text/image 已在上游退化为 1）
        context: 变量替换 / API 参数上下文
    Returns:
        (ok, message, content)
    """
    async with async_session_maker() as session:
        order = (
            await session.execute(select(YtxOrder).where(YtxOrder.order_no == order_no))
        ).scalars().first()
        if not order:
            return False, "订单不存在", None

        # 幂等：并发下另一路已完成发货，直接返回已有内容
        if order.agree_deliver_agreed and order.delivery_content:
            return True, "您已同意发货", order.delivery_content

        card = (
            await session.execute(select(Card).where(Card.id == card_id))
        ).scalars().first()
        if not card:
            return False, "卡券不存在，请联系卖家", None

        contents: List[str] = []
        for _ in range(max(1, quantity)):
            piece = await build_delivery_content(session, card, context)
            if not piece:
                # data 库存不足 / api 拉取失败：整体回滚，不消费不落库，交由买家重试或联系卖家
                await session.rollback()
                return False, "卡券库存不足或获取失败，请联系卖家", None
            contents.append(piece)

        content = "\n".join(contents)
        order.delivery_content = content
        order.agree_deliver_agreed = True
        order.agree_deliver_agreed_at = get_beijing_now_naive()
        # 复用「仅发卡券」的防重复语义：内容已取出即标记，定时补发货不再处理
        order.card_only_delivered = True
        # 同步订单状态为已发货：走到这里平台「确认发货」已真实成功，本地不同步会导致
        # 订单列表显示待发货、且依赖 status 的下游任务（如自动评价只捞 shipped/completed）
        # 要等到下一轮「定时获取闲鱼订单」才生效。终态/退款中的订单不覆盖。
        old_status = str(order.status or '') or '空'
        if old_status.lower() not in _KEEP_STATUS_ON_DELIVER:
            order.status = "shipped"
            logger.info(f"【同意后发货】订单 {order_no} 状态已同步为 shipped（原状态: {old_status}）")
        else:
            logger.info(f"【同意后发货】订单 {order_no} 当前状态 {old_status} 为终态/退款中，保留不覆盖")
        await session.commit()

    return True, "发货成功", content
