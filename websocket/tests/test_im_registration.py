import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.services.xianyu.xianyu_async import XianyuAsync
from app.services.xianyu.connection_errors import is_token_or_session_failure
from app.services.xianyu.cookie_token_manager import CookieTokenManager
from common.services.im_registration import ImRegistrationError


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        client = XianyuAsync.__new__(XianyuAsync)
        client.cookie_id = "seller"
        client.current_token = "cached-token"
        client.device_id = "device"
        client._registered_ws = None
        client._pending_mid_futures = {}
        client.connection_manager = SimpleNamespace(
            ws=None, handle_heartbeat_response=Mock(return_value=True),
        )
        client._create_tracked_task = Mock()
        client._handle_message_with_semaphore = Mock(return_value="message-task")
        return client

    def make_socket(self, code, body=None, early_push=False, sync_state=None, ack_code=200):
        sent = []
        async def send(raw):
            sent.append(json.loads(raw))
        responses = [{"body": {"syncPushPackage": {"data": [{"data": "payment"}]}}}] if early_push else []
        async def recv():
            if responses:
                return json.dumps(responses.pop())
            request = sent[-1]
            reply_code, reply_body = code, body or {}
            if request["lwp"] == "/r/SyncStatus/getState":
                reply_code = 200
                reply_body = sync_state if sync_state is not None else {"topic": "sync", "pts": 123456, "seq": 7}
            elif request["lwp"] == "/r/SyncStatus/ackDiff":
                reply_code, reply_body = ack_code, {}
            return json.dumps({"headers": {"mid": request["headers"]["mid"]}, "code": reply_code, "body": reply_body})
        ws = SimpleNamespace(send=AsyncMock(side_effect=send), recv=AsyncMock(side_effect=recv))
        return ws, sent

    async def test_rejected_token_never_becomes_online_or_starts_sync(self):
        client = self.make_client()
        ws, sent = self.make_socket(401, {"code": "4000001", "reason": "token is not found"})
        with self.assertRaises(ImRegistrationError) as ctx:
            await client.init(ws)
        self.assertTrue(is_token_or_session_failure(ctx.exception))
        self.assertIsNone(client._registered_ws)
        self.assertEqual([m["lwp"] for m in sent], ["/reg"])

    async def test_registration_timeout_does_not_become_online(self):
        client = self.make_client()
        ws, _ = self.make_socket(200)
        ws.recv = AsyncMock(side_effect=asyncio.TimeoutError)
        with self.assertRaises(asyncio.TimeoutError):
            await client.init(ws)
        self.assertIsNone(client._registered_ws)

    async def test_early_payment_push_is_preserved_until_registration_succeeds(self):
        client = self.make_client()
        ws, sent = self.make_socket(200, early_push=True)
        pending = await client.init(ws)
        self.assertIs(client._registered_ws, ws)
        self.assertEqual(len(pending), 1)
        self.assertIn("syncPushPackage", pending[0]["body"])
        self.assertEqual(sent[-1]["lwp"], "/r/SyncStatus/ackDiff")
        client.connection_manager.handle_heartbeat_response.return_value = False
        client._route_incoming_message(pending[0], ws)
        client._handle_message_with_semaphore.assert_called_once_with(pending[0], ws)

    async def test_bodyless_business_response_is_not_consumed_as_heartbeat(self):
        client = self.make_client()
        pending = asyncio.get_running_loop().create_future()
        client._pending_mid_futures["request"] = pending
        response = {"headers": {"mid": "request"}, "code": 200}
        client._route_incoming_message(response, object())
        self.assertEqual(await pending, response)
        client.connection_manager.handle_heartbeat_response.assert_not_called()
        client._create_tracked_task.assert_not_called()

    async def test_sync_handshake_uses_server_cursor_verbatim(self):
        client = self.make_client()
        state = {"topic": "sync", "pts": "123456789", "seq": 12, "timestamp": 99}
        ws, sent = self.make_socket(200, sync_state=state)
        await client.init(ws)
        self.assertEqual([m["lwp"] for m in sent], [
            "/reg", "/r/SyncStatus/getState", "/r/SyncStatus/ackDiff",
        ])
        self.assertEqual(sent[-1]["body"], [state])
        self.assertIs(client._registered_ws, ws)

    async def test_missing_server_cursor_cannot_become_online(self):
        client = self.make_client()
        ws, sent = self.make_socket(200, sync_state={"topic": "sync"})
        with self.assertRaises(RuntimeError):
            await client.init(ws)
        self.assertIsNone(client._registered_ws)
        self.assertEqual(len(sent), 2)

    async def test_sync_ack_failure_cannot_become_online(self):
        client = self.make_client()
        ws, _ = self.make_socket(200, ack_code=400)
        with self.assertRaises(ImRegistrationError):
            await client.init(ws)
        self.assertIsNone(client._registered_ws)

    async def test_unregistered_socket_cannot_create_chat(self):
        client = self.make_client()
        client.connection_manager.ws = object()
        client._send_create_chat_once = AsyncMock()
        with self.assertRaises(ConnectionError):
            await client.create_chat_conversation("buyer", "item")
        client._send_create_chat_once.assert_not_called()

    async def test_generic_400_does_not_invalidate_valid_token(self):
        client = self.make_client()
        client._registered_ws = client.connection_manager.ws = object()
        client._send_create_chat_once = AsyncMock(return_value=(None, {"code": 400}))
        client._reconnect_with_new_token = AsyncMock()
        with self.assertRaises(ValueError):
            await client.create_chat_conversation("buyer", "item")
        client._reconnect_with_new_token.assert_not_called()

    async def test_explicit_auth_failure_reconnects_and_retries(self):
        client = self.make_client()
        client._registered_ws = client.connection_manager.ws = object()
        client._send_create_chat_once = AsyncMock(side_effect=[
            (None, {"code": 401, "body": {"reason": "token is not found"}}),
            ("chat-id", {"code": 200}),
        ])
        client._reconnect_with_new_token = AsyncMock(return_value=True)
        self.assertEqual(await client.create_chat_conversation("buyer", "item"), "chat-id")
        client._reconnect_with_new_token.assert_awaited_once()

    async def test_changed_shared_token_reconnects(self):
        client = self.make_client()
        manager = CookieTokenManager.__new__(CookieTokenManager)
        manager.parent = client
        manager._reconnect_websocket_for_renewed_token = AsyncMock()
        await manager._use_cached_token({"token": "new-token", "device_id": "device"})
        manager._reconnect_websocket_for_renewed_token.assert_awaited_once()
        manager._reconnect_websocket_for_renewed_token.reset_mock()
        await manager._use_cached_token({"token": "new-token", "device_id": "device"})
        manager._reconnect_websocket_for_renewed_token.assert_not_called()
