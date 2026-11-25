import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.services.xianyu.message_sync_health import MessageSyncHealth
from app.services.xianyu.cookie_manager import CookieManager
from app.services.xianyu.connection_manager import ConnectionState
from app.services.xianyu.message_handler import MessageHandler


def history(mid='new', stamp=2000, sender='2001', text='口令'):
    return {'userMessageModels': [{'message': {
        'messageId': mid, 'createAt': stamp, 'extension': {'senderUserId': sender},
        'content': {'custom': {'data': json.dumps({'contentType': 1, 'text': {'text': text}})}}}}]}


def listing(mid='old', stamp=1000):
    return {'userConvs': [{'singleChatUserConversation': {
        'singleChatConversation': {'cid': '4001@goofish'},
        'lastMessage': {'message': {'messageId': mid, 'createAt': stamp}},
    }}]}


def push(mid='new', stamp=2000, sender='2001'):
    return {'1': {'2': '4001@goofish', '3': mid, '5': stamp,
                  '10': {'senderUserId': sender, 'reminderContent': '口令'}}}


class HealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = SimpleNamespace(cookie_id='1001', myid='1001', _registered_ws=object(),
                                        _pending_mid_futures={}, _reconnect_with_new_token=AsyncMock(return_value=True),
                                        _cookie_token_manager=SimpleNamespace(
                                            try_password_login_refresh=AsyncMock(return_value='no_credentials')))
        self.health = MessageSyncHealth(self.instance)
        self.health.write_risk_log = AsyncMock()
        self.now = 0
        patcher = patch('app.services.xianyu.message_sync_health.time',
                        SimpleNamespace(monotonic=lambda: self.now))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def baseline(self):
        self.health.request = AsyncMock(return_value=listing())
        await self.health.poll(self.instance._registered_ws)

    def miss(self, mid='new', stamp=2000):
        self.health.check_history('4001', history(mid, stamp))
        self.now += 61
        self.health.check_history('4001', history(mid, stamp))

    async def test_idle_old_history_and_wall_clock_changes_never_mark_offline(self):
        await self.baseline()
        with patch('time.time', return_value=-999999):
            self.miss('old', 1000)
        self.assertFalse(self.health.gap_detected)
        self.assertFalse(self.health.unhealthy)
        self.assertEqual(self.instance._reconnect_with_new_token.await_count, 0)

    async def test_two_reconnections_then_new_confirmed_miss_marks_offline_once(self):
        await self.baseline()
        for i in range(3):
            self.miss(f'miss-{i}', 2000 + i * 1000)
            self.assertTrue(self.health.gap_detected)
            await self.health.recover()
            self.assertEqual(self.health.unhealthy, i == 2)
            if i < 2:
                self.health.request.return_value = listing(f'miss-{i}', 2000 + i * 1000)
                await self.health.poll(self.instance._registered_ws)
        self.assertEqual(self.instance._reconnect_with_new_token.await_count, 2)
        await self.health.flush_events()
        self.assertEqual(self.health.write_risk_log.await_count, 4)
        self.assertIn('重试 2 次', self.health.write_risk_log.call_args.args[1])
        self.assertTrue(self.health.status()['relogin_required'])
        self.health.observe(push('fresh', 6000))
        self.assertFalse(self.health.unhealthy)
        self.assertEqual(self.health.recovery_attempts, 0)

    async def reach_password_login(self, result=True):
        self.instance._cookie_token_manager.try_password_login_refresh.return_value = result
        await self.baseline()
        for i in range(3):
            self.miss(f'miss-{i}', 2000 + i * 1000)
            await self.health.recover()
            if i < 2:
                self.health.request.return_value = listing(f'miss-{i}', 2000 + i * 1000)
                await self.health.poll(self.instance._registered_ws)

    def rebuild_health(self):
        old = self.health
        self.health = MessageSyncHealth(self.instance)
        self.health.restore_recovery(old.recovery_snapshot())
        self.health.write_risk_log = AsyncMock()
        return old

    async def test_two_token_retries_then_real_login_and_new_instance_verifies_fresh_push(self):
        await self.reach_password_login()
        self.assertEqual(self.instance._reconnect_with_new_token.await_count, 2)
        login = self.instance._cookie_token_manager.try_password_login_refresh
        login.assert_awaited_once()
        self.assertTrue(login.call_args.kwargs['force_password_login'])
        self.assertFalse(self.health.status()['relogin_required'])
        self.health.observe(push('old-socket', 4500))
        self.assertEqual(self.health.recovery_attempts, 2)
        self.rebuild_health()
        self.assertEqual(self.health.status()['message_sync_status'], 'verifying')
        self.assertEqual(self.health.password_login_attempts, 1)
        self.health.observe(push('before-baseline', 5000))
        self.assertEqual(self.health.recovery_attempts, 2)
        self.health.request = AsyncMock(return_value=listing('before-baseline', 5000))
        await self.health.poll(self.instance._registered_ws)
        self.health.observe(push('old', 4000))
        self.health.observe(push('wrong-direction', 6000, sender='1001'))
        self.assertEqual(self.health.status()['message_sync_status'], 'verifying')
        self.now += 3600
        await self.health.poll(self.instance._registered_ws)
        self.assertFalse(self.health.unhealthy)
        self.assertEqual(self.health.status()['message_sync_status'], 'verifying')
        self.health.observe(push('fresh', 7000))
        self.assertEqual(self.health.status()['message_sync_status'], 'monitoring')
        self.assertEqual(self.health.password_login_attempts, 0)

    async def test_new_miss_after_password_login_stops_without_another_login(self):
        await self.reach_password_login()
        self.rebuild_health()
        self.health.request = AsyncMock(return_value=listing('after-login', 5000))
        await self.health.poll(self.instance._registered_ws)
        self.miss('still-missing', 6000)
        await self.health.recover()
        await self.health.recover()
        self.assertTrue(self.health.unhealthy)
        self.assertEqual(self.health.recovery_attempts, 2)
        self.instance._cookie_token_manager.try_password_login_refresh.assert_awaited_once()
        self.assertIn('仍漏收新消息', self.health.status()['offline_reason'])

    async def test_face_verification_is_exposed_and_failure_requires_manual_action(self):
        async def login(*args, on_verification, **kwargs):
            on_verification('人脸验证', '/test.png', 'https://example.test/verify')
            status = self.health.status()
            self.assertEqual(status['message_sync_status'], 'verification_required')
            self.assertEqual(status['message_sync_verification_url'], 'https://example.test/verify')
            self.assertFalse(status['relogin_required'])
            return False
        self.instance._cookie_token_manager.try_password_login_refresh.side_effect = login
        await self.reach_password_login()
        self.assertTrue(self.health.unhealthy)
        self.assertIn('人脸或安全验证', self.health.status()['offline_reason'])
        self.assertFalse(self.health.status()['message_sync_verification_url'])

    async def test_login_cooldown_retries_are_bounded_and_do_not_reset_token_budget(self):
        await self.reach_password_login('skipped_cooldown')
        self.assertEqual(self.health.password_login_attempts, 0)
        login = self.instance._cookie_token_manager.try_password_login_refresh
        await self.health.recover()
        login.assert_awaited_once()
        self.now += 61
        await self.health.recover()
        self.assertEqual(login.await_count, 2)
        self.assertEqual(self.instance._reconnect_with_new_token.await_count, 2)
        self.now += 600
        await self.health.recover()
        self.assertTrue(self.health.unhealthy)
        self.assertEqual(login.await_count, 2)

    async def test_login_exception_is_terminal_for_this_incident(self):
        self.instance._cookie_token_manager.try_password_login_refresh.side_effect = RuntimeError('fixture')
        await self.reach_password_login()
        await self.health.recover()
        self.instance._cookie_token_manager.try_password_login_refresh.assert_awaited_once()
        self.assertTrue(self.health.unhealthy)

    async def test_cancelled_login_does_not_leave_permanent_login_spinner(self):
        self.instance._cookie_token_manager.try_password_login_refresh.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.reach_password_login()
        self.assertTrue(self.health.unhealthy)
        self.assertFalse(self.health.awaiting_rebuild)
        self.assertIn('已中断', self.health.status()['offline_reason'])

    async def test_expected_restart_cancellation_keeps_state_for_replacement(self):
        self.instance._instance_restart_scheduled = True
        self.instance._cookie_token_manager.try_password_login_refresh.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.reach_password_login()
        self.assertFalse(self.health.unhealthy)
        self.rebuild_health()
        self.assertEqual(self.health.status()['message_sync_status'], 'verifying')
        self.assertEqual(self.health.password_login_attempts, 1)

    async def test_due_login_retry_runs_even_if_history_queries_fail(self):
        await self.reach_password_login('skipped_cooldown')
        login = self.instance._cookie_token_manager.try_password_login_refresh
        login.return_value = True
        self.now += 61
        self.health.poll = AsyncMock(side_effect=ConnectionError())
        with patch('app.services.xianyu.message_sync_health.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.health.run()
        self.assertEqual(login.await_count, 2)
        self.assertEqual(self.health.status()['message_sync_status'], 'verifying')

    async def test_credential_failure_has_specific_reason_and_does_not_retry(self):
        for result, expected in [('no_credentials', '未配置可用账密'),
                                 ('credential_unavailable', '无法解密'),
                                 ('verification_required', '人脸或安全验证'),
                                 ('account_disabled', '已停用')]:
            with self.subTest(result=result):
                self.health = MessageSyncHealth(self.instance)
                self.health.recovery_attempts = 2
                login = self.instance._cookie_token_manager.try_password_login_refresh
                login.reset_mock()
                login.return_value = result
                await self.health.recover()
                await self.health.recover()
                self.assertTrue(self.health.unhealthy)
                self.assertIn(expected, self.health.status()['offline_reason'])
                login.assert_awaited_once()

    async def test_connection_failures_exhaust_only_two_retries(self):
        self.instance._reconnect_with_new_token.return_value = False
        await self.health.recover()
        self.assertTrue(self.health.unhealthy)
        self.assertEqual(self.instance._reconnect_with_new_token.await_count, 2)

    async def test_fresh_push_during_reconnect_clears_retry_and_offline_state(self):
        async def reconnect():
            self.health.observe(push('recovered', 3000))
            return True
        await self.baseline()
        self.miss()
        self.instance._reconnect_with_new_token.side_effect = reconnect
        await self.health.recover()
        self.assertEqual(self.health.recovery_attempts, 0)
        self.assertFalse(self.health.unhealthy)
        await self.health.flush_events()
        self.assertEqual(self.health.write_risk_log.await_count, 1)
        self.assertIn('已确认收到', self.health.write_risk_log.call_args.args[1])

    async def test_query_failure_still_flushes_recovery_log(self):
        self.health.poll = AsyncMock(side_effect=ConnectionError())
        self.health.events.append(('success', '已确认收到新的买家消息'))
        with patch('app.services.xianyu.message_sync_health.asyncio.sleep',
                   side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.health.run()
        self.health.write_risk_log.assert_awaited_once()
        self.assertFalse(self.health.unhealthy)

    async def test_risk_log_keeps_account_owner_and_recovery_result(self):
        account = SimpleNamespace(id=7, owner_id=9)
        db = SimpleNamespace(execute=AsyncMock(return_value=Mock(
            scalars=Mock(return_value=Mock(first=Mock(return_value=account))))),
            add=Mock(), commit=AsyncMock())
        session = AsyncMock()
        session.__aenter__.return_value = db
        with patch('common.db.session.async_session_maker', return_value=session):
            await MessageSyncHealth.write_risk_log(self.health, 'failed', '第 2/2 次自动重连：连接恢复失败')
        event = db.add.call_args.args[0]
        self.assertEqual((event.account_pk, event.owner_id, event.account_identifier), (7, 9, '1001'))
        self.assertEqual(event.event_type, 'message_sync')
        self.assertEqual(event.processing_status, 'failed')
        self.assertIn('2/2', event.processing_result)
        db.commit.assert_awaited_once()

    async def test_delayed_push_and_forwarded_duplicates_prevent_false_alarm(self):
        await self.baseline()
        self.health.check_history('4001', history())
        self.health.observe(push())
        self.health.observe(push())
        self.now += 100
        self.health.check_history('4001', history())
        self.assertFalse(self.health.gap_detected)
        self.assertFalse(self.health.pending)

    async def test_single_query_failure_own_messages_and_recalled_message_do_not_count(self):
        await self.baseline()
        self.health.check_history('4001', history(sender='1001'))
        self.assertFalse(self.health.pending)
        self.health.check_history('4001', history())
        self.now += 100
        with self.assertRaises(ValueError):
            self.health.check_history('4001', {'reason': 'rate_limited'})
        self.assertFalse(self.health.gap_detected)
        self.health.check_history('4001', {'userMessageModels': []})
        self.assertFalse(self.health.pending)

    async def test_poll_checks_buyer_history_even_when_latest_message_is_seller_reply(self):
        await self.baseline()
        self.health.request.side_effect = [listing('seller-reply', 3000), history(),
                                          listing('seller-reply', 3000), history()]
        await self.health.poll(self.instance._registered_ws)
        self.now += 61
        await self.health.poll(self.instance._registered_ws)
        self.assertTrue(self.health.gap_detected)

    async def test_missing_seller_command_is_not_hidden_by_working_buyer_direction(self):
        await self.baseline()
        command = history(sender='1001', text='口令生成')
        self.health.check_history('4001', command)
        self.now += 61
        self.health.check_history('4001', command)
        self.assertTrue(self.health.gap_detected)
        self.assertEqual(self.health.failure_directions, {'seller'})
        await self.health.recover()
        self.health.observe(push('buyer-still-works', 3000))
        self.assertEqual(self.health.recovery_attempts, 1)
        self.health.observe(push('seller-recovered', 4000, sender='1001'))
        self.assertEqual(self.health.recovery_attempts, 0)
        self.assertFalse(self.health.failure_directions)

    async def test_observed_seller_command_and_automatic_metadata_do_not_trigger_recovery(self):
        await self.baseline()
        self.health.observe(push(sender='1001'))
        self.health.check_history('4001', history(sender='1001', text='口令生成'))
        self.assertFalse(self.health.pending)
        for flag in ('isAutoReply', 'automatic'):
            automatic = history(mid='automatic', sender='1001', text='口令生成')
            automatic['userMessageModels'][0]['message']['extension'][flag] = True
            self.health.check_history('4001', automatic)
            self.assertFalse(self.health.pending)

    async def test_both_directions_must_recover_when_both_were_missing(self):
        await self.baseline()
        commands = history(sender='1001', text='生成口令')
        commands['userMessageModels'] += history(mid='buyer')['userMessageModels']
        self.health.check_history('4001', commands)
        self.now += 61
        self.health.check_history('4001', commands)
        await self.health.recover()
        self.health.observe(push('fresh-buyer', 3000))
        self.assertEqual(self.health.failure_directions, {'seller'})
        self.assertEqual(self.health.recovery_attempts, 1)
        self.health.observe(push('fresh-seller', 4000, sender='1001'))
        self.assertEqual(self.health.recovery_attempts, 0)

    async def test_recovery_waits_for_new_evidence_idle_is_not_failure(self):
        await self.baseline()
        self.miss()
        await self.health.recover()
        self.health.request.return_value = listing('new', 2000)
        await self.health.poll(self.instance._registered_ws)
        self.now += 3600
        await self.health.poll(self.instance._registered_ws)
        self.assertEqual(self.health.recovery_attempts, 1)
        self.assertFalse(self.health.unhealthy)
        self.assertFalse(self.health.gap_detected)

    async def test_stats_and_task_status_exclude_unhealthy_account(self):
        manager = CookieManager.__new__(CookieManager)
        self.instance.connection_manager = SimpleNamespace(connection_state=ConnectionState.CONNECTED)
        self.instance.message_sync_health = self.health
        manager.instances = {'1001': self.instance}
        manager.tasks = {'1001': SimpleNamespace(done=lambda: False)}
        self.health.unhealthy = True
        self.assertFalse(manager.get_task_status('1001')['is_connected'])
        stats = manager.get_connection_stats()
        self.assertEqual(stats['connected'], 0)
        self.assertEqual(stats['connected_account_ids'], [])
        self.assertTrue(stats['account_health']['1001']['relogin_required'])

    async def test_observer_runs_for_native_and_forwarded_chat_before_dedup(self):
        with patch.object(MessageHandler, '_load_message_expire_time', return_value=3600):
            handler = MessageHandler('1001')
        handler.message_observer = self.health.observe
        handler._on_chat_message = AsyncMock()
        event = push()
        await handler._process_single_message(event, object())
        await handler._process_single_message(event, object())
        self.assertIn(('4001', 'new'), self.health.seen)

    async def test_cancel_query_cleans_pending_response(self):
        ws = SimpleNamespace(send=AsyncMock())
        task = asyncio.create_task(self.health.request(ws, '/read-only', []))
        await asyncio.sleep(0)
        self.assertEqual(len(self.instance._pending_mid_futures), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.instance._pending_mid_futures)
