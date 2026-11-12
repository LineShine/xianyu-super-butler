"""Isolated catalog regression tests; platform and credential failures are mocked."""
import copy
import json
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from common.models.ytx_catalog_item import YtxCatalogItem
from common.security.vault import CredentialUnavailable
from common.services.item_service import ItemService, ItemSyncCapabilityError


class ItemSyncCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        YtxCatalogItem.__table__.create(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.session = SimpleNamespace(
            execute=AsyncMock(side_effect=self.db.execute),
            commit=AsyncMock(side_effect=self.db.commit),
            rollback=AsyncMock(side_effect=self.db.rollback),
            add=self.db.add,
        )
        self.service = ItemService(self.session)
        self.account = SimpleNamespace(
            id=1, owner_id=1, account_id="fixture-account", unb="1001",
            cookie="unb=1001; fixture_cookie=value", status="active",
        )
        self.seller_detail = {
            "id": "3001", "source": "seller", "title": "卖家商品",
            "price_text": "9.99", "quantity": 8, "gmt_shelf": "2026-11-01 10:00:00",
            "item_status_desc": "在售", "idle_item_sku_list": [{"sku_id": "sku-1", "quantity": 8}],
        }
        self.personal_detail = {"id": "3001", "title": "个人商品", "price_text": "12.00", "card_type": 1003}
        self.item = YtxCatalogItem(
            id=1, account_pk=1, owner_id=1, item_id="3001", title="卖家商品", price="9.99",
            metadata_json={"detail": json.dumps(self.seller_detail), "category": "kept-category", "code_generation_enabled": True},
            created_at=datetime.now(), updated_at=datetime.now(),
        )
        self.db.add(self.item)
        self.db.commit()

    def manager(self, item):
        return SimpleNamespace(
            get_item_list_info=AsyncMock(return_value={"success": True, "items": [item], "current_count": 1}),
            close=AsyncMock(),
        )

    def assert_original_item(self):
        self.db.expire_all()
        self.assertEqual(self.item.title, "卖家商品")
        self.assertEqual(self.item.price, "9.99")
        self.assertEqual(json.loads(self.item.metadata_json["detail"]), self.seller_detail)
        self.assertTrue(self.item.metadata_json["code_generation_enabled"])
        self.assertEqual(self.account.status, "active")

    async def test_failed_or_unreliable_detection_never_fetches_or_overwrites(self):
        cases = [
            FileNotFoundError("missing backend capability module"),
            {"success": False, "message": "检测接口暂不可用"},
            {"success": True, "is_fish_shop": False, "detection_reliable": False,
             "detection_unreliable_reason": "FAIL_SYS_FLOW_LIMITED"},
            {"success": True, "is_fish_shop": True, "detection_reliable": False},
            {"success": True, "is_fish_shop": False},
            {"success": True, "is_fish_shop": "false", "detection_reliable": True},
            None,
        ]
        for method in ("fetch_items_page_from_account", "_fetch_all_items_from_account_impl"):
            for response in cases:
                with self.subTest(method=method, response=response):
                    detection = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
                    with patch("common.services.xianyu_publish_service.detect_publish_account_capability", detection), \
                         patch("common.utils.item_info_manager.ItemInfoManager") as personal, \
                         patch("common.services.xianyu_seller_item_client.SellerItemInfoManager") as seller:
                        result = await getattr(self.service, method)(self.account)
                    self.assertFalse(result["success"])
                    self.assertIn("本次未同步", result["message"])
                    personal.assert_not_called()
                    seller.assert_not_called()
                    self.session.commit.assert_not_awaited()
                    self.assertNotIn(self.account.account_id, self.service._fish_shop_cache)
                    self.assert_original_item()

    async def test_decryption_failure_is_not_an_ordinary_account_or_disabled(self):
        with patch("common.services.xianyu_publish_service.detect_publish_account_capability",
                   side_effect=CredentialUnavailable("凭据密钥不可用")), \
             patch("common.utils.item_info_manager.ItemInfoManager") as personal:
            with self.assertRaises(CredentialUnavailable):
                await self.service._detect_is_fish_shop(self.account)
            result = await self.service.fetch_items_page_from_account(self.account)
        self.assertFalse(result["success"])
        self.assertIn("凭据密钥不可用", result["message"])
        personal.assert_not_called()
        self.assert_original_item()

    async def test_cookie_read_failure_stops_before_detection(self):
        class UnreadableAccount:
            account_id = "fixture-account"
            @property
            def cookie(self):
                raise CredentialUnavailable("凭据密钥不可用")
        with patch("common.services.xianyu_publish_service.detect_publish_account_capability") as detect, \
             patch("common.utils.item_info_manager.ItemInfoManager") as personal:
            result = await self.service._fetch_all_items_from_account_impl(UnreadableAccount())
        self.assertFalse(result["success"])
        detect.assert_not_called()
        personal.assert_not_called()
        self.assert_original_item()

    async def test_reliable_personal_account_can_replace_previous_seller_snapshot(self):
        manager = self.manager(self.personal_detail)
        with patch("common.services.xianyu_publish_service.detect_publish_account_capability", AsyncMock(
            return_value={"success": True, "is_fish_shop": False, "detection_reliable": True})), \
             patch("common.utils.item_info_manager.ItemInfoManager", return_value=manager) as personal, \
             patch("common.services.xianyu_seller_item_client.SellerItemInfoManager") as seller:
            result = await self.service.fetch_items_page_from_account(self.account)
        self.assertTrue(result["success"])
        self.assertEqual(result["saved_count"], 1)
        personal.assert_called_once()
        seller.assert_not_called()
        manager.close.assert_awaited_once()
        self.db.expire_all()
        self.assertEqual(json.loads(self.item.metadata_json["detail"]), self.personal_detail)
        self.assertFalse(self.service._serialize_item(self.item, self.account.account_id)["is_seller_item"])

    async def test_reliable_seller_account_updates_stock_status_and_edit_capability(self):
        fetched = {**self.seller_detail, "quantity": 0, "item_status_desc": "售罄"}
        manager = self.manager(fetched)
        with patch("common.services.xianyu_publish_service.detect_publish_account_capability", AsyncMock(
            return_value={"success": True, "is_fish_shop": True, "detection_reliable": True})), \
             patch("common.utils.item_info_manager.ItemInfoManager") as personal, \
             patch("common.services.xianyu_seller_item_client.SellerItemInfoManager", return_value=manager) as seller:
            result = await self.service._fetch_all_items_from_account_impl(self.account)
        self.assertTrue(result["success"])
        self.assertEqual(result["saved_count"], 1)
        personal.assert_not_called()
        seller.assert_called_once()
        manager.close.assert_awaited_once()
        self.db.expire_all()
        serialized = self.service._serialize_item(self.item, self.account.account_id)
        self.assertEqual(serialized["item_quantity"], 0)
        self.assertEqual(serialized["item_status_desc"], "售罄")
        self.assertEqual(serialized["item_shelf_time"], self.seller_detail["gmt_shelf"])
        self.assertTrue(serialized["is_seller_item"])

    async def test_weak_personal_snapshot_without_reliable_detection_cannot_overwrite(self):
        for raw_detail in (json.dumps(self.seller_detail), copy.deepcopy(self.seller_detail)):
            with self.subTest(detail_type=type(raw_detail).__name__):
                self.item.metadata_json = {"detail": raw_detail}
                self.db.commit()
                with self.assertRaises(ItemSyncCapabilityError):
                    await self.service.save_fetched_items(self.account, [self.personal_detail])
                self.db.expire_all()
                self.assertEqual(self.service._parse_item_detail(self.item.metadata_json["detail"]), self.seller_detail)
                self.assertEqual(self.item.title, "卖家商品")

    async def test_sparse_seller_snapshot_preserves_missing_fields_but_accepts_zero_and_empty_skus(self):
        self.item.metadata_json = {"detail": copy.deepcopy(self.seller_detail), "category": "kept-category"}
        self.db.commit()
        saved, _ = await self.service.save_fetched_items(self.account, [
            {"id": "3001", "source": "seller", "quantity": 0, "idle_item_sku_list": []},
        ])
        self.assertEqual(saved, 1)
        self.db.expire_all()
        serialized = self.service._serialize_item(self.item, self.account.account_id)
        self.assertEqual(serialized["item_quantity"], 0)
        self.assertEqual(serialized["item_sku_list"], [])
        self.assertEqual(serialized["item_status_desc"], "在售")
        self.assertEqual(serialized["item_shelf_time"], self.seller_detail["gmt_shelf"])
        self.assertEqual(self.item.title, "卖家商品")
        self.assertEqual(self.item.price, "9.99")
        self.assertEqual(self.item.metadata_json["category"], "kept-category")
        self.assertTrue(serialized["is_seller_item"])

    async def test_structured_and_string_seller_details_serialize_equally(self):
        self.item.metadata_json = {"detail": json.dumps(self.seller_detail)}
        original = self.service._serialize_item(self.item, self.account.account_id)
        self.item.metadata_json = {"detail": copy.deepcopy(self.seller_detail)}
        structured = self.service._serialize_item(self.item, self.account.account_id)
        self.assertIsInstance(structured["item_detail"], str)
        self.assertEqual(json.loads(structured["item_detail"]), self.seller_detail)
        for name in ("is_seller_item", "item_quantity", "item_status_desc", "item_shelf_time", "item_sku_list"):
            self.assertEqual(structured[name], original[name])


if __name__ == "__main__":
    unittest.main()
