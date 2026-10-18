"""Owned-stock matching and ownership validation regression tests."""
import unittest
from unittest.mock import AsyncMock,Mock
from common.services.card_matcher import CardMatcher

class OwnedCardTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_spec_and_generic_cards_remain_distinct(self):
        matcher=CardMatcher(AsyncMock())
        cards=[{'id':1,'name':'A','is_multi_spec':True,'spec_name':'类型','spec_value':' A '},
               {'id':2,'name':'B','is_multi_spec':True,'spec_name':'类型','spec_value':'B'},
               {'id':3,'name':'通用','is_multi_spec':False}]
        self.assertEqual([x['id'] for x in matcher._match_card_dicts_by_spec(cards,'类型','a')],[1])
        self.assertEqual([x['id'] for x in matcher._match_card_dicts_by_spec(cards,None,None)],[3])
    async def test_docked_stock_cannot_be_bound_and_old_links_are_not_deleted(self):
        session=AsyncMock();matcher=CardMatcher(session)
        with self.assertRaisesRegex(ValueError,'自有'):
            await matcher.update_item_card_relations('item',1,[{'card_id':2,'source':'dock_l1','dock_record_id':7}])
        session.execute.assert_not_called()
    async def test_foreign_card_cannot_be_bound(self):
        session=AsyncMock();result=Mock();result.scalars.return_value.all.return_value=[];session.execute.return_value=result
        with self.assertRaisesRegex(ValueError,'不属于'):
            await CardMatcher(session).update_item_card_relations('item',1,[{'card_id':2,'source':'own'}])
        self.assertEqual(session.execute.await_count,1)

    def test_pickup_requires_one_owned_card(self):
        from common.services.agree_pickup_delivery import pick_unique_own_card
        owned={'id':1,'card_source':'own'}
        self.assertEqual(pick_unique_own_card([owned]),(owned,None))
        self.assertIsNone(pick_unique_own_card([owned,{'id':2,'card_source':'own'}])[0])
        self.assertIsNone(pick_unique_own_card([{'id':3,'card_source':'external'}])[0])
