"""Buyer self-service: durable, bounded conversations and at-most-once order entitlements."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select

from common.db.session import async_session_maker
from common.models.auto_reply_message_log import YtxAutoReplyMessageLog as ReplyLog
from common.models.code_self_service import CodeSelfServiceAttempt as Attempt, CodeSelfServiceSession as Flow
from common.models.ytx_account import YtxAccount
from common.models.ytx_catalog_item import YtxCatalogItem
from common.models.ytx_order import YtxOrder
from common.services import code_generation_config as config
from common.services.code_order_entitlement import check_order_entitlement
from app.services.xianyu.code_generation_service import (
    Claim, CodeGenerationService, CommandStopped, conversation_lock,
    is_plain_chat_message, normalize_id, original_timestamp, source_key,
)

PROMPT = '请提供订单号，将自动发放口令（接下来严格执行，不要发送多余内容，避免自动发放失败）'
FORMAT_HINT = '请仅发送数字订单号，不要附加其他内容'
PROCESSING = '已收到，正在自动处理ing...'
SCRIPT_ERROR = '机器人回复：自动处理失败，请稍后重试或联系卖家'
LIMIT_HINT = '该订单今日已领取超过限制'
WINDOW_MS = 300_000
BUSY_MS = 90_000
DAILY_LIMIT = 3
ACTIVE = {'prompting', 'waiting', 'checking', 'notifying'}
COUNTED = {'reserved', 'success', 'unknown'}


def now_ms() -> int:
    return int(time.time() * 1000)


def quota_day(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, ZoneInfo('Asia/Shanghai')).date().isoformat()


def buyer_envelope(message, seller) -> bool:
    """Only an authenticated single-chat sender is a buyer, including invalid-format image replies."""
    raw = message.get('raw_message')
    body = raw.get('1') if isinstance(raw, dict) else None
    info = body.get('10') if isinstance(body, dict) else None
    buyer = normalize_id(message.get('send_user_id'))
    return bool(isinstance(info, dict) and not raw.get('chatType')
                and re.fullmatch(r'[0-9]+', buyer) and buyer not in ('0', seller)
                and normalize_id(info.get('senderUserId')) == buyer
                and body.get('conversationType', 1) in (1, '1')
                and not info.get('isAutoReply') and not info.get('automatic')
                and not info.get('isSystem') and not body.get('isSystem')
                and normalize_id(body.get('2')) == normalize_id(message.get('chat_id'))
                and normalize_id(message.get('chat_id')))


def explicit_items(message) -> set[str]:
    items = {str(message['item_id'])} if message.get('item_id') else set()
    info = message['raw_message']['1']['10']
    for key in ('bizTag', 'extJson'):
        value = info.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        if isinstance(value, dict) and value.get('itemId'):
            items.add(str(value['itemId']))
    if isinstance(info.get('reminderUrl'), str):
        items.update(parse_qs(urlsplit(info['reminderUrl']).query).get('itemId', []))
    return items


@dataclass(frozen=True)
class Action:
    kind: str
    attempt_id: str
    claim: Claim
    order_no: str | None = None


class CodeSelfService:
    def __init__(self, account_id: str, instance):
        self.account_id = account_id
        self.instance = instance
        self.generator = CodeGenerationService(account_id, instance)

    async def handle(self, message: dict, websocket) -> bool:
        seller = normalize_id(getattr(self.instance, 'myid', ''))
        timestamp = original_timestamp(message)
        if not seller or not buyer_envelope(message, seller):
            return False
        if timestamp is None or not 0 <= now_ms() - timestamp <= WINDOW_MS:
            # Old replayed messages cannot start a flow or consume a pending one.
            return False
        consumed, action = await self._accept(message)
        if action is None:
            return consumed
        try:
            async with conversation_lock(self.account_id, action.claim.chat_id) as lock:
                if lock is None:
                    raise CommandStopped('code_busy', '会话正在处理其他口令任务，本次未执行')
                async with asyncio.timeout(75):
                    await self._run(action, websocket, lock)
        except asyncio.CancelledError:
            await asyncio.shield(self._abort(action, 'code_cancelled', '任务已中断，不会自动重放'))
            raise
        except CommandStopped as exc:
            await self._abort(action, exc.reason, exc.detail)
        except Exception as exc:
            logger.error(f'【{self.account_id}】自助口令处理异常: {type(exc).__name__}')
            await self._abort(action, 'code_error', '自动处理异常，不会自动重放')
        return True

    async def _account(self, db):
        return (await db.execute(select(YtxAccount).where(
            YtxAccount.account_id == self.account_id).with_for_update())).scalars().first()

    async def _resolve_item(self, db, account, message):
        items = explicit_items(message)
        if not items:
            chat, buyer = normalize_id(message['chat_id']), normalize_id(message['send_user_id'])
            rows = (await db.execute(select(ReplyLog.item_id).where(
                ReplyLog.account_pk == account.id, ReplyLog.owner_id == account.owner_id,
                ReplyLog.chat_id.in_([chat, chat + '@goofish']),
                ReplyLog.sender_user_id.in_([buyer, buyer + '@goofish']),
                ReplyLog.decision_reason.notin_(['system_message', 'auto_delivery_trigger']),
                ReplyLog.item_id.is_not(None),
            ).distinct())).scalars().all()
            orders = (await db.execute(select(YtxOrder.item_id).where(
                YtxOrder.owner_id == account.owner_id, YtxOrder.account_id == self.account_id,
                YtxOrder.chat_id.in_([chat, chat + '@goofish']), YtxOrder.buyer_id == buyer,
                YtxOrder.item_id.is_not(None),
            ).distinct())).scalars().all()
            items = {str(value) for value in [*rows, *orders] if value}
        if len(items) != 1:
            raise CommandStopped('code_context', '无法确认本会话的唯一商品，未开始自助领取')
        return items.pop()

    async def _items(self, db, account, item_id):
        return (await db.execute(select(YtxCatalogItem).where(
            YtxCatalogItem.owner_id == account.owner_id, YtxCatalogItem.account_pk == account.id,
            YtxCatalogItem.item_id == item_id).with_for_update())).scalars().all()

    async def _record(self, db, account, message, attempt_id, flow, reason, detail=None):
        timestamp = original_timestamp(message)
        log = ReplyLog(owner_id=account.owner_id, account_pk=account.id, account_id=self.account_id,
            account_name=account.remark or account.display_name or self.account_id,
            chat_id=normalize_id(message['chat_id']), sender_user_id=normalize_id(message['send_user_id']),
            sender_user_name=message.get('send_user_name'), item_id=flow.item_id if flow else None,
            source_message_id=source_key(message), source_message=message.get('send_message'),
            source_message_time=datetime.fromtimestamp(timestamp / 1000),
            reply_strategy='code_generation', matched_rule_type='code_generation',
            process_status='processing' if reason == 'processing' else 'skipped', decision_reason=reason,
            send_status='unknown', error_message=detail, raw_message_json=message['raw_message'],
            context_snapshot={'entry_source': 'buyer_auto', 'step': 'received'}, reply_segments=[])
        db.add(log)
        await db.flush()
        attempt = Attempt(id=attempt_id, session_id=flow.id if flow else None,
            owner_id=account.owner_id, account_pk=account.id, chat_id=log.chat_id,
            buyer_id=log.sender_user_id, item_id=log.item_id, source_message_id=log.source_message_id,
            source_timestamp_ms=timestamp, log_id=log.id, stage='received', reason=reason,
            quota_state='none', created_ms=now_ms(), busy_until_ms=0)
        db.add(attempt)
        return attempt, log

    async def _recover(self, db, attempt, now):
        if attempt and attempt.finished_ms is None and attempt.busy_until_ms <= now:
            if attempt.quota_state == 'reserved':
                attempt.quota_state = 'unknown' if attempt.stage == 'sending' else 'released'
            attempt.reason = 'code_interrupted'
            attempt.finished_ms = now
            if attempt.log_id:
                log = (await db.execute(select(ReplyLog).where(ReplyLog.id == attempt.log_id))).scalars().first()
                if log:
                    log.process_status = 'failed'
                    log.decision_reason = 'code_interrupted'
                    log.send_status = 'unknown' if attempt.quota_state == 'unknown' else 'failed'
                    log.error_message = '上次处理已中断，不自动重放；口令可能已发送时保留额度'

    async def _accept(self, message) -> tuple[bool, Action | None]:
        now, timestamp = now_ms(), original_timestamp(message)
        chat, buyer = normalize_id(message['chat_id']), normalize_id(message['send_user_id'])
        plain = is_plain_chat_message(message)
        keyword = plain and '口令' in message.get('send_message', '')
        async with async_session_maker() as db:
            account = await self._account(db)
            if account is None:
                return False, None
            key = hashlib.sha256(json.dumps([account.id, chat, source_key(message)]).encode()).hexdigest()
            flow = (await db.execute(select(Flow).where(Flow.account_pk == account.id, Flow.chat_id == chat)
                                     .with_for_update())).scalars().first()
            if flow and flow.state in ACTIVE - {'waiting'} and flow.busy_until_ms <= now:
                previous = (await db.execute(select(Attempt).where(Attempt.id == flow.active_attempt_id)
                                             .with_for_update())).scalars().first()
                await self._recover(db, previous, now)
                flow.state = 'finished'
            if flow and flow.state == 'waiting' and now >= flow.expires_ms:
                flow.state = 'finished'
            existing = (await db.execute(select(Attempt.id).where(Attempt.id == key).with_for_update())).first()
            if existing:
                await db.commit()
                return True, None
            active = bool(flow and flow.state in ACTIVE)
            if not active and not keyword:
                await db.commit()
                return False, None
            reason, detail, kind = 'processing', None, None
            if account.status != 'active' or normalize_id(account.unb or account.account_id) != normalize_id(self.instance.myid):
                reason, detail = 'code_account_inactive', '账号未启用，未开始自助领取'
                if flow:
                    flow.state = 'finished'
            elif active:
                items = await self._items(db, account, flow.item_id)
                if (flow.owner_id != account.owner_id or flow.buyer_id != buyer or not items
                        or any(not config.enabled(i.metadata_json) or i.metadata_json.get(config.ENABLED_AT_KEY) != flow.enabled_at_ms for i in items)):
                    flow.state = 'finished'
                    reason, detail = 'code_disabled', '会话归属或商品开关已变化，本轮已结束'
                elif timestamp < flow.started_ms or explicit_items(message) - {flow.item_id}:
                    reason, detail = 'code_context', '消息早于本轮开始或商品不匹配'
                elif flow.state != 'waiting':
                    reason = 'code_flow_busy'
                elif keyword:
                    reason = 'code_keyword_cooldown'
                elif plain and re.fullmatch(r'[0-9]{1,64}', message.get('send_message', '')):
                    kind = 'order'
                elif not flow.reminded:
                    flow.reminded = True
                    kind = 'format'
                else:
                    reason = 'code_format_ignored'
            elif flow and now < flow.cooldown_until_ms:
                reason = 'code_keyword_cooldown'
            else:
                try:
                    item_id = await self._resolve_item(db, account, message)
                    items = await self._items(db, account, item_id)
                    if not items or any(not config.enabled(i.metadata_json) for i in items):
                        # Disabled goods retain the usual AI/keyword reply behavior.
                        await db.commit()
                        return False, None
                    if any(not config.message_allowed(i.metadata_json, timestamp, now) for i in items):
                        raise CommandStopped('code_stale', '消息早于商品本次开启时间')
                    enabled_at = items[0].metadata_json[config.ENABLED_AT_KEY]
                    if any(i.metadata_json[config.ENABLED_AT_KEY] != enabled_at for i in items):
                        raise CommandStopped('code_context', '商品配置不一致')
                    if flow is None:
                        flow = Flow(id=uuid4().hex, account_pk=account.id, chat_id=chat)
                        db.add(flow)
                    flow.owner_id, flow.buyer_id, flow.item_id = account.owner_id, buyer, item_id
                    flow.enabled_at_ms, flow.started_ms = enabled_at, now
                    flow.expires_ms = flow.cooldown_until_ms = now + WINDOW_MS
                    flow.reminded = False
                    kind = 'prompt'
                except CommandStopped as exc:
                    reason, detail = exc.reason, exc.detail
            attempt, log = await self._record(db, account, message, key, flow, reason, detail)
            action = None
            if kind:
                flow.state = {'prompt': 'prompting', 'format': 'notifying', 'order': 'checking'}[kind]
                flow.active_attempt_id = key
                flow.busy_until_ms = attempt.busy_until_ms = now + BUSY_MS
                attempt.order_no = message['send_message'] if kind == 'order' else None
                log.order_no = attempt.order_no
                action = Action(kind, key, Claim(log.id, account.id, account.owner_id, chat, buyer,
                    flow.item_id, flow.enabled_at_ms, timestamp), attempt.order_no)
            else:
                attempt.finished_ms = now
            await db.commit()  # No send or subprocess can happen before the durable claim.
            return True, action

    async def _update(self, action, *, stage=None, reason=None, detail=None, outcome=None,
                      sent_text=None, end_state=None, process_status=None, **values):
        async with async_session_maker() as db:
            account = await self._account(db)
            attempt = (await db.execute(select(Attempt).where(Attempt.id == action.attempt_id).with_for_update())).scalars().one()
            flow = (await db.execute(select(Flow).where(Flow.id == attempt.session_id).with_for_update())).scalars().one()
            if account is None or account.id != action.claim.account_pk or flow.active_attempt_id != attempt.id:
                raise CommandStopped('code_context', '本轮会话已被替换，停止处理')
            if stage:
                attempt.stage = stage
            if reason:
                attempt.reason = reason
            for key, value in values.items():
                setattr(attempt, key, value)
            log = (await db.execute(select(ReplyLog).where(ReplyLog.id == attempt.log_id))).scalars().first()
            if log:
                log.context_snapshot = {**(log.context_snapshot or {}), 'entry_source': 'buyer_auto',
                    'step': attempt.stage, 'quota_used': attempt.quota_used, 'quota_limit': DAILY_LIMIT,
                    'quota_day': attempt.quota_day, 'quota_state': attempt.quota_state}
                if reason:
                    log.decision_reason = reason
                if detail:
                    log.error_message = detail
                if process_status:
                    log.process_status = process_status
                if sent_text is not None:
                    segments = [*(log.reply_segments or []), sent_text]
                    log.reply_segments, log.reply_text, log.reply_mode = segments, '\n'.join(segments), 'text'
                if outcome:
                    log.send_status = outcome.status
                    log.send_result_json = [*(log.send_result_json or []), outcome.snapshot]
                    if outcome.detail:
                        log.send_fail_reason = outcome.detail
            if end_state:
                flow.state = end_state if now_ms() < flow.expires_ms else 'finished'
                flow.busy_until_ms = 0
                attempt.finished_ms = now_ms()
            await db.commit()

    async def _abort(self, action, reason, detail):
        async with async_session_maker() as db:
            await self._account(db)
            attempt = (await db.execute(select(Attempt).where(Attempt.id == action.attempt_id).with_for_update())).scalars().one()
            # Sending can have reached the server before cancellation/connection loss.
            if attempt.quota_state == 'reserved':
                attempt.quota_state = 'unknown' if attempt.stage == 'sending' else 'released'
            attempt.reason, attempt.finished_ms = reason, now_ms()
            flow = (await db.execute(select(Flow).where(Flow.id == attempt.session_id).with_for_update())).scalars().first()
            if flow and flow.active_attempt_id == attempt.id:
                flow.state = 'finished'
            log = (await db.execute(select(ReplyLog).where(ReplyLog.id == attempt.log_id))).scalars().first()
            if log:
                log.process_status, log.decision_reason, log.error_message = 'failed', reason, detail
                log.send_status = 'unknown' if attempt.quota_state == 'unknown' or reason == 'code_notice_unknown' else 'failed'
                log.context_snapshot = {**(log.context_snapshot or {}), 'step': attempt.stage,
                    'quota_state': attempt.quota_state, 'quota_used': attempt.quota_used, 'quota_limit': DAILY_LIMIT}
            await db.commit()

    async def _notice(self, action, text, websocket, lock, stage):
        if not await lock.extend():
            raise CommandStopped('code_busy', '会话锁已失效')
        await self.generator._recheck(action.claim)
        await self._update(action, stage=stage,
            **({'marker_at_ms': now_ms()} if stage == 'marker' else {}))
        outcome = await self.generator.send_text_confirmed(action.claim, text, websocket)
        await self._update(action, outcome=outcome, sent_text=text)
        if outcome.status != 'success':
            raise CommandStopped('code_notice_unknown' if outcome.status == 'unknown' else 'code_notice_failed',
                                 '前置消息未获发送确认，已停止后续处理，不自动补发')

    async def _reserve(self, action, *, dispatch=False):
        """Lock the account row before reading quota; never hold a DB lock over network IO."""
        now, used, latest = now_ms(), 0, 0
        day = quota_day(now)
        async with async_session_maker() as db:
            account = await self._account(db)
            if not account or account.id != action.claim.account_pk or account.owner_id != action.claim.owner_id:
                raise CommandStopped('code_account_inactive', '账号归属已变化')
            rows = (await db.execute(select(Attempt).where(
                Attempt.account_pk == account.id, Attempt.owner_id == account.owner_id,
                Attempt.order_no == action.order_no, Attempt.quota_state.in_(COUNTED),
            ).with_for_update())).scalars().all()
            for row in rows:
                if row.id == action.attempt_id:
                    continue
                if row.quota_state == 'reserved' and row.busy_until_ms <= now:
                    await self._recover(db, row, now)
                if row.quota_state not in COUNTED:
                    continue
                if row.quota_state == 'reserved':
                    await db.commit()
                    return '该订单正在处理，请稍后再试', 'code_order_busy'
                if row.quota_day == day:
                    used += 1
                latest = max(latest, (row.finished_ms if row.quota_state == 'success' else row.sent_at_ms) or row.created_ms)
            if used >= DAILY_LIMIT:
                await db.commit()
                return LIMIT_HINT, 'code_daily_limit'
            if latest and now < latest + WINDOW_MS:
                await db.commit()
                return f'请等待 {math.ceil((latest + WINDOW_MS - now) / 1000)} 秒后再领取口令', 'code_order_cooldown'
            attempt = (await db.execute(select(Attempt).where(Attempt.id == action.attempt_id).with_for_update())).scalars().one()
            attempt.quota_day, attempt.quota_state, attempt.quota_used = day, 'reserved', used + 1
            attempt.stage = 'sending' if dispatch else 'reserved'
            if dispatch:
                attempt.sent_at_ms = now
            await db.commit()
        return None

    async def _run(self, action, websocket, lock):
        if action.kind in ('prompt', 'format'):
            await self._notice(action, PROMPT if action.kind == 'prompt' else FORMAT_HINT,
                               websocket, lock, action.kind)
            await self._update(action, stage='waiting', reason='code_waiting_order',
                               end_state='waiting', process_status='success')
            return
        await self._notice(action, PROCESSING, websocket, lock, 'validating')
        async with async_session_maker() as db:
            account = (await db.execute(select(YtxAccount).where(
                YtxAccount.id == action.claim.account_pk, YtxAccount.owner_id == action.claim.owner_id,
                YtxAccount.account_id == self.account_id))).scalars().one()
            cookies = account.cookie
        result = await check_order_entitlement(cookies=cookies, seller_id=normalize_id(self.instance.myid),
            buyer_id=action.claim.buyer_id, item_id=action.claim.item_id, order_no=action.order_no)
        if not result.allowed:
            await self._notice(action, f'机器人回复：订单异常，{result.detail}（五分钟内可重新发送订单号）',
                               websocket, lock, 'order_rejected')
            await self._update(action, reason=result.reason, detail=result.detail, end_state='waiting', process_status='failed')
            return
        limit = await self._reserve(action)
        if limit:
            text, reason = limit
            await self._notice(action, text, websocket, lock, 'quota_rejected')
            await self._update(action, reason=reason, detail=text, end_state='finished', process_status='skipped')
            return
        await self._notice(action, '口令生成', websocket, lock, 'marker')
        await self._update(action, stage='scripting')
        try:
            code = await self.generator.generate_code(action.claim, lock, record_output=False)
        except CommandStopped as exc:
            if exc.reason not in ('code_script_failed', 'code_script_timeout', 'code_invalid_output'):
                raise
            await self._update(action, quota_state='released')
            await self._notice(action, SCRIPT_ERROR, websocket, lock, 'script_failed')
            await self._update(action, reason=exc.reason, detail=exc.detail, end_state='finished', process_status='failed')
            return
        # Re-evaluate the day and quota immediately before dispatch (including midnight).
        await self.generator._recheck(action.claim)
        self.generator.check_connection(websocket)
        limit = await self._reserve(action, dispatch=True)
        if limit:
            await self._update(action, quota_state='released')
            await self._notice(action, limit[0], websocket, lock, 'quota_rejected')
            await self._update(action, reason=limit[1], end_state='finished', process_status='skipped')
            return
        try:
            outcome = await self.generator.send_text_confirmed(action.claim, code, websocket)
        except CommandStopped:
            # The shared transport raises this only for its pre-send connection check.
            await self._update(action, quota_state='released')
            raise
        await self._update(action, stage='finished', outcome=outcome, sent_text=code,
            quota_state={'success': 'success', 'unknown': 'unknown', 'failed': 'released'}[outcome.status],
            reason=outcome.reason, detail=outcome.detail, end_state='finished',
            process_status='success' if outcome.status == 'success' else 'failed')
