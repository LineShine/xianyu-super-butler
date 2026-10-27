"""Real SQL state transitions; fake clock, official responses, subprocess and IM transport."""
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

import test_code_generation as base_tests
from app.services.xianyu import code_self_service as self_service
from app.services.xianyu import code_generation_service as commands
from app.services.xianyu.auto_reply_service import AutoReplyService
from app.services.xianyu.message_handler import MessageHandler
from common.models.auto_reply_message_log import YtxAutoReplyMessageLog as ReplyLog
from common.models.code_self_service import CodeSelfServiceSession as Flow, CodeSelfServiceAttempt as Attempt
from common.models.ytx_account import YtxAccount
from common.services.code_generation_config import ENABLED_KEY, ENABLED_AT_KEY
from common.services.code_order_entitlement import validate_order_response, OrderEntitlement

ORDER = '3316963513050017696'
message = base_tests.message


def official_response(order=ORDER, seller='1001', buyer='2001', item='3001', title='待发货'):
    return {'ret': ['SUCCESS::调用成功'], 'data': {
        'orderId': order, 'sellerId': seller, 'buyerId': buyer, 'itemId': item,
        'refundStatus': 'NO_REFUND', 'components': [
            {'render': 'orderStatusVO', 'data': {'orderStatusInfo': {'title': title},
             'orderStatusNodeList': [{'title': '已付款', 'completed': True}]}}]}}


class EntitlementTests(unittest.TestCase):
    def validate(self, raw):
        return validate_order_response(raw, order_no=ORDER, seller_id='1001', buyer_id='2001', item_id='3001')

    def test_paid_unshipped_shipped_completed_all_eligible(self):
        for title in ('待发货', '已发货', '待收货', '交易成功', '已完成'):
            self.assertTrue(self.validate(official_response(title=title)).allowed)

    def test_identity_missing_conflicting_or_cross_scope_rejected(self):
        for field in ('orderId', 'sellerId', 'buyerId', 'itemId'):
            for value in (None, '9999'):
                raw = official_response()
                raw['data'][field] = value
                self.assertFalse(self.validate(raw).allowed, (field, value))
        raw = official_response()
        raw['data']['components'].append({'render': 'orderInfoVO', 'data': {'buyerUserId': '9999'}})
        self.assertFalse(self.validate(raw).allowed)

    def test_peer_id_requires_server_seller_identity_and_order_info_is_supported(self):
        raw = official_response()
        raw['data']['peerUserId'] = raw['data'].pop('buyerId')
        self.assertTrue(self.validate(raw).allowed)
        del raw['data']['sellerId']
        self.assertFalse(self.validate(raw).allowed)
        raw = official_response()
        raw['data']['components'].append({'render': 'orderInfoVO', 'data': {
            'orderInfoList': [{'label': '订单编号', 'value': raw['data'].pop('orderId')}],
            'itemInfo': {'itemId': raw['data'].pop('itemId')}}})
        self.assertTrue(self.validate(raw).allowed)

    def test_refunds_partial_refund_unknown_and_missing_proof_rejected(self):
        for fields in ({'refundStatus': None}, {'refundStatus': 123}, {'refundStatus': 'REFUNDING'},
                       {'refundStatus': 'REFUNDED'}, {'refundStatus': 'PARTIAL_REFUNDED'},
                       {'refundAmount': '0.01'}, {'refundAmount': 'NaN'}, {'refundAmount': '-1'},
                       {'hasRefund': True}, {'disputeStatus': 1}, {'disputeStatus': 5}):
            raw = official_response()
            raw['data'].update(fields)
            self.assertFalse(self.validate(raw).allowed, fields)
        for title in ('退款中', '部分退款成功', '待付款', '交易关闭', '未知状态'):
            self.assertFalse(self.validate(official_response(title=title)).allowed, title)
        raw = official_response()
        raw['data'].pop('refundStatus')
        raw['data']['isRefunded'] = False
        self.assertFalse(self.validate(raw).allowed)
        raw['data']['inRefund'] = 'false'
        self.assertTrue(self.validate(raw).allowed)
        raw['data']['inRefund'] = 'true'
        self.assertFalse(self.validate(raw).allowed)
        for unknown in (0, 'unknown', None):
            raw['data']['inRefund'] = unknown
            self.assertFalse(self.validate(raw).allowed)

    def test_withdrawn_refund_requires_explicit_zero_actual_refund(self):
        raw = official_response()
        raw['data']['refundStatus'] = 'WITHDRAWN'
        self.assertFalse(self.validate(raw).allowed)
        raw['data']['actualRefundAmount'] = '0.00'
        self.assertTrue(self.validate(raw).allowed)
        raw['data']['actualRefundAmount'] = '1'
        self.assertFalse(self.validate(raw).allowed)

    def test_unpaid_contradiction_and_malformed_responses_fail_closed(self):
        raw = official_response()
        raw['data']['components'][0]['data']['orderStatusNodeList'][0]['completed'] = False
        self.assertFalse(self.validate(raw).allowed)
        for raw in ({}, None, {'ret': ['SUCCESS::ok'], 'data': None},
                    {'ret': ['SUCCESS::ok'], 'data': {'components': None}},
                    {'ret': ['FAIL_SYS_SESSION_EXPIRED::secret internal details']},
                    {'ret': ['FAIL_BIZ_ORDER_NOT_EXIST::secret internal details']}):
            result = self.validate(raw)
            self.assertFalse(result.allowed)
            self.assertNotIn('secret', result.detail)


