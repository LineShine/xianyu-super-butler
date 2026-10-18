import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.chat_new.im_client import GoofishImClient
from common.services.im_registration import ImRegistrationError


class ChatRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        client = GoofishImClient("seller", "unb=seller")
        client.token = "cached-token"
        client._send_and_wait = AsyncMock()
        client._send_raw = AsyncMock()
        return client

    async def test_registration_failure_does_not_send_sync(self):
        client = self.make_client()
        client._send_and_wait.return_value = {"code": 401, "body": {"reason": "token is not found"}}
        with self.assertRaises(ImRegistrationError):
            await client._register()
        client._send_raw.assert_not_called()
        self.assertFalse(client.is_connected)

    async def test_timeout_does_not_pretend_to_be_registered(self):
        client = self.make_client()
        client._send_and_wait.side_effect = asyncio.TimeoutError
        with self.assertRaises(asyncio.TimeoutError):
            await client._register()
        client._send_raw.assert_not_called()

    async def test_rejected_but_not_expired_cache_is_invalidated_by_token(self):
        client = self.make_client()
        with patch("app.services.chat_new.im_client.mark_token_cache_expired", new_callable=AsyncMock) as invalidate:
            invalidate.return_value = SimpleNamespace(success=True, message="expired")
            await client._mark_cached_token_expired()
            self.assertEqual(invalidate.await_args.kwargs["expected_token"], "cached-token")
            self.assertEqual(invalidate.await_args.kwargs["expected_device_id"], client.device_id)

    async def test_sync_handshake_acknowledges_server_cursor(self):
        client = self.make_client()
        state = {"topic": "sync", "pts": 1234, "seq": 9}
        client._send_and_wait.side_effect = [
            {"code": 200}, {"code": 200, "body": state}, {"code": 200},
        ]
        await client._register()
        requests = [call.args[1] for call in client._send_and_wait.await_args_list]
        self.assertEqual([r["lwp"] for r in requests], [
            "/reg", "/r/SyncStatus/getState", "/r/SyncStatus/ackDiff",
        ])
        self.assertEqual(requests[-1]["body"], [state])

    async def test_missing_sync_cursor_does_not_send_ack(self):
        client = self.make_client()
        client._send_and_wait.side_effect = [{"code": 200}, {"code": 200, "body": {}}]
        with self.assertRaises(RuntimeError):
            await client._register()
        self.assertEqual(client._send_and_wait.await_count, 2)

    async def test_auth_failure_retries_only_once_and_stays_offline(self):
        client = self.make_client()
        client._get_im_token = AsyncMock(return_value="bad-token")
        client._recv_loop = AsyncMock()
        client._register = AsyncMock(side_effect=ImRegistrationError({"code": 401}))
        client._mark_cached_token_expired = AsyncMock()
        session = SimpleNamespace(
            ws_connect=AsyncMock(return_value=SimpleNamespace(closed=False, close=AsyncMock())),
            closed=False, close=AsyncMock(),
        )
        with patch("app.services.chat_new.im_client.aiohttp.ClientSession", return_value=session):
            self.assertFalse(await client.connect())
        self.assertEqual(client._register.await_count, 2)
        self.assertFalse(client._registered)
        self.assertFalse(client.is_connected)
