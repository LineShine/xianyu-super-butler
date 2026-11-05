import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.dashboard_stats_service import DashboardStatsService


class AccountMessageHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        DashboardStatsService._online_ids_cache = None
        DashboardStatsService._online_cookies_cache = None
        DashboardStatsService._account_health_cache = {}
        self.service = DashboardStatsService(None)
        self.http = SimpleNamespace(get=AsyncMock(return_value={'success': True, 'data': {
            'connected': 1, 'connected_account_ids': ['good'],
            'account_health': {'bad': {'relogin_required': True, 'message_sync_status': 'unhealthy',
                                      'offline_reason': '消息接收异常，请重新扫码登录'}}}}))
        p = patch('app.services.dashboard_stats_service.get_http_client', return_value=self.http)
        p.start()
        self.addCleanup(p.stop)

    async def test_online_filter_count_and_notice_use_same_health_snapshot(self):
        ids = await self.service.get_online_account_ids()
        health = await self.service.get_account_message_health()
        self.assertEqual(ids, frozenset({'good'}))
        self.assertTrue(health['bad']['relogin_required'])
        self.assertEqual(self.http.get.await_count, 1)
        self.assertEqual(await self.service._get_online_cookies_count(), 1)

    async def test_relogin_recovery_clears_cached_offline_reason_on_refresh(self):
        await self.service.get_online_account_ids()
        self.http.get.return_value = {'success': True, 'data': {
            'connected': 2, 'connected_account_ids': ['good', 'bad'], 'account_health': {}}}
        self.assertEqual(await self.service.get_online_account_ids(force_refresh=True), frozenset({'good', 'bad'}))
        self.assertEqual(await self.service.get_account_message_health(), {})

    async def test_service_outage_does_not_invent_relogin_diagnosis(self):
        await self.service.get_online_account_ids()
        self.http.get.side_effect = ConnectionError('offline')
        self.assertEqual(await self.service.get_online_account_ids(force_refresh=True), frozenset())
        self.assertEqual(await self.service.get_account_message_health(), {})
