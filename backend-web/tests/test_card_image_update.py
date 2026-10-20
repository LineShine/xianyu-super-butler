"""Card image edits must clear both modern and legacy image fields."""
import json
import unittest
from unittest.mock import AsyncMock, Mock

from app.services.card_service import CardService
from common.models.card import Card


class CardImageUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.card = Card(id=1, user_id=1, name='TEST-CARD', type='text',
                         text_content='保留的卡券文本', image_url='/old.png',
                         image_urls=json.dumps(['/old.png']), is_multi_spec=False,
                         use_no_logistics_form=False)
        result = Mock()
        result.scalars.return_value.first.return_value = self.card
        self.session = AsyncMock()
        self.session.execute.return_value = result
        self.service = CardService(self.session)
        self.service.check_card_duplicate = AsyncMock(return_value=None)

    async def test_deleting_last_image_clears_legacy_fallback(self):
        await self.service.update_card(1, 1, image_urls=[])
        self.session.commit.assert_awaited_once()
        self.assertEqual(json.loads(self.card.image_urls), [])
        self.assertIsNone(self.card.image_url)
        self.assertEqual(self.card.text_content, '保留的卡券文本')

    async def test_replacing_images_updates_legacy_field(self):
        await self.service.update_card(1, 1, image_urls=['/new.png', '/second.png'])
        self.assertEqual(json.loads(self.card.image_urls), ['/new.png', '/second.png'])
        self.assertEqual(self.card.image_url, '/new.png')

    async def test_unrelated_edit_preserves_images(self):
        await self.service.update_card(1, 1, name='UPDATED')
        self.assertEqual(json.loads(self.card.image_urls), ['/old.png'])
        self.assertEqual(self.card.image_url, '/old.png')

    async def test_legacy_image_edit_updates_image_list(self):
        await self.service.update_card(1, 1, image_url='/new.png')
        self.assertEqual(json.loads(self.card.image_urls), ['/new.png'])
        await self.service.update_card(1, 1, image_url=None)
        self.assertEqual(json.loads(self.card.image_urls), [])
        self.assertIsNone(self.card.image_url)