class SelfServiceTests(unittest.IsolatedAsyncioTestCase):
    make_instance = base_tests.CommandTests.make_instance
    records = base_tests.CommandTests.records
    modify_item = base_tests.CommandTests.modify_item

    def setUp(self):
        base_tests.CommandTests.setUp(self)
        self.clock, self.mid = self.now, 10000
        self.api = AsyncMock(return_value=OrderEntitlement(True, 'code_order_valid', '有效订单'))
        for target, value in (
            ('app.services.xianyu.code_self_service.async_session_maker', self.sessions),
            ('app.services.xianyu.code_self_service.now_ms', lambda: self.clock),
            ('app.services.xianyu.code_generation_service.time.time', lambda: self.clock / 1000),
            ('app.services.xianyu.code_self_service.check_order_entitlement', self.api),
        ):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.auto = self_service.CodeSelfService('1001', self.instance)

    def event(self, text='我想要口令', **kwargs):
        self.mid += 1
        return message(mid=str(self.mid), text=text, seller=kwargs.pop('buyer', '2001'), timestamp=self.clock, **kwargs)

    def attempts(self):
        with Session(self.engine) as db:
            return db.query(Attempt).order_by(Attempt.created_ms, Attempt.id).all()

    def flows(self):
        with Session(self.engine) as db:
            return db.query(Flow).all()

    def sent(self, instance=None):
        return [call.args[3] for call in (instance or self.instance).send_msg.await_args_list]

    async def begin(self, **kwargs):
        self.clock += 1
        self.assertTrue(await self.auto.handle(self.event(**kwargs), self.ws))

    async def submit(self, order=ORDER, **kwargs):
        self.clock += 1
        event = self.event(order, **kwargs)
        self.assertTrue(await self.auto.handle(event, self.ws))
        return event

    async def cycle(self, **kwargs):
        await self.begin(**kwargs)
        return await self.submit(**kwargs)

    def send_behavior(self, target, status):
        original = self.instance.send_msg.side_effect
        async def send(ws, chat, buyer, text):
            if text != target:
                return await original(ws, chat, buyer, text)
            if status == 'failed':
                return {'success': False, 'error_message': '模拟拦截'}
            if status == 'exception':
                raise ConnectionError('simulated disconnect')
            return {'success': True}  # no explicit receipt
        self.instance.send_msg.side_effect = send

    async def test_success_has_exact_ordered_messages_and_persisted_quota(self):
        await self.cycle()
        self.assertEqual(self.sent(), [self_service.PROMPT, self_service.PROCESSING, '口令生成', 'aB09zZ'])
        self.assertEqual(self.runner.await_count, 1)
        self.api.assert_awaited_once_with(cookies='test-only', seller_id='1001', buyer_id='2001', item_id='3001', order_no=ORDER)
        record = next(a for a in self.attempts() if a.order_no)
        self.assertEqual((record.quota_state, record.quota_used), ('success', 1))
        log = next(r for r in self.records() if r.order_no)
        self.assertEqual(log.context_snapshot['entry_source'], 'buyer_auto')
        self.assertEqual(log.reply_text, '\n'.join(self.sent()[1:]))
        self.assertTrue(all(c.args[1:3] == ('4001', '2001') for c in self.instance.send_msg.await_args_list))

    async def test_official_response_contract_reaches_generation_and_refunds_still_block(self):
        fixtures = Path(__file__).parent / 'fixtures'
        detail, sold = [json.loads((fixtures / f'code_order_shipped_{kind}.json').read_text())
                        for kind in ('detail', 'sold')]
        order = detail['data']['orderId']
        def check(**kwargs):
            kwargs.pop('cookies')  # The request-layer tests independently verify cookie binding.
            return validate_order_response(detail, sold_response=sold,
                authenticated_user_id='1001', **kwargs)
        self.api.side_effect = check
        sold['data']['module']['items'][0]['priceVO']['refundFee'] = '0.01'
        await self.begin()
        await self.submit(order)
        self.assertIn('已退款', self.sent()[-1])
        self.runner.assert_not_awaited()
        self.assertTrue(all(a.quota_state == 'none' for a in self.attempts()))
        sold['data']['module']['items'][0]['priceVO']['refundFee'] = '0.00'
        await self.submit(order)
        self.assertEqual(self.sent()[-3:], [self_service.PROCESSING, '口令生成', 'aB09zZ'])
        self.runner.assert_awaited_once()
        self.assertEqual(sum(a.quota_state == 'success' for a in self.attempts()), 1)

    async def test_keyword_cooldown_is_fixed_and_other_buyers_are_independent(self):
        await self.begin()
        expiry = self.flows()[0].expires_ms
        self.clock += 1000
        for _ in range(3):
            await self.auto.handle(self.event('口令呢'), self.ws)
        self.assertEqual(self.sent(), [self_service.PROMPT])
        self.assertEqual(self.flows()[0].expires_ms, expiry)
        await self.begin(chat='other', buyer='2002')
        self.assertEqual(self.sent(), [self_service.PROMPT] * 2)

    async def test_non_numeric_format_reminded_once_no_trim_or_unicode_conversion(self):
        await self.begin()
        for text in ('订单号' + ORDER, ' ' + ORDER, ORDER + '\n', '１２３４', '9' * 65, '你好'):
            await self.auto.handle(self.event(text), self.ws)
        image = self.event('[图片]')
        image['is_plain_text'] = False
        await self.auto.handle(image, self.ws)
        self.assertEqual(self.sent(), [self_service.PROMPT, self_service.FORMAT_HINT])
        self.api.assert_not_awaited()
        await self.submit()
        self.runner.assert_awaited_once()

    async def test_validation_failure_allows_correction_without_extending_wait(self):
        await self.begin()
        expiry = self.flows()[0].expires_ms
        self.api.return_value = OrderEntitlement(False, 'code_order_missing', '订单不存在，请检查订单号')
        await self.submit('123456')
        self.assertEqual(self.sent()[-1], '机器人回复：订单异常，订单不存在，请检查订单号（五分钟内可重新发送订单号）')
        self.assertEqual(self.flows()[0].state, 'waiting')
        self.assertEqual(self.flows()[0].expires_ms, expiry)
        self.api.return_value = OrderEntitlement(True, 'code_order_valid', '有效订单')
        await self.submit()
        self.assertEqual(self.runner.await_count, 1)

    async def test_wait_expiration_is_silent_and_next_keyword_restarts(self):
        await self.begin()
        self.clock += self_service.WINDOW_MS
        self.assertFalse(await self.auto.handle(self.event(ORDER), self.ws))
        self.assertEqual(self.sent(), [self_service.PROMPT])
        await self.begin()
        self.assertEqual(self.sent(), [self_service.PROMPT] * 2)

    async def test_seller_card_quoted_auto_missing_stale_and_pre_enable_never_trigger(self):
        invalid = [self.event(buyer='1001')]
        for key, value in [('isAutoReply', True), ('isSystem', True)]:
            event = self.event()
            event['raw_message']['1']['10'][key] = value
            invalid.append(event)
        for timestamp in (0, self.clock - 301000, self.clock + 1000):
            event = self.event()
            event['raw_message']['1']['5'] = timestamp
            invalid.append(event)
        quote = self.event()
        quote['raw_message']['1']['6']['3']['5'] = json.dumps({'contentType': 1, 'text': {'text': '我想要口令'}, 'quote': {'text': 'x'}})
        invalid.append(quote)
        card = self.event()
        card['is_plain_text'] = False
        invalid.append(card)
        for event in invalid:
            self.assertFalse(await self.auto.handle(event, self.ws))
        self.modify_item(**{ENABLED_AT_KEY: self.clock + 1})
        await self.auto.handle(self.event(), self.ws)
        self.assertFalse(self.sent())

    async def test_closed_item_and_inactive_account_do_not_send(self):
        self.modify_item(**{ENABLED_KEY: False})
        self.assertFalse(await self.auto.handle(self.event(), self.ws))
        self.modify_item(**{ENABLED_KEY: True})
        with Session(self.engine) as db:
            db.get(YtxAccount, 1).status = 'disabled'
            db.commit()
        await self.auto.handle(self.event(), self.ws)
        self.assertFalse(self.sent())

    async def test_unique_fallback_context_and_conflicting_item_fail_closed(self):
        await self.begin(item='')
        self.assertEqual(self.sent(), [self_service.PROMPT])
        conflict = self.event(chat='other')
        conflict['raw_message']['1']['10']['reminderUrl'] = 'https://www.goofish.com/item?itemId=9999'
        await self.auto.handle(conflict, self.ws)
        await self.auto.handle(self.event(item='', chat='unknown'), self.ws)
        self.assertEqual(len(self.sent()), 1)

    async def test_duplicate_sources_and_log_deletion_never_replay_generation(self):
        await self.begin()
        event = self.event(ORDER)
        forwarded = deepcopy(event)
        forwarded['raw_message']['transport'] = 'web'
        await asyncio.gather(*(self.auto.handle(e, self.ws) for e in [event, forwarded, event]))
        with Session(self.engine) as db:
            db.query(ReplyLog).delete()
            db.commit()
        await self_service.CodeSelfService('1001', self.instance).handle(event, self.ws)
        self.assertEqual(self.runner.await_count, 1)
        self.assertEqual(sum(a.quota_state == 'success' for a in self.attempts()), 1)

    async def test_fingerprint_duplicate_order_text_requires_original_timestamp(self):
        await self.begin()
        event = self.event(ORDER)
        event['raw_message']['1']['3'] = ''
        forwarded = deepcopy(event)
        forwarded['raw_message']['transport'] = 'web'
        await self.auto.handle(event, self.ws)
        await self.auto.handle(forwarded, self.ws)
        self.assertEqual(self.runner.await_count, 1)

    async def test_daily_limit_three_and_log_cleanup_does_not_reset(self):
        for _ in range(3):
            await self.cycle()
            self.clock += self_service.WINDOW_MS + 1
        with Session(self.engine) as db:
            db.query(ReplyLog).delete()
            db.commit()
        await self.cycle()
        self.assertEqual(self.sent()[-1], self_service.LIMIT_HINT)
        self.assertEqual(self.runner.await_count, 3)

    async def test_order_cooldown_across_chats_and_midnight_uses_beijing_day(self):
        self.clock = int(datetime.fromisoformat('2026-10-26T23:59:59+08:00').timestamp() * 1000)
        await self.cycle()
        self.clock += 2000
        await self.cycle(chat='another-chat')
        self.assertIn('秒后再领取', self.sent()[-1])
        self.clock += self_service.WINDOW_MS + 1
        await self.cycle()
        counted = [a for a in self.attempts() if a.quota_state == 'success']
        self.assertEqual([a.quota_day for a in counted], ['2026-10-26', '2026-10-27'])
        self.assertEqual([a.quota_used for a in counted], [1, 1])

    async def test_concurrent_submissions_do_not_queue_and_other_account_can_progress(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def validate(**kw):
            if kw['seller_id'] == '1001':
                started.set()
                await release.wait()
            return OrderEntitlement(True, 'code_order_valid', '有效订单')
        self.api.side_effect = validate
        await self.begin()
        first = asyncio.create_task(self.auto.handle(self.event(ORDER), self.ws))
        await started.wait()
        await self.auto.handle(self.event(ORDER), self.ws)
        await self.auto.handle(self.event('99999'), self.ws)
        other = self.make_instance('1002')
        service = self_service.CodeSelfService('1002', other)
        await service.handle(self.event(buyer='2002', chat='4002'), self.ws)
        await service.handle(self.event(ORDER, buyer='2002', chat='4002'), self.ws)
        self.assertEqual(self.sent(other)[-1], 'aB09zZ')
        release.set()
        await first
        self.assertEqual(self.api.await_count, 2)
        self.assertEqual(self.runner.await_count, 2)

    async def test_same_order_reservation_blocks_parallel_other_chat(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def generate():
            started.set()
            await release.wait()
            return 'aB09zZ'
        self.runner.side_effect = generate
        await self.begin()
        first = asyncio.create_task(self.auto.handle(self.event(ORDER), self.ws))
        await started.wait()
        await self.cycle(chat='other')
        self.assertEqual(self.sent()[-1], '该订单正在处理，请稍后再试')
        release.set()
        await first
        self.runner.assert_awaited_once()
        self.assertTrue(any('ytx_accounts' in sql and 'FOR UPDATE' in sql for sql in self.executed_sql))

    async def test_restart_preserves_wait_but_never_replays_busy_attempt(self):
        await self.begin()
        self.auto = self_service.CodeSelfService('1001', self.instance)
        await self.submit()
        self.runner.assert_awaited_once()
        self.clock += self_service.WINDOW_MS + 1
        await self.begin()
        event = self.event(ORDER)
        _, action = await self.auto._accept(event)
        await self.auto._reserve(action, dispatch=True)
        self.clock += self_service.BUSY_MS + 1
        await self_service.CodeSelfService('1001', self.instance).handle(event, self.ws)
        attempt = next(a for a in self.attempts() if a.id == action.attempt_id)
        self.assertEqual(attempt.quota_state, 'unknown')
        self.runner.assert_awaited_once()

    async def test_interruption_before_send_releases_quota_and_never_replays(self):
        await self.begin()
        event = self.event(ORDER)
        _, action = await self.auto._accept(event)
        await self.auto._reserve(action)
        self.clock += self_service.BUSY_MS + 1
        await self.auto.handle(event, self.ws)
        attempt = next(a for a in self.attempts() if a.id == action.attempt_id)
        self.assertEqual(attempt.quota_state, 'released')
        self.runner.assert_not_awaited()

    async def test_failed_final_send_releases_quota_unknown_reserves_it(self):
        for status in ('failed', 'unknown', 'exception'):
            with self.subTest(status=status):
                self.instance = self.make_instance('1001')
                self.auto = self_service.CodeSelfService('1001', self.instance)
                self.send_behavior('aB09zZ', status)
                await self.cycle()
                last = max((a for a in self.attempts() if a.order_no), key=lambda a: a.created_ms)
                self.assertEqual(last.quota_state, 'released' if status == 'failed' else 'unknown')
                self.clock += self_service.WINDOW_MS + 1

    async def test_socket_error_flag_is_unknown_even_when_transport_returns_success_false(self):
        original = self.instance.send_msg.side_effect
        async def send(ws, chat, buyer, text):
            if text == 'aB09zZ':
                return {'success': False, 'delivery_uncertain': True, 'error_message': '网络中断'}
            return await original(ws, chat, buyer, text)
        self.instance.send_msg.side_effect = send
        await self.cycle()
        self.assertEqual(next(a for a in self.attempts() if a.order_no).quota_state, 'unknown')

    async def test_midnight_between_reservation_and_dispatch_moves_quota_to_send_day(self):
        self.clock = int(datetime.fromisoformat('2026-10-26T23:59:59+08:00').timestamp() * 1000)
        async def generate():
            self.clock += 2000
            return 'aB09zZ'
        self.runner.side_effect = generate
        await self.cycle()
        self.assertEqual(next(a for a in self.attempts() if a.order_no).quota_day, '2026-10-27')

    async def test_prompt_processing_and_marker_unknown_stop_pipeline(self):
        for target in (self_service.PROMPT, self_service.PROCESSING, '口令生成'):
            self.instance = self.make_instance('1001')
            self.auto = self_service.CodeSelfService('1001', self.instance)
            self.send_behavior(target, 'unknown')
            await self.begin()
            if target != self_service.PROMPT:
                await self.submit()
            self.assertEqual(self.sent()[-1], target)
            self.assertFalse(any(a.quota_state in self_service.COUNTED for a in self.attempts()))
            self.clock += self_service.WINDOW_MS + 1
        self.runner.assert_not_awaited()

    async def test_script_failures_release_and_send_generic_error(self):
        for reason in ('code_script_failed', 'code_script_timeout', 'code_invalid_output'):
            self.runner.side_effect = commands.CommandStopped(reason, '测试失败')
            await self.cycle()
            self.assertEqual(self.sent()[-1], self_service.SCRIPT_ERROR)
            self.assertFalse(any(a.quota_state in self_service.COUNTED for a in self.attempts()))
            self.clock += self_service.WINDOW_MS + 1

    async def test_disable_or_reenable_during_script_never_sends_code(self):
        for values in ({ENABLED_KEY: False}, {ENABLED_AT_KEY: self.clock + 1000}):
            self.modify_item(**{ENABLED_KEY: True, ENABLED_AT_KEY: self.clock - 1000})
            async def generate():
                self.modify_item(**values)
                return 'aB09zZ'
            self.runner.side_effect = generate
            await self.cycle()
            self.assertNotIn('aB09zZ', self.sent())
            self.clock += self_service.WINDOW_MS + 1
        self.assertFalse(any(a.quota_state in self_service.COUNTED for a in self.attempts()))

    async def test_cancel_while_final_send_keeps_unknown_reservation(self):
        started = asyncio.Event()
        original = self.instance.send_msg.side_effect
        async def send(ws, chat, buyer, text):
            if text == 'aB09zZ':
                started.set()
                await asyncio.Event().wait()
            return await original(ws, chat, buyer, text)
        self.instance.send_msg.side_effect = send
        await self.begin()
        event = self.event(ORDER)
        task = asyncio.create_task(self.auto.handle(event, self.ws))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(next(a for a in self.attempts() if a.order_no).quota_state, 'unknown')
        await self.auto.handle(event, self.ws)
        self.runner.assert_awaited_once()

    async def test_automatic_marker_echo_after_restart_never_pauses_ai(self):
        await self.cycle()
        reply = AutoReplyService('1001', self.instance)
        reply._record_auto_reply_log = AsyncMock()
        reply._pause_ai_reply_after_manual_message = AsyncMock()
        marker = message(mid='marker-echo', text='口令生成', timestamp=self.clock)
        await reply.handle_chat_message(marker, self.ws)
        reply._pause_ai_reply_after_manual_message.assert_not_awaited()
        self.runner.assert_awaited_once()

    async def test_waiting_consumes_buyer_messages_before_ai_pause_and_native_web_share_path(self):
        import base64
        reply = AutoReplyService('1001', self.instance)
        reply.get_reply = AsyncMock()
        reply._record_auto_reply_log = AsyncMock()
        with patch.object(MessageHandler, '_load_message_expire_time', return_value=3600):
            handler = MessageHandler('1001')
        handler.set_chat_message_handler(reply.handle_chat_message)
        for event in (self.event(), self.event(ORDER)):
            frame = {'body': {'syncPushPackage': {'data': [{'data': base64.b64encode(json.dumps(event['raw_message']).encode()).decode()}]}}}
            await asyncio.gather(handler.handle_message(frame, self.ws), handler.handle_message(frame, self.ws, acknowledge=False))
        reply.get_reply.assert_not_awaited()
        reply._record_auto_reply_log.assert_not_awaited()
        self.runner.assert_awaited_once()
