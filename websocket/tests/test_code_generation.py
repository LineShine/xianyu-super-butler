"""No live IM calls: real SQL claims + simulated native/Web pushes and acknowledgements."""
import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.xianyu import code_generation_service as commands
from app.services.xianyu.auto_reply_service import AutoReplyService
from app.services.xianyu.message_handler import MessageHandler
from app.services.xianyu.outgoing_message_tracker import OutgoingMessageTracker
from app.services.xianyu.xianyu_async import XianyuAsync
from common.models.auto_reply_message_log import YtxAutoReplyMessageLog as ReplyLog
from common.models.ytx_account import YtxAccount
from common.models.ytx_catalog_item import YtxCatalogItem
from common.models.ytx_order import YtxOrder
from common.models.code_self_service import CodeSelfServiceAttempt, CodeSelfServiceSession
from common.services.code_generation_config import ENABLED_KEY, ENABLED_AT_KEY


def message(mid='5001', text='生成口令', seller='1001', chat='4001', item='3001', timestamp=None):
    timestamp = int(time.time() * 1000) - 500 if timestamp is None else timestamp
    raw = {'1': {'2': f'{chat}@goofish', '3': mid, '5': timestamp,
                 '6': {'3': {'5': json.dumps({'contentType': 1, 'text': {'text': text}})}},
                 '10': {'senderUserId': seller, 'senderNick': '测试卖家', 'reminderContent': text,
                        'extJson': json.dumps({'itemId': item})}}}
    return dict(send_user_id=seller, send_user_name='测试卖家', send_message=text,
                chat_id=chat, item_id=item, is_plain_text=True, raw_message=raw)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        for model in (YtxAccount, YtxCatalogItem, ReplyLog, YtxOrder, CodeSelfServiceAttempt, CodeSelfServiceSession):
            model.__table__.create(self.engine)
        self.addCleanup(self.engine.dispose)
        self.counter = 100
        self.executed_sql = []
        self.now = int(time.time() * 1000)
        with Session(self.engine) as db:
            for n in (1, 2):
                db.add(YtxAccount(id=n, owner_id=n, account_id=f'100{n}', cookie='test-only',
                                 login_method='test', status='active'))
                db.add(YtxCatalogItem(id=n, owner_id=n, account_pk=n, item_id='3001', title='模拟商品',
                    created_at=datetime.now(), updated_at=datetime.now(),
                    metadata_json={ENABLED_KEY: True, ENABLED_AT_KEY: self.now - 2000}))
                db.add(ReplyLog(id=n, owner_id=n, account_pk=n, account_id=f'100{n}', chat_id=f'400{n}',
                    sender_user_id=f'200{n}', item_id='3001', source_message='测试买家上下文',
                    decision_reason='chat_paused', reply_strategy='none', process_status='skipped'))
            db.commit()

        @asynccontextmanager
        async def sessions():
            with Session(self.engine, expire_on_commit=False) as db:
                def add(record):
                    if isinstance(record, ReplyLog) and record.id is None:
                        self.counter += 1
                        record.id = self.counter
                    db.add(record)
                def execute(stmt):
                    self.executed_sql.append(str(stmt))
                    return db.execute(stmt)
                yield SimpleNamespace(execute=AsyncMock(side_effect=execute), add=add,
                    flush=AsyncMock(side_effect=db.flush), commit=AsyncMock(side_effect=db.commit))
        self.sessions = sessions

        @asynccontextmanager
        async def lock(*args, **kwargs):
            yield SimpleNamespace(is_locked=True, extend=AsyncMock(return_value=True))

        self.runner = AsyncMock(return_value='aB09zZ')
        for target, value in [('async_session_maker', sessions), ('distributed_lock', lock), ('run_code_script', self.runner)]:
            p = patch.object(commands, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('app.services.xianyu.auto_reply_service.async_session_maker', sessions)
        p.start()
        self.addCleanup(p.stop)
        self.instance = self.make_instance('1001')
        self.service = commands.CodeGenerationService('1001', self.instance)
        self.ws = SimpleNamespace(send=AsyncMock())

    def make_instance(self, account):
        instance = SimpleNamespace(myid=account, cookie_id=account, _pending_mid_futures={},
            _extract_send_reject_reason=XianyuAsync._extract_send_reject_reason)
        async def send(ws, chat, buyer, text):
            future = asyncio.get_running_loop().create_future()
            future.set_result({'code': 200, 'body': {}})
            instance._pending_mid_futures['out'] = future
            return {'success': True, 'mid': 'out', 'uuid': 'uuid-out', 'send_future': future}
        instance.send_msg = AsyncMock(side_effect=send)
        return instance

    def records(self):
        with Session(self.engine) as db:
            return db.query(ReplyLog).filter(ReplyLog.reply_strategy == 'code_generation').order_by(ReplyLog.id).all()

    def modify_item(self, **metadata):
        with Session(self.engine) as db:
            item = db.get(YtxCatalogItem, 1)
            item.metadata_json = {**item.metadata_json, **metadata}
            db.commit()

    async def test_seller_command_executes_after_committed_claim_and_targets_original_chat(self):
        async def generate():
            self.assertEqual(self.records()[0].process_status, 'processing')
            self.instance.current_chat = 'unrelated-page'
            return 'aB09zZ'
        self.runner.side_effect = generate
        self.assertTrue(await self.service.handle(message(), self.ws))
        self.instance.send_msg.assert_awaited_once_with(self.ws, '4001', '2001', 'aB09zZ')
        record = self.records()[0]
        self.assertEqual((record.send_status, record.decision_reason), ('success', 'code_sent'))
        self.assertEqual(record.reply_text, 'aB09zZ')
        self.assertTrue(any('ytx_accounts' in sql and 'FOR UPDATE' in sql for sql in self.executed_sql))
        self.assertFalse(self.instance._pending_mid_futures)

    async def test_buyer_extra_characters_quotes_cards_and_automatic_metadata_never_run(self):
        invalid = [message(text=text) for text in (' 生成口令', '生成口令 ', '生成口令\n', '\n口令生成', '生成口令！', '请生成口令', '“生成口令”')]
        invalid.append(message(seller='2001'))
        quoted = message()
        quoted['raw_message']['1']['6']['3']['5'] = json.dumps({'contentType': 1, 'text': {'text': '生成口令'}, 'quote': {'text': 'other'}})
        invalid.append(quoted)
        card = message()
        card['is_plain_text'] = False
        invalid.append(card)
        auto = message()
        auto['raw_message']['1']['10']['isAutoReply'] = True
        invalid.append(auto)
        for value in invalid:
            self.assertFalse(await self.service.handle(value, self.ws), value['send_message'])
        self.runner.assert_not_awaited()
        self.instance.send_msg.assert_not_awaited()

    async def test_missing_old_future_or_pre_enable_time_never_runs(self):
        for n, timestamp in enumerate((0, '', True, 'NaN', self.now - 301000, self.now + 50000, self.now - 3000)):
            await self.service.handle(message(mid=str(n), timestamp=timestamp), self.ws)
        self.assertEqual(len(self.records()), 7)
        self.runner.assert_not_awaited()

    async def test_closed_item_and_disabled_account_never_runs(self):
        self.modify_item(**{ENABLED_KEY: False})
        await self.service.handle(message(), self.ws)
        self.assertEqual(self.records()[-1].decision_reason, 'code_disabled')
        self.modify_item(**{ENABLED_KEY: True})
        with Session(self.engine) as db:
            db.get(YtxAccount, 1).status = 'disabled'
            db.commit()
        await self.service.handle(message(mid='5002'), self.ws)
        self.assertEqual(self.records()[-1].decision_reason, 'code_account_inactive')
        self.runner.assert_not_awaited()

    async def test_parallel_duplicates_and_new_service_replay_claim_once_new_ids_run_again(self):
        event = message()
        await asyncio.gather(*(self.service.handle(deepcopy(event), self.ws) for _ in range(12)))
        restarted = commands.CodeGenerationService('1001', self.instance)
        await restarted.handle(event, self.ws)
        self.assertEqual(self.runner.await_count, 1)
        await restarted.handle(message(mid='5002'), self.ws)
        self.assertEqual(self.runner.await_count, 2)
        self.assertEqual(len(self.records()), 2)

    async def test_fingerprint_includes_original_time_but_ignores_forwarding_wrapper(self):
        event = message(mid='')
        forwarded = deepcopy(event)
        forwarded['raw_message']['transport'] = 'web'
        await self.service.handle(event, self.ws)
        await self.service.handle(forwarded, self.ws)
        later = deepcopy(event)
        later['raw_message']['1']['5'] += 1
        await self.service.handle(later, self.ws)
        self.assertEqual(self.runner.await_count, 2)

    async def test_interrupted_durable_claim_never_regenerates_after_restart(self):
        event = message()
        claim = await self.service._claim(event)
        self.assertIsNotNone(claim)
        self.assertEqual(self.records()[0].process_status, 'processing')
        restarted = commands.CodeGenerationService('1001', self.instance)
        await restarted.handle(event, self.ws)
        self.runner.assert_not_awaited()
        self.assertEqual(self.records()[0].send_status, 'unknown')
        self.assertEqual(self.records()[0].decision_reason, 'code_send_unknown')

    async def test_conflicting_item_metadata_never_generates(self):
        event = message()
        event['raw_message']['1']['10']['reminderUrl'] = 'https://www.goofish.com/item?itemId=3002'
        await self.service.handle(event, self.ws)
        self.runner.assert_not_awaited()
        self.assertEqual(self.records()[0].decision_reason, 'code_context')

    async def test_disabled_account_during_script_never_sends(self):
        async def generate():
            with Session(self.engine) as db:
                db.get(YtxAccount, 1).status = 'disabled'
                db.commit()
            return 'aB09zZ'
        self.runner.side_effect = generate
        await self.service.handle(message(), self.ws)
        self.instance.send_msg.assert_not_awaited()
        self.assertEqual(self.records()[0].decision_reason, 'code_account_inactive')

    async def test_replaced_socket_never_sends_through_stale_connection(self):
        self.instance.ws = object()
        await self.service.handle(message(), self.ws)
        self.instance.send_msg.assert_not_awaited()
        self.assertEqual(self.records()[0].decision_reason, 'code_account_inactive')

    async def test_same_chat_serializes_and_other_accounts_progress_independently(self):
        first_started, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def generate():
            nonlocal count
            count += 1
            if count == 1:
                first_started.set()
                await release.wait()
            return 'aB09zZ'
        self.runner.side_effect = generate
        first = asyncio.create_task(self.service.handle(message(), self.ws))
        await first_started.wait()
        second = asyncio.create_task(self.service.handle(message(mid='5002'), self.ws))
        other = self.make_instance('1002')
        await commands.CodeGenerationService('1002', other).handle(message(seller='1002', chat='4002'), self.ws)
        other.send_msg.assert_awaited_once_with(self.ws, '4002', '2002', 'aB09zZ')
        self.assertEqual(count, 2)  # second command in chat 4001 is still queued
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.instance.send_msg.await_count, 2)

    async def test_fallback_context_uses_unique_same_account_chat_and_rejects_ambiguity(self):
        await self.service.handle(message(item=''), self.ws)
        self.instance.send_msg.assert_awaited_once()
        await self.service.handle(message(mid='5002', chat='missing'), self.ws)
        with Session(self.engine) as db:
            db.add(ReplyLog(id=20, owner_id=1, account_pk=1, account_id='1001', chat_id='4001',
                sender_user_id='2001', item_id='3002', source_message='其他商品', decision_reason='chat_paused'))
            db.commit()
        await self.service.handle(message(mid='5003', item=''), self.ws)
        self.assertEqual(self.records()[-1].decision_reason, 'code_context')
        self.assertEqual(self.runner.await_count, 1)

    async def test_order_fallback_and_conflicting_buyers_fail_closed(self):
        with Session(self.engine) as db:
            db.add(YtxOrder(id=1, owner_id=1, order_no='test-order', status='paid', account_id='1001',
                            chat_id='order-chat', buyer_id='2010', item_id='3001'))
            db.commit()
        await self.service.handle(message(chat='order-chat', item=''), self.ws)
        self.instance.send_msg.assert_awaited_once_with(self.ws, 'order-chat', '2010', 'aB09zZ')
        conflicting = message(mid='5002')
        conflicting['raw_message']['1']['10']['toUserId'] = '2999'
        await self.service.handle(conflicting, self.ws)
        self.assertEqual(self.records()[-1].decision_reason, 'code_context')

    async def test_disable_or_reenable_during_execution_drops_generated_code(self):
        for n, changes in enumerate(({ENABLED_KEY: False}, {ENABLED_KEY: True, ENABLED_AT_KEY: self.now + 1})):
            self.modify_item(**{ENABLED_KEY: True, ENABLED_AT_KEY: self.now - 2000})
            async def generate():
                self.modify_item(**changes)
                return 'aB09zZ'
            self.runner.side_effect = generate
            await self.service.handle(message(mid=str(n)), self.ws)
        self.instance.send_msg.assert_not_awaited()
        self.assertEqual([r.decision_reason for r in self.records()], ['code_disabled', 'code_stale'])

    async def test_script_errors_are_logged_without_buyer_error_messages_or_retry(self):
        for n, reason in enumerate(('code_script_failed', 'code_script_timeout', 'code_invalid_output')):
            self.runner.side_effect = commands.CommandStopped(reason, '测试错误')
            event = message(mid=str(n))
            await self.service.handle(event, self.ws)
            await self.service.handle(event, self.ws)
        self.assertEqual(self.runner.await_count, 3)
        self.instance.send_msg.assert_not_awaited()
        self.assertTrue(all(row.process_status == 'failed' for row in self.records()))

    async def test_rejected_no_ack_and_no_explicit_success_are_not_retried(self):
        responses = [{'code': 200, 'body': {'reason': '禁止发送', 'moreInfo': 'CSI_FORBID'}},
                     None, {}, {'code': 500}, {'code': 200}]
        for n, ack in enumerate(responses):
            future = asyncio.get_running_loop().create_future()
            if ack is not None:
                future.set_result(ack)
            self.instance.send_msg.side_effect = None
            self.instance.send_msg.return_value = {'success': True, 'mid': f'out-{n}', 'send_future': future}
            event = message(mid=str(n))
            with patch.object(commands, 'ACK_TIMEOUT', .01):
                await self.service.handle(event, self.ws)
                await self.service.handle(event, self.ws)
        self.assertEqual([row.send_status for row in self.records()], ['failed', 'unknown', 'unknown', 'failed', 'success'])
        self.assertEqual(self.instance.send_msg.await_count, 5)
        self.assertEqual(self.runner.await_count, 5)

    async def test_send_failure_and_cancellation_stay_claimed(self):
        self.instance.send_msg.side_effect = None
        self.instance.send_msg.return_value = {'success': False, 'error_message': '已断线'}
        await self.service.handle(message(), self.ws)
        self.assertEqual(self.records()[0].send_status, 'failed')
        started = asyncio.Event()
        async def block():
            started.set()
            await asyncio.Event().wait()
        self.runner.side_effect = block
        event = message(mid='cancelled')
        task = asyncio.create_task(self.service.handle(event, self.ws))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.records()[-1].decision_reason, 'code_cancelled')
        await self.service.handle(event, self.ws)
        self.assertEqual(self.runner.await_count, 2)

    async def test_unified_native_and_web_pushes_preserve_manual_pause_and_dedupe(self):
        reply = AutoReplyService('1001', self.instance)
        reply._record_auto_reply_log = AsyncMock()
        reply._pause_ai_reply_after_manual_message = AsyncMock(return_value=True)
        with patch.object(MessageHandler, '_load_message_expire_time', return_value=3600):
            handler = MessageHandler('1001')
        handler.set_chat_message_handler(reply.handle_chat_message)
        raw = message(text='口令生成')['raw_message']
        frame = {'body': {'syncPushPackage': {'data': [{'data': base64.b64encode(json.dumps(raw).encode()).decode()}]}}}
        await asyncio.gather(handler.handle_message(frame, self.ws), handler.handle_message(frame, self.ws, acknowledge=False))
        self.runner.assert_awaited_once()
        reply._pause_ai_reply_after_manual_message.assert_awaited_once()
        reply._record_auto_reply_log.assert_not_awaited()

    async def test_automatic_command_echo_never_pauses_or_runs(self):
        tracker = OutgoingMessageTracker()
        tracker.remember('1001', '4001', '生成口令')
        reply = AutoReplyService('1001', self.instance)
        reply._record_auto_reply_log = AsyncMock()
        reply._pause_ai_reply_after_manual_message = AsyncMock()
        with patch('app.services.xianyu.auto_reply_service.outgoing_message_tracker', tracker):
            await reply.handle_chat_message(message(), self.ws)
        reply._pause_ai_reply_after_manual_message.assert_not_awaited()
        self.runner.assert_not_awaited()

    async def test_restart_automatic_echo_excluded_using_persisted_outgoing_log(self):
        with Session(self.engine) as db:
            db.add(ReplyLog(id=20, owner_id=1, account_pk=1, account_id='1001', chat_id='4001',
                sender_user_id='2001', reply_strategy='ai', reply_text='生成口令', process_status='success',
                created_at=datetime.now()))
            db.commit()
        await self.service.handle(message(), self.ws)
        self.runner.assert_not_awaited()
        self.assertEqual(self.records()[0].decision_reason, 'automatic_message_echo')


class ScriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_script_returns_unmodified_six_characters(self):
        self.assertTrue(commands.SCRIPT_PATH.is_absolute())
        self.assertTrue(commands.SCRIPT_PATH.is_file())
        before = commands.SCRIPT_PATH.read_bytes()
        output = await commands.run_code_script()
        self.assertRegex(output, r'^[A-Za-z0-9]{6}$')
        self.assertEqual(commands.SCRIPT_PATH.read_bytes(), before)

    async def test_failure_invalid_output_and_timeout(self):
        for source, reason in [
            ('raise SystemExit(2)', 'code_script_failed'),
            ('print("invalid!!")', 'code_invalid_output'),
            ('print(" aB09zZ")', 'code_invalid_output'),
            ('print("aB09zZ"); print("extra")', 'code_invalid_output'),
            ('import sys,time;sys.stdout.write("aB09zZ");sys.stdout.flush();time.sleep(.02);print("extra")', 'code_invalid_output'),
            ('print("x" * 100000)', 'code_invalid_output'),
            ('import time;time.sleep(5)', 'code_script_timeout'),
        ]:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'fixture.py'
                path.write_text(source)
                with patch.object(commands, 'SCRIPT_PATH', path), patch.object(commands, 'SCRIPT_TIMEOUT', .2):
                    with self.assertRaises(commands.CommandStopped) as error:
                        await commands.run_code_script()
                    self.assertEqual(error.exception.reason, reason)

    async def test_cancel_reaps_child_and_uses_no_shell_or_chat_arguments(self):
        real_spawn = asyncio.create_subprocess_exec
        started, children = asyncio.Event(), []
        async def spawn(*args, **kwargs):
            self.assertEqual(len(args), 2)
            self.assertNotIn('shell', kwargs)
            process = await real_spawn(*args, **kwargs)
            children.append(process)
            started.set()
            return process
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.py'
            path.write_text('import time;time.sleep(10)')
            with patch.object(commands, 'SCRIPT_PATH', path), patch.object(commands.asyncio, 'create_subprocess_exec', spawn):
                task = asyncio.create_task(commands.run_code_script())
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNotNone(children[0].returncode)
