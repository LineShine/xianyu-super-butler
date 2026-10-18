"""自动续售成功后的商品关联迁移服务。"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.card_item_relation import CardItemRelation
from common.models.default_reply import DefaultReply
from common.models.relist_association_migration import RelistAssociationMigration
from common.models.ytx_account import YtxAccount
from common.models.ytx_catalog_item import YtxCatalogItem
from common.models.ytx_delivery_block_rule import YtxDeliveryBlockRule
from common.models.ytx_keyword_rule import YtxKeywordRule
from common.models.ytx_personal_blacklist import YtxPersonalBlacklist


class RelistAssociationMigrationService:
    """将持续配置幂等追加到新商品，支持失败后只补偿未完成步骤。"""

    STEPS = (
        "catalog_item",
        "card_relations",
        "keyword_rules",
        "default_replies",
        "ai_prompt",
        "delivery_exclusions",
        "personal_blacklist",
    )

    def __init__(self, session: AsyncSession):
        self.session = session

    async def migrate_after_relist(
        self,
        *,
        owner_id: int,
        account_id: str,
        material_id: int,
        old_item_id: str,
        new_item_id: str,
        event_id: int,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        """执行续售后关联迁移并返回每一步状态。"""
        del material_id, source_type
        account = (
            await self.session.execute(
                select(YtxAccount).where(YtxAccount.owner_id == owner_id, YtxAccount.account_id == account_id)
            )
        ).scalars().first()
        if not account:
            return {"success": False, "message": "续售关联迁移失败：闲鱼账号不存在", "steps": {}}

        handlers: dict[str, Callable[[], Awaitable[None]]] = {
            "catalog_item": lambda: self._ensure_catalog_item(owner_id, account.id, new_item_id),
            "card_relations": lambda: self._migrate_card_relations(owner_id, old_item_id, new_item_id),
            "keyword_rules": lambda: self._migrate_keyword_rules(owner_id, account.id, old_item_id, new_item_id),
            "default_replies": lambda: self._migrate_default_replies(account_id, old_item_id, new_item_id),
            "ai_prompt": lambda: self._migrate_ai_prompt(owner_id, account.id, old_item_id, new_item_id),
            "delivery_exclusions": lambda: self._migrate_delivery_exclusions(owner_id, account_id, old_item_id, new_item_id),
            "personal_blacklist": lambda: self._migrate_personal_blacklist(owner_id, account_id, old_item_id, new_item_id),
        }
        statuses: dict[str, str] = {}
        for step in self.STEPS:
            record = await self._get_step(event_id, owner_id, step)
            if record.status == "success":
                statuses[step] = "success"
                continue
            record.status = "running"
            record.attempt_count = int(record.attempt_count or 0) + 1
            try:
                await handlers[step]()
                record.status = "success"
                record.error_message = None
                statuses[step] = "success"
                await self.session.commit()
            except Exception as exc:
                await self.session.rollback()
                record = await self._get_step(event_id, owner_id, step)
                record.status = "failed"
                record.error_message = str(exc)[:1000]
                await self.session.commit()
                statuses[step] = "failed"
                logger.error("[自动续售] 关联迁移失败 event_id={} step={} error={}", event_id, step, exc)
                return {"success": False, "message": f"关联迁移失败（{step}）：{exc}", "steps": statuses}
        return {"success": True, "message": "关联迁移完成", "steps": statuses}

    async def _ensure_catalog_item(self, owner_id: int, account_pk: int, new_item_id: str) -> None:
        """确认发布后的新商品已同步到本地目录，避免关联迁移早于商品同步。"""
        exists = (
            await self.session.execute(
                select(YtxCatalogItem.id).where(
                    YtxCatalogItem.owner_id == owner_id,
                    YtxCatalogItem.account_pk == account_pk,
                    YtxCatalogItem.item_id == new_item_id,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise ValueError("新商品目录尚未同步，稍后重试关联迁移")

    async def _get_step(self, event_id: int, owner_id: int, step: str) -> RelistAssociationMigration:
        record = (
            await self.session.execute(
                select(RelistAssociationMigration)
                .where(
                    RelistAssociationMigration.event_id == event_id,
                    RelistAssociationMigration.user_id == owner_id,
                    RelistAssociationMigration.step == step,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record:
            return record
        record = RelistAssociationMigration(event_id=event_id, user_id=owner_id, step=step, status="pending")
        self.session.add(record)
        try:
            await self.session.flush()
            return record
        except IntegrityError:
            # 同一事件的补偿任务可能在极端并发下同时初始化步骤；唯一键竞争时
            # 回滚临时插入并读取已提交记录，避免把可恢复竞争误报为迁移失败。
            await self.session.rollback()
            existing = (
                await self.session.execute(
                    select(RelistAssociationMigration)
                    .where(
                        RelistAssociationMigration.event_id == event_id,
                        RelistAssociationMigration.user_id == owner_id,
                        RelistAssociationMigration.step == step,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing:
                return existing
            raise

    async def _migrate_card_relations(self, owner_id: int, old_item_id: str, new_item_id: str) -> None:
        rows = (
            await self.session.execute(
                select(CardItemRelation).where(
                    CardItemRelation.user_id == owner_id,
                    CardItemRelation.item_id == old_item_id,
                )
            )
        ).scalars().all()
        for row in rows:
            exists = (
                await self.session.execute(
                    select(CardItemRelation.id).where(
                        CardItemRelation.user_id == owner_id,
                        CardItemRelation.card_id == row.card_id,
                        CardItemRelation.item_id == new_item_id,
                    )
                )
            ).scalar_one_or_none()
            if not exists:
                self.session.add(
                    CardItemRelation(
                        user_id=row.user_id,
                        card_id=row.card_id,
                        item_id=new_item_id,
                        source="own",
                    )
                )
        await self.session.flush()

    async def _migrate_keyword_rules(self, owner_id: int, account_pk: int, old_item_id: str, new_item_id: str) -> None:
        rows = (
            await self.session.execute(
                select(YtxKeywordRule).where(
                    YtxKeywordRule.owner_id == owner_id,
                    YtxKeywordRule.account_pk == account_pk,
                    YtxKeywordRule.item_id == old_item_id,
                )
            )
        ).scalars().all()
        for row in rows:
            exists = (
                await self.session.execute(
                    select(YtxKeywordRule.id).where(
                        YtxKeywordRule.owner_id == owner_id,
                        YtxKeywordRule.account_pk == account_pk,
                        YtxKeywordRule.item_id == new_item_id,
                        YtxKeywordRule.keyword == row.keyword,
                    )
                )
            ).scalar_one_or_none()
            if not exists:
                self.session.add(
                    YtxKeywordRule(
                        owner_id=owner_id,
                        account_pk=account_pk,
                        keyword=row.keyword,
                        reply_content=row.reply_content,
                        reply_type=row.reply_type,
                        image_url=row.image_url,
                        item_id=new_item_id,
                        priority=row.priority,
                        is_active=row.is_active,
                    )
                )
        await self.session.flush()

    async def _migrate_default_replies(self, account_id: str, old_item_id: str, new_item_id: str) -> None:
        rows = (
            await self.session.execute(
                select(DefaultReply).where(DefaultReply.account_id == account_id, DefaultReply.item_id == old_item_id)
            )
        ).scalars().all()
        for row in rows:
            exists = (
                await self.session.execute(
                    select(DefaultReply.id).where(DefaultReply.account_id == account_id, DefaultReply.item_id == new_item_id)
                )
            ).scalar_one_or_none()
            if not exists:
                self.session.add(
                    DefaultReply(
                        account_id=account_id,
                        item_id=new_item_id,
                        enabled=row.enabled,
                        reply_type=row.reply_type,
                        reply_content=row.reply_content,
                        reply_image=row.reply_image,
                        api_url=row.api_url,
                        api_timeout=row.api_timeout,
                        reply_once=row.reply_once,
                    )
                )
        await self.session.flush()

    async def _migrate_ai_prompt(self, owner_id: int, account_pk: int, old_item_id: str, new_item_id: str) -> None:
        old_item = (
            await self.session.execute(
                select(YtxCatalogItem).where(
                    YtxCatalogItem.owner_id == owner_id,
                    YtxCatalogItem.account_pk == account_pk,
                    YtxCatalogItem.item_id == old_item_id,
                )
            )
        ).scalar_one_or_none()
        new_item = (
            await self.session.execute(
                select(YtxCatalogItem).where(
                    YtxCatalogItem.owner_id == owner_id,
                    YtxCatalogItem.account_pk == account_pk,
                    YtxCatalogItem.item_id == new_item_id,
                )
            )
        ).scalar_one_or_none()
        if old_item and old_item.ai_prompt:
            if not new_item:
                raise ValueError("新商品目录尚未同步，稍后重试关联迁移")
            new_item.ai_prompt = old_item.ai_prompt
            await self.session.flush()

    async def _migrate_delivery_exclusions(self, owner_id: int, account_id: str, old_item_id: str, new_item_id: str) -> None:
        account = (
            await self.session.execute(select(YtxAccount).where(YtxAccount.owner_id == owner_id, YtxAccount.account_id == account_id))
        ).scalars().first()
        if account:
            values = list(account.delivery_disabled_excluded_items or [])
            if old_item_id in values and new_item_id not in values:
                account.delivery_disabled_excluded_items = values + [new_item_id]
        rules = (
            await self.session.execute(select(YtxDeliveryBlockRule).where(YtxDeliveryBlockRule.account_id == account_id))
        ).scalars().all()
        for rule in rules:
            values = list(rule.excluded_item_ids or [])
            if old_item_id in values and new_item_id not in values:
                rule.excluded_item_ids = values + [new_item_id]
        await self.session.flush()

    async def _migrate_personal_blacklist(self, owner_id: int, account_id: str, old_item_id: str, new_item_id: str) -> None:
        rows = (
            await self.session.execute(
                select(YtxPersonalBlacklist).where(
                    YtxPersonalBlacklist.owner_id == owner_id,
                    YtxPersonalBlacklist.account_id == account_id,
                    YtxPersonalBlacklist.item_id == old_item_id,
                )
            )
        ).scalars().all()
        for row in rows:
            exists = (
                await self.session.execute(
                    select(YtxPersonalBlacklist.id).where(
                        YtxPersonalBlacklist.owner_id == owner_id,
                        YtxPersonalBlacklist.account_id == account_id,
                        YtxPersonalBlacklist.buyer_id == row.buyer_id,
                        YtxPersonalBlacklist.item_id == new_item_id,
                    )
                )
            ).scalar_one_or_none()
            if not exists:
                self.session.add(
                    YtxPersonalBlacklist(
                        owner_id=owner_id,
                        account_id=account_id,
                        buyer_id=row.buyer_id,
                        buyer_nick=row.buyer_nick,
                        item_id=new_item_id,
                        reason=row.reason,
                        is_enabled=row.is_enabled,
                    )
                )
        await self.session.flush()
