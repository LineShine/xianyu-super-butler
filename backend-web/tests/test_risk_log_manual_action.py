import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routes.risk_control_logs import ManualCaptchaStart, start_manual_verification
from app.services.risk_control_log_service import RiskControlLogService
from common.models.risk_control_log import YtxRiskControlLog
from common.models.user import UserRole
from common.models.ytx_account import YtxAccount


class RiskLogManualActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        YtxAccount.__table__.create(self.engine)
        YtxRiskControlLog.__table__.create(self.engine)
        self.db = Session(self.engine)
        self.service = RiskControlLogService(SimpleNamespace(execute=AsyncMock(side_effect=self.db.execute)))
        self.online = patch('app.services.dashboard_stats_service.DashboardStatsService.get_online_account_ids',
                            AsyncMock(return_value=frozenset({'online'})))
        self.online_mock = self.online.start()
        self.addCleanup(self.online.stop)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.now = datetime(2026, 10, 21, 12)

    def account(self, aid, owner=1, status='disabled'):
        account = YtxAccount(id=len(self.db.query(YtxAccount).all()) + 1, account_id=aid,
                             owner_id=owner, status=status, cookie='test-only', login_method='test')
        self.db.add(account)
        self.db.flush()
        return account

    def log(self, account, log_id, status='failed', when=None, call_type='local'):
        log = YtxRiskControlLog(id=log_id, owner_id=account.owner_id, account_pk=account.id,
                               account_identifier=account.account_id, event_type='slider_captcha',
                               processing_status=status, call_type=call_type, created_at=when or self.now)
        self.db.add(log)
        self.db.flush()
        return log

    async def test_only_latest_offline_record_has_button_and_other_account_is_independent(self):
        offline, online, other = self.account('offline'), self.account('online'), self.account('other')
        self.log(offline, 1)
        self.log(offline, 2, 'cancelled')
        self.log(online, 3)
        self.log(other, 4)
        rows, total = await self.service.list_logs(owner_id=1)
        self.assertEqual(total, 4)
        self.assertEqual([r['id'] for r in rows if r['can_manual_verify']], [4, 2])

    async def test_pagination_does_not_promote_an_old_record(self):
        a = self.account('offline')
        self.log(a, 1)
        self.log(a, 2)
        rows, _ = await self.service.list_logs(owner_id=1, limit=1, offset=1)
        self.assertEqual(rows[0]['id'], 1)
        self.assertFalse(rows[0]['can_manual_verify'])

    async def test_filters_cannot_bring_back_failure_after_a_newer_success(self):
        a = self.account('offline')
        self.log(a, 1, when=self.now - timedelta(days=1))
        self.log(a, 2, 'success')
        for filters in ({'processing_status': 'failed'}, {'end_date': '2026-10-20'}):
            rows, total = await self.service.list_logs(owner_id=1, **filters)
            self.assertEqual(total, 1)
            self.assertFalse(rows[0]['can_manual_verify'])

    async def test_latest_uses_time_then_id_and_hides_remote_deleted_and_pending(self):
        offline = self.account('offline')
        self.log(offline, 11, when=self.now - timedelta(days=1))
        self.log(offline, 10)
        self.log(self.account('deleted', status='deleted'), 12)
        self.log(self.account('remote'), 13, call_type='remote')
        self.log(self.account('queued'), 14, 'queued')
        self.log(self.account('processing'), 15, 'processing')
        rows, _ = await self.service.list_logs(owner_id=1)
        self.assertEqual([r['id'] for r in rows if r['can_manual_verify']], [10])

    async def test_permissions_and_removed_account_hide_actions(self):
        mine, other = self.account('mine'), self.account('other', owner=2)
        self.log(mine, 1)
        self.log(other, 2)
        ids = await self.service.get_manual_verification_log_ids(owner_id=1, account_ids={'mine', 'other'})
        self.assertEqual(ids, {1})
        mine.owner_id = 2  # Old owner's records must not control a transferred account.
        rows, _ = await self.service.list_logs(owner_id=1)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['can_manual_verify'])
        self.db.delete(mine)
        self.db.flush()
        rows, _ = await self.service.list_logs(owner_id=None)
        self.assertEqual([r['id'] for r in rows if r['can_manual_verify']], [2])

    async def test_click_rechecks_online_state_and_rejects_stale_record(self):
        a = self.account('offline')
        self.log(a, 1)
        self.log(a, 2)
        user = SimpleNamespace(id=1, role=UserRole.MEMBER)
        with patch('common.services.manual_verification_queue.enqueue', AsyncMock(return_value=3)) as enqueue:
            with self.assertRaises(HTTPException) as stale:
                await start_manual_verification(ManualCaptchaStart(account_id='offline', log_id=1), user, self.service)
            self.assertEqual(stale.exception.status_code, 409)
            enqueue.assert_not_awaited()
            self.online_mock.return_value = frozenset({'offline'})
            with self.assertRaises(HTTPException):
                await start_manual_verification(ManualCaptchaStart(account_id='offline', log_id=2), user, self.service)
            enqueue.assert_not_awaited()
            self.online_mock.return_value = frozenset()
            await start_manual_verification(ManualCaptchaStart(account_id='offline', log_id=2), user, self.service)
            enqueue.assert_awaited_once_with('offline', user=user)
            self.online_mock.assert_awaited_with(force_refresh=True)
