"""Date-range aggregates against local SQL; no live account or production DB."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.api import deps
from app.api.routes.admin import router
from app.services.dashboard_stats_service import DashboardStatsService
from common.models.user import User
from common.models.ytx_account import YtxAccount
from common.models.ytx_order import YtxOrder


class DashboardDateRangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        for model in (User, YtxAccount, YtxOrder):
            model.__table__.create(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.session = SimpleNamespace(execute=AsyncMock(side_effect=self.db.execute))
        self.service = DashboardStatsService(self.session)
        self.today = datetime(2026, 11, 2)
        for index, offset in enumerate((-31, -29, -6, -2, -1, 0, 1), 1):
            at = self.today + timedelta(days=offset)
            self.db.add(User(id=index, username=f'u{index}', email=f'u{index}@test.local', password_hash='test', created_at=at))
            self.db.add(YtxAccount(id=index, owner_id=index, account_id=str(index), cookie='test', login_method='test', created_at=at))
            self.db.add(YtxOrder(id=index, owner_id=index, order_no=str(index), status='shipped', amount=10, placed_at=at))
        # End-of-day included; next midnight excluded. Refunds/cancelled/unknown placed_at excluded.
        for index, status, at in ((20, 'pending', self.today - timedelta(microseconds=1)),
                                  (21, 'refunded', self.today - timedelta(hours=1)),
                                  (22, 'cancelled', self.today - timedelta(hours=1)),
                                  (23, 'shipped', None)):
            self.db.add(YtxOrder(id=index, owner_id=1, order_no=str(index), status=status, amount=5, placed_at=at, created_at=self.today))
        self.db.commit()

    async def test_presets_and_inclusive_custom_end_use_placed_at(self):
        for start, end, count, amount, pending in (
            (date(2026, 11, 2), date(2026, 11, 2), 1, 10, 0),
            (date(2026, 11, 1), date(2026, 11, 1), 2, 15, 1),
            (date(2026, 10, 31), date(2026, 10, 31), 1, 10, 0),
            (date(2026, 10, 27), date(2026, 11, 2), 5, 45, 1),
            (date(2026, 10, 4), date(2026, 11, 2), 6, 55, 1),
            (date(2026, 10, 31), date(2026, 11, 1), 3, 25, 1),
        ):
            with self.subTest(start=start, end=end):
                result = await self.service.get_admin_today_stats(start_date=start, end_date=end)
                self.assertEqual(result['today_orders'], count)
                self.assertEqual(result['today_amount'], amount)
                self.assertEqual(result['today_pending'], pending)
                self.assertEqual(result['today_shipped'], count - pending)
                self.assertEqual(result['today_users'], count - pending)
                self.assertEqual(result['today_accounts'], count - pending)

    async def test_default_today_and_refresh_bypasses_cached_data(self):
        with patch.object(DashboardStatsService, '_build_today_start', return_value=self.today):
            self.assertEqual((await self.service.get_admin_today_stats())['today_amount'], 10)
            self.db.add(YtxOrder(id=30, owner_id=2, order_no='30', status='pending', amount=99, placed_at=self.today))
            self.db.commit()
            self.assertEqual((await self.service.get_admin_today_stats())['today_amount'], 109)

    async def test_http_date_validation_and_admin_access(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[deps.get_current_admin_user] = lambda: SimpleNamespace(id=1)
        app.dependency_overrides[deps.get_db_session] = lambda: self.session
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for params in ({'start_date': '2026-11-01'}, {'end_date': '2026-11-01'},
                           {'start_date': '2026-11-02', 'end_date': '2026-11-01'},
                           {'start_date': 'bad', 'end_date': '2026-11-01'}):
                self.assertEqual((await client.get('/stats/today', params=params)).status_code, 422)
            response = await client.get('/stats/today', params={'start_date': '2026-11-01', 'end_date': '2026-11-01'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['today_amount'], 15)
            def deny():
                raise HTTPException(status_code=403)
            app.dependency_overrides[deps.get_current_admin_user] = deny
            self.assertEqual((await client.get('/stats/today')).status_code, 403)
