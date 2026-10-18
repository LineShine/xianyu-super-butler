"""Live chat pushes reach automation without replaying history or blocking the UI."""
import asyncio
import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services.chat_new.im_client import GoofishImClient
from app.services.websocket_client import WebSocketServiceClient


def push():
    event = {'1': {'2': 'TEST-CHAT@goofish', '3': 'TEST-MSG', '5': 1234567890000,
                   '10': {'senderUserId': 'TEST-BUYER', 'reminderContent': '在吗'}}}
    return {'body': {'syncPushPackage': {'data': [
        {'data': base64.b64encode(json.dumps(event).encode()).decode()}
    ]}}}


class PushForwardingTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        client = GoofishImClient('TEST-SELLER', 'unb=TEST-SELLER')
        client._send_raw = AsyncMock()
        return client

    async def test_live_push_forwards_without_frontend_subscribers(self):
        client = self.make_client()
        frame = push()
        with patch('app.services.chat_new.im_client.websocket_client.forward_im_push', new_callable=AsyncMock) as forward:
            forward.return_value = {'success': True}
            await client._handle_message(json.dumps(frame))
            await client._automation_task
            forward.assert_awaited_once_with('TEST-SELLER', frame['body']['syncPushPackage']['data'])
            client._send_raw.assert_awaited_once()

    async def test_frontend_receives_while_forwarding_is_waiting(self):
        client = self.make_client()
        callback = AsyncMock()
        client.add_push_callback(callback)
        release = asyncio.Event()
        async def slow(*args):
            await release.wait()
            return {'success': True}
        with patch('app.services.chat_new.im_client.websocket_client.forward_im_push', side_effect=slow):
            await asyncio.wait_for(client._handle_message(json.dumps(push())), .5)
            callback.assert_awaited_once()
            self.assertEqual(callback.await_args.args[0]['message']['text'], '在吗')
            release.set()
            await client._automation_task

    async def test_history_response_and_heartbeat_are_not_forwarded(self):
        client = self.make_client()
        frame = push()
        frame['headers'] = {'mid': 'history-request'}
        future = asyncio.get_running_loop().create_future()
        client._pending['history-request'] = future
        with patch('app.services.chat_new.im_client.websocket_client.forward_im_push', new_callable=AsyncMock) as forward:
            await client._handle_message(json.dumps(frame))
            await client._handle_message(json.dumps({'code': 200, 'headers': {'mid': 'heartbeat'}}))
            self.assertIsNone(client._automation_task)
            self.assertTrue(future.done())
            forward.assert_not_awaited()

    async def test_failed_forward_does_not_stop_later_pushes(self):
        client = self.make_client()
        with patch('app.services.chat_new.im_client.websocket_client.forward_im_push', new_callable=AsyncMock) as forward:
            forward.side_effect = [TimeoutError(), {'success': True}]
            await client._handle_push_message(push())
            await client._handle_push_message(push())
            await client._automation_task
            self.assertEqual(forward.await_count, 2)
            self.assertTrue(client._automation_queue.empty())

    async def test_internal_forward_uses_bounded_retry_timeout(self):
        client = WebSocketServiceClient()
        client.http_client = AsyncMock()
        await client.forward_im_push('TEST-SELLER', [{'data': 'fixture'}])
        self.assertTrue(client.http_client.post.await_args.args[0].endswith('/internal/accounts/TEST-SELLER/im-push'))
        self.assertEqual(client.http_client.post.await_args.kwargs['timeout'], 4)
        self.assertEqual(client.http_client.post.await_args.kwargs['max_retries'], 3)


class ChatReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_subscription_restores_upstream_after_restart(self):
        from types import SimpleNamespace
        from app.api.routes import chat_new_ws
        ws = AsyncMock()
        manager = SimpleNamespace(clients={}, get_or_connect=AsyncMock(),
            register_ws_client=AsyncMock(), unregister_ws_client=AsyncMock())
        with patch.object(chat_new_ws, '_authenticate_ws_user', AsyncMock(return_value=SimpleNamespace(id=1))), \
             patch.object(chat_new_ws, '_user_can_access_account', AsyncMock(return_value=True)), \
             patch.object(chat_new_ws, 'get_im_session_manager', return_value=manager), \
             patch.object(chat_new_ws, '_receive_loop', AsyncMock()):
            await chat_new_ws.chat_new_websocket(ws, 'TEST-SELLER', token='fixture')
        manager.get_or_connect.assert_awaited_once_with('TEST-SELLER')
        manager.register_ws_client.assert_awaited_once_with('TEST-SELLER', ws)
        ws.close.assert_not_awaited()

    async def test_unauthorized_user_cannot_restore_upstream(self):
        from app.api.routes import chat_new_ws
        ws = AsyncMock()
        with patch.object(chat_new_ws, '_authenticate_ws_user', AsyncMock(return_value=None)), \
             patch.object(chat_new_ws, 'get_im_session_manager') as manager:
            await chat_new_ws.chat_new_websocket(ws, 'TEST-SELLER', token='invalid')
        manager.assert_not_called()
        ws.close.assert_awaited_once_with(code=4401)
