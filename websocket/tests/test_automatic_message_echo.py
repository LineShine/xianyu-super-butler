"""Automatic echoes keep AI enabled; genuine manual messages still pause it."""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.xianyu.auto_reply_service import AutoReplyService
from app.services.xianyu.outgoing_message_tracker import (
    OutgoingMessageTracker, incoming_message_identity, snapshot_matches_message,
)
from app.services.xianyu.xianyu_async import XianyuAsync


class EchoTrackerTests(unittest.TestCase):
    def test_platform_identity_is_distinct_from_request_mid_and_uuid(self):
        message = {'raw_message': {'1': {'3': 'platform-id', '4': 1}}}
        self.assertEqual(incoming_message_identity(message), ('platform-id', None))
        self.assertTrue(snapshot_matches_message([
            {'mid': 'transport-id', 'uuid': 'client-id', 'ack': {'body': {'messageId': 'platform-id'}}},
        ], message))
        for snapshot in ({'mid': 'platform-id'}, {'uuid': 'platform-id'},
                         {'ack': {'body': {'1': 'platform-id'}}}):
            self.assertFalse(snapshot_matches_message(snapshot, message))
        self.assertFalse(snapshot_matches_message({'messageId': 'fp:fingerprint'}, 'fp:fingerprint'))

    def test_explicit_extension_id_and_nested_ack_id_are_supported(self):
        message = {'raw_message': {'1': {'10': {'extJson': json.dumps({'messageId': 'platform-id'})}}}}
        self.assertTrue(snapshot_matches_message({'ack': {'body': {'1': {'messageId': 'platform-id'}}}}, message))
        self.assertFalse(snapshot_matches_message({'messageId': 'platform-id'}, {'raw_message': {'1': {}}}))

    def test_match_is_scoped_and_consumed_once(self):
        tracker = OutgoingMessageTracker()
        tracker.remember('seller', 'chat@goofish', '在的')
        for args in [('other', 'chat', '在的'), ('seller', 'other', '在的'),
                     ('seller', 'chat', '人工回复')]:
            self.assertFalse(tracker.consume_echo(*args))
        self.assertTrue(tracker.consume_echo('seller', 'chat', '在的'))
        self.assertFalse(tracker.consume_echo('seller', 'chat', '在的'))

    def test_expired_or_old_messages_cannot_match(self):
        tracker = OutgoingMessageTracker(ttl=120)
        with patch('app.services.xianyu.outgoing_message_tracker.time.monotonic', return_value=10), \
             patch('app.services.xianyu.outgoing_message_tracker.time.time', return_value=1000):
            tracker.remember('seller', 'chat', '在的')
            self.assertFalse(tracker.consume_echo('seller', 'chat', '在的', 990000))
        with patch('app.services.xianyu.outgoing_message_tracker.time.monotonic', return_value=131):
            self.assertFalse(tracker.consume_echo('seller', 'chat', '在的'))

    def test_repeated_sends_have_separate_entries_and_memory_is_bounded(self):
        tracker = OutgoingMessageTracker(max_entries=2)
        tracker.remember('seller', 'chat', 'old')
        tracker.remember('seller', 'chat', '在的')
        tracker.remember('seller', 'chat', '在的')
        self.assertFalse(tracker.consume_echo('seller', 'chat', 'old'))
        self.assertTrue(tracker.consume_echo('seller', 'chat', '在的'))
        self.assertTrue(tracker.consume_echo('seller', 'chat', '在的'))
        self.assertFalse(tracker.consume_echo('seller', 'chat', '在的'))


class AutomaticEchoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tracker = OutgoingMessageTracker()
        for module in ('xianyu_async', 'auto_reply_service'):
            p = patch(f'app.services.xianyu.{module}.outgoing_message_tracker', self.tracker)
            p.start()
            self.addCleanup(p.stop)
        self.client = XianyuAsync.__new__(XianyuAsync)
        self.client.cookie_id = self.client.myid = 'TEST-SELLER'
        self.client._pending_mid_futures = {}
        self.reply = AutoReplyService('TEST-SELLER', self.client)
        self.reply._record_auto_reply_log = AsyncMock(return_value=None)
        self.reply._pause_ai_reply_after_manual_message = AsyncMock(return_value=True)
        self.ws = SimpleNamespace(send=AsyncMock())

    def message(self, text='在的', sender='TEST-SELLER'):
        return {'send_user_id': sender, 'chat_id': 'TEST-CHAT', 'send_message': text,
                'raw_message': {'1': {'5': int(time.time() * 1000)}}}

    def command(self, message_id='automatic-id', text='口令生成'):
        message = self.message(text)
        message['is_plain_text'] = True
        message['raw_message']['1'].update({
            '2': 'TEST-CHAT@goofish', '3': message_id,
            '6': {'3': {'5': json.dumps({'contentType': 1, 'text': {'text': text}})}},
            '10': {'senderUserId': 'TEST-SELLER', 'reminderContent': text},
        })
        return message

    async def test_command_echo_before_send_returns_uses_ack_id_and_keeps_both_copies(self):
        async def on_send(payload):
            mid = json.loads(payload)['headers']['mid']
            echoes = [asyncio.create_task(self.reply.handle_chat_message(self.command(), self.ws))
                      for _ in range(2)]
            await asyncio.sleep(0)
            self.assertTrue(all(not task.done() for task in echoes))
            self.client._dispatch_mid_response({'headers': {'mid': mid}, 'code': 200,
                                               'body': {'messageId': 'automatic-id'}})
            await asyncio.gather(*echoes)
        self.ws.send.side_effect = on_send
        result = await self.client.send_msg(self.ws, 'TEST-CHAT', 'TEST-BUYER', '口令生成')
        self.assertTrue(result['success'])
        self.reply._pause_ai_reply_after_manual_message.assert_not_awaited()
        self.assertTrue(await self.tracker.matches_command_echo(
            'TEST-SELLER', 'TEST-CHAT', '口令生成', self.command()))

    async def test_distinct_manual_command_after_automatic_send_reaches_generator(self):
        result = await self.client.send_msg(self.ws, 'TEST-CHAT', 'TEST-BUYER', '口令生成')
        self.client._dispatch_mid_response({'headers': {'mid': result['mid']}, 'code': 200,
                                           'body': {'messageId': 'automatic-id'}})
        generator = SimpleNamespace(_is_recorded_automatic_echo=AsyncMock(return_value=False),
                                    handle=AsyncMock(return_value=True))
        with patch('app.services.xianyu.auto_reply_service.CodeGenerationService', return_value=generator), \
             patch('app.services.xianyu.auto_reply_service.async_session_maker', return_value=AsyncMock()):
            for text in ('口令生成', '生成口令'):
                await self.reply.handle_chat_message(self.command('manual-' + text, text), self.ws)
        self.assertEqual(generator.handle.await_count, 2)
        self.assertEqual(self.reply._pause_ai_reply_after_manual_message.await_count, 2)

    async def test_pending_ack_timeout_failure_and_cancellation_do_not_match_new_message(self):
        for outcome in ('timeout', 'failed', 'cancelled'):
            tracker = OutgoingMessageTracker()
            future = asyncio.get_running_loop().create_future()
            tracker.remember('TEST-SELLER', 'TEST-CHAT', '口令生成',
                             request_mid='request-id', ack_future=future)
            task = asyncio.create_task(tracker.matches_command_echo(
                'TEST-SELLER', 'TEST-CHAT', '口令生成', self.command('manual-id'), wait_timeout=.01))
            await asyncio.sleep(0)
            if outcome == 'failed':
                future.set_exception(ConnectionError('disconnected'))
            elif outcome == 'cancelled':
                future.cancel()
            self.assertFalse(await task)
            if outcome == 'timeout':
                self.assertFalse(future.done())
                future.cancel()

    async def test_late_ack_is_bound_after_send_waiter_has_gone(self):
        tracker = OutgoingMessageTracker()
        tracker.remember('TEST-SELLER', 'TEST-CHAT', '口令生成', request_mid='request-id')
        tracker.record_response('OTHER-SELLER', {'headers': {'mid': 'request-id'},
                                               'body': {'messageId': 'automatic-id'}})
        self.assertFalse(await tracker.matches_command_echo(
            'TEST-SELLER', 'TEST-CHAT', '口令生成', self.command()))
        tracker.record_response('TEST-SELLER', {'headers': {'mid': 'request-id'},
                                              'body': {'messageId': 'automatic-id'}})
        self.assertTrue(await tracker.matches_command_echo(
            'TEST-SELLER', 'TEST-CHAT', '口令生成', self.command()))

    async def test_two_buyer_messages_with_automatic_echo_between_both_reply(self):
        self.reply._check_chat_processed = AsyncMock(return_value=False)
        self.reply.get_filter_keywords = AsyncMock(return_value=[])
        self.reply._send_notification = AsyncMock()
        self.reply.get_reply = AsyncMock(return_value='在的')
        for text in ('你好', '还可以咨询吗'):
            await self.reply.handle_chat_message(self.message(text, 'TEST-BUYER'), self.ws)
            await self.reply.handle_chat_message(self.message(), self.ws)
            self.assertEqual(self.reply._record_auto_reply_log.await_args.args[0]['decision_reason'],
                             'automatic_message_echo')
        self.assertEqual(self.ws.send.await_count, 2)
        self.reply._pause_ai_reply_after_manual_message.assert_not_awaited()
        # A subsequent manual reply, even with identical content, still pauses AI.
        await self.reply.handle_chat_message(self.message(), self.ws)
        self.reply._pause_ai_reply_after_manual_message.assert_awaited_once()

    async def test_echo_can_arrive_before_socket_send_returns(self):
        async def on_send(payload):
            await self.reply.handle_chat_message(self.message(), self.ws)
        self.ws.send.side_effect = on_send
        result = await self.client.send_msg(self.ws, 'TEST-CHAT', 'TEST-BUYER', '在的')
        self.assertTrue(result['success'])
        self.reply._pause_ai_reply_after_manual_message.assert_not_awaited()

    async def test_manual_send_is_not_registered_as_automatic(self):
        result = await self.client.send_msg(self.ws, 'TEST-CHAT', 'TEST-BUYER', '在的', automatic=False)
        self.assertTrue(result['success'])
        await self.reply.handle_chat_message(self.message(), self.ws)
        self.reply._pause_ai_reply_after_manual_message.assert_awaited_once()

    async def test_failed_and_cancelled_sends_do_not_hide_manual_messages(self):
        for failure in (OSError('socket closed'), asyncio.CancelledError()):
            self.ws.send.side_effect = failure
            with self.assertRaises(type(failure)):
                await self.client._send_with_echo_tracking(self.ws, '{}', 'TEST-CHAT', '在的')
            self.assertFalse(self.tracker.consume_echo('TEST-SELLER', 'TEST-CHAT', '在的'))

    async def test_manual_pause_after_restart_uses_database_buyer_context(self):
        from app.services.xianyu.resource_manager import AutoReplyPauseManager
        manager = AutoReplyPauseManager()
        engine = SimpleNamespace(get_ai_settings=AsyncMock(return_value={
            'manual_reply_ai_pause_enabled': True, 'manual_reply_ai_pause_minutes': 5}))
        session = AsyncMock()
        self.reply._lookup_buyer_context_from_db = AsyncMock(return_value=('TEST-BUYER', 'OLD-ITEM'))
        with patch('app.services.xianyu.auto_reply_service.pause_manager', manager), \
             patch('app.services.xianyu.auto_reply_service.async_session_maker', return_value=session), \
             patch('app.services.xianyu.ai_reply_engine.get_ai_reply_engine', return_value=engine):
            # Call the real method, bypassing the fixture's mock.
            paused = await AutoReplyService._pause_ai_reply_after_manual_message(
                self.reply, 'TEST-CHAT', 'TEST-ITEM', {})
        self.assertTrue(paused)
        self.reply._lookup_buyer_context_from_db.assert_awaited_once_with('TEST-CHAT')
        self.assertTrue(manager.is_ai_reply_paused('TEST-SELLER', 'TEST-BUYER', 'TEST-ITEM'))
