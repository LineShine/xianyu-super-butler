"""Read-only, fail-closed eligibility checks for self-service codes (not shipping checks)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import time

import aiohttp

from common.services.order_service import get_goofish_connector
from common.utils.xianyu_utils import generate_sign, trans_cookies


@dataclass(frozen=True)
class OrderEntitlement:
    allowed: bool
    reason: str
    detail: str


def denied(reason: str, detail: str) -> OrderEntitlement:
    return OrderEntitlement(False, reason, detail)


def _response_error(raw: dict) -> OrderEntitlement | None:
    ret = raw.get('ret') if isinstance(raw, dict) else None
    if not isinstance(ret, list) or not ret or not all(str(value).startswith('SUCCESS::') for value in ret):
        if isinstance(ret, list) and any(any(word in str(value) for word in ('ORDER_NOT_EXIST', 'TRADE_NOT_EXIST', '订单不存在')) for value in ret):
            return denied('code_order_missing', '订单不存在，请检查订单号')
        return denied('code_order_query', '订单查询暂时失败，请稍后重试')
    return None


def validate_order_response(raw: dict, *, order_no: str, seller_id: str,
                            buyer_id: str, item_id: str, authenticated_user_id: str | None = None,
                            sold_response: dict | None = None) -> OrderEntitlement:
    try:
        return _validate_order_response(raw, order_no=order_no, seller_id=seller_id,
                                        buyer_id=buyer_id, item_id=item_id,
                                        authenticated_user_id=authenticated_user_id,
                                        sold_response=sold_response)
    except (TypeError, ValueError, AttributeError, KeyError):
        return denied('code_order_unknown', '无法确认订单状态，请联系卖家')


def _validate_order_response(raw: dict, *, order_no: str, seller_id: str,
                             buyer_id: str, item_id: str, authenticated_user_id: str | None,
                             sold_response: dict | None) -> OrderEntitlement:
    """Only response evidence is authoritative. Unknown fields/status codes never imply permission."""
    if error := _response_error(raw):
        return error
    data = raw.get('data')
    if not isinstance(data, dict):
        return denied('code_order_unknown', '无法确认订单状态，请联系卖家')
    sources = [data]
    if isinstance(data.get('commonData'), dict):
        sources.append(data['commonData'])
    status_sources, refund_sources = [], []
    for component in data.get('components', []):
        if not isinstance(component, dict) or not isinstance(component.get('data'), dict):
            continue
        kind, fields = component.get('render'), component['data']
        if kind == 'orderInfoVO':
            sources.append(fields)
        elif kind == 'orderStatusVO':
            status_sources.append(fields)
        elif kind in ('refundInfoVO', 'refundStatusVO', 'disputeInfoVO'):
            refund_sources.append(fields)

    # The actual web-detail response contains seller: bool, not sellerId. It describes
    # the authenticated viewer's role; an expected/requested seller ID alone is NOT proof.
    seller_role = data.get('seller')
    if 'seller' in data:
        if type(seller_role) is not bool:
            return denied('code_order_unknown', '无法确认订单卖家身份，请联系卖家')
        if seller_role is False:
            return denied('code_order_seller', '订单不属于当前卖家')
        if authenticated_user_id != seller_id:
            return denied('code_order_unknown', '无法确认订单卖家身份，请联系卖家')

    # Detail pages omit refund flags/amounts. Read the exact order from the current
    # seller's sold-order API too; never infer "no refund" from an empty/local list.
    sold_order = None
    if sold_response is not None:
        if error := _response_error(sold_response):
            return error
        rows = sold_response['data']['module']['items']
        if not isinstance(rows, list):
            return denied('code_order_unknown', '无法确认订单退款状态，请联系卖家')
        matches = [row for row in rows if isinstance(row, dict)
                   and str((row.get('commonData') or {}).get('orderId', '')) == order_no]
        if len(matches) != 1:
            return denied('code_order_unknown', '无法在当前卖家订单中确认该订单，请稍后重试或联系卖家')
        sold_order = matches[0]
        common, price = sold_order['commonData'], sold_order.get('priceVO') or {}
        if str(common.get('itemId', '')) != item_id:
            return denied('code_order_item', '订单商品不匹配或无法确认商品')
        if str((sold_order.get('buyerInfoVO') or {}).get('buyerId', '')) != buyer_id:
            return denied('code_order_buyer', '订单不属于当前买家或无法确认归属')
        if 'inRefund' not in common or price.get('refundFee') in (None, ''):
            return denied('code_order_unknown', '无法确认订单退款状态，请联系卖家')
        sources.append(common)
        refund_sources.append(price)

    def identities(keys, nested=None):
        values = {str(s[k]) for s in sources for k in keys if s.get(k) not in (None, '')}
        if nested:
            parent, keys2 = nested
            for s in sources:
                obj = s.get(parent)
                if isinstance(obj, dict):
                    values.update(str(obj[k]) for k in keys2 if obj.get(k) not in (None, ''))
        return values

    orders = identities(('orderId', 'orderNo', 'bizOrderId', 'tid'))
    for s in sources:
        for info in s.get('orderInfoList', []):
            if isinstance(info, dict) and (info.get('title') or info.get('label') or info.get('key') or info.get('name')) in ('订单编号', '订单号'):
                value = info.get('value') or info.get('text')
                if value:
                    orders.add(str(value))
    sellers = identities(('sellerUserId', 'sellerId'), ('sellerInfo', ('userId', 'sellerId')))
    if seller_role is True and authenticated_user_id == seller_id:
        sellers.add(authenticated_user_id)
    buyers = identities(('buyerUserId', 'buyerId'), ('buyerInfo', ('userId', 'buyerId')))
    if isinstance(data.get('buyerInfoVO'), dict) and data['buyerInfoVO'].get('buyerId'):
        buyers.add(str(data['buyerInfoVO']['buyerId']))
    if sold_order:
        buyers.add(str(sold_order['buyerInfoVO']['buyerId']))
    items = identities(('itemId',), ('itemInfo', ('itemId',)))
    # peerUserId alone cannot establish which side of an order the authenticated user is on.
    if sellers == {seller_id} and data.get('peerUserId'):
        buyers.add(str(data['peerUserId']))
    for values, expected, reason, detail in (
        (orders, order_no, 'code_order_identity', '无法确认订单编号'),
        (sellers, seller_id, 'code_order_seller', '订单不属于当前卖家'),
        (buyers, buyer_id, 'code_order_buyer', '订单不属于当前买家'),
        (items, item_id, 'code_order_item', '订单商品不匹配或无法确认商品'),
    ):
        if not values and reason in ('code_order_seller', 'code_order_buyer'):
            role = '卖家' if reason == 'code_order_seller' else '买家'
            return denied('code_order_unknown', f'无法确认订单{role}身份，请联系卖家')
        if values != {expected}:
            return denied(reason, detail)

    titles = set()
    for s in [*sources, *status_sources]:
        title = (s.get('orderStatusInfo') or {}).get('title')
        if title:
            titles.add(str(title))
        if isinstance(s.get('statusTitle'), str):
            titles.add(s['statusTitle'])
        if isinstance(s.get('orderStatus'), str):
            titles.add(s['orderStatus'])
    refunds = [*sources, *status_sources, *refund_sources]
    for fields in refunds:
        if 'inRefund' in fields and not (type(fields['inRefund']) is bool or fields['inRefund'] in ('true', 'false')):
            return denied('code_order_unknown', '无法确认订单退款状态，请联系卖家')
    refund_states = {str(s[k]).upper() for s in refunds for k in ('refundStatus', 'refundState', 'disputeStatus')
                     if s.get(k) not in (None, '')}
    if any(s.get('inRefund') in (True, 'true') for s in refunds):
        return denied('code_order_refunding', '订单退款处理中')
    # These dispute states are the same ones used by the existing refund-order synchronizer.
    if any(s.get('disputeStatus') in (1, 2, 3, '1', '2', '3') for s in refunds):
        return denied('code_order_refunding', '订单退款处理中')
    if any(s.get('disputeStatus') in (5, '5') for s in refunds):
        return denied('code_order_refunded', '订单已退款')
    if any(any(word in title for word in ('退款中', '退款申请', '申请退款', '退货中')) for title in titles) or refund_states & {'REFUNDING', 'APPLIED', 'PROCESSING', '退款中'}:
        return denied('code_order_refunding', '订单退款处理中')
    if any(any(word in title for word in ('已退款', '退款成功', '部分退款')) for title in titles) or refund_states & {'REFUNDED', 'SUCCESS', 'PARTIAL_REFUNDED', '退款成功', '已退款', '部分退款'}:
        return denied('code_order_refunded', '订单已退款或部分退款')
    amounts = []
    try:
        for s in refunds:
            for key in ('refundAmount', 'refundedAmount', 'actualRefundAmount', 'refundFee', 'actualRefundFee'):
                if s.get(key) not in (None, ''):
                    amount = Decimal(str(s[key]))
                    if not amount.is_finite() or amount < 0:
                        raise InvalidOperation
                    amounts.append(amount)
    except (InvalidOperation, ValueError):
        return denied('code_order_unknown', '无法确认订单退款状态，请联系卖家')
    if any(amount > 0 for amount in amounts) or any(s.get('hasRefund') is True or s.get('isRefunded') is True or s.get('partialRefund') is True for s in refunds):
        return denied('code_order_refunded', '订单已退款或存在退款，请联系卖家')
    clear_states = {'NONE', 'NO_REFUND', 'NOT_APPLIED', '无退款', '未退款'}
    withdrawn_states = {'CANCELLED', 'CANCELED', 'WITHDRAWN', '退款关闭', '退款撤销'}
    if refund_states - clear_states - withdrawn_states:
        return denied('code_order_unknown', '无法确认订单退款状态，请联系卖家')
    # isRefunded=False only rules out a completed refund; a request could still be pending.
    explicit_no_refund = bool(refund_states & clear_states) or any(s.get('hasRefund') is False or s.get('inRefund') is False or s.get('inRefund') == 'false' for s in refunds)
    if refund_states & withdrawn_states:
        explicit_no_refund = bool(amounts) and all(amount == 0 for amount in amounts)
    if not explicit_no_refund:
        return denied('code_order_unknown', '无法确认订单退款状态，请联系卖家')
    if any(any(word in title for word in ('交易关闭', '订单关闭', '已取消')) for title in titles):
        return denied('code_order_closed', '订单已关闭')
    if any('待付款' in title or '未付款' in title for title in titles):
        return denied('code_order_unpaid', '订单尚未付款')
    paid_titles = {'待发货', '已发货', '待收货', '交易成功', '已完成', '已发货，待买家确认收货'}
    if not titles or not titles.issubset(paid_titles):
        return denied('code_order_unknown', '无法确认订单已付款且有效，请联系卖家')
    for s in [*sources, *status_sources]:
        if s.get('isPaid') is False or s.get('paid') is False:
            return denied('code_order_unknown', '订单付款信息不一致，请联系卖家')
        for node in s.get('orderStatusNodeList', []):
            if isinstance(node, dict) and node.get('title') == '已付款' and node.get('completed') is not True:
                return denied('code_order_unknown', '订单付款信息不一致，请联系卖家')
    return OrderEntitlement(True, 'code_order_valid', '订单已付款且未退款')


async def _request_order_api(session, *, cookies: str, token: str, api: str,
                             payload: dict, origin: str) -> dict:
    data_val = json.dumps(payload, separators=(',', ':'))
    timestamp = str(int(time.time() * 1000))
    params = {'jsv': '2.7.2', 'appKey': '34839810', 't': timestamp,
              'sign': generate_sign(timestamp, token, data_val), 'v': '1.0',
              'type': 'originaljson', 'dataType': 'json', 'accountSite': 'xianyu',
              'api': api, 'sessionOption': 'AutoLoginOnly', 'timeout': '20000',
              'valueType': 'string', 'spm_cnt': 'a21107h.42826273.0.0'}
    async with session.post(f'https://h5api.m.goofish.com/h5/{api}/1.0/', params=params,
                            data={'data': data_val}, headers={
                                'cookie': cookies.replace('\n', '').replace('\r', ''),
                                'origin': origin, 'referer': origin + '/',
                                'accept': 'application/json',
                                'content-type': 'application/x-www-form-urlencoded',
                                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36',
                            }, timeout=aiohttp.ClientTimeout(total=20)) as response:
        if response.status != 200:
            return {}
        return await response.json(content_type=None)


async def check_order_entitlement(*, cookies: str, seller_id: str, buyer_id: str,
                                  item_id: str, order_no: str) -> OrderEntitlement:
    """Two read-only queries within 20s total. No login/refresh retries or order mutations."""
    cookie_map = trans_cookies(cookies)
    authenticated_user_id = str(cookie_map.get('unb', ''))
    if authenticated_user_id != seller_id:
        return denied('code_order_query', '账号登录信息异常，请联系卖家')
    token = cookie_map.get('_m_h5_tk', '').split('_')[0]
    try:
        async with asyncio.timeout(20):
            async with aiohttp.ClientSession(connector=get_goofish_connector(), connector_owner=False,
                                            cookie_jar=aiohttp.DummyCookieJar()) as session:
                raw = await _request_order_api(session, cookies=cookies, token=token,
                    api='mtop.idle.web.trade.order.detail', payload={'tid': order_no},
                    origin='https://www.goofish.com')
                if error := _response_error(raw):
                    return error
                sold_response = await _request_order_api(session, cookies=cookies, token=token,
                    api='mtop.taobao.idle.trade.merchant.sold.get', payload={
                        'pageNumber': 1, 'rowsPerPage': 30, 'orderIds': order_no,
                        'queryCode': 'ALL', 'orderSearchParam': '{}',
                    }, origin='https://seller.goofish.com')
        return validate_order_response(raw, order_no=order_no, seller_id=seller_id,
                                       buyer_id=buyer_id, item_id=item_id,
                                       authenticated_user_id=authenticated_user_id,
                                       sold_response=sold_response)
    except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, KeyError):
        return denied('code_order_query', '订单查询暂时失败，请稍后重试')
