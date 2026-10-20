"""Image/text delivery must not reuse the platform's message deduplication ID."""
import base64
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.xianyu.xianyu_async import XianyuAsync
from common.utils.xianyu_utils import generate_uuid


class MessageIdTests(unittest.TestCase):
    def test_same_millisecond_burst_is_unique(self):
        with patch('common.utils.xianyu_utils.time.time', return_value=1800000000):
            ids = [generate_uuid() for _ in range(1000)]
        self.assertEqual(len(set(ids)), 1000)
        self.assertTrue(all(value.startswith('-') and value[1:].isdigit() for value in ids))

    def test_clock_rollback_does_not_reuse_an_id(self):
        with patch('common.utils.xianyu_utils.time.time', side_effect=[1800000001, 1800000000, 1800000001]):
            self.assertEqual(len({generate_uuid() for _ in range(3)}), 3)

    def test_concurrent_callers_do_not_reuse_ids(self):
        with patch('common.utils.xianyu_utils.time.time', return_value=1800000000), \
                ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: generate_uuid(), range(1000)))
        self.assertEqual(len(set(ids)), 1000)


class ImageTextDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_then_text_have_distinct_ids_even_without_a_delay(self):
        client = XianyuAsync.__new__(XianyuAsync)
        client.cookie_id = client.myid = 'TEST-SELLER'
        client._pending_mid_futures = {}
        client._get_image_size_from_url = AsyncMock(return_value=(800, 600))
        ws = SimpleNamespace(send=AsyncMock())
        with patch('common.utils.xianyu_utils.time.time', return_value=1800000000):
            image = await client.send_image_msg(ws, 'TEST-CHAT', 'TEST-BUYER',
                                                'https://img.alicdn.com/test.png')
            text = await client.send_msg(ws, 'TEST-CHAT', 'TEST-BUYER', '卡券文本\n提取码：TEST')
        self.assertTrue(image['success'])
        self.assertTrue(text['success'])
        frames = [json.loads(call.args[0]) for call in ws.send.await_args_list]
        self.assertEqual(len(frames), 2)
        self.assertNotEqual(frames[0]['body'][0]['uuid'], frames[1]['body'][0]['uuid'])
        contents = [json.loads(base64.b64decode(frame['body'][0]['content']['custom']['data']))
                    for frame in frames]
        self.assertEqual([content['contentType'] for content in contents], [2, 1])
        self.assertEqual(contents[1]['text']['text'], '卡券文本\n提取码：TEST')
