"""Local SQL + mocked HTTP tests; never contact a live account."""
import time
from datetime import datetime
from contextlib import asynccontextmanager
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import deps
from app.api.routes.items import items_router
from app.api.routes.chat_new import SendMessageRequest, send_message
from app.services.account_service import AccountService
from app.services.auto_reply_log_service import AutoReplyLogService
from common.models.auto_reply_message_log import YtxAutoReplyMessageLog as ReplyLog
from common.models.user import UserRole
from common.models.ytx_account import YtxAccount
from common.models.ytx_catalog_item import YtxCatalogItem
from common.services.code_generation_config import ENABLED_KEY, ENABLED_AT_KEY
from common.services.item_service import ItemService


class ItemCodeGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        for model in (YtxAccount, YtxCatalogItem, ReplyLog):
            model.__table__.create(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.session = SimpleNamespace(execute=AsyncMock(side_effect=self.db.execute),
            commit=AsyncMock(side_effect=self.db.commit), rollback=AsyncMock(side_effect=self.db.rollback),
            add=self.db.add, flush=AsyncMock(side_effect=self.db.flush))
        self.items = ItemService(self.session)
        self.a = YtxAccount(id=1, account_id='1001', owner_id=1, cookie='fixture', login_method='test', status='active')
        self.b = YtxAccount(id=2, account_id='1002', owner_id=2, cookie='fixture', login_method='test', status='active')
        self.item = YtxCatalogItem(id=1, owner_id=1, account_pk=1, item_id='3001', title='测试商品', created_at=datetime.now(), updated_at=datetime.now())
        self.other = YtxCatalogItem(id=2, owner_id=2, account_pk=2, item_id='3002', title='其他商品', created_at=datetime.now(), updated_at=datetime.now())
        self.db.add_all([self.a, self.b, self.item, self.other])
        self.db.commit()

    async def test_default_off_toggle_persistence_and_enable_epoch(self):
        self.assertFalse(self.items._serialize_item(self.item, '1001')['code_generation_enabled'])
        with patch('common.services.code_generation_config.time.time', return_value=1700000000):
            self.assertTrue(await self.items.set_code_generation(self.a, '3001', True))
        first = self.item.metadata_json[ENABLED_AT_KEY]
        await self.items.set_code_generation(self.a, '3001', True)
        self.assertEqual(self.item.metadata_json[ENABLED_AT_KEY], first)
        self.db.expire_all()
        self.assertTrue(self.items._serialize_item(self.item, '1001')['code_generation_enabled'])
        self.assertFalse(self.items._serialize_item(self.other, '1002')['code_generation_enabled'])
        await self.items.set_code_generation(self.a, '3001', False)
        await self.items.set_code_generation(self.a, '3001', True)
        self.assertGreater(self.item.metadata_json[ENABLED_AT_KEY], first)

    async def test_sync_and_other_item_edits_keep_configuration(self):
        await self.items.set_code_generation(self.a, '3001', True)
        since = self.item.metadata_json[ENABLED_AT_KEY]
        await self.items._apply_single_item(self.a, '3001', {'title': '同步标题', 'price_text': '9.99', 'category_id': '5'})
        self.db.commit()
        await self.items.update_item(self.a, '3001', {'item_description': '新的描述'})
        self.db.expire_all()
        self.assertTrue(self.item.metadata_json[ENABLED_KEY])
        self.assertEqual(self.item.metadata_json[ENABLED_AT_KEY], since)
        self.assertEqual(self.item.title, '同步标题')

    async def test_cross_account_item_cannot_be_toggled(self):
        self.assertFalse(await self.items.set_code_generation(self.a, '3002', True))
        self.assertFalse(self.items._serialize_item(self.other, '1002')['code_generation_enabled'])

    async def test_legacy_detail_sync_keeps_switch_and_enable_time(self):
        from common.db.compat import DBManagerCompat
        await self.items.set_code_generation(self.a, '3001', True)
        since = self.item.metadata_json[ENABLED_AT_KEY]
        @asynccontextmanager
        async def sessions():
            yield self.session
        compat = DBManagerCompat()
        # Capture the compatibility bridge's coroutine and execute it against isolated SQL.
        with patch.object(compat, '_run_async', side_effect=lambda callback: callback):
            save = compat.save_item_info('1001', '3001', {'title': '旧接口同步'})
            self.assertTrue(await save(sessions))
            update = compat.update_item_detail('1001', '3001', '{"detail":"new"}')
            self.assertTrue(await update(sessions))
        self.db.expire_all()
        self.assertTrue(self.item.metadata_json[ENABLED_KEY])
        self.assertEqual(self.item.metadata_json[ENABLED_AT_KEY], since)
        self.assertEqual(self.item.metadata_json['detail'], '{"detail":"new"}')

    async def test_http_validates_owner_item_and_strict_boolean(self):
        app = FastAPI()
        app.include_router(items_router, prefix='/api/v1')
        user = SimpleNamespace(id=1, role=UserRole.MEMBER)
        app.dependency_overrides[deps.get_current_active_user] = lambda: user
        app.dependency_overrides[deps.get_account_service] = lambda: AccountService(self.session)
        app.dependency_overrides[deps.get_item_service] = lambda: self.items
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            for path in ('1002/3002', '1001/3002', '1001/missing'):
                response = await client.put(f'/api/v1/items/{path}/code-generation', json={'enabled': True})
                self.assertEqual(response.status_code, 404, response.text)
            for body in ({'enabled': 'false'}, {'enabled': 1}, {}):
                response = await client.put('/api/v1/items/1001/3001/code-generation', json=body)
                self.assertEqual(response.status_code, 422)
            response = await client.put('/api/v1/items/1001/3001/code-generation', json={'enabled': True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['data']['code_generation_enabled'])

    async def test_web_send_preserves_original_text_and_rejects_other_owner(self):
        client = SimpleNamespace(is_connected=True, send_text_message=AsyncMock(return_value={'messageId': 'out'}))
        user = SimpleNamespace(id=1, role=UserRole.MEMBER)
        with patch('app.api.routes.chat_new.get_im_session_manager', return_value=SimpleNamespace(clients={'1001': client, '1002': client})):
            for text in ('生成口令', ' 生成口令', '口令生成\n'):
                result = await send_message('1001', SendMessageRequest(cid='chat', toUserId='2001', text=text), user, self.session)
                self.assertTrue(result.success)
                self.assertEqual(client.send_text_message.await_args.kwargs['text'], text)
            result = await send_message('1002', SendMessageRequest(cid='chat', toUserId='2001', text='生成口令'), user, self.session)
            self.assertFalse(result.success)
            self.assertEqual(client.send_text_message.await_count, 3)

    async def test_command_failures_and_unknown_results_visible_in_existing_logs(self):
        for log_id, status, owner in ((1, 'failed', 1), (2, 'unknown', 1), (3, 'success', 2)):
            self.db.add(ReplyLog(id=log_id, owner_id=owner, account_id=f'100{owner}', chat_id='chat',
                sender_user_id=f'100{owner}', reply_strategy='code_generation', matched_rule_type='code_generation',
                process_status='failed', decision_reason='code_send_unknown', send_status=status))
        self.db.commit()
        service = AutoReplyLogService(self.session)
        rows, count = await service.list_logs(owner_id=1, matched_rule_type='code_generation')
        self.assertEqual(count, 2)
        self.assertEqual({row['id'] for row in rows}, {1, 2})
        rows, count = await service.list_logs(owner_id=1, send_status='failed')
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]['id'], 1)
