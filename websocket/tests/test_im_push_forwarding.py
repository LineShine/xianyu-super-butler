"""Sanitized forwarded/native push regressions: no platform or model calls."""
import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from app.api.deps import require_internal_auth
from app.api.routes.internal import router, ForwardImPushRequest, forward_im_push
from app.services.xianyu.message_handler import MessageHandler
from app.services.xianyu.xianyu_async import XianyuAsync


def event(message_id='TEST-MSG', timestamp=1234567890000):
    return {'1': {'2': 'TEST-CHAT@goofish', '3': message_id, '5': timestamp,
                   '10': {'senderUserId': 'TEST-BUYER', 'reminderContent': '在吗'}}}


def frame(message):
    return {'body': {'syncPushPackage': {'data': [
        {'data': base64.b64encode(json.dumps(message).encode()).decode()}
    ]}}}


class ForwardedPushTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.object(MessageHandler, '_load_message_expire_time', return_value=3600):
            self.handler = MessageHandler('TEST-SELLER')
        self.ws = SimpleNamespace(send=AsyncMock())
        self.callback = AsyncMock()
        self.handler.set_chat_message_handler(self.callback)

    async def test_native_and_forwarded_duplicate_dispatch_once_with_one_ack(self):
        message = frame(event())
        await asyncio.gather(
            self.handler.handle_message(message, self.ws),
            self.handler.handle_message(message, self.ws, acknowledge=False),
            self.handler.handle_message(message, self.ws, acknowledge=False),
        )
        self.callback.assert_awaited_once()
        self.assertEqual(self.callback.await_args.args[0]['send_message'], '在吗')
        self.ws.send.assert_awaited_once()

    async def test_distinct_message_ids_with_same_text_are_not_transport_duplicates(self):
        await self.handler._process_single_message(event('FIRST'), self.ws)
        await self.handler._process_single_message(event('SECOND'), self.ws)
        self.assertEqual(self.callback.await_count, 2)

    async def test_missing_id_deduplicates_identical_payload_but_preserves_later_events(self):
        first = event('', 1234567890000)
        await asyncio.gather(*(self.handler._process_single_message(first, self.ws) for _ in range(12)))
        self.callback.assert_awaited_once()
        await self.handler._process_single_message(event('', 1234567891000), self.ws)
        self.assertEqual(self.callback.await_count, 2)

    async def test_normal_auto_reply_callback_receives_forwarded_push(self):
        client = XianyuAsync.__new__(XianyuAsync)
        client.cookie_id = client.myid = 'TEST-SELLER'
        client._process_order_status = AsyncMock()
        reply = SimpleNamespace(is_rate_request_message=lambda _: False,
                                is_auto_delivery_trigger=lambda _: False,
                                handle_chat_message=AsyncMock())
        with patch.object(MessageHandler, '_load_message_expire_time', return_value=3600), \
             patch('app.services.xianyu.auto_reply_service.AutoReplyService', return_value=reply):
            await client.handle_message(frame(event()), self.ws, acknowledge=False)
        reply.handle_chat_message.assert_awaited_once()
        self.assertEqual(reply.handle_chat_message.await_args.args[0]['send_message'], '在吗')
        self.ws.send.assert_not_awaited()

    def manager(self, *, enabled=True, registered=True):
        tasks = []
        instance = SimpleNamespace(ws=self.ws, _registered_ws=self.ws if registered else None,
            connection_manager=SimpleNamespace(is_ws_closed=lambda _: False),
            _handle_message_with_semaphore=AsyncMock(),
            _create_tracked_task=lambda coro: tasks.append(asyncio.create_task(coro)))
        manager = SimpleNamespace(instances={'TEST-SELLER': instance}, get_cookie_status=lambda _: enabled)
        return manager, instance, tasks

    async def test_internal_endpoint_submits_to_existing_instance_without_ack(self):
        manager, instance, tasks = self.manager()
        payload = ForwardImPushRequest(sync_data=frame(event())['body']['syncPushPackage']['data'])
        with patch('app.services.xianyu.cookie_manager.get_manager', return_value=manager):
            response = await forward_im_push('TEST-SELLER', payload)
        self.assertTrue(response['success'])
        await asyncio.gather(*tasks)
        self.assertFalse(instance._handle_message_with_semaphore.await_args.kwargs['acknowledge'])

    async def test_disabled_account_is_not_dispatched(self):
        manager, instance, tasks = self.manager(enabled=False)
        with patch('app.services.xianyu.cookie_manager.get_manager', return_value=manager):
            response = await forward_im_push('TEST-SELLER', ForwardImPushRequest(sync_data=[{'data': 'fixture'}]))
        self.assertFalse(response['success'])
        self.assertEqual(tasks, [])

    async def test_unregistered_connection_is_retryable(self):
        manager, _, tasks = self.manager(registered=False)
        with patch('app.services.xianyu.cookie_manager.get_manager', return_value=manager):
            with self.assertRaises(HTTPException) as raised:
                await forward_im_push('TEST-SELLER', ForwardImPushRequest(sync_data=[{'data': 'fixture'}]))
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(tasks, [])

    async def test_endpoint_requires_internal_auth_and_rejects_invalid_payload(self):
        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/internal/accounts/TEST-SELLER/im-push', json={'sync_data': [{'data': 'fixture'}]})
            self.assertIn(response.status_code, (401, 403, 503))
            app.dependency_overrides[require_internal_auth] = lambda: None
            response = await client.post('/internal/accounts/TEST-SELLER/im-push', json={'sync_data': []})
            self.assertEqual(response.status_code, 422)
