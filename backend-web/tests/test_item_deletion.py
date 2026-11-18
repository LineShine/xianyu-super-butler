"""Deleting an orphan must not delete another catalog row or its shared card links."""
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api import deps
from app.api.routes.items import items_router
from app.services.account_service import AccountService
from common.models.card_item_relation import CardItemRelation
from common.models.user import UserRole
from common.models.ytx_account import YtxAccount
from common.models.ytx_catalog_item import YtxCatalogItem
from common.services.item_service import ItemService


class ItemDeletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        for model in (YtxAccount, YtxCatalogItem, CardItemRelation):
            model.__table__.create(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.db.close)
        self.session = SimpleNamespace(
            execute=AsyncMock(side_effect=self.db.execute),
            delete=AsyncMock(side_effect=self.db.delete),
            commit=AsyncMock(side_effect=self.db.commit),
        )
        self.service = ItemService(self.session)
        self.account = YtxAccount(id=6, owner_id=1, account_id='seller', cookie='fixture',
                                 login_method='cookie', status='active')
        self.db.add(self.account)
        for row_id, owner, account in ((3, 1, 2), (6, 1, 6), (8, 2, 9)):
            self.db.add(YtxCatalogItem(id=row_id, owner_id=owner, account_pk=account,
                item_id='same-item', title='fixture', created_at=datetime.now()))
        for relation_id, owner in ((1, 1), (2, 2)):
            self.db.add(CardItemRelation(id=relation_id, user_id=owner, card_id=relation_id,
                item_id='same-item', created_at=datetime.now(), updated_at=datetime.now()))
        self.db.commit()

    def ids(self, model):
        return set(self.db.scalars(select(model.id)).all())

    async def test_selected_orphan_deleted_while_live_item_and_links_remain(self):
        self.assertEqual(await self.service.delete_item_smart(1, 'same-item', catalog_id=3), 'ok')
        self.assertEqual(self.ids(YtxCatalogItem), {6, 8})
        self.assertEqual(self.ids(CardItemRelation), {1, 2})

    async def test_legacy_client_can_remove_orphan_next_to_live_item(self):
        self.assertEqual(await self.service.delete_item_smart(1, 'same-item'), 'ok')
        self.assertEqual(self.ids(YtxCatalogItem), {6, 8})
        self.assertEqual(self.ids(CardItemRelation), {1, 2})

    async def test_owner_item_and_account_must_all_match_selected_row(self):
        for owner, item, account, catalog_id, expected in (
                (1, 'same-item', None, 8, 'not_found'),
                (1, 'another-item', None, 3, 'not_found'),
                (1, 'same-item', None, 6, 'account_required'),
                (1, 'same-item', self.account, 3, 'not_found')):
            with self.subTest(catalog_id=catalog_id, account=account):
                self.assertEqual(await self.service.delete_item_smart(owner, item, account, catalog_id), expected)
        self.assertEqual(self.ids(YtxCatalogItem), {3, 6, 8})
        self.assertEqual(self.ids(CardItemRelation), {1, 2})

    async def test_last_owner_row_cleans_only_that_owners_links(self):
        await self.service.delete_item_smart(1, 'same-item', catalog_id=3)
        self.assertTrue(await self.service.delete_item(self.account, 'same-item', 6))
        self.assertEqual(self.ids(YtxCatalogItem), {8})
        self.assertEqual(self.ids(CardItemRelation), {2})

    async def test_admin_exact_row_does_not_delete_other_owners_orphans(self):
        self.assertEqual(await self.service.delete_item_smart(None, 'same-item', catalog_id=3), 'ok')
        self.assertEqual(self.ids(YtxCatalogItem), {6, 8})
        self.assertEqual(self.ids(CardItemRelation), {1, 2})

    def app(self):
        app = FastAPI()
        app.include_router(items_router, prefix='/api/v1')
        app.dependency_overrides[deps.get_current_active_user] = lambda: SimpleNamespace(id=1, role=UserRole.MEMBER)
        app.dependency_overrides[deps.get_account_service] = lambda: AccountService(self.session)
        app.dependency_overrides[deps.get_item_service] = lambda: self.service
        return app

    async def test_http_single_delete_uses_catalog_id_and_reports_failure(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url='http://fixture') as client:
            for catalog_id in (6, 8):
                response = await client.request('DELETE', '/api/v1/items/delete', json={
                    'cookie_id': None, 'item_id': 'same-item', 'catalog_id': catalog_id})
                self.assertFalse(response.json()['success'], response.text)
            response = await client.request('DELETE', '/api/v1/items/delete', json={
                'cookie_id': None, 'item_id': 'same-item', 'catalog_id': 3})
            self.assertTrue(response.json()['success'], response.text)
        self.assertEqual(self.ids(YtxCatalogItem), {6, 8})
        self.assertEqual(self.ids(CardItemRelation), {1, 2})

    async def test_batch_rejects_unknown_account_instead_of_deleting_orphan(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url='http://fixture') as client:
            response = await client.request('DELETE', '/api/v1/items/batch', json={'items': [{
                'cookie_id': 'unknown-seller', 'item_id': 'same-item', 'catalog_id': 3}]})
            self.assertFalse(response.json()['success'], response.text)
            self.assertEqual(self.ids(YtxCatalogItem), {3, 6, 8})
            response = await client.request('DELETE', '/api/v1/items/batch', json={'items': [{
                'cookie_id': None, 'item_id': 'same-item', 'catalog_id': 3}]})
            self.assertTrue(response.json()['success'], response.text)
        self.assertEqual(self.ids(YtxCatalogItem), {6, 8})
        self.assertEqual(self.ids(CardItemRelation), {1, 2})


if __name__ == '__main__':
    unittest.main()
