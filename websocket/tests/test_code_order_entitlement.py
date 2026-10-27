"""Regression contract from official responses; IDs replaced, personal fields removed.

All HTTP responses are mocked. No cookies, live buyers or real sends are used.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from common.services.code_order_entitlement import check_order_entitlement, validate_order_response


ORDER = '1000000000000000001'
SCOPE = dict(order_no=ORDER, seller_id='1001', buyer_id='2001', item_id='3001')
FIXTURES = Path(__file__).parent / 'fixtures'


def responses():
    return [json.loads((FIXTURES / f'code_order_shipped_{kind}.json').read_text())
            for kind in ('detail', 'sold')]


class OfficialOrderContractTests(unittest.TestCase):
    def setUp(self):
        self.detail, self.sold = responses()

    def validate(self, **scope):
        return validate_order_response(self.detail, sold_response=self.sold,
            **{'authenticated_user_id': '1001', **SCOPE, **scope})

    def test_real_shape_seller_role_peer_buyer_long_title_and_zero_refund(self):
        result = self.validate()
        self.assertTrue(result.allowed, result)
        self.assertEqual(result.reason, 'code_order_valid')

    def test_seller_role_requires_matching_authenticated_cookie_identity(self):
        for authenticated in (None, '', '9999'):
            self.assertFalse(self.validate(authenticated_user_id=authenticated).allowed)
        self.detail['data'].pop('seller')
        self.assertFalse(self.validate().allowed)

    def test_buyer_view_and_unknown_seller_role_are_not_seller_evidence(self):
        for role in (False, None, 1, 'true', '1001'):
            self.detail['data']['seller'] = role
            self.assertFalse(self.validate().allowed, role)

    def test_cross_buyer_item_order_and_conflicting_seller_rejected(self):
        for key, value in (('peerUserId', '9999'), ('itemId', '9999'),
                           ('orderId', '9999'), ('sellerId', '9999')):
            with self.subTest(key=key):
                self.detail, self.sold = responses()
                self.detail['data'][key] = value
                self.assertFalse(self.validate().allowed)

    def test_order_info_title_is_checked_and_can_supply_missing_top_level_id(self):
        self.detail['data'].pop('orderId')
        self.assertTrue(self.validate().allowed)
        self.detail['data']['components'][1]['data']['orderInfoList'][0]['value'] = '9999'
        self.assertFalse(self.validate().allowed)

    def test_sold_response_must_contain_exactly_one_matching_order(self):
        row = self.sold['data']['module']['items'][0]
        for rows in ([], [dict(commonData={'orderId': '9999'})], [row, row], None, {}):
            self.sold['data']['module']['items'] = rows
            self.assertFalse(self.validate().allowed, rows)

    def test_other_orders_in_sold_response_do_not_provide_evidence(self):
        other = deepcopy(self.sold['data']['module']['items'][0])
        other['commonData'].update(orderId=9999, inRefund=True)
        self.sold['data']['module']['items'].insert(0, other)
        self.assertTrue(self.validate().allowed)

    def test_sold_and_detail_buyer_item_must_agree(self):
        for path, field in (('commonData', 'itemId'), ('buyerInfoVO', 'buyerId')):
            for value in (None, '9999'):
                self.detail, self.sold = responses()
                self.sold['data']['module']['items'][0][path][field] = value
                self.assertFalse(self.validate().allowed)

    def test_pending_and_partial_refunds_denied_even_while_shipped(self):
        row = self.sold['data']['module']['items'][0]
        row['commonData']['inRefund'] = True
        self.assertEqual(self.validate().reason, 'code_order_refunding')
        row['commonData']['inRefund'] = False
        row['priceVO']['refundFee'] = '0.01'
        self.assertEqual(self.validate().reason, 'code_order_refunded')

    def test_missing_or_unknown_refund_flag_or_amount_fails_closed(self):
        for flag in (None, 'unknown', 0):
            self.sold['data']['module']['items'][0]['commonData']['inRefund'] = flag
            self.assertFalse(self.validate().allowed, flag)
        self.detail, self.sold = responses()
        row = self.sold['data']['module']['items'][0]
        del row['commonData']['inRefund']
        self.assertFalse(self.validate().allowed)
        row['commonData']['inRefund'] = False
        for amount in (None, '', '-1', 'NaN', 'unknown'):
            row['priceVO']['refundFee'] = amount
            self.assertFalse(self.validate().allowed, amount)
        del row['priceVO']['refundFee']
        self.assertFalse(self.validate().allowed)

    def test_withdrawn_refund_needs_zero_actual_refund_not_just_in_refund_false(self):
        row = self.sold['data']['module']['items'][0]
        row['commonData']['refundStatus'] = 'WITHDRAWN'
        self.assertTrue(self.validate().allowed)
        row['priceVO']['refundFee'] = '1.00'
        self.assertFalse(self.validate().allowed)

    def test_unpaid_closed_unknown_or_inconsistent_state_denied(self):
        for title in ('待付款', '交易关闭', '部分退款', '未知状态'):
            self.sold['data']['module']['items'][0]['commonData']['orderStatus'] = title
            self.assertFalse(self.validate().allowed, title)
        self.detail, self.sold = responses()
        self.detail['data']['components'][0]['data']['orderStatusNodeList'][1]['completed'] = False
        self.assertFalse(self.validate().allowed)

    def test_missing_refund_source_and_interface_failures_do_not_imply_eligible(self):
        result = validate_order_response(self.detail, authenticated_user_id='1001', **SCOPE)
        self.assertFalse(result.allowed)
        for raw in ({}, {'ret': ['FAIL_SYS_SESSION_EXPIRED::private internal message']},
                    {'ret': ['SUCCESS::ok'], 'data': None}):
            self.sold = raw
            result = self.validate()
            self.assertFalse(result.allowed)
            self.assertNotIn('private', result.detail)


class OfficialOrderRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.detail, self.sold = responses()
        self.session = MagicMock()
        self.session.__aenter__.return_value = self.session
        for target, value in (
            ('common.services.code_order_entitlement.aiohttp.ClientSession', MagicMock(return_value=self.session)),
            ('common.services.code_order_entitlement.get_goofish_connector', MagicMock(return_value=None)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def response(self, data, status=200):
        result = MagicMock()
        result.__aenter__.return_value = result
        result.status = status
        result.json = AsyncMock(return_value=data)
        return result

    async def check(self, cookies='unb=1001; _m_h5_tk=fake_123'):
        return await check_order_entitlement(cookies=cookies, **SCOPE)

    async def test_signed_read_only_requests_filter_exact_order_and_use_seller_origin(self):
        self.session.post.side_effect = [self.response(self.detail), self.response(self.sold)]
        self.assertTrue((await self.check()).allowed)
        calls = self.session.post.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn('mtop.idle.web.trade.order.detail', calls[0].args[0])
        self.assertEqual(json.loads(calls[0].kwargs['data']['data']), {'tid': ORDER})
        self.assertIn('mtop.taobao.idle.trade.merchant.sold.get', calls[1].args[0])
        self.assertEqual(json.loads(calls[1].kwargs['data']['data'])['orderIds'], ORDER)
        self.assertEqual(calls[1].kwargs['headers']['origin'], 'https://seller.goofish.com')
        self.assertEqual(calls[1].kwargs['headers']['cookie'], calls[0].kwargs['headers']['cookie'])

    async def test_wrong_cookie_account_stops_before_any_query(self):
        self.assertFalse((await self.check(cookies='unb=9999; _m_h5_tk=fake_123')).allowed)
        self.session.post.assert_not_called()

    async def test_query_failure_is_not_retried_and_keeps_private_errors_hidden(self):
        self.session.post.side_effect = [self.response({'ret': ['FAIL::private error details']})]
        result = await self.check()
        self.assertFalse(result.allowed)
        self.assertNotIn('private', result.detail)
        self.assertEqual(self.session.post.call_count, 1)

    async def test_sold_endpoint_http_failure_stops_without_fallback_or_retry(self):
        self.session.post.side_effect = [self.response(self.detail), self.response({}, 503)]
        self.assertFalse((await self.check()).allowed)
        self.assertEqual(self.session.post.call_count, 2)

    async def test_timeout_stops_and_cancellation_propagates_to_caller(self):
        self.session.post.side_effect = TimeoutError
        self.assertFalse((await self.check()).allowed)
        self.assertEqual(self.session.post.call_count, 1)
        self.session.post.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.check()


if __name__ == '__main__':
    unittest.main()
