"""Sanitized protocol fixtures: no real accounts, orders, tokens or network requests."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from app.services.xianyu.message_handler import MessageHandler
from app.services.xianyu.xianyu_async import XianyuAsync
from app.services.xianyu.auto_reply_service import AutoReplyService

class PaymentReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.object(MessageHandler,'_load_message_expire_time',return_value=3600):
            self.handler=MessageHandler('TEST-SELLER')
        self.client=XianyuAsync.__new__(XianyuAsync);self.client.cookie_id='TEST-SELLER'
        self.client.auto_delivery_handler=SimpleNamespace(_handle_auto_delivery=AsyncMock())
        self.reply=AutoReplyService.__new__(AutoReplyService);self.reply.cookie_id='TEST-SELLER'
        async def callback(parsed,ws):
            if self.reply.is_auto_delivery_trigger(parsed['send_message']):
                await self.client._handle_auto_delivery_from_message(parsed,ws)
        self.handler.set_card_update_message_handler(callback)
        self.handler.set_chat_message_handler(callback)
    async def test_paid_card_update_reaches_delivery_without_debounce(self):
        event={'1':'TEST-ITEM','2':'TEST-CHAT@goofish','4':{'reminderContent':'[我已付款，等待你发货]','senderUserId':'TEST-BUYER','reminderUrl':'https://www.goofish.com/item?itemId=TEST-ITEM'}}
        self.assertTrue(await asyncio.wait_for(self.handler._process_single_message(event,object()),timeout=.25))
        call=self.client.auto_delivery_handler._handle_auto_delivery
        call.assert_awaited_once();self.assertEqual(call.await_args.kwargs['item_id'],'TEST-ITEM');self.assertEqual(call.await_args.kwargs['chat_id'],'TEST-CHAT')
    async def test_plain_text_is_still_parsed_and_dispatched(self):
        callback=AsyncMock();self.handler.set_chat_message_handler(callback)
        event={'1':{'2':'TEST-CHAT@goofish','10':{'reminderContent':'你好','senderUserId':'TEST-BUYER','senderNick':'测试买家'}}}
        self.assertTrue(await self.handler._process_single_message(event,object()))
        callback.assert_awaited_once();self.assertEqual(callback.await_args.args[0]['send_message'],'你好')
        self.client.auto_delivery_handler._handle_auto_delivery.assert_not_called()
    async def test_plain_text_cannot_spoof_payment_delivery(self):
        await self.client._handle_auto_delivery_from_message({'is_plain_text':True,'send_message':'[我已付款，等待你发货]'},object())
        self.client.auto_delivery_handler._handle_auto_delivery.assert_not_called()
